from __future__ import annotations

from harn_gibson.events import GibsonEvent
from harn_gibson.rendering import RenderRequest
from harn_gibson.routing import (
    EventRouter,
    EventRouteRule,
    RendererEventInterest,
    RenderInputBatch,
    RouteDecision,
    StreamBinding,
    TimelineWindow,
    default_stream_bindings,
    event_route_rules_from_value,
    renderer_event_interest_from_renderer,
    renderer_event_interest_from_value,
    stream_buffer_mutations,
    stream_text_for_event,
)


def event(sequence: int, event_type: str, payload: dict[str, object] | None = None) -> GibsonEvent:
    raw = {"type": event_type, **dict(payload or {})}
    return GibsonEvent.from_raw(raw, sequence, timestamp_ms=1000 + sequence * 250)


def test_timeline_window_and_render_input_batch() -> None:
    first = RenderRequest(event(1, "tool_call"), metadata={"a": 1})
    second = RenderRequest(event(3, "tool_result"), route="direct_scene", coalesced_count=2)
    batch = RenderInputBatch.from_requests((first, second), metadata={"window": "test"})

    assert TimelineWindow.from_events(()).to_dict() == {"startMs": 0, "endMs": 0, "durationMs": 0}
    assert batch.timeline.to_dict() == {"startMs": 1250, "endMs": 1750, "durationMs": 500}
    assert batch.requests[0].timeline_offset_ms == 0
    assert batch.requests[1].timeline_offset_ms == 500
    assert batch.to_dict()["requests"][1]["route"] == "direct_scene"
    assert batch.to_dict()["metadata"] == {"window": "test"}


def test_stream_binding_route_decision_and_defaults_to_dict() -> None:
    binding = StreamBinding("message_update", "main", "target", "Main stream", flush_ms=50)
    decision = RouteDecision("stream_buffer", "local append", False, "main", "target", {"binding": binding.to_dict()})
    rule = EventRouteRule("tool_result", "direct_scene", "local result handling", {"sample": True})
    interest = RendererEventInterest(
        event_types=("tool_call",),
        phases=("before",),
        exclude_event_types=("browser_input",),
        fallback_route="debug_only",
        reason="selective renderer",
        metadata={"owner": "renderer"},
    )

    assert default_stream_bindings()[0].event_type == "message_update"
    assert binding.to_dict()["flushMs"] == 50
    assert decision.to_dict() == {
        "route": "stream_buffer",
        "reason": "local append",
        "rendererVisible": False,
        "streamId": "main",
        "targetId": "target",
        "metadata": {"binding": binding.to_dict()},
    }
    assert rule.to_dict() == {
        "eventType": "tool_result",
        "route": "direct_scene",
        "reason": "local result handling",
        "metadata": {"sample": True},
    }
    assert interest.to_dict() == {
        "fallbackRoute": "debug_only",
        "reason": "selective renderer",
        "eventTypes": ["tool_call"],
        "phases": ["before"],
        "excludeEventTypes": ["browser_input"],
        "metadata": {"owner": "renderer"},
    }
    assert interest.wants(event(1, "tool_call")) is True
    assert interest.wants(event(2, "tool_result")) is False
    assert RendererEventInterest().to_dict() == {
        "fallbackRoute": "direct_scene",
        "reason": "renderer not interested",
    }


def test_event_router_routes_non_stream_events_to_renderer() -> None:
    router = EventRouter()
    result = router.route(event(1, "tool_call"))

    assert result.uses_renderer is True
    assert result.decision.route == "renderer_agent"
    assert result.request.metadata["route"]["reason"] == "default renderer route"
    assert result.batch.to_dict()["route"] == "renderer_agent"


def test_event_router_route_rules_cover_renderer_direct_debug_and_drop() -> None:
    router = EventRouter(
        route_rules=(
            EventRouteRule("tool_result", "direct_scene", "local result render"),
            EventRouteRule("session_tree", "debug_only", "debug snapshot"),
            EventRouteRule("model_select", "drop", "sampled out"),
            EventRouteRule("tool_call", "renderer_agent", "force renderer"),
        )
    )

    direct = router.route(event(1, "tool_result", {"toolName": "bash"}))
    debug = router.route(event(2, "session_tree"))
    dropped = router.route(event(3, "model_select"))
    renderer = router.route(event(4, "tool_call"))

    assert direct.uses_renderer is False
    assert direct.dropped is False
    assert direct.decision.route == "direct_scene"
    assert direct.request.route == "direct_scene"
    assert direct.batch.route == "direct_scene"
    assert direct.direct_mutations[0].target_id == "status"
    assert direct.request.metadata["route"]["metadata"]["rule"]["route"] == "direct_scene"
    assert debug.decision.route == "debug_only"
    assert debug.direct_mutations == ()
    assert dropped.dropped is True
    assert dropped.batch.to_dict()["route"] == "drop"
    assert renderer.uses_renderer is True
    assert renderer.decision.reason == "force renderer"


def test_event_router_route_rules_can_sample_noisy_events() -> None:
    router = EventRouter(
        route_rules=(
            EventRouteRule.from_mapping(
                {
                    "eventType": "model_select",
                    "route": "renderer_agent",
                    "reason": "sample model chatter",
                    "sampleEvery": 3,
                    "sampleOffset": 1,
                    "fallbackRoute": "debug_only",
                }
            ),
            EventRouteRule.from_mapping(
                {
                    "eventType": "session_tree",
                    "route": "direct_scene",
                    "reason": "sample tree snapshots",
                    "sampleEvery": 2,
                    "fallbackRoute": "drop",
                }
            ),
        )
    )

    skipped = router.route(event(1, "model_select"))
    sampled = router.route(event(2, "model_select"))
    skipped_again = router.route(event(3, "model_select"))
    direct_sampled = router.route(event(4, "session_tree"))
    direct_skipped = router.route(event(5, "session_tree"))

    assert skipped.decision.route == "debug_only"
    assert skipped.uses_renderer is False
    assert skipped.direct_mutations == ()
    assert skipped.decision.reason == "sample model chatter sample skipped"
    assert skipped.request.metadata["route"]["metadata"]["sample"] == {
        "index": 0,
        "sampleEvery": 3,
        "sampleOffset": 1,
        "sampled": False,
        "fallbackRoute": "debug_only",
    }
    assert sampled.uses_renderer is True
    assert sampled.decision.metadata["sample"]["sampled"] is True
    assert sampled.request.metadata["route"]["metadata"]["rule"]["fallbackRoute"] == "debug_only"
    assert skipped_again.decision.route == "debug_only"
    assert skipped_again.decision.metadata["sample"]["index"] == 2
    assert direct_sampled.decision.route == "direct_scene"
    assert direct_sampled.direct_mutations[0].target_id == "status"
    assert direct_sampled.decision.metadata["sample"]["sampled"] is True
    assert direct_skipped.dropped is True
    assert direct_skipped.decision.metadata["sample"]["index"] == 1


def test_event_route_rule_mapping_and_validation() -> None:
    first = EventRouteRule.from_mapping(
        {
            "eventType": "runtime_error",
            "route": "debug_only",
            "reason": "debug failures",
            "metadata": {"source": "env"},
        }
    )
    second = EventRouteRule.from_mapping({"event_type": "model_select", "route": "drop"})
    sampled = EventRouteRule.from_mapping(
        {
            "eventType": "session_tree",
            "route": "renderer_agent",
            "sampleEvery": "4",
            "sampleOffset": "2",
            "sampleFallbackRoute": "debug_only",
        }
    )
    direct = EventRouteRule("tool_result", "direct_scene", "local result")

    assert first.to_dict() == {
        "eventType": "runtime_error",
        "route": "debug_only",
        "reason": "debug failures",
        "metadata": {"source": "env"},
    }
    assert second.reason == "drop route rule"
    assert sampled.to_dict() == {
        "eventType": "session_tree",
        "route": "renderer_agent",
        "reason": "renderer_agent route rule",
        "sampleEvery": 4,
        "sampleOffset": 2,
        "fallbackRoute": "debug_only",
    }
    assert event_route_rules_from_value(None) == ()
    assert event_route_rules_from_value([first.to_dict(), direct]) == (first, direct)

    for value, message in (
        ({"route": "drop"}, "eventType"),
        ({"eventType": "tool_call", "route": "stream_buffer"}, "unsupported"),
        ({"eventType": "tool_call", "sampleEvery": 0}, "sampleEvery"),
        ({"eventType": "tool_call", "sampleEvery": "bad"}, "integer"),
        ({"eventType": "tool_call", "sampleEvery": 2, "sampleOffset": -1}, "sampleOffset"),
        ({"eventType": "tool_call", "sampleEvery": 2, "sampleOffset": 2}, "sampleOffset"),
        ({"eventType": "tool_call", "sampleEvery": 2, "fallbackRoute": "renderer_agent"}, "fallback"),
    ):
        try:
            EventRouteRule.from_mapping(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")

    for value, message in (
        ("bad", "list"),
        ([object()], "object"),
    ):
        try:
            event_route_rules_from_value(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")


def test_event_router_uses_renderer_interest_after_streams_and_rules() -> None:
    router = EventRouter(
        route_rules=(EventRouteRule("session_tree", "debug_only", "rule before interest"),),
        renderer_interest=RendererEventInterest(event_types=("tool_call",), fallback_route="direct_scene"),
    )

    renderer = router.route(event(1, "tool_call"))
    direct = router.route(event(2, "tool_result", {"toolName": "bash"}))
    stream = router.route(event(3, "message_update", {"assistantMessageEvent": {"delta": "abc"}}))
    rule = router.route(event(4, "session_tree"))

    assert renderer.uses_renderer is True
    assert direct.decision.route == "direct_scene"
    assert direct.request.metadata["route"]["metadata"]["rendererInterest"]["eventTypes"] == ["tool_call"]
    assert direct.direct_mutations[0].target_id == "status"
    assert stream.decision.route == "stream_buffer"
    assert rule.decision.reason == "rule before interest"


def test_renderer_interest_mapping_and_resolution_variants() -> None:
    interest = RendererEventInterest.from_mapping(
        {
            "eventTypes": ["tool_call", "tool_result"],
            "phases": ["before", "after"],
            "excludeEventTypes": ["tool_result"],
            "fallbackRoute": "drop",
            "reason": "only preflight",
            "metadata": {"renderer": "test"},
        }
    )

    class MappingRenderer:
        event_interest = {
            "event_types": ["tool_call"],
            "fallback_route": "debug_only",
        }

    class CallableRenderer:
        def event_interest(self) -> RendererEventInterest:
            return interest

    assert interest.wants(event(1, "tool_call")) is True
    assert interest.wants(event(2, "tool_result")) is False
    assert interest.to_dict()["fallbackRoute"] == "drop"
    assert renderer_event_interest_from_renderer(MappingRenderer()).fallback_route == "debug_only"  # type: ignore[union-attr]
    assert renderer_event_interest_from_renderer(CallableRenderer()) is interest
    assert renderer_event_interest_from_renderer(object()) is None
    assert renderer_event_interest_from_value(None) is None
    assert renderer_event_interest_from_value(interest) is interest
    assert renderer_event_interest_from_value({"eventTypes": ["input"]}).event_types == ("input",)  # type: ignore[union-attr]
    assert RendererEventInterest.from_mapping({"eventTypes": "tool_call"}).event_types == ()
    assert RendererEventInterest.from_mapping({"phases": ["after"]}).wants(event(8, "tool_call")) is False


def test_renderer_interest_validation_errors() -> None:
    class BadRenderer:
        event_interest = object()

    for payload, message in (
        ({"fallbackRoute": "renderer_agent"}, "unsupported renderer interest fallback route"),
        ({"phases": ["sideways"]}, "unsupported renderer interest phase"),
    ):
        try:
            RendererEventInterest.from_mapping(payload)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")

    try:
        renderer_event_interest_from_renderer(BadRenderer())
    except ValueError as error:
        assert "event_interest" in str(error)
    else:
        raise AssertionError("expected ValueError")

    try:
        renderer_event_interest_from_value(object())
    except ValueError as error:
        assert "renderer interest value" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_event_router_routes_text_streams_to_local_buffer() -> None:
    router = EventRouter()

    first = router.route(event(1, "message_update", {"assistantMessageEvent": {"delta": "hello "}}))
    second = router.route(event(2, "message_update", {"assistantMessageEvent": {"text": "world"}}))

    assert first.uses_renderer is False
    assert first.decision.route == "stream_buffer"
    assert first.decision.stream_id == "assistant-main"
    assert first.direct_mutations[0].primitive is not None
    assert first.direct_mutations[0].primitive.props["text"] == "hello "
    assert second.direct_mutations[0].primitive is not None
    assert second.direct_mutations[0].primitive.props["text"] == "hello world"
    assert router.stream_snapshot()["assistant-main"]["updateCount"] == 2


def test_event_router_streams_only_deltas_from_harn_text_lifecycle_events() -> None:
    router = EventRouter()
    partial = {"content": [{"type": "text", "text": "GIBSON_INTEGRATION_OK"}]}

    start = router.route(
        event(1, "message_update", {"assistantMessageEvent": {"type": "text_start", "partial": partial}})
    )
    first = router.route(
        event(2, "message_update", {"assistantMessageEvent": {"type": "text_delta", "delta": "GIBSON_"}})
    )
    second = router.route(
        event(3, "message_update", {"assistantMessageEvent": {"type": "text_delta", "delta": "INTEGRATION_OK"}})
    )
    end = router.route(
        event(
            4,
            "message_update",
            {
                "assistantMessageEvent": {
                    "type": "text_end",
                    "content": "GIBSON_INTEGRATION_OK",
                    "partial": partial,
                }
            },
        )
    )

    assert start.decision.route == "debug_only"
    assert first.decision.route == "stream_buffer"
    assert second.decision.route == "stream_buffer"
    assert end.decision.route == "debug_only"
    assert router.stream_snapshot()["assistant-main"]["text"] == "GIBSON_INTEGRATION_OK"
    assert router.stream_snapshot()["assistant-main"]["updateCount"] == 2


def test_event_router_routes_empty_stream_updates_debug_only() -> None:
    result = EventRouter().route(event(1, "message_update", {"assistantMessageEvent": {"type": "ping"}}))

    assert result.uses_renderer is False
    assert result.decision.route == "debug_only"
    assert result.direct_mutations == ()
    assert result.batch.to_dict()["route"] == "debug_only"


def test_stream_text_extraction_variants_and_clipping() -> None:
    assert stream_text_for_event(event(1, "message_update", {"delta": "a"})) == "a"
    assert (
        stream_text_for_event(
            event(1, "message_update", {"assistantMessageEvent": {"type": "text_delta", "delta": "a"}})
        )
        == "a"
    )
    assert (
        stream_text_for_event(
            event(
                1,
                "message_update",
                {"assistantMessageEvent": {"type": "text_start", "partial": {"content": [{"text": "full"}]}}},
            )
        )
        == ""
    )
    assert (
        stream_text_for_event(
            event(1, "message_update", {"assistantMessageEvent": {"type": "text_end", "content": "full"}})
        )
        == ""
    )
    assert stream_text_for_event(event(1, "message_update", {"content": [{"text": "b"}, {"text": "c"}]})) == "bc"
    assert stream_text_for_event(event(1, "message_update", {"content": [{"image": "x"}]})) == ""
    assert stream_text_for_event(event(1, "message_update", {"content": ["x"]})) == ""

    binding = StreamBinding("message_update", "s", "t", "Small", max_chars=6)
    router = EventRouter((binding,))
    result = router.route(event(1, "message_update", {"delta": "abcdefghi"}))

    assert router.stream_snapshot()["s"]["text"] == "...ghi"
    assert result.direct_mutations[0].primitive is not None
    assert result.direct_mutations[0].primitive.props["maxChars"] == 6


def test_stream_buffer_mutation_shape() -> None:
    router = EventRouter()
    routed = router.route(event(5, "message_update", {"text": "signal"}))
    mutations = stream_buffer_mutations(routed.request.event, router.stream_buffers["assistant-main"])

    assert [mutation.op for mutation in mutations] == ["upsert", "patch", "start_animation"]
    assert mutations[0].primitive is not None
    assert mutations[0].primitive.kind == "text_stream"
    assert mutations[1].target_id == "status"
    assert mutations[2].animation is not None
    assert mutations[2].animation.kind == "stream-pulse"
