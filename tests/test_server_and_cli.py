from __future__ import annotations

import json
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from harn_gibson import (
    BrowserScreenshotResult,
    DeterministicSceneRenderer,
    EventRouter,
    EventRouteRule,
    ExternalRenderer,
    PromptedModelRenderer,
    RendererEventInterest,
    RenderPlan,
    RenderRequest,
    RenderStep,
    ReplayFrameScreenshot,
    SceneMutation,
    __version__,
    cli,
)
from harn_gibson.scene import SCENE_MUTATION_OPS
from harn_gibson.server import (
    CORE_PRIMITIVE_KINDS,
    INPUT_DELIVERY_KINDS,
    SUPPORTED_RENDER_MODES,
    SUPPORTED_RENDER_TIMING_MODES,
    BrowserInputQueue,
    GibsonServerState,
    HarnBridgeState,
    apply_event_to_scene,
    backend_contract_payload,
    browser_input_event_payload,
    build_state_from_env,
    create_server,
    diagnostic_event_payload,
    enqueue_browser_input,
    event_from_payload,
    format_sse,
    health_payload,
    project_name_from_env,
    project_root_from_env,
    publish_diagnostic_event,
    renderer_context_config_from_env,
    renderer_interest_from_env,
    route_rules_from_env,
    submit_event_to_renderer,
)
from harn_gibson.styles import STYLE_PACKS, style_pack_from_name


def request_text(url: str, data: bytes | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def request_sse_once(
    base: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, str, dict[str, Any], tuple[int, str, str]]:
    with urllib.request.urlopen(f"{base}{path}", timeout=2) as response:  # noqa: S310
        posted = request_text(f"{base}/events", json.dumps(payload).encode("utf-8"))
        line = response.readline().decode("utf-8").strip()
        assert response.readline().decode("utf-8") == "\n"
        assert line.startswith("data: ")
        streamed = json.loads(line.removeprefix("data: "))
        status = response.status
        content_type = response.headers.get("Content-Type", "")
    return status, content_type, streamed, posted


def start_server() -> tuple[ThreadingHTTPServer, str]:
    state = GibsonServerState()
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_http_server_routes() -> None:
    server, base = start_server()
    try:
        assert server.daemon_threads is True
        assert request_text(f"{base}/")[0:2] == (200, "text/html; charset=utf-8")
        assert request_text(f"{base}/?capture=1")[0:2] == (200, "text/html; charset=utf-8")
        assert "GIBSON LINK" in request_text(f"{base}/index.html")[2]
        assert "Tracebacks" in request_text(f"{base}/index.html")[2]
        assert "Render Intents" in request_text(f"{base}/index.html")[2]
        assert request_text(f"{base}/assets/app.css")[1] == "text/css; charset=utf-8"
        app_status, app_content_type, app_js = request_text(f"{base}/assets/app.js")
        assert (app_status, app_content_type) == (200, "application/javascript; charset=utf-8")
        assert 'fetch("/health"' in app_js
        assert 'EventSource("/events/stream")' in app_js
        health = json.loads(request_text(f"{base}/healthz")[2])
        health_alias = json.loads(request_text(f"{base}/health?probe=1")[2])
        assert health_alias == health
        assert health["ok"] is True
        assert health["events"] == 0
        assert health["sceneRevision"] == 0
        assert health["renderMode"] == "blocking"
        assert health["renderTiming"] == "immediate"
        assert health["displayStyle"] == "gibson"
        assert health["stylePack"]["id"] == "gibson"
        assert health["pendingRenderJobs"] == 0
        assert health["streams"] == {}
        assert health["inputBridge"] == {
            "pendingInputs": 0,
            "inputPollerSeen": False,
            "inputPollerConnected": False,
            "lastInputPollMs": None,
            "lastInputPollAgeMs": None,
            "lastInputDeliveryMs": None,
            "pollCount": 0,
            "deliveredInputs": 0,
        }
        assert json.loads(request_text(f"{base}/scene")[2])["schema"] == "harn-gibson.scene.v1"
        assert json.loads(request_text(f"{base}/scene?capture=1")[2])["schema"] == "harn-gibson.scene.v1"
        catalog = json.loads(request_text(f"{base}/catalog")[2])
        assert catalog["schema"] == "harn-gibson.visual-catalog.v1"
        assert any(entry["id"] == "text_stream" for entry in catalog["primitives"])
        backend_contract = json.loads(request_text(f"{base}/backend-contract")[2])
        assert backend_contract["schema"] == "harn-gibson.display-backend-contract.v1"
        assert backend_contract["transport"] == "http+sse"
        assert backend_contract["sceneSchema"] == "harn-gibson.scene.v1"
        assert backend_contract["sceneUpdateSchema"] == "harn-gibson.scene-update.v1"
        assert backend_contract["catalogSchema"] == "harn-gibson.visual-catalog.v1"
        assert backend_contract["mutationSchema"] == "harn-gibson.scene-mutation.v1"
        assert backend_contract["inputSchema"] == "harn-gibson.browser-input.v1"
        assert backend_contract["endpoints"]["scene"] == {
            "method": "GET",
            "path": "/scene",
            "schema": "harn-gibson.scene.v1",
        }
        assert backend_contract["endpoints"]["sceneStream"]["contentType"] == "text/event-stream"
        assert backend_contract["displayBackend"] == {
            "id": "browser-canvas",
            "primary": True,
            "renderTarget": "html-canvas",
            "catalogSupport": "full",
            "styleSupport": "style-pack-v1",
        }
        assert backend_contract["stylePackSchema"] == "harn-gibson.style-pack.v1"
        assert backend_contract["activeStylePack"]["id"] == "gibson"
        assert backend_contract["supportedStylePackIds"] == [style.id for style in STYLE_PACKS]
        assert backend_contract["supportedMutationOps"] == list(SCENE_MUTATION_OPS)
        assert backend_contract["supportedInputDeliverAs"] == list(INPUT_DELIVERY_KINDS)
        assert backend_contract["supportedRenderModes"] == list(SUPPORTED_RENDER_MODES)
        assert backend_contract["supportedRenderTimingModes"] == list(SUPPORTED_RENDER_TIMING_MODES)
        assert backend_contract["capabilityProfile"]["primitiveLayer"]["supportsCustomPrimitiveLayer"] is True
        assert backend_contract["capabilityProfile"]["mutationLayer"]["supportedOps"] == list(SCENE_MUTATION_OPS)
        assert backend_contract["capabilityProfile"]["input"]["deliverAs"] == list(INPUT_DELIVERY_KINDS)
        assert backend_contract["corePrimitiveKinds"] == list(CORE_PRIMITIVE_KINDS)
        assert "status" in backend_contract["supportedPrimitiveKinds"]
        assert "city_block" in backend_contract["supportedPrimitiveKinds"]
        assert "spatial_map" in backend_contract["supportedPrimitiveKinds"]
        assert "route_trace" in backend_contract["supportedEffectKinds"]
        assert json.loads(request_text(f"{base}/missing")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/bad", b"{}")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/events", b"{")[2]) == {"error": "invalid json"}
        assert json.loads(request_text(f"{base}/events", b"[]")[2]) == {"error": "event payload must be an object"}
        assert json.loads(request_text(f"{base}/events", b'{"sequence":1}')[2]) == {
            "error": "event payload missing eventType"
        }
        assert request_text(f"{base}/input/next?poll=1")[0:2] == (204, "")
        assert json.loads(request_text(f"{base}/input", b"{")[2]) == {"error": "invalid json"}
        assert json.loads(request_text(f"{base}/input", b"[]")[2]) == {"error": "input payload must be an object"}
        assert json.loads(request_text(f"{base}/input", b'{"message":1}')[2]) == {"error": "message must be a string"}
        assert json.loads(request_text(f"{base}/input", b'{"message":"hi","deliverAs":1}')[2]) == {
            "error": "deliverAs must be a string"
        }

        payload = {
            "sequence": 1,
            "timestampMs": 10,
            "source": "test",
            "eventType": "input",
            "phase": "before",
            "title": "Input intercept",
            "summary": "interactive input: hi",
            "payload": {"type": "input", "text": "hi", "source": "interactive"},
        }
        status, stream_type, streamed, posted = request_sse_once(base, "/events/stream?capture=1", payload)
        assert status == 200
        assert stream_type == "text/event-stream"
        assert posted[0] == 202
        assert streamed["schema"] == "harn-gibson.scene-update.v1"
        assert streamed["scene"]["revision"] == 1
        status, _content_type, body = posted
        assert status == 202
        assert json.loads(body) == {"ok": True, "renderMode": "blocking", "sceneRevision": 1}
        scene = json.loads(request_text(f"{base}/scene")[2])
        assert scene["metadata"]["lastRenderIntent"]["renderer"] == "deterministic"
        assert scene["metadata"]["lastRenderIntent"]["intent"] == "visualize input"
        assert scene["primitives"]["gibson-city"]["kind"] == "city_block"
        assert scene["primitives"]["signal-graph"]["kind"] == "node_graph"
        assert scene["primitives"]["packet-field"]["kind"] == "particle_field"
        assert scene["primitives"]["repo-map"]["kind"] == "node_graph"
        assert scene["primitives"]["repo-city"]["kind"] == "city_block"
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["ok"] is True
        assert health["events"] == 1
        assert health["sceneRevision"] == 1
        assert health["renderMode"] == "blocking"
        assert health["pendingRenderJobs"] == 0
        assert health["inputBridge"]["pendingInputs"] == 0
        assert health["inputBridge"]["inputPollerSeen"] is True
        assert health["inputBridge"]["inputPollerConnected"] is True
        assert health["inputBridge"]["pollCount"] == 1

        status, _content_type, body = request_text(
            f"{base}/input?capture=1",
            json.dumps({"message": " launch sequence ", "deliverAs": "steer"}).encode("utf-8"),
        )
        accepted = json.loads(body)
        assert status == 202
        assert accepted["ok"] is True
        assert accepted["renderMode"] == "blocking"
        assert accepted["input"] == {
            "id": "input-1",
            "sequence": 1,
            "message": "launch sequence",
            "deliverAs": "steer",
        }
        assert accepted["pendingInputs"] == 1
        assert accepted["inputBridge"]["pendingInputs"] == 1
        assert accepted["inputBridge"]["inputPollerSeen"] is True
        assert accepted["inputBridge"]["deliveredInputs"] == 0
        assert json.loads(request_text(f"{base}/input/next")[2]) == accepted["input"]
        assert request_text(f"{base}/input/next")[0] == 204
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["inputBridge"]["pendingInputs"] == 0
        assert health["inputBridge"]["deliveredInputs"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_apply_event_to_scene_and_event_from_payload() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 3,
        "timestampMs": 33,
        "source": "unit",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash"},
        "recentContext": ["ctx"],
        "visualizationContext": ["scene"],
        "decisions": [{"block": True, "reason": "no"}],
    }

    event = event_from_payload(payload)
    update = apply_event_to_scene(payload, state)
    submit_result = submit_event_to_renderer(payload, state)

    assert event.event_type == "tool_call"
    assert event.recent_context == ("ctx",)
    assert update["schema"] == "harn-gibson.scene-update.v1"
    assert update["decisions"] == [{"block": True, "reason": "no"}]
    assert update["scene"]["revision"] == 1
    assert submit_result.scene_revision == 2


def test_diagnostic_event_payload_and_publish() -> None:
    state = GibsonServerState()
    payload = diagnostic_event_payload(
        10,
        event_type="runtime_error",
        severity="error",
        message="delivery failed",
        details="input=input-1",
        traceback_text="Traceback...",
    )
    result = publish_diagnostic_event(
        state,
        10,
        event_type="runtime_error",
        severity="error",
        message="delivery failed",
        details="input=input-1",
        traceback_text="Traceback...",
    )

    assert payload["eventType"] == "runtime_error"
    assert payload["payload"]["traceback"] == "Traceback..."
    assert result.scene_revision == 1
    assert state.scene.state.primitives["trace-log"].props["text"][0]["details"] == "input=input-1"


def test_stream_update_routes_to_local_scene_buffer() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 11,
        "timestampMs": 1100,
        "source": "unit",
        "eventType": "message_update",
        "phase": "during",
        "title": "Stream update",
        "summary": "assistant stream {type, delta}",
        "payload": {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "booting"}},
    }

    result = submit_event_to_renderer(payload, state)
    scene = state.scene.state

    assert result.scene_revision == 1
    assert "assistant-stream" in scene.primitives
    assert scene.primitives["assistant-stream"].props["text"] == "booting"
    assert scene.log == []
    assert state.router.stream_snapshot()["assistant-main"]["text"] == "booting"
    update = result.updates[0]
    assert update["renderPlan"]["metadata"]["route"]["route"] == "stream_buffer"
    assert update["renderRequests"][0]["route"] == "stream_buffer"


def test_empty_stream_update_routes_debug_only_without_scene_mutation() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 12,
        "timestampMs": 1200,
        "source": "unit",
        "eventType": "message_update",
        "phase": "during",
        "title": "Stream update",
        "summary": "assistant stream {type}",
        "payload": {"type": "message_update", "assistantMessageEvent": {"type": "ping"}},
    }

    result = submit_event_to_renderer(payload, state)

    assert result.scene_revision == 0
    assert state.scene.state.revision == 0
    assert state.router.stream_snapshot() == {}
    assert result.updates[0]["renderPlan"]["metadata"]["route"]["route"] == "debug_only"
    assert result.updates[0]["renderRequests"][0]["route"] == "debug_only"


def test_direct_scene_route_rule_bypasses_renderer_and_updates_scene() -> None:
    state = GibsonServerState(router=EventRouter(route_rules=(EventRouteRule("tool_result", "direct_scene", "local"),)))
    payload = {
        "sequence": 13,
        "timestampMs": 1300,
        "source": "unit",
        "eventType": "tool_result",
        "phase": "after",
        "title": "Tool result",
        "summary": "bash completed: ok",
        "payload": {"type": "tool_result", "toolName": "bash"},
        "decisions": [{"content": "ok"}],
    }

    result = submit_event_to_renderer(payload, state)
    update = result.updates[0]

    assert result.scene_revision == 1
    assert state.scene.state.primitives["status"].props["text"] == "after:tool_result"
    assert update["renderPlan"]["metadata"]["route"]["route"] == "direct_scene"
    assert update["renderRequests"][0]["route"] == "direct_scene"
    assert state.buffer.snapshot() == [update]


def test_drop_route_rule_accepts_without_scene_update() -> None:
    state = GibsonServerState(router=EventRouter(route_rules=(EventRouteRule("model_select", "drop", "sampled"),)))
    payload = {
        "sequence": 14,
        "timestampMs": 1400,
        "source": "unit",
        "eventType": "model_select",
        "phase": "lifecycle",
        "title": "Model select",
        "summary": "model selected",
        "payload": {"type": "model_select", "model": {"provider": "openai", "id": "test"}},
    }

    result = submit_event_to_renderer(payload, state)
    accepted = apply_event_to_scene(payload, state)

    assert result.updates == ()
    assert result.scene_revision is None
    assert state.scene.state.revision == 0
    assert state.buffer.snapshot() == []
    assert accepted == {"ok": True, "renderMode": "blocking", "sceneRevision": 0}


def test_renderer_advertised_interest_controls_default_routing() -> None:
    class SelectiveRenderer:
        event_interest = RendererEventInterest(event_types=("tool_call",), fallback_route="direct_scene")

        def __init__(self) -> None:
            self.requests: list[tuple[RenderRequest, ...]] = []

        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            self.requests.append(requests)
            return RenderPlan(
                requests=requests,
                steps=(
                    RenderStep(
                        (SceneMutation("append_log", entry={"eventType": "renderer", "summary": "renderer saw it"}),),
                        event_index=0,
                    ),
                ),
                metadata={"renderer": "selective"},
            )

    renderer = SelectiveRenderer()
    state = GibsonServerState(renderer=renderer)
    fallback_payload = {
        "sequence": 15,
        "timestampMs": 1500,
        "source": "unit",
        "eventType": "tool_result",
        "phase": "after",
        "title": "Tool result",
        "summary": "bash completed: ok",
        "payload": {"type": "tool_result", "toolName": "bash"},
    }
    renderer_payload = {
        "sequence": 16,
        "timestampMs": 1600,
        "source": "unit",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
    }

    fallback = submit_event_to_renderer(fallback_payload, state)
    rendered = submit_event_to_renderer(renderer_payload, state)

    assert renderer.requests[0][0].event.event_type == "tool_call"
    assert fallback.updates[0]["renderPlan"]["metadata"]["route"]["route"] == "direct_scene"
    assert fallback.updates[0]["renderPlan"]["metadata"]["route"]["metadata"]["rendererInterest"]["eventTypes"] == [
        "tool_call"
    ]
    assert rendered.updates[0]["renderPlan"]["metadata"] == {"renderer": "selective"}


def test_explicit_router_interest_is_not_overwritten_by_renderer() -> None:
    class InterestedRenderer:
        event_interest = RendererEventInterest(event_types=("tool_call",), fallback_route="direct_scene")

        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            return RenderPlan(requests=requests, steps=(), metadata={"renderer": "unused"})

    router = EventRouter(renderer_interest=RendererEventInterest(event_types=("tool_result",), fallback_route="drop"))
    state = GibsonServerState(renderer=InterestedRenderer(), router=router)

    assert state.router.renderer_interest is router.renderer_interest
    assert state.router.renderer_interest.event_types == ("tool_result",)  # type: ignore[union-attr]


def test_event_from_payload_validation() -> None:
    state = GibsonServerState()
    for payload, message in (
        ({"eventType": "", "phase": "before", "payload": {}}, "missing eventType"),
        ({"eventType": "x", "phase": "bad", "payload": {}}, "invalid phase"),
        ({"eventType": "x", "phase": "before", "payload": []}, "missing payload object"),
    ):
        try:
            apply_event_to_scene(payload, state)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")


def test_browser_input_queue_and_payload_validation(monkeypatch: Any) -> None:
    state = GibsonServerState()
    queue = BrowserInputQueue()
    item = queue.enqueue(" hello ", "followUp")

    assert item.to_dict() == {"id": "input-1", "sequence": 1, "message": "hello", "deliverAs": "followUp"}
    assert queue.pending_count() == 1
    assert queue.pop() == item
    assert queue.pop() is None

    for message, deliver_as, error in (
        (" ", "followUp", "message cannot be empty"),
        ("hi", "later", "deliverAs must be followUp or steer"),
    ):
        try:
            queue.enqueue(message, deliver_as)
        except ValueError as exc:
            assert error in str(exc)
        else:
            raise AssertionError("expected ValueError")

    queued = enqueue_browser_input({"message": "abc"}, state)
    assert queued.deliver_as == "followUp"
    monkeypatch.setattr("harn_gibson.server.time.time", lambda: 12.3)
    payload = browser_input_event_payload(queued)
    assert payload["timestampMs"] == 12300
    assert payload["eventType"] == "browser_input"
    assert payload["summary"] == "gibson input queued: abc"
    assert "..." in browser_input_event_payload(queue.enqueue("x" * 120))["summary"]


def test_harn_bridge_state_and_health_payload() -> None:
    bridge = HarnBridgeState(connected_window_ms=100)

    assert bridge.snapshot(pending_inputs=2, timestamp_ms=1000) == {
        "pendingInputs": 2,
        "inputPollerSeen": False,
        "inputPollerConnected": False,
        "lastInputPollMs": None,
        "lastInputPollAgeMs": None,
        "lastInputDeliveryMs": None,
        "pollCount": 0,
        "deliveredInputs": 0,
    }
    bridge.record_input_poll(delivered=False, timestamp_ms=1000)
    assert bridge.snapshot(pending_inputs=1, timestamp_ms=1050)["inputPollerConnected"] is True
    stale = bridge.snapshot(pending_inputs=1, timestamp_ms=1201)
    assert stale["inputPollerConnected"] is False
    assert stale["lastInputPollAgeMs"] == 201
    bridge.record_input_poll(delivered=True, timestamp_ms=1250)
    delivered = bridge.snapshot(pending_inputs=0, timestamp_ms=1250)
    assert delivered["deliveredInputs"] == 1
    assert delivered["lastInputDeliveryMs"] == 1250

    state = GibsonServerState(input_bridge=bridge)
    health = health_payload(state)
    assert health["inputBridge"]["deliveredInputs"] == 1
    assert health["displayStyle"] == "gibson"

    styled = GibsonServerState(style_pack=style_pack_from_name("neon-noir"))
    styled_health = health_payload(styled)
    assert styled_health["displayStyle"] == "neon-noir"
    assert styled_health["stylePack"]["canvas"]["gridTone"] == "magenta"
    assert styled.scene.state.primitives["stage"].props["theme"] == "neon-noir"
    styled.pipeline.stop()


def test_backend_contract_payload_describes_non_web_backend_surface() -> None:
    contract = backend_contract_payload(GibsonServerState())

    assert contract["schema"] == "harn-gibson.display-backend-contract.v1"
    assert contract["endpoints"]["catalog"]["path"] == "/catalog"
    assert contract["endpoints"]["sceneStream"]["schema"] == "harn-gibson.scene-update.v1"
    assert contract["contracts"]["scene"] == "A full scene snapshot is authoritative for backend state."
    assert contract["contracts"]["mutation"] == (
        "Scene mutations are state deltas; display backends own drawing and animation loops."
    )
    assert contract["contracts"]["stylePack"].startswith("Style packs are presentation hints")
    assert contract["corePrimitiveKinds"] == list(CORE_PRIMITIVE_KINDS)
    assert set(CORE_PRIMITIVE_KINDS) <= set(contract["supportedPrimitiveKinds"])
    assert {"terminal_wall", "svg_layer", "data_rain", "spatial_map"} <= set(contract["catalogPrimitiveKinds"])
    assert {"timeline_cue", "route_trace", "camera_path"} <= set(contract["supportedEffectKinds"])
    assert contract["mutationSchema"] == "harn-gibson.scene-mutation.v1"
    assert contract["inputSchema"] == "harn-gibson.browser-input.v1"
    assert contract["supportedMutationOps"] == list(SCENE_MUTATION_OPS)
    assert contract["supportedInputDeliverAs"] == list(INPUT_DELIVERY_KINDS)
    assert contract["supportedRenderModes"] == ["blocking", "async"]
    assert contract["supportedRenderTimingModes"] == ["immediate", "scheduled"]
    assert contract["stylePackSchema"] == "harn-gibson.style-pack.v1"
    assert contract["activeStylePack"]["schema"] == "harn-gibson.style-pack.v1"
    assert contract["activeStylePack"]["id"] == "gibson"
    assert contract["displayBackend"]["styleSupport"] == "style-pack-v1"
    assert contract["supportedStylePackIds"] == [style.id for style in STYLE_PACKS]
    assert {style["id"] for style in contract["supportedStylePacks"]} == {style.id for style in STYLE_PACKS}
    assert all(style["schema"] == "harn-gibson.style-pack.v1" for style in contract["supportedStylePacks"])
    assert contract["capabilityProfile"] == {
        "schema": "harn-gibson.backend-capability-profile.v1",
        "backendId": "browser-canvas",
        "primitiveLayer": {
            "contract": "harn-gibson.visual-catalog.v1",
            "catalogSupport": "full",
            "supportsCustomPrimitiveLayer": True,
            "customPrimitivePolicy": (
                "Implement the advertised catalog directly, translate it to a backend-native vocabulary, "
                "or pair a custom vocabulary with a renderer that targets that vocabulary."
            ),
            "unknownPrimitivePolicy": "preserve-scene-state-render-noop",
            "supportedPrimitiveKinds": contract["supportedPrimitiveKinds"],
            "supportedEffectKinds": contract["supportedEffectKinds"],
        },
        "mutationLayer": {
            "schema": "harn-gibson.scene-mutation.v1",
            "supportedOps": list(SCENE_MUTATION_OPS),
            "patchSemantics": "shallow-props-merge",
            "sceneSnapshotAuthority": True,
        },
        "timing": {
            "renderModes": ["blocking", "async"],
            "renderTimingModes": ["immediate", "scheduled"],
            "supportsRenderStepDelayMs": True,
            "supportsRenderStepStartOffsetMs": True,
            "coalescedBatchTimeline": True,
        },
        "input": {
            "schema": "harn-gibson.browser-input.v1",
            "deliverAs": ["followUp", "steer"],
            "queueEndpoint": "/input",
            "pollEndpoint": "/input/next",
        },
        "style": {
            "schema": "harn-gibson.style-pack.v1",
            "support": "style-pack-v1",
            "activeStylePackId": "gibson",
            "supportedStylePackIds": [style.id for style in STYLE_PACKS],
        },
    }


def test_async_state_accepts_without_immediate_scene_update() -> None:
    state = GibsonServerState(render_mode="async", render_batch_window_ms=0)
    state.pipeline.start = lambda: None  # type: ignore[method-assign]
    payload = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "input",
        "phase": "before",
        "title": "Input intercept",
        "summary": "input",
        "payload": {"type": "input", "text": "hi", "source": "test"},
    }

    accepted = apply_event_to_scene(payload, state)
    assert "schema" not in accepted
    assert accepted["ok"] is True
    assert accepted["renderMode"] == "async"
    assert accepted["sceneRevision"] == 0
    state.pipeline.stop()


def test_build_state_from_env(tmp_path: Path) -> None:
    project_root = tmp_path / "tiny-project"
    project_root.mkdir()
    state = build_state_from_env(
        {
            "HARN_GIBSON_RENDER_MODE": "async",
            "HARN_GIBSON_RENDER_BATCH_MS": "5",
            "HARN_GIBSON_RENDER_TIMING": "scheduled",
            "HARN_GIBSON_STYLE": "mainframe",
        }
    )
    renderer_state = build_state_from_env(
        {
            "HARN_GIBSON_RENDERER_COMMAND": json.dumps([sys.executable, "-c", "print('{}')"]),
            "HARN_GIBSON_RENDERER_TIMEOUT_MS": "250",
        }
    )
    model_renderer_state = build_state_from_env(
        {
            "HARN_GIBSON_RENDERER_MODEL_COMMAND": json.dumps([sys.executable, "-c", "print('{}')"]),
            "HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS": "125",
            "HARN_GIBSON_RENDERER_COMMAND": json.dumps([sys.executable, "-c", "print('external')"]),
        }
    )
    project_state = build_state_from_env(
        {
            "HARN_GIBSON_PROJECT_ROOT": str(project_root),
            "HARN_GIBSON_PROJECT_NAME": "tiny dogfood",
        }
    )
    derived_project_state = build_state_from_env({"HARN_GIBSON_PROJECT_ROOT": str(project_root)})
    context_state = build_state_from_env(
        {
            "HARN_GIBSON_RENDERER_COMPACTION_EVENTS": "9",
            "HARN_GIBSON_RENDERER_MAX_RECENT_PLANS": "3",
            "HARN_GIBSON_RENDERER_MAX_RECENT_LOG_ENTRIES": "4",
            "HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS": "80",
            "HARN_GIBSON_RENDERER_MAX_VISUAL_ANCHORS": "5",
            "HARN_GIBSON_RENDERER_MAX_VISUAL_OBJECTS_PER_ANCHOR": "4",
            "HARN_GIBSON_RENDERER_MAX_VISUAL_RECENT_ITEMS": "6",
            "HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES": "7",
            "HARN_GIBSON_RENDERER_MAX_REPO_CHILDREN": "2",
            "HARN_GIBSON_RENDERER_MAX_TOUCHED_FILES": "8",
            "HARN_GIBSON_RENDERER_MAX_TOUCHED_PATH_CHARS": "40",
            "HARN_GIBSON_RENDERER_MAX_WORLD_ENTITIES": "11",
            "HARN_GIBSON_RENDERER_MAX_SEMANTIC_FILES": "12",
            "HARN_GIBSON_RENDERER_MAX_SEMANTIC_EDGES": "13",
            "HARN_GIBSON_RENDERER_MAX_SEMANTIC_SYMBOLS": "14",
        }
    )
    clamped_context = renderer_context_config_from_env(
        {
            "HARN_GIBSON_RENDERER_COMPACTION_EVENTS": "0",
            "HARN_GIBSON_RENDERER_MAX_RECENT_PLANS": "-5",
            "HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES": "bad",
        }
    )

    assert state.pipeline.mode == "async"
    assert state.pipeline.batch_window_ms == 5
    assert state.pipeline.timing_mode == "scheduled"
    assert state.style_pack.id == "mainframe"
    assert state.scene.state.metadata["displayStyle"] == "mainframe"
    assert state.pipeline.context_builder.config.display_style == "mainframe"
    assert isinstance(renderer_state.renderer, ExternalRenderer)
    assert renderer_state.renderer.command == (sys.executable, "-c", "print('{}')")
    assert renderer_state.renderer.timeout_seconds == 0.25
    assert isinstance(model_renderer_state.renderer, PromptedModelRenderer)
    assert model_renderer_state.renderer.client.command == (sys.executable, "-c", "print('{}')")  # type: ignore[attr-defined]
    assert model_renderer_state.renderer.client.timeout_seconds == 0.125  # type: ignore[attr-defined]
    assert project_state.pipeline.context_builder.config.project_root == str(project_root)
    assert project_state.pipeline.context_builder.config.project_name == "tiny dogfood"
    assert derived_project_state.pipeline.context_builder.config.project_name == "tiny-project"
    context_config = context_state.pipeline.context_builder.config
    assert context_config.compaction_interval_events == 9
    assert context_config.max_recent_plans == 3
    assert context_config.max_recent_log_entries == 4
    assert context_config.max_prop_preview_chars == 80
    assert context_config.max_visual_anchors == 5
    assert context_config.max_visual_objects_per_anchor == 4
    assert context_config.max_visual_recent_items == 6
    assert context_config.max_repo_entries == 7
    assert context_config.max_repo_children_per_dir == 2
    assert context_config.max_touched_files == 8
    assert context_config.max_touched_path_chars == 40
    assert context_config.max_world_entities == 11
    assert context_config.max_semantic_files == 12
    assert context_config.max_semantic_edges == 13
    assert context_config.max_semantic_symbols == 14
    assert clamped_context.compaction_interval_events == 1
    assert clamped_context.max_recent_plans == 0
    assert clamped_context.max_repo_entries == 64
    assert project_root_from_env(None) is None
    assert project_root_from_env("   ") is None
    assert project_root_from_env(str(project_root)) == str(project_root)
    assert project_name_from_env(None, None) == "harn-gibson"
    assert project_name_from_env("  named  ", None) == "named"
    assert project_name_from_env("", str(project_root)) == "tiny-project"
    assert project_name_from_env(None, "/") == "workspace"
    state.pipeline.stop()
    renderer_state.pipeline.stop()
    model_renderer_state.pipeline.stop()
    project_state.pipeline.stop()
    derived_project_state.pipeline.stop()
    context_state.pipeline.stop()


def test_renderer_interest_from_env_and_build_state() -> None:
    interest_payload = json.dumps(
        {
            "eventTypes": ["tool_call"],
            "phases": ["before"],
            "fallbackRoute": "drop",
            "reason": "dogfood renderer scope",
        }
    )
    rules_payload = json.dumps(
        [
            {
                "eventType": "runtime_error",
                "route": "debug_only",
                "reason": "local diagnostics",
            },
            {
                "eventType": "model_select",
                "route": "drop",
                "sampleEvery": 4,
                "fallbackRoute": "debug_only",
            },
        ]
    )
    interest = renderer_interest_from_env(interest_payload)
    route_rules = route_rules_from_env(rules_payload)
    state = build_state_from_env(
        {
            "HARN_GIBSON_RENDERER_INTEREST": interest_payload,
            "HARN_GIBSON_ROUTE_RULES": rules_payload,
        }
    )

    assert renderer_interest_from_env(None) is None
    assert renderer_interest_from_env("") is None
    assert route_rules_from_env(None) == ()
    assert route_rules_from_env("") == ()
    assert interest is not None
    assert interest.event_types == ("tool_call",)
    assert interest.fallback_route == "drop"
    assert route_rules[0].event_type == "runtime_error"
    assert route_rules[0].route == "debug_only"
    assert route_rules[1].reason == "drop route rule"
    assert route_rules[1].sample_every == 4
    assert route_rules[1].sample_fallback_route == "debug_only"
    assert state.router.renderer_interest == interest
    assert state.router.route_rules["runtime_error"] == route_rules[0]

    for value, message in (
        ("{", "JSON object"),
        ("[]", "invalid"),
        (json.dumps({"fallbackRoute": "renderer_agent"}), "fallback route"),
    ):
        try:
            renderer_interest_from_env(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")

    for value, message in (
        ("{", "JSON list"),
        (json.dumps({"eventType": "x", "route": "drop"}), "invalid"),
        (json.dumps([{"eventType": "x", "route": "stream_buffer"}]), "unsupported"),
    ):
        try:
            route_rules_from_env(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")


def test_format_sse() -> None:
    assert format_sse({"a": 1}) == 'data: {"a":1}\n\n'


def test_cli_harn_args_with_project_defaults() -> None:
    assert cli._harn_args_with_project_defaults(["-p", "hello"]) == [
        "--provider",
        cli.PROJECT_HARN_PROVIDER,
        "--model",
        cli.PROJECT_HARN_MODEL,
        "--thinking",
        cli.PROJECT_HARN_THINKING,
        "--no-extensions",
        "--extension",
        cli.extension_path(),
        "-p",
        "hello",
    ]
    extension = cli.extension_path()
    assert cli._harn_args_with_project_defaults([f"--extension={extension}", "-p", "hello"]) == [
        "--provider",
        cli.PROJECT_HARN_PROVIDER,
        "--model",
        cli.PROJECT_HARN_MODEL,
        "--thinking",
        cli.PROJECT_HARN_THINKING,
        "--no-extensions",
        f"--extension={extension}",
        "-p",
        "hello",
    ]
    supplied = [
        "--provider=custom",
        "--model",
        "custom-model",
        "--thinking=low",
        "-ne",
        "--extension",
        extension,
        "-p",
        "hello",
    ]
    assert cli._harn_args_with_project_defaults(supplied) == supplied
    assert cli._argv_has_option(["--provider=custom"], "--provider") is True
    assert cli._argv_has_option(["--providers=custom"], "--provider") is False
    assert cli._argv_has_extension([extension], extension) is True
    assert cli._argv_has_extension(["--extension", extension], extension) is True
    assert cli._argv_has_extension(["-e", extension], extension) is True
    assert cli._argv_has_extension([f"--extension={extension}"], extension) is True
    assert cli._argv_has_extension([f"-e={extension}"], extension) is True
    assert cli._argv_has_extension(["--extension"], extension) is False
    assert cli._argv_has_extension(["--extension", "other.py"], extension) is False


def test_cli_parser_and_run(monkeypatch: Any, capsys: Any) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as version_exit:
        parser.parse_args(["--version"])
    assert version_exit.value.code == 0
    assert capsys.readouterr().out.strip() == f"harn-gibson {__version__}"
    project_metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == project_metadata["project"]["version"]
    assert parser.parse_args(["extension-path"]).command == "extension-path"
    parsed_dogfood = parser.parse_args(
        ["run", "--no-browser", "--style", "neon-noir", "--cwd", "work", "--", "-p", "hello"]
    )
    assert parsed_dogfood.command == "run"
    assert parsed_dogfood.browser is False
    assert parsed_dogfood.style == "neon-noir"
    assert parsed_dogfood.cwd == "work"
    assert parsed_dogfood.renderer == "gibson1"
    assert parsed_dogfood.renderer_command is None
    assert parsed_dogfood.renderer_timeout_ms == cli.DEFAULT_RENDERER_TIMEOUT_MS
    assert parsed_dogfood.harn_args == ["--", "-p", "hello"]
    parsed_capture = parser.parse_args(
        [
            "capture",
            "--no-browser",
            "--style",
            "mainframe",
            "--cwd",
            "capture-work",
            "--event-log",
            "events.jsonl",
            "--renderer-command",
            "python renderer.py",
            "--renderer-timeout-ms",
            "1234",
            "--split-every",
            "200",
            "--list-trajectories",
            "--trajectory",
            "tiny-project",
            "--",
            "-p",
            "capture",
        ]
    )
    assert parsed_capture.command == "capture"
    assert parsed_capture.browser is False
    assert parsed_capture.style == "mainframe"
    assert parsed_capture.cwd == "capture-work"
    assert parsed_capture.event_log == "events.jsonl"
    assert parsed_capture.renderer == "dogfood"
    assert parsed_capture.renderer_command == "python renderer.py"
    assert parsed_capture.renderer_timeout_ms == "1234"
    assert parsed_capture.split_every == 200
    assert parsed_capture.list_trajectories is True
    assert parsed_capture.trajectory == "tiny-project"
    assert parsed_capture.harn_args == ["--", "-p", "capture"]
    parsed_capture_list = parser.parse_args(["capture", "--list-trajectories"])
    assert parsed_capture_list.command == "capture"
    assert parsed_capture_list.list_trajectories is True
    parsed_auth = parser.parse_args(["import-codex-auth", "--codex-auth", "codex.json", "--harn-auth", "harn.json"])
    assert parsed_auth.command == "import-codex-auth"
    assert parser.parse_args(["backend-contract"]).command == "backend-contract"
    parsed_catalog = parser.parse_args(["catalog", "--kind", "effect", "--tag", "camera", "--compact"])
    assert parsed_catalog.command == "catalog"
    assert parsed_catalog.kind == "effect"
    assert parsed_catalog.tag == ["camera"]
    assert parsed_catalog.entry_ids is None
    assert parsed_catalog.compact is True
    parsed_replay = parser.parse_args(
        [
            "replay",
            "fixture.json",
            "--output-scene",
            "scene.json",
            "--output-result",
            "result.json",
            "--output-render-contexts",
            "contexts.json",
            "--output-render-prompts",
            "prompts.json",
            "--output-render-chunks",
            "chunks.json",
            "--render-chunk-size",
            "3",
            "--render-chunk-review",
            "chunks.html",
            "--render-prompt-review",
            "prompts.html",
            "--output-render-intents",
            "intents.json",
            "--render-intent-review",
            "intents.html",
            "--review-dir",
            "review",
            "--output-timeline",
            "timeline.json",
            "--timeline-screenshot-dir",
            "frames",
            "--screenshot",
            "scene.png",
            "--screenshot-width",
            "800",
            "--screenshot-height",
            "600",
            "--style",
            "mainframe",
            "--renderer-model-command",
            '["python", "model-renderer.py"]',
            "--renderer-model-timeout-ms",
            "2500",
            "--project-root",
            "workspace",
            "--project-name",
            "tiny-project",
        ]
    )
    assert parsed_replay.command == "replay"
    assert parsed_replay.path == "fixture.json"
    assert parsed_replay.output_scene == "scene.json"
    assert parsed_replay.output_result == "result.json"
    assert parsed_replay.output_render_contexts == "contexts.json"
    assert parsed_replay.output_render_prompts == "prompts.json"
    assert parsed_replay.output_render_chunks == "chunks.json"
    assert parsed_replay.render_chunk_size == 3
    assert parsed_replay.render_chunk_review == "chunks.html"
    assert parsed_replay.render_prompt_review == "prompts.html"
    assert parsed_replay.output_render_intents == "intents.json"
    assert parsed_replay.render_intent_review == "intents.html"
    assert parsed_replay.review_dir == "review"
    assert parsed_replay.output_timeline == "timeline.json"
    assert parsed_replay.timeline_screenshot_dir == "frames"
    assert parsed_replay.screenshot == "scene.png"
    assert parsed_replay.screenshot_width == 800
    assert parsed_replay.screenshot_height == 600
    assert parsed_replay.style == "mainframe"
    assert parsed_replay.renderer_model_command == '["python", "model-renderer.py"]'
    assert parsed_replay.renderer_model_timeout_ms == "2500"
    assert parsed_replay.project_root == "workspace"
    assert parsed_replay.project_name == "tiny-project"
    parsed_watch = parser.parse_args(
        [
            "watch-replay",
            "fixture.json",
            "--host",
            "0.0.0.0",
            "--port",
            "8766",
            "--no-browser",
            "--no-hold",
            "--start-delay-ms",
            "250",
            "--step-delay-ms",
            "125",
            "--playback-timing",
            "real-time",
            "--speed",
            "2.5",
            "--max-step-delay-ms",
            "5000",
            "--start-step",
            "2",
            "--end-step",
            "4",
            "--no-check-expectations",
            "--style",
            "satellite-uplink",
            "--renderer-command",
            "python renderer.py",
            "--renderer-timeout-ms",
            "1750",
            "--project-root",
            "workspace",
            "--project-name",
            "tiny-project",
        ]
    )
    assert parsed_watch.command == "watch-replay"
    assert parsed_watch.path == "fixture.json"
    assert parsed_watch.host == "0.0.0.0"
    assert parsed_watch.port == 8766
    assert parsed_watch.browser is False
    assert parsed_watch.hold is False
    assert parsed_watch.start_delay_ms == 250
    assert parsed_watch.step_delay_ms == 125
    assert parsed_watch.playback_timing == "real-time"
    assert parsed_watch.speed == 2.5
    assert parsed_watch.max_step_delay_ms == 5000
    assert parsed_watch.start_step == 2
    assert parsed_watch.end_step == 4
    assert parsed_watch.check_expectations is False
    assert parsed_watch.style == "satellite-uplink"
    assert parsed_watch.renderer_command == "python renderer.py"
    assert parsed_watch.renderer_timeout_ms == "1750"
    assert parsed_watch.project_root == "workspace"
    assert parsed_watch.project_name == "tiny-project"
    parsed_replay_dir = parser.parse_args(
        [
            "replay-dir",
            "examples/replays",
            "--output-result",
            "suite.json",
            "--screenshot-dir",
            "shots",
            "--screenshot-width",
            "1024",
            "--screenshot-height",
            "768",
            "--baseline-dir",
            "baselines",
            "--review-dir",
            "suite-review",
            "--render-chunk-size",
            "5",
            "--style",
            "neon-noir",
            "--renderer-command",
            "python renderer.py",
            "--renderer-timeout-ms",
            "1500",
            "--project-root",
            "workspace",
            "--project-name",
            "tiny-project",
            "--update-baselines",
        ]
    )
    assert parsed_replay_dir.command == "replay-dir"
    assert parsed_replay_dir.path == "examples/replays"
    assert parsed_replay_dir.output_result == "suite.json"
    assert parsed_replay_dir.screenshot_dir == "shots"
    assert parsed_replay_dir.screenshot_width == 1024
    assert parsed_replay_dir.screenshot_height == 768
    assert parsed_replay_dir.baseline_dir == "baselines"
    assert parsed_replay_dir.review_dir == "suite-review"
    assert parsed_replay_dir.render_chunk_size == 5
    assert parsed_replay_dir.style == "neon-noir"
    assert parsed_replay_dir.renderer_command == "python renderer.py"
    assert parsed_replay_dir.renderer_timeout_ms == "1500"
    assert parsed_replay_dir.project_root == "workspace"
    assert parsed_replay_dir.project_name == "tiny-project"
    assert parsed_replay_dir.update_baselines is True
    parsed_event_log = parser.parse_args(
        [
            "event-log-to-replay",
            "events.jsonl",
            "--output",
            "fixture.json",
            "--output-dir",
            "split-fixtures",
            "--output-result",
            "result.json",
            "--name",
            "captured",
            "--review-dir",
            "review",
            "--screenshot-width",
            "640",
            "--screenshot-height",
            "480",
            "--style",
            "mainframe",
            "--renderer-command",
            "python renderer.py",
            "--renderer-timeout-ms",
            "2000",
            "--project-root",
            "workspace",
            "--project-name",
            "tiny-project",
            "--render-chunk-size",
            "2",
            "--split-every",
            "50",
            "--visual-fixture",
            "--no-redact-sensitive",
            "--screenshot-lit-min",
            "0.03",
            "--screenshot-max-channel-min",
            "80",
        ]
    )
    assert parsed_event_log.command == "event-log-to-replay"
    assert parsed_event_log.path == "events.jsonl"
    assert parsed_event_log.output == "fixture.json"
    assert parsed_event_log.output_dir == "split-fixtures"
    assert parsed_event_log.output_result == "result.json"
    assert parsed_event_log.name == "captured"
    assert parsed_event_log.review_dir == "review"
    assert parsed_event_log.screenshot_width == 640
    assert parsed_event_log.screenshot_height == 480
    assert parsed_event_log.style == "mainframe"
    assert parsed_event_log.renderer_command == "python renderer.py"
    assert parsed_event_log.renderer_timeout_ms == "2000"
    assert parsed_event_log.project_root == "workspace"
    assert parsed_event_log.project_name == "tiny-project"
    assert parsed_event_log.render_chunk_size == 2
    assert parsed_event_log.split_every == 50
    assert parsed_event_log.visual_fixture is True
    assert parsed_event_log.redact_sensitive is False
    assert parsed_event_log.screenshot_lit_min == 0.03
    assert parsed_event_log.screenshot_max_channel_min == 80
    assert cli.run(["extension-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("extension.py")
    assert cli.run(["backend-contract"]) == 0
    contract = json.loads(capsys.readouterr().out)
    assert contract["schema"] == "harn-gibson.display-backend-contract.v1"
    assert contract["displayBackend"]["id"] == "browser-canvas"
    assert contract["stylePackSchema"] == "harn-gibson.style-pack.v1"
    assert contract["mutationSchema"] == "harn-gibson.scene-mutation.v1"
    assert contract["activeStylePack"]["id"] == "gibson"
    assert "mainframe" in contract["supportedStylePackIds"]
    assert contract["capabilityProfile"]["timing"]["renderTimingModes"] == ["immediate", "scheduled"]
    assert "terminal_wall" in contract["supportedPrimitiveKinds"]
    assert "spatial_map" in contract["supportedPrimitiveKinds"]
    assert cli.run(["catalog", "--kind", "effect", "--tag", "camera", "--compact"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["schema"] == "harn-gibson.visual-catalog.v1"
    assert catalog["filters"] == {"kind": "effect", "tags": ["camera"], "ids": [], "compact": True}
    assert catalog["primitives"] == []
    assert [entry["id"] for entry in catalog["effects"]] == ["camera_jolt", "camera_path"]

    calls: list[tuple[str, int]] = []

    def fake_run_server(host: str, port: int, *, style: str | None = None) -> None:
        calls.append((host, port, style))

    monkeypatch.setattr("harn_gibson.server.run_server", fake_run_server)
    assert cli.run(["serve", "--host", "0.0.0.0", "--port", "9999", "--style", "mainframe"]) == 0
    assert cli.run([]) == 0
    assert calls == [("0.0.0.0", 9999, "mainframe"), ("127.0.0.1", 8765, None)]
    monkeypatch.setattr(cli, "run_watch_replay", lambda args: 42)
    assert cli.run(["watch-replay", "fixture.json", "--no-browser", "--no-hold"]) == 42


def test_cli_watch_replay_runs_browser_playback(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    replay_path = tmp_path / "watch.json"
    replay_path.write_text(
        json.dumps(
            {
                "name": "watch fixture",
                "expect": {"sceneRevision": 1},
                "steps": [
                    {
                        "type": "mutations",
                        "mutations": [{"op": "patch", "targetId": "status", "props": {"text": "watching"}}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    opened: list[str] = []
    held: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "_hold_display", held.append)
    args = cli.build_parser().parse_args(
        [
            "watch-replay",
            str(replay_path),
            "--start-delay-ms",
            "0",
            "--step-delay-ms",
            "0",
            "--end-step",
            "1",
        ]
    )

    assert cli.run_watch_replay(args) == 0

    captured = capsys.readouterr()
    assert opened and opened[0].startswith("http://127.0.0.1:")
    assert held == opened
    assert "watch-replay 1/1: mutations, revision 1, updates 1" in captured.err
    assert "watched 1 replay steps; scene revision 1" in captured.err


def test_await_replay_directive_polls_and_handles_interrupt(monkeypatch: Any) -> None:
    class _State:
        def __init__(self) -> None:
            self.inputs = BrowserInputQueue()

    state = _State()
    state.inputs.enqueue("make me a million dollars")
    assert cli._await_replay_directive(state) == "make me a million dollars"

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        state.inputs.enqueue("second directive")

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert cli._await_replay_directive(state) == "second directive"
    assert sleeps == [0.4]

    def interrupted_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupted_sleep)
    assert cli._await_replay_directive(state) is None


def test_cli_watch_replay_wait_for_input_gates_playback(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    replay_path = tmp_path / "watch.json"
    replay_path.write_text(
        json.dumps(
            {
                "name": "watch fixture",
                "expect": {"sceneRevision": 1},
                "steps": [
                    {
                        "type": "mutations",
                        "mutations": [{"op": "patch", "targetId": "status", "props": {"text": "watching"}}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(cli, "_hold_display", lambda url: None)
    # the typed directive is the curtain cue: playback starts once it arrives
    monkeypatch.setattr(cli, "_await_replay_directive", lambda state: "make me a million dollars")
    args = cli.build_parser().parse_args(
        [
            "watch-replay",
            str(replay_path),
            "--wait-for-input",
            "--start-delay-ms",
            "0",
            "--step-delay-ms",
            "0",
            "--end-step",
            "1",
        ]
    )
    assert cli.run_watch_replay(args) == 0
    captured = capsys.readouterr()
    assert "directive received: 'make me a million dollars'" in captured.err
    assert "watched 1 replay steps" in captured.err

    # walking away (Ctrl-C) during the wait shuts the display down cleanly
    monkeypatch.setattr(cli, "_await_replay_directive", lambda state: None)
    assert cli.run_watch_replay(args) == 130
    captured = capsys.readouterr()
    assert "closed without a directive" in captured.err


def test_cli_watch_replay_error_paths(monkeypatch: Any, capsys: Any) -> None:
    parser = cli.build_parser()
    bad_start = parser.parse_args(["watch-replay", "fixture.json", "--start-delay-ms", "-1"])
    bad_step = parser.parse_args(["watch-replay", "fixture.json", "--step-delay-ms", "-1"])
    bad_speed = parser.parse_args(["watch-replay", "fixture.json", "--speed", "0"])
    bad_max_step = parser.parse_args(["watch-replay", "fixture.json", "--max-step-delay-ms", "-1"])
    bad_start_step = parser.parse_args(["watch-replay", "fixture.json", "--start-step", "0"])
    bad_end_step = parser.parse_args(["watch-replay", "fixture.json", "--start-step", "3", "--end-step", "2"])

    assert cli.run_watch_replay(bad_start) == 2
    assert cli.run_watch_replay(bad_step) == 2
    assert cli.run_watch_replay(bad_speed) == 2
    assert cli.run_watch_replay(bad_max_step) == 2
    assert cli.run_watch_replay(bad_start_step) == 2
    assert cli.run_watch_replay(bad_end_step) == 2

    from harn_gibson.replay import ReplayExpectationError, ReplayExpectationResult

    def fail_replay(*_args: Any, **_kwargs: Any) -> None:
        raise ReplayExpectationError(
            (
                ReplayExpectationResult(
                    path="revision",
                    op="equals",
                    passed=False,
                    expected=2,
                    actual=1,
                    message="revision expected to equal 2, got 1",
                ),
            )
        )

    monkeypatch.setattr("harn_gibson.replay.play_replay_file", fail_replay)
    failure_args = parser.parse_args(
        ["watch-replay", "fixture.json", "--no-browser", "--no-hold", "--no-check-expectations"]
    )
    assert cli.run_watch_replay(failure_args) == 1

    def interrupt_replay(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("harn_gibson.replay.play_replay_file", interrupt_replay)
    interrupt_args = parser.parse_args(["watch-replay", "fixture.json", "--no-browser", "--no-hold"])
    assert cli.run_watch_replay(interrupt_args) == 130
    captured = capsys.readouterr()
    assert "--start-delay-ms must be non-negative" in captured.err
    assert "--step-delay-ms must be non-negative" in captured.err
    assert "--speed must be positive" in captured.err
    assert "--max-step-delay-ms must be non-negative" in captured.err
    assert "--start-step must be at least 1" in captured.err
    assert "--end-step must be greater than or equal to --start-step" in captured.err
    assert "revision expected to equal 2, got 1" in captured.err
    assert "watch-replay interrupted" in captured.err


def test_cli_replay_renderer_env_helpers(monkeypatch: Any) -> None:
    parser = cli.build_parser()
    for key in cli.REPLAY_STATE_ENV_PASSTHROUGH:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HARN_GIBSON_RENDERER_COMMAND", "python ambient-renderer.py")
    monkeypatch.setenv("HARN_GIBSON_RENDERER_SEMANTIC_GRAPH", "1")
    deterministic = parser.parse_args(["replay", "fixture.json"])
    project_only = parser.parse_args(
        ["replay", "fixture.json", "--project-root", "/tmp/workspace", "--project-name", "fixture workspace"]
    )
    external_no_timeout = parser.parse_args(["replay", "fixture.json", "--renderer-command", "python renderer.py"])
    external_with_timeout = parser.parse_args(
        ["replay", "fixture.json", "--renderer-command", "python renderer.py", "--renderer-timeout-ms", "1500"]
    )
    model_shared_timeout = parser.parse_args(
        ["replay", "fixture.json", "--renderer-model-command", "python model.py", "--renderer-timeout-ms", "2500"]
    )
    model_specific_timeout = parser.parse_args(
        [
            "replay",
            "fixture.json",
            "--renderer-model-command",
            "python model.py",
            "--renderer-model-timeout-ms",
            "3500",
        ]
    )
    renderer_timeout = parser.parse_args(
        ["watch-replay", "fixture.json", "--renderer", "dogfood", "--renderer-timeout-ms", "4500"]
    )
    renderer_no_timeout = parser.parse_args(["replay", "fixture.json", "--renderer", "gibson1"])
    renderer_none = parser.parse_args(["watch-replay", "fixture.json", "--renderer", "none"])
    renderer_spec = parser.parse_args(
        ["replay", "fixture.json", "--renderer", "examples/projections/gibson-sector.json"]
    )
    default_state = cli._replay_state_from_args(deterministic)
    try:
        assert isinstance(default_state.renderer, DeterministicSceneRenderer)
        assert default_state.pipeline.context_builder.config.include_semantic_graph is True
    finally:
        default_state.pipeline.stop()
    project_state = cli._replay_state_from_args(project_only)
    try:
        assert isinstance(project_state.renderer, DeterministicSceneRenderer)
        assert project_state.pipeline.context_builder.config.project_root == "/tmp/workspace"
        assert project_state.pipeline.context_builder.config.project_name == "fixture workspace"
    finally:
        project_state.pipeline.stop()
    state = cli._replay_state_from_args(external_no_timeout)
    try:
        assert isinstance(state.renderer, ExternalRenderer)
        assert state.style_pack.id == "gibson"
    finally:
        state.pipeline.stop()

    assert cli._explicit_replay_renderer_env_from_args(external_no_timeout) == {
        "HARN_GIBSON_RENDERER_COMMAND": "python renderer.py"
    }
    assert cli._explicit_replay_renderer_env_from_args(external_with_timeout) == {
        "HARN_GIBSON_RENDERER_COMMAND": "python renderer.py",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "1500",
    }
    assert cli._explicit_replay_renderer_env_from_args(model_shared_timeout) == {
        "HARN_GIBSON_RENDERER_MODEL_COMMAND": "python model.py",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "2500",
    }
    assert cli._explicit_replay_renderer_env_from_args(model_specific_timeout) == {
        "HARN_GIBSON_RENDERER_MODEL_COMMAND": "python model.py",
        "HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS": "3500",
    }
    renderer_env = cli._explicit_replay_renderer_env_from_args(renderer_timeout)
    assert "gibson_dogfood_renderer.py" in renderer_env["HARN_GIBSON_RENDERER_COMMAND"]
    assert renderer_env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] == "4500"
    renderer_no_timeout_env = cli._explicit_replay_renderer_env_from_args(renderer_no_timeout)
    assert "gibson1_renderer.py" in renderer_no_timeout_env["HARN_GIBSON_RENDERER_COMMAND"]
    assert "HARN_GIBSON_RENDERER_TIMEOUT_MS" not in renderer_no_timeout_env
    assert cli._explicit_replay_renderer_env_from_args(renderer_none) == {"HARN_GIBSON_RENDERER": "none"}
    assert cli._explicit_replay_renderer_env_from_args(renderer_spec) == {
        "HARN_GIBSON_RENDERER": "examples/projections/gibson-sector.json"
    }
    assert cli._explicit_replay_state_env_from_args(project_only) == {
        "HARN_GIBSON_RENDERER_SEMANTIC_GRAPH": "1",
        "HARN_GIBSON_PROJECT_ROOT": "/tmp/workspace",
        "HARN_GIBSON_PROJECT_NAME": "fixture workspace",
    }


def test_cli_replay_writes_outputs(tmp_path: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    scene_path = tmp_path / "scene.json"
    result_path = tmp_path / "result.json"
    contexts_path = tmp_path / "contexts.json"
    prompts_path = tmp_path / "prompts.json"
    chunks_path = tmp_path / "chunks.json"
    chunks_review_path = tmp_path / "chunks.html"
    prompts_review_path = tmp_path / "prompts.html"
    intents_path = tmp_path / "intents.json"
    intents_review_path = tmp_path / "intents.html"
    timeline_path = tmp_path / "timeline.json"
    event = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
    }
    replay_path.write_text(json.dumps({"steps": [{"type": "event", "event": event}]}), encoding="utf-8")

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--output-scene",
                str(scene_path),
                "--output-result",
                str(result_path),
                "--output-render-contexts",
                str(contexts_path),
                "--output-render-prompts",
                str(prompts_path),
                "--output-render-chunks",
                str(chunks_path),
                "--render-chunk-size",
                "1",
                "--render-chunk-review",
                str(chunks_review_path),
                "--render-prompt-review",
                str(prompts_review_path),
                "--output-render-intents",
                str(intents_path),
                "--render-intent-review",
                str(intents_review_path),
                "--output-timeline",
                str(timeline_path),
                "--style",
                "mainframe",
            ]
        )
        == 0
    )

    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["revision"] == 1
    assert scene["metadata"]["displayStyle"] == "mainframe"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    contexts = json.loads(contexts_path.read_text(encoding="utf-8"))
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks_review = chunks_review_path.read_text(encoding="utf-8")
    prompts_review = prompts_review_path.read_text(encoding="utf-8")
    intents = json.loads(intents_path.read_text(encoding="utf-8"))
    intents_review = intents_review_path.read_text(encoding="utf-8")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    assert result["steps"][0]["updates"] == 1
    assert result["rendererContexts"][0]["context"]["mode"] == "compaction"
    assert contexts["contextCount"] == 1
    assert contexts["contexts"][0]["context"]["project"]["displayStyle"] == "mainframe"
    assert prompts["schema"] == "harn-gibson.replay-renderer-prompts.v1"
    assert prompts["promptCount"] == 1
    assert prompts["prompts"][0]["metadata"]["displayStyle"] == "mainframe"
    assert chunks["schema"] == "harn-gibson.replay-renderer-chunks.v1"
    assert chunks["chunkCount"] == 1
    assert chunks["chunkSize"] == 1
    assert chunks["chunks"][0]["displayStyles"] == ["mainframe"]
    assert chunks["chunks"][0]["prompts"][0]["metadata"]["eventTypes"] == ["tool_call"]
    assert "renderer chunk review" in chunks_review
    assert "window.__gibsonRendererChunks" in chunks_review
    assert "renderer prompt review" in prompts_review
    assert "window.__gibsonRendererPrompts" in prompts_review
    assert intents["schema"] == "harn-gibson.replay-render-intents.v1"
    assert intents["intentCount"] == 1
    assert intents["intents"][0]["intent"]["renderer"] == "deterministic"
    assert "render intent review" in intents_review
    assert "deterministic" in intents_review
    assert result["frames"][0]["scene"]["metadata"]["displayStyle"] == "mainframe"
    assert timeline["frameCount"] == 1
    assert timeline["frames"][0]["step"]["sceneRevision"] == 1
    assert capsys.readouterr().out.strip() == "replayed 1 steps; scene revision 1"


def test_cli_replay_without_outputs(tmp_path: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "message_update",
                            "phase": "during",
                            "title": "Stream update",
                            "summary": "assistant stream {delta}",
                            "payload": {"type": "message_update", "assistantMessageEvent": {"delta": "ok"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert cli.run(["replay", str(replay_path)]) == 0
    assert capsys.readouterr().out.strip() == "replayed 1 steps; scene revision 1"


def test_cli_replay_captures_timeline_screenshots(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    screenshot_dir = tmp_path / "frames"
    calls: list[tuple[int, Path, int, int]] = []
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "message_update",
                            "phase": "during",
                            "title": "Stream update",
                            "summary": "assistant stream {delta}",
                            "payload": {"type": "message_update", "assistantMessageEvent": {"delta": "ok"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_capture_frames(result: Any, output_dir: str | Path, *, width: int, height: int) -> tuple[Any, ...]:
        calls.append((len(result.frames), Path(output_dir), width, height))
        screenshot = BrowserScreenshotResult(
            Path(output_dir) / "frame-0000.png",
            "http://127.0.0.1:1",
            result.frames[0].scene["revision"],
            width,
            height,
            {"nonblank": True},
        )
        return (ReplayFrameScreenshot(0, result.steps[0], screenshot.to_dict()),)

    monkeypatch.setattr("harn_gibson.replay.capture_replay_frame_screenshots", fake_capture_frames)

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--timeline-screenshot-dir",
                str(screenshot_dir),
                "--screenshot-width",
                "640",
                "--screenshot-height",
                "480",
            ]
        )
        == 0
    )

    manifest = json.loads((screenshot_dir / "manifest.json").read_text(encoding="utf-8"))
    review_html = (screenshot_dir / "index.html").read_text(encoding="utf-8")
    assert calls == [(1, screenshot_dir, 640, 480)]
    assert manifest["screenshotCount"] == 1
    assert manifest["frames"][0]["screenshot"]["path"] == str(screenshot_dir / "frame-0000.png")
    assert "unnamed replay timeline review" in review_html
    assert 'src="frame-0000.png"' in review_html
    assert 'id="timelineScrubber"' in review_html
    assert "window.__gibsonReplayFrames" in review_html
    assert capsys.readouterr().out.splitlines() == [
        f"captured replay timeline screenshots: {screenshot_dir} (1 frames)",
        "replayed 1 steps; scene revision 1",
    ]


def test_cli_replay_writes_review_bundle(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    review_dir = tmp_path / "review"
    calls: list[tuple[int, int, Path, int, int]] = []
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "tool_call",
                            "phase": "before",
                            "title": "Tool preflight",
                            "summary": "bash starting",
                            "payload": {"type": "tool_call", "toolName": "bash"},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_capture_frames(result: Any, output_dir: str | Path, *, width: int, height: int) -> tuple[Any, ...]:
        calls.append((len(result.frames), len(result.renderer_contexts), Path(output_dir), width, height))
        screenshot = BrowserScreenshotResult(
            Path(output_dir) / "frame-0000.png",
            "http://127.0.0.1:1",
            result.frames[0].scene["revision"],
            width,
            height,
            {"nonblank": True},
        )
        return (ReplayFrameScreenshot(0, result.steps[0], screenshot.to_dict()),)

    monkeypatch.setattr("harn_gibson.replay.capture_replay_frame_screenshots", fake_capture_frames)

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--review-dir",
                str(review_dir),
                "--screenshot-width",
                "640",
                "--screenshot-height",
                "480",
            ]
        )
        == 0
    )

    manifest = json.loads((review_dir / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((review_dir / "result.json").read_text(encoding="utf-8"))
    contexts = json.loads((review_dir / "renderer-contexts.json").read_text(encoding="utf-8"))
    prompts = json.loads((review_dir / "renderer-prompts.json").read_text(encoding="utf-8"))
    chunks = json.loads((review_dir / "renderer-chunks.json").read_text(encoding="utf-8"))
    intents = json.loads((review_dir / "render-intents.json").read_text(encoding="utf-8"))
    frame_manifest = json.loads((review_dir / "frames" / "manifest.json").read_text(encoding="utf-8"))
    overview = (review_dir / "index.html").read_text(encoding="utf-8")
    frame_review = (review_dir / "frames" / "index.html").read_text(encoding="utf-8")
    intent_review = (review_dir / "render-intents.html").read_text(encoding="utf-8")
    chunk_review = (review_dir / "renderer-chunks.html").read_text(encoding="utf-8")

    assert calls == [(1, 1, review_dir / "frames", 640, 480)]
    assert manifest["schema"] == "harn-gibson.replay-review-bundle.v1"
    assert manifest["artifacts"]["rendererContexts"] == "renderer-contexts.json"
    assert manifest["artifacts"]["rendererPrompts"] == "renderer-prompts.json"
    assert manifest["artifacts"]["rendererChunks"] == "renderer-chunks.json"
    assert manifest["artifacts"]["rendererChunkReview"] == "renderer-chunks.html"
    assert manifest["artifacts"]["rendererPromptReview"] == "renderer-prompts.html"
    assert manifest["artifacts"]["frameReview"] == "frames/index.html"
    assert manifest["contextCount"] == 1
    assert manifest["promptCount"] == 1
    assert manifest["chunkCount"] == 1
    assert manifest["renderChunkSize"] == 4
    assert manifest["intentCount"] == 1
    assert manifest["screenshotCount"] == 1
    assert result["rendererContexts"][0]["context"]["mode"] == "compaction"
    assert contexts["contextCount"] == 1
    assert prompts["promptCount"] == 1
    assert chunks["chunkCount"] == 1
    assert chunks["chunks"][0]["contextIndexes"] == [0]
    assert intents["intentCount"] == 1
    assert frame_manifest["screenshotCount"] == 1
    assert "unnamed replay replay review" in overview
    assert 'href="frames/index.html"' in overview
    assert "window.__gibsonReplayReview" in overview
    assert "Renderer Chunks JSON" in overview
    assert "Renderer Chunk Review" in overview
    assert "window.__gibsonReplayFrames" in frame_review
    assert "window.__gibsonRendererChunks" in chunk_review
    assert "renderer prompt review" in (review_dir / "renderer-prompts.html").read_text(encoding="utf-8")
    assert "render intent review" in intent_review
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay review bundle: {review_dir} (1 frames)",
        "replayed 1 steps; scene revision 1",
    ]


def test_cli_replay_captures_screenshot(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    screenshot_path = tmp_path / "replay.png"
    calls: list[tuple[int, int, int]] = []
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "tool_call",
                            "phase": "before",
                            "title": "Tool preflight",
                            "summary": "bash starting with {command}",
                            "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_capture(state: Any, path: str, *, width: int, height: int) -> BrowserScreenshotResult:
        calls.append((state.scene.state.revision, width, height))
        return BrowserScreenshotResult(Path(path), "http://127.0.0.1:1", state.scene.state.revision, width, height)

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--screenshot",
                str(screenshot_path),
                "--screenshot-width",
                "1024",
                "--screenshot-height",
                "768",
            ]
        )
        == 0
    )

    assert calls == [(1, 1024, 768)]
    assert capsys.readouterr().out.splitlines() == [
        f"captured replay screenshot: {screenshot_path}",
        "replayed 1 steps; scene revision 1",
    ]


def test_cli_replay_reports_expectation_failures(tmp_path: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "steps": [],
                "expect": {
                    "sceneRevision": 99,
                    "checks": [{"path": "primitives.status.props.text", "equals": "wrong"}],
                },
            }
        ),
        encoding="utf-8",
    )

    assert cli.run(["replay", str(replay_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "replay expectation failed: revision expected to equals 99, got 0",
        "replay expectation failed: primitives.status.props.text expected to equals 'wrong', got 'awaiting signal'",
    ]


def test_cli_replay_can_use_model_renderer_command(tmp_path: Any, capsys: Any) -> None:
    script = tmp_path / "model_renderer.py"
    replay_path = tmp_path / "replay.json"
    scene_path = tmp_path / "scene.json"
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
assert payload["schema"] == "harn-gibson.model-renderer-request.v1"
assert payload["metadata"]["renderer"] == "model-command"
assert payload["metadata"]["prompt"]["schema"] == "harn-gibson.renderer-prompt.v1"
json.dump(
    {
        "metadata": {"intent": "cli model replay"},
        "steps": [
            {
                "eventIndex": 0,
                "mutations": [
                    {
                        "op": "patch",
                        "targetId": "status",
                        "props": {"text": "model:replay", "tone": "magenta"},
                    }
                ],
            }
        ],
    },
    sys.stdout,
)
""".lstrip(),
        encoding="utf-8",
    )
    event = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool call",
        "summary": "running pytest",
        "payload": {"type": "tool_call", "toolName": "pytest"},
    }
    replay_path.write_text(
        json.dumps(
            {
                "steps": [{"type": "event", "event": event}],
                "expect": {
                    "sceneRevision": 1,
                    "checks": [
                        {"path": "metadata.displayStyle", "equals": "mainframe"},
                        {"path": "primitives.status.props.text", "equals": "model:replay"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--renderer-model-command",
                json.dumps([sys.executable, str(script)]),
                "--renderer-model-timeout-ms",
                "2000",
                "--style",
                "mainframe",
                "--output-scene",
                str(scene_path),
            ]
        )
        == 0
    )

    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["metadata"]["displayStyle"] == "mainframe"
    assert scene["primitives"]["status"]["props"]["text"] == "model:replay"
    assert capsys.readouterr().out.splitlines() == ["replayed 1 steps; scene revision 1"]


def test_cli_replay_dir_writes_suite_result(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    replay_dir = tmp_path / "replays"
    output = tmp_path / "out" / "suite.json"
    screenshot_dir = tmp_path / "shots"
    captures: list[tuple[int, int, int]] = []
    replay_dir.mkdir()
    event = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "message_update",
        "phase": "during",
        "title": "Stream update",
        "summary": "assistant stream {delta}",
        "payload": {"type": "message_update", "assistantMessageEvent": {"delta": "ok"}},
    }
    (replay_dir / "ok.json").write_text(
        json.dumps({"steps": [{"type": "event", "event": event}], "expect": {"sceneRevision": 1}}),
        encoding="utf-8",
    )

    def fake_capture(state: Any, path: str | Path, *, width: int, height: int) -> BrowserScreenshotResult:
        assert state.scene.state.metadata["displayStyle"] == "neon-noir"
        captures.append((state.scene.state.revision, width, height))
        return BrowserScreenshotResult(Path(path), "http://127.0.0.1:1", state.scene.state.revision, width, height)

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    assert (
        cli.run(
            [
                "replay-dir",
                str(replay_dir),
                "--output-result",
                str(output),
                "--screenshot-dir",
                str(screenshot_dir),
                "--screenshot-width",
                "1024",
                "--screenshot-height",
                "768",
                "--style",
                "neon-noir",
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["files"][0]["screenshot"]["path"] == str(screenshot_dir / "ok.png")
    assert captures == [(1, 1024, 768)]
    assert capsys.readouterr().out.splitlines() == [
        f"ok ok.json: 1 steps, revision 1, screenshot {screenshot_dir / 'ok.png'}",
        "replayed 1 replay files; 0 failed",
    ]


def test_cli_replay_dir_writes_suite_result_without_screenshots(tmp_path: Any, capsys: Any) -> None:
    replay_dir = tmp_path / "replays"
    replay_dir.mkdir()
    (replay_dir / "ok.json").write_text(
        json.dumps({"steps": [{"type": "mutations", "mutations": []}], "expect": {"sceneRevision": 0}}),
        encoding="utf-8",
    )

    assert cli.run(["replay-dir", str(replay_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "ok ok.json: 1 steps, revision 0",
        "replayed 1 replay files; 0 failed",
    ]


def test_cli_replay_dir_writes_review_bundle(
    tmp_path: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    replay_dir = tmp_path / "replays"
    review_dir = tmp_path / "suite-review"
    replay_dir.mkdir()
    (replay_dir / "ok.json").write_text(
        json.dumps(
            {
                "name": "suite fixture",
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "tool_call",
                            "phase": "before",
                            "title": "Tool call",
                            "summary": "bash starting",
                            "payload": {"type": "tool_call", "toolName": "bash"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, Path, int, int]] = []

    def fake_capture(result: Any, output_dir: str | Path, *, width: int, height: int) -> tuple[Any, ...]:
        calls.append((result.name, Path(output_dir), width, height))
        screenshot = BrowserScreenshotResult(
            Path(output_dir) / "frame-0000.png",
            "http://127.0.0.1:1",
            result.frames[0].scene["revision"],
            width,
            height,
            {"nonblank": True},
        )
        return (ReplayFrameScreenshot(0, result.steps[0], screenshot.to_dict()),)

    monkeypatch.setattr("harn_gibson.replay.capture_replay_frame_screenshots", fake_capture)

    assert (
        cli.run(
            [
                "replay-dir",
                str(replay_dir),
                "--review-dir",
                str(review_dir),
                "--screenshot-width",
                "400",
                "--screenshot-height",
                "300",
                "--render-chunk-size",
                "2",
            ]
        )
        == 0
    )

    manifest = json.loads((review_dir / "manifest.json").read_text(encoding="utf-8"))
    assert calls == [("suite fixture", review_dir / "files" / "ok" / "frames", 400, 300)]
    assert manifest["schema"] == "harn-gibson.replay-suite-review.v1"
    assert manifest["total"] == 1
    assert manifest["failed"] == 0
    assert manifest["renderChunkSize"] == 2
    assert manifest["files"][0]["review"] == "files/ok/index.html"
    assert (review_dir / "index.html").exists()
    assert (review_dir / "files" / "ok" / "renderer-chunks.html").exists()
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay suite review bundle: {review_dir} (1 files, 0 failed)",
        "ok ok.json: 1 steps, revision 1",
        "replayed 1 replay files; 0 failed",
    ]


def test_cli_replay_dir_can_use_external_renderer_command(tmp_path: Any, capsys: Any) -> None:
    replay_dir = tmp_path / "replays"
    output = tmp_path / "suite.json"
    script = tmp_path / "renderer.py"
    replay_dir.mkdir()
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
request = payload["requests"][-1]["event"]
assert payload["schema"] == "harn-gibson.external-renderer-request.v1"
assert payload["context"]["schema"] == "harn-gibson.renderer-context.v1"
assert payload["scene"]["metadata"]["displayStyle"] == "neon-noir"
json.dump(
    {
        "metadata": {"intent": "cli external replay-dir"},
        "steps": [
            {
                "eventIndex": 0,
                "mutations": [
                    {
                        "op": "patch",
                        "targetId": "status",
                        "props": {"text": "external:" + request["eventType"], "tone": "cyan"},
                    }
                ],
            }
        ],
    },
    sys.stdout,
)
""".lstrip(),
        encoding="utf-8",
    )
    event = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool call",
        "summary": "running tests",
        "payload": {"type": "tool_call", "toolName": "pytest"},
    }
    (replay_dir / "ok.json").write_text(
        json.dumps(
            {
                "steps": [{"type": "event", "event": event}],
                "expect": {
                    "sceneRevision": 1,
                    "checks": [{"path": "primitives.status.props.text", "equals": "external:tool_call"}],
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        cli.run(
            [
                "replay-dir",
                str(replay_dir),
                "--output-result",
                str(output),
                "--renderer-command",
                json.dumps([sys.executable, str(script)]),
                "--renderer-timeout-ms",
                "2000",
                "--style",
                "neon-noir",
            ]
        )
        == 0
    )

    suite = json.loads(output.read_text(encoding="utf-8"))
    assert suite["ok"] is True
    assert suite["files"][0]["expectations"] == 2
    assert capsys.readouterr().out.splitlines() == [
        "ok ok.json: 1 steps, revision 1",
        "replayed 1 replay files; 0 failed",
    ]


def test_cli_replay_dir_updates_and_checks_baselines(tmp_path: Any, capsys: Any) -> None:
    replay_dir = tmp_path / "replays"
    baseline_dir = tmp_path / "baselines"
    output = tmp_path / "suite.json"
    replay_dir.mkdir()
    (replay_dir / "ok.json").write_text(
        json.dumps({"steps": [{"type": "mutations", "mutations": []}], "expect": {"sceneRevision": 0}}),
        encoding="utf-8",
    )

    assert cli.run(["replay-dir", str(replay_dir), "--update-baselines"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "--update-baselines requires --baseline-dir"

    assert (
        cli.run(
            [
                "replay-dir",
                str(replay_dir),
                "--baseline-dir",
                str(baseline_dir),
                "--update-baselines",
            ]
        )
        == 0
    )
    assert (baseline_dir / "ok.json").exists()
    assert capsys.readouterr().out.splitlines() == [
        f"ok ok.json: 1 steps, revision 0, baseline updated {baseline_dir / 'ok.json'}",
        "replayed 1 replay files; 0 failed",
    ]

    assert (
        cli.run(
            [
                "replay-dir",
                str(replay_dir),
                "--baseline-dir",
                str(baseline_dir),
                "--output-result",
                str(output),
            ]
        )
        == 0
    )
    suite = json.loads(output.read_text(encoding="utf-8"))
    assert suite["files"][0]["baseline"]["ok"] is True
    assert capsys.readouterr().out.splitlines() == [
        f"ok ok.json: 1 steps, revision 0, baseline checked {baseline_dir / 'ok.json'}",
        "replayed 1 replay files; 0 failed",
    ]


def test_cli_replay_dir_reports_failures(tmp_path: Any, capsys: Any) -> None:
    replay_dir = tmp_path / "replays"
    replay_dir.mkdir()
    (replay_dir / "bad.json").write_text(
        json.dumps({"steps": [], "expect": {"sceneRevision": 1}}),
        encoding="utf-8",
    )

    assert cli.run(["replay-dir", str(replay_dir)]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == "replayed 1 replay files; 1 failed"
    assert "failed bad.json: replay expectations failed" in captured.err


def test_cli_event_log_to_replay_writes_and_prints(tmp_path: Any, capsys: Any) -> None:
    event_log = tmp_path / "events.jsonl"
    output = tmp_path / "fixtures" / "captured.json"
    result_path = tmp_path / "results" / "captured-result.json"
    event_log.write_text(
        json.dumps(
            {
                "sequence": 1,
                "timestampMs": 10,
                "source": "test",
                "eventType": "message_update",
                "phase": "during",
                "title": "Stream update",
                "summary": "assistant stream ok",
                "payload": {"type": "message_update", "assistantMessageEvent": {"delta": "ok"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output",
                str(output),
                "--output-result",
                str(result_path),
                "--name",
                "captured dogfood",
                "--visual-fixture",
                "--screenshot-lit-min",
                "0.04",
                "--screenshot-max-channel-min",
                "90",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture: {output} (1 events)",
        f"wrote event-log replay result: {result_path}",
    ]
    written = json.loads(output.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert written["name"] == "captured dogfood"
    assert written["metadata"]["eventCount"] == 1
    assert written["metadata"]["redaction"] == {"enabled": True, "count": 0}
    assert written["metadata"]["visualFixture"] is True
    assert written["metadata"]["captureSummary"]["eventTypes"] == ["message_update"]
    assert written["screenshotExpect"]["checks"] == [
        {"path": "canvasMetrics.litRatio", "min": 0.04},
        {"path": "canvasMetrics.maxChannelTotal", "min": 90},
    ]
    assert written["steps"][0]["event"]["eventType"] == "message_update"
    assert result["schema"] == "harn-gibson.replay-result.v1"
    assert result["name"] == "captured dogfood"
    assert result["metadata"]["captureSummary"]["eventTypes"] == ["message_update"]

    assert cli.run(["event-log-to-replay", str(event_log)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["name"] == "event log: events.jsonl"
    assert printed["steps"][0]["type"] == "event"


def test_cli_event_log_to_replay_writes_split_chunks(tmp_path: Any, capsys: Any) -> None:
    event_log = tmp_path / "events.jsonl"
    output_dir = tmp_path / "split"
    result_path = tmp_path / "results" / "split-result.json"
    events = [
        {
            "sequence": index,
            "timestampMs": 10 + index,
            "source": "test",
            "eventType": "tool_call",
            "phase": "before",
            "title": "Tool preflight",
            "summary": "bash starting",
            "payload": {"type": "tool_call", "toolName": "bash"},
        }
        for index in range(1, 4)
    ]
    event_log.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output-dir",
                str(output_dir),
                "--output-result",
                str(result_path),
                "--split-every",
                "2",
                "--name",
                "captured dogfood",
                "--visual-fixture",
            ]
        )
        == 0
    )

    first_path = output_dir / "captured-dogfood-0001.json"
    second_path = output_dir / "captured-dogfood-0002.json"
    manifest_path = output_dir / "manifest.json"
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture chunk: {first_path} (2 events)",
        f"wrote replay fixture chunk: {second_path} (1 events)",
        f"wrote event-log split manifest: {manifest_path} (2 chunks, 3 events)",
        f"wrote event-log split replay result: {result_path}",
    ]
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert first["name"] == "captured dogfood chunk 1/2"
    assert first["metadata"]["eventLogChunk"]["endEventOffset"] == 1
    assert first["metadata"]["redaction"] == {"enabled": True, "count": 0}
    assert first["metadata"]["visualFixture"] is True
    assert len(first["steps"]) == 2
    assert second["metadata"]["eventLogChunk"]["startEventOffset"] == 2
    assert len(second["steps"]) == 1
    assert manifest["schema"] == "harn-gibson.event-log-split.v1"
    assert manifest["redaction"] == {"enabled": True, "count": 0}
    assert manifest["captureSummary"]["eventTypeCounts"] == {"tool_call": 3}
    assert [entry["path"] for entry in manifest["fixtures"]] == [
        "captured-dogfood-0001.json",
        "captured-dogfood-0002.json",
    ]
    assert result["schema"] == "harn-gibson.replay-suite-result.v1"
    assert result["total"] == 2
    assert result["splitManifest"]["schema"] == "harn-gibson.event-log-split.v1"
    assert result["splitManifest"]["chunkCount"] == 2
    assert result["captureSummary"] == manifest["captureSummary"]
    assert result["summary"]["eventSummary"]["eventTypeCounts"] == {"tool_call": 3}

    output_dir_without_result = tmp_path / "split-without-result"
    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output-dir",
                str(output_dir_without_result),
                "--split-every",
                "2",
                "--name",
                "captured dogfood",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture chunk: {output_dir_without_result / 'captured-dogfood-0001.json'} (2 events)",
        f"wrote replay fixture chunk: {output_dir_without_result / 'captured-dogfood-0002.json'} (1 events)",
        f"wrote event-log split manifest: {output_dir_without_result / 'manifest.json'} (2 chunks, 3 events)",
    ]


def test_cli_event_log_to_replay_split_argument_errors(tmp_path: Any, capsys: Any) -> None:
    event_log = tmp_path / "events.jsonl"
    event_log.write_text("{}\n", encoding="utf-8")
    output_dir = tmp_path / "split"

    assert cli.run(["event-log-to-replay", str(event_log), "--split-every", "0", "--output-dir", str(output_dir)]) == 2
    assert "--split-every must be positive" in capsys.readouterr().err
    assert cli.run(["event-log-to-replay", str(event_log), "--split-every", "2"]) == 2
    assert "--split-every requires --output-dir" in capsys.readouterr().err
    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--split-every",
                "2",
                "--output-dir",
                str(output_dir),
                "--output",
                str(tmp_path / "fixture.json"),
            ]
        )
        == 2
    )
    assert "--split-every cannot be used with --output" in capsys.readouterr().err
    assert cli.run(["event-log-to-replay", str(event_log), "--output-dir", str(output_dir)]) == 2
    assert "--output-dir requires --split-every" in capsys.readouterr().err


def test_cli_event_log_to_replay_split_writes_review_bundle(
    tmp_path: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    event_log = tmp_path / "events.jsonl"
    output_dir = tmp_path / "split"
    review_dir = tmp_path / "review"
    events = [
        {
            "sequence": index,
            "timestampMs": 10 + index,
            "source": "test",
            "eventType": "tool_call",
            "phase": "before",
            "title": "Tool preflight",
            "summary": "bash starting",
            "payload": {"type": "tool_call", "toolName": "bash"},
        }
        for index in range(1, 4)
    ]
    event_log.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    calls: list[tuple[str, Path, int, int]] = []

    def fake_capture(result: Any, output_dir: str | Path, *, width: int, height: int) -> tuple[Any, ...]:
        calls.append((result.name, Path(output_dir), width, height))
        screenshot = BrowserScreenshotResult(
            Path(output_dir) / "frame-0000.png",
            "http://127.0.0.1:1",
            result.frames[0].scene["revision"],
            width,
            height,
            {"nonblank": True},
        )
        return (ReplayFrameScreenshot(0, result.steps[0], screenshot.to_dict()),)

    monkeypatch.setattr("harn_gibson.replay.capture_replay_frame_screenshots", fake_capture)

    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output-dir",
                str(output_dir),
                "--split-every",
                "2",
                "--name",
                "captured dogfood",
                "--visual-fixture",
                "--review-dir",
                str(review_dir),
                "--screenshot-width",
                "640",
                "--screenshot-height",
                "480",
                "--render-chunk-size",
                "2",
            ]
        )
        == 0
    )

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    review_manifest = json.loads((review_dir / "manifest.json").read_text(encoding="utf-8"))
    assert calls == [
        ("captured dogfood chunk 1/2", review_dir / "files" / "captured-dogfood-0001" / "frames", 640, 480),
        ("captured dogfood chunk 2/2", review_dir / "files" / "captured-dogfood-0002" / "frames", 640, 480),
    ]
    assert manifest["chunkCount"] == 2
    assert review_manifest["schema"] == "harn-gibson.replay-suite-review.v1"
    assert review_manifest["total"] == 2
    assert review_manifest["failed"] == 0
    assert review_manifest["renderChunkSize"] == 2
    assert review_manifest["splitManifest"]["chunkCount"] == 2
    assert review_manifest["captureSummary"]["eventTypeCounts"] == {"tool_call": 3}
    assert (review_dir / "files" / "captured-dogfood-0001" / "renderer-chunks.html").exists()
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture chunk: {output_dir / 'captured-dogfood-0001.json'} (2 events)",
        f"wrote replay fixture chunk: {output_dir / 'captured-dogfood-0002.json'} (1 events)",
        f"wrote event-log split manifest: {output_dir / 'manifest.json'} (2 chunks, 3 events)",
        f"wrote event-log split review bundle: {review_dir} (2 chunks, 0 failed)",
    ]

    output_dir_with_result = tmp_path / "split-with-result"
    review_dir_with_result = tmp_path / "review-with-result"
    result_path = tmp_path / "results" / "split-review-result.json"
    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output-dir",
                str(output_dir_with_result),
                "--split-every",
                "2",
                "--name",
                "captured dogfood",
                "--visual-fixture",
                "--review-dir",
                str(review_dir_with_result),
                "--output-result",
                str(result_path),
            ]
        )
        == 0
    )
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result_payload["schema"] == "harn-gibson.replay-suite-result.v1"
    assert result_payload["total"] == 2
    assert result_payload["splitManifest"]["chunkCount"] == 2
    assert result_payload["captureSummary"]["eventTypeCounts"] == {"tool_call": 3}
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture chunk: {output_dir_with_result / 'captured-dogfood-0001.json'} (2 events)",
        f"wrote replay fixture chunk: {output_dir_with_result / 'captured-dogfood-0002.json'} (1 events)",
        f"wrote event-log split manifest: {output_dir_with_result / 'manifest.json'} (2 chunks, 3 events)",
        f"wrote event-log split review bundle: {review_dir_with_result} (2 chunks, 0 failed)",
        f"wrote event-log split replay result: {result_path}",
    ]


def test_cli_event_log_to_replay_writes_review_bundle(
    tmp_path: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    event_log = tmp_path / "events.jsonl"
    output = tmp_path / "fixtures" / "captured.json"
    result_output = tmp_path / "fixtures" / "captured-result.json"
    review_dir = tmp_path / "review"
    calls: list[tuple[int, int, Path, int, int]] = []
    event_log.write_text(
        json.dumps(
            {
                "sequence": 1,
                "timestampMs": 10,
                "source": "test",
                "eventType": "tool_call",
                "phase": "before",
                "title": "Tool preflight",
                "summary": "bash starting",
                "payload": {"type": "tool_call", "toolName": "bash"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_capture_frames(result: Any, output_dir: str | Path, *, width: int, height: int) -> tuple[Any, ...]:
        calls.append((len(result.frames), len(result.renderer_contexts), Path(output_dir), width, height))
        screenshot = BrowserScreenshotResult(
            Path(output_dir) / "frame-0000.png",
            "http://127.0.0.1:1",
            result.frames[0].scene["revision"],
            width,
            height,
            {"nonblank": True},
        )
        return (ReplayFrameScreenshot(0, result.steps[0], screenshot.to_dict()),)

    monkeypatch.setattr("harn_gibson.replay.capture_replay_frame_screenshots", fake_capture_frames)

    assert (
        cli.run(
            [
                "event-log-to-replay",
                str(event_log),
                "--output",
                str(output),
                "--output-result",
                str(result_output),
                "--name",
                "captured dogfood",
                "--review-dir",
                str(review_dir),
                "--visual-fixture",
                "--screenshot-width",
                "640",
                "--screenshot-height",
                "480",
                "--render-chunk-size",
                "2",
            ]
        )
        == 0
    )

    manifest = json.loads((review_dir / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((review_dir / "result.json").read_text(encoding="utf-8"))
    result_artifact = json.loads(result_output.read_text(encoding="utf-8"))
    frame_manifest = json.loads((review_dir / "frames" / "manifest.json").read_text(encoding="utf-8"))
    assert calls == [(1, 1, review_dir / "frames", 640, 480)]
    assert manifest["replayName"] == "captured dogfood"
    assert manifest["renderChunkSize"] == 2
    assert manifest["screenshotCount"] == 1
    assert result["metadata"]["captureSummary"]["eventTypes"] == ["tool_call"]
    assert result["metadata"]["visualFixture"] is True
    assert result_artifact == result
    assert frame_manifest["screenshotCount"] == 1
    assert (review_dir / "index.html").exists()
    assert capsys.readouterr().out.splitlines() == [
        f"wrote replay fixture: {output} (1 events)",
        f"wrote event-log review bundle: {review_dir} (1 frames)",
        f"wrote event-log replay result: {result_output}",
    ]


def test_cli_dogfood_launches_display_browser_and_harn(monkeypatch: Any, capsys: Any) -> None:
    server_calls: list[str] = []
    browser_urls: list[str] = []
    harn_calls: list[tuple[list[str], dict[str, str]]] = []
    state = GibsonServerState()
    state.pipeline.stop = lambda: server_calls.append("pipeline.stop")  # type: ignore[method-assign]

    class FakeServer:
        server_address = ("127.0.0.1", 9876)

        def serve_forever(self) -> None:
            server_calls.append("serve_forever")

        def shutdown(self) -> None:
            server_calls.append("shutdown")

        def server_close(self) -> None:
            server_calls.append("server_close")

    def fake_call(command: list[str], env: dict[str, str]) -> int:
        harn_calls.append((command, env))
        return 23

    def fake_build_state(env: dict[str, str] | None = None) -> GibsonServerState:
        if env is not None:
            assert env["HARN_GIBSON_STYLE"] == "neon-noir"
        return state

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", fake_build_state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: browser_urls.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert (
        cli.run(
            [
                "run",
                "--port",
                "0",
                "--harn-bin",
                "harn-dev",
                "--style",
                "neon-noir",
                "--no-codex-auth-import",
                "--no-hold-on-error",
                "--",
                "-p",
                "hello",
            ]
        )
        == 23
    )
    assert browser_urls == ["http://127.0.0.1:9876"]
    assert harn_calls[0][0] == ["harn-dev", "-p", "hello"]
    assert harn_calls[0][1]["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9876/events"
    assert harn_calls[0][1]["HARN_GIBSON_INPUT_ENDPOINT"] == "http://127.0.0.1:9876/input/next"
    assert harn_calls[0][1]["HARN_GIBSON_STYLE"] == "neon-noir"
    assert "pipeline.stop" in server_calls
    assert "shutdown" in server_calls
    assert "server_close" in server_calls
    assert "harn-gibson display: http://127.0.0.1:9876" in capsys.readouterr().err


def test_cli_dogfood_smoke_starts_real_server_on_dynamic_port(tmp_path: Path, capsys: Any) -> None:
    probe_script = tmp_path / "fake_harn_probe.py"
    probe_output = tmp_path / "probe.json"
    probe_script.write_text(
        """
from __future__ import annotations

import json
import os
import sys
import urllib.request

endpoint = os.environ["HARN_GIBSON_ENDPOINT"]
input_endpoint = os.environ["HARN_GIBSON_INPUT_ENDPOINT"]
if not endpoint.endswith("/events"):
    raise SystemExit("unexpected endpoint")
base = endpoint[: -len("/events")]
with urllib.request.urlopen(base + "/health", timeout=2) as response:
    health = json.loads(response.read().decode("utf-8"))
Path = __import__("pathlib").Path
Path(sys.argv[1]).write_text(
    json.dumps({"base": base, "endpoint": endpoint, "inputEndpoint": input_endpoint, "health": health}),
    encoding="utf-8",
)
""".lstrip(),
        encoding="utf-8",
    )

    assert (
        cli.run_dogfood(
            port=0,
            harn_bin=sys.executable,
            harn_args=[str(probe_script), str(probe_output)],
            launch_browser=False,
            codex_auth_import=False,
            hold_on_error=False,
        )
        == 0
    )

    payload = json.loads(probe_output.read_text(encoding="utf-8"))
    assert payload["base"].startswith("http://127.0.0.1:")
    assert payload["endpoint"] == payload["base"] + "/events"
    assert payload["inputEndpoint"] == payload["base"] + "/input/next"
    assert payload["health"]["ok"] is True
    assert payload["health"]["sceneRevision"] == 0
    assert "harn-gibson display: http://127.0.0.1:" in capsys.readouterr().err


def test_cli_dogfood_reports_missing_harn(monkeypatch: Any, capsys: Any) -> None:
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9877)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def fake_call(_command: list[str], env: dict[str, str]) -> int:
        assert env["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9877/events"
        assert "gibson1_renderer.py" in env["HARN_GIBSON_RENDERER_COMMAND"]
        raise FileNotFoundError

    opened: list[str] = []
    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda _env=None: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert cli.run_dogfood(harn_bin="missing-harn", launch_browser=False, codex_auth_import=False) == 127
    assert opened == []
    assert "harn executable not found: missing-harn" in capsys.readouterr().err


def test_cli_dogfood_cwd_injects_project_config(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    harn_calls: list[tuple[list[str], dict[str, str], str | None]] = []
    build_state_envs: list[dict[str, str]] = []
    workspace = tmp_path / "bare-project"
    workspace.mkdir()
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9878)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def fake_call(command: list[str], env: dict[str, str], cwd: str | None = None) -> int:
        harn_calls.append((command, env, cwd))
        return 0

    def fake_build_state(env: dict[str, str] | None = None) -> GibsonServerState:
        assert env is not None
        build_state_envs.append(env)
        return state

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", fake_build_state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert (
        cli.run(
            [
                "run",
                "--cwd",
                str(workspace),
                "--no-browser",
                "--no-codex-auth-import",
                "--no-hold-on-error",
                "--",
                "-p",
                "bootstrap",
            ]
        )
        == 0
    )
    command, env, cwd = harn_calls[0]
    assert cwd == str(workspace.resolve())
    assert build_state_envs[0]["HARN_GIBSON_PROJECT_ROOT"] == str(workspace.resolve())
    assert build_state_envs[0]["HARN_GIBSON_PROJECT_NAME"] == "bare-project"
    assert command == [
        "harn",
        "--provider",
        cli.PROJECT_HARN_PROVIDER,
        "--model",
        cli.PROJECT_HARN_MODEL,
        "--thinking",
        cli.PROJECT_HARN_THINKING,
        "--no-extensions",
        "--extension",
        cli.extension_path(),
        "-p",
        "bootstrap",
    ]
    assert env["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9878/events"
    assert env["HARN_GIBSON_PROJECT_ROOT"] == str(workspace.resolve())
    assert env["HARN_GIBSON_PROJECT_NAME"] == "bare-project"
    assert cli.run_dogfood(cwd=str(tmp_path / "missing"), launch_browser=False, codex_auth_import=False) == 2
    assert "--cwd must be an existing directory" in capsys.readouterr().err


def test_cli_dogfood_applies_env_overrides_to_state_and_harn(monkeypatch: Any) -> None:
    harn_calls: list[tuple[list[str], dict[str, str]]] = []
    build_state_envs: list[dict[str, str]] = []
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9881)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def fake_build_state(env: dict[str, str] | None = None) -> GibsonServerState:
        assert env is not None
        build_state_envs.append(env)
        return state

    def fake_call(command: list[str], env: dict[str, str]) -> int:
        harn_calls.append((command, env))
        return 0

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", fake_build_state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert (
        cli.run_dogfood(
            harn_args=["--", "-p", "capture"],
            launch_browser=False,
            codex_auth_import=False,
            hold_on_error=False,
            env_overrides={
                "HARN_GIBSON_EVENT_LOG": "events.jsonl",
                "HARN_GIBSON_RENDERER_COMMAND": "python renderer.py",
            },
        )
        == 0
    )
    assert build_state_envs[0]["HARN_GIBSON_EVENT_LOG"] == "events.jsonl"
    assert build_state_envs[0]["HARN_GIBSON_RENDERER_COMMAND"] == "python renderer.py"
    assert harn_calls[0][0] == ["harn", "-p", "capture"]
    assert harn_calls[0][1]["HARN_GIBSON_EVENT_LOG"] == "events.jsonl"
    assert harn_calls[0][1]["HARN_GIBSON_RENDERER_COMMAND"] == "python renderer.py"
    assert harn_calls[0][1]["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9881/events"


def test_cli_dogfood_capture_sets_env_and_replay_hint(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []
    event_log = tmp_path / "events.jsonl"

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 7

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)

    assert (
        cli.run(
            [
                "capture",
                "--event-log",
                str(event_log),
                "--renderer-command",
                "python renderer.py",
                "--renderer-timeout-ms",
                "1234",
                "--style",
                "mainframe",
                "--no-browser",
                "--no-codex-auth-import",
                "--no-hold-on-error",
                "--",
                "-p",
                "capture",
            ]
        )
        == 7
    )
    assert event_log.parent.exists()
    assert dogfood_calls == [
        {
            "host": "127.0.0.1",
            "port": 0,
            "harn_bin": "harn",
            "harn_args": ["--", "-p", "capture"],
            "launch_browser": False,
            "codex_auth_import": False,
            "hold_on_error": False,
            "style": "mainframe",
            "cwd": None,
            "env_overrides": {
                "HARN_GIBSON_EVENT_LOG": str(event_log),
                "HARN_GIBSON_RENDERER_COMMAND": "python renderer.py",
                "HARN_GIBSON_RENDERER_TIMEOUT_MS": "1234",
            },
        }
    ]
    stderr = capsys.readouterr().err
    assert f"harn-gibson capture log: {event_log}" in stderr
    assert "harn-gibson capture renderer: python renderer.py" in stderr
    assert "uv run harn-gibson event-log-to-replay" in stderr
    assert f"--output {event_log.with_suffix('.replay.json')}" in stderr
    assert f"--output-result {event_log.with_suffix('.result.json')}" in stderr
    assert "--redact-sensitive" in stderr
    assert f"--review-dir {event_log.with_name('events-review')}" in stderr
    assert "--renderer-command 'python renderer.py'" in stderr
    assert "--renderer-timeout-ms 1234 --style mainframe" in stderr


def test_cli_dogfood_capture_split_hint(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []
    event_log = tmp_path / "long-events.jsonl"

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)

    assert (
        cli.run(
            [
                "capture",
                "--event-log",
                str(event_log),
                "--renderer-command",
                "python renderer.py",
                "--renderer-timeout-ms",
                "1234",
                "--split-every",
                "200",
                "--no-browser",
                "--no-codex-auth-import",
                "--no-hold-on-error",
            ]
        )
        == 0
    )
    assert dogfood_calls[0]["env_overrides"]["HARN_GIBSON_EVENT_LOG"] == str(event_log)
    stderr = capsys.readouterr().err
    assert f"--output-dir {event_log.with_suffix('.replays')}" in stderr
    assert "--split-every 200" in stderr
    assert f"--output-result {event_log.with_suffix('.result.json')}" in stderr
    assert f"--review-dir {event_log.with_name('long-events-review')}" in stderr
    assert f"--output {event_log.with_suffix('.replay.json')}" not in stderr

    assert cli.run_dogfood_capture(split_every=0, launch_browser=False, codex_auth_import=False) == 2
    assert "--split-every must be positive" in capsys.readouterr().err


def test_cli_dogfood_capture_lists_trajectory_presets(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    dogfood_called = False

    def fake_run_dogfood(**_kwargs: Any) -> int:
        nonlocal dogfood_called
        dogfood_called = True
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)

    assert cli.run(["capture", "--list-trajectories"]) == 0
    assert dogfood_called is False
    stdout = capsys.readouterr().out
    assert "available dogfood capture trajectories:" in stdout
    assert "tiny-project" in stdout
    assert "repo-map" in stdout
    assert "depth-2 repository map" in stdout


def test_cli_dogfood_capture_trajectory_preset_creates_workspace_and_prompt(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)
    monkeypatch.setattr(cli.time, "strftime", lambda _format: "20260604-001122")
    monkeypatch.chdir(tmp_path)

    assert (
        cli.run(
            [
                "capture",
                "--trajectory",
                "tiny-project",
                "--no-browser",
                "--no-codex-auth-import",
                "--no-hold-on-error",
            ]
        )
        == 0
    )
    workspace = tmp_path / "test-artifacts" / "dogfood-workspaces" / "tiny-project-20260604-001122"
    event_log = tmp_path / "test-artifacts" / "captures" / "tiny-project-20260604-001122.jsonl"
    assert workspace.is_dir()
    assert dogfood_calls[0]["cwd"] == str(workspace)
    assert dogfood_calls[0]["harn_args"][:2] == ["--", "-p"]
    assert "Initialize a git repository" in dogfood_calls[0]["harn_args"][2]
    assert dogfood_calls[0]["env_overrides"]["HARN_GIBSON_EVENT_LOG"] == str(event_log)
    stderr = capsys.readouterr().err
    assert "harn-gibson capture trajectory: tiny-project" in stderr
    assert f"harn-gibson capture workspace: {workspace}" in stderr
    assert f"--output-dir {event_log.with_suffix('.replays')}" in stderr
    assert "--split-every 200" in stderr
    assert f"--project-root {workspace}" in stderr
    assert "--project-name tiny-project-20260604-001122" in stderr


def test_cli_dogfood_capture_repo_map_trajectory_uses_repo_topology_prompt(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)
    monkeypatch.setattr(cli.time, "strftime", lambda _format: "20260604-004455")
    monkeypatch.chdir(tmp_path)

    assert (
        cli.run(
            [
                "capture",
                "--trajectory",
                "repo-map",
                "--no-browser",
                "--no-codex-auth-import",
                "--no-hold-on-error",
            ]
        )
        == 0
    )
    workspace = tmp_path / "test-artifacts" / "dogfood-workspaces" / "repo-map-20260604-004455"
    event_log = tmp_path / "test-artifacts" / "captures" / "repo-map-20260604-004455.jsonl"
    assert workspace.is_dir()
    assert dogfood_calls[0]["cwd"] == str(workspace)
    assert dogfood_calls[0]["harn_args"][:2] == ["--", "-p"]
    assert "depth-2 project layout" in dogfood_calls[0]["harn_args"][2]
    assert "line-count summary" in dogfood_calls[0]["harn_args"][2]
    assert dogfood_calls[0]["env_overrides"]["HARN_GIBSON_EVENT_LOG"] == str(event_log)
    stderr = capsys.readouterr().err
    assert "harn-gibson capture trajectory: repo-map" in stderr
    assert f"--output-dir {event_log.with_suffix('.replays')}" in stderr
    assert "--split-every 200" in stderr
    assert f"--project-root {workspace}" in stderr
    assert "--project-name repo-map-20260604-004455" in stderr


def test_cli_dogfood_capture_trajectory_respects_overrides(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []
    workspace = tmp_path / "custom-workspace"
    event_log = tmp_path / "custom" / "events.jsonl"

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)

    assert (
        cli.run_dogfood_capture(
            trajectory="tiny-project",
            cwd=str(workspace),
            harn_args=["--", "-p", "custom prompt"],
            event_log=str(event_log),
            split_every=25,
            launch_browser=False,
            codex_auth_import=False,
            hold_on_error=False,
        )
        == 0
    )
    assert workspace.is_dir()
    assert dogfood_calls[0]["cwd"] == str(workspace.resolve())
    assert dogfood_calls[0]["harn_args"] == ["--", "-p", "custom prompt"]
    assert dogfood_calls[0]["env_overrides"]["HARN_GIBSON_EVENT_LOG"] == str(event_log)
    stderr = capsys.readouterr().err
    assert "--split-every 25" in stderr
    assert f"--project-root {workspace.resolve()}" in stderr


def test_cli_dogfood_capture_cwd_resolves_event_log(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_calls: list[dict[str, Any]] = []
    workspace = tmp_path / "workspace"
    launcher_dir = tmp_path / "launcher"
    workspace.mkdir()
    launcher_dir.mkdir()

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)
    monkeypatch.chdir(launcher_dir)

    assert (
        cli.run_dogfood_capture(
            cwd=str(workspace),
            event_log="captures/events.jsonl",
            launch_browser=False,
            codex_auth_import=False,
            hold_on_error=False,
        )
        == 0
    )
    assert dogfood_calls[0]["cwd"] == str(workspace.resolve())
    assert dogfood_calls[0]["env_overrides"]["HARN_GIBSON_EVENT_LOG"] == str(
        launcher_dir / "captures" / "events.jsonl"
    )
    assert (launcher_dir / "captures").is_dir()
    stderr = capsys.readouterr().err
    assert f"--project-root {workspace.resolve()}" in stderr
    assert "--project-name workspace" in stderr


def test_cli_dogfood_capture_rejects_missing_cwd_before_artifacts(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    dogfood_called = False

    def fake_run_dogfood(**_kwargs: Any) -> int:
        nonlocal dogfood_called
        dogfood_called = True
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)
    monkeypatch.chdir(tmp_path)

    assert (
        cli.run_dogfood_capture(
            cwd=str(tmp_path / "missing"),
            event_log="captures/events.jsonl",
            launch_browser=False,
            codex_auth_import=False,
            hold_on_error=False,
        )
        == 2
    )
    assert dogfood_called is False
    assert (tmp_path / "captures").exists() is False
    assert "--cwd must be an existing directory" in capsys.readouterr().err


def test_cli_dogfood_capture_defaults_to_ignored_timestamped_log(monkeypatch: Any, capsys: Any) -> None:
    dogfood_calls: list[dict[str, Any]] = []

    def fake_run_dogfood(**kwargs: Any) -> int:
        dogfood_calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_dogfood", fake_run_dogfood)
    monkeypatch.setattr(cli.time, "strftime", lambda _format: "20260604-001122")

    assert cli.run_dogfood_capture(launch_browser=False, codex_auth_import=False, hold_on_error=False) == 0
    env_overrides = dogfood_calls[0]["env_overrides"]
    assert env_overrides["HARN_GIBSON_EVENT_LOG"] == "test-artifacts/captures/dogfood-20260604-001122.jsonl"
    assert env_overrides["HARN_GIBSON_RENDERER_TIMEOUT_MS"] == cli.DEFAULT_RENDERER_TIMEOUT_MS
    renderer_command = json.loads(env_overrides["HARN_GIBSON_RENDERER_COMMAND"])
    assert renderer_command[0] == sys.executable
    assert renderer_command[1].endswith("examples/renderers/gibson_dogfood_renderer.py")
    assert "--style" not in capsys.readouterr().err


def test_cli_dogfood_capture_trajectory_helpers_reject_unknown() -> None:
    assert cli._has_forwarded_harn_args([]) is False
    assert cli._has_forwarded_harn_args(["--"]) is False
    assert cli._has_forwarded_harn_args(["--", "-p", "prompt"]) is True
    assert cli._dogfood_capture_trajectory_ids() == ("tiny-project", "repo-map")
    assert "repo-map" in cli._dogfood_capture_trajectory_listing()
    assert cli._dogfood_capture_trajectory("repo-map").prompt_path.name == "dogfood-repo-map.md"
    assert "depth-2 project layout" in cli._dogfood_trajectory_prompt("repo-map")
    try:
        cli._prepare_dogfood_capture_options(
            trajectory="unknown",
            cwd=None,
            harn_args=(),
            event_log=None,
            split_every=None,
        )
    except ValueError as error:
        assert "unknown dogfood capture trajectory: unknown" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unknown trajectory should fail")
    try:
        cli._dogfood_trajectory_prompt("unknown")
    except ValueError as error:
        assert "unknown dogfood capture trajectory: unknown" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unknown trajectory prompt should fail")


def test_cli_run_renderer_env_helpers() -> None:
    env = {
        "HARN_GIBSON_RENDERER_COMMAND": "ambient renderer",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "50",
    }

    assert (
        cli._apply_run_renderer_env(
            env,
            renderer="gibson1",
            renderer_command=None,
            renderer_timeout_ms="1234",
        )
        is True
    )
    assert "gibson1_renderer.py" in env["HARN_GIBSON_RENDERER_COMMAND"]
    assert env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] == "1234"

    preserved = {"HARN_GIBSON_RENDERER_COMMAND": "capture renderer"}
    assert (
        cli._apply_run_renderer_env(
            preserved,
            renderer="gibson1",
            renderer_command=None,
            renderer_timeout_ms="5678",
            preserve_existing=True,
        )
        is True
    )
    assert preserved == {
        "HARN_GIBSON_RENDERER_COMMAND": "capture renderer",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "5678",
    }

    disabled = {
        "HARN_GIBSON_RENDERER_COMMAND": "ambient renderer",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "50",
    }
    assert (
        cli._apply_run_renderer_env(
            disabled,
            renderer="none",
            renderer_command=None,
            renderer_timeout_ms="9999",
        )
        is True
    )
    assert disabled["HARN_GIBSON_RENDERER"] == "none"
    assert "HARN_GIBSON_RENDERER_COMMAND" not in disabled
    assert "HARN_GIBSON_RENDERER_TIMEOUT_MS" not in disabled
    assert (
        cli._apply_run_renderer_env(
            {},
            renderer="none",
            renderer_command=None,
            renderer_timeout_ms="9999",
        )
        is True
    )

    custom = {}
    assert (
        cli._apply_run_renderer_env(
            custom,
            renderer="dogfood",
            renderer_command="python renderer.py",
            renderer_timeout_ms="4321",
        )
        is True
    )
    assert custom == {
        "HARN_GIBSON_RENDERER_COMMAND": "python renderer.py",
        "HARN_GIBSON_RENDERER_TIMEOUT_MS": "4321",
    }
    stress = {}
    assert (
        cli._apply_run_renderer_env(
            stress,
            renderer="dogfood",
            renderer_command=None,
            renderer_timeout_ms="2222",
        )
        is True
    )
    assert "gibson_dogfood_renderer.py" in stress["HARN_GIBSON_RENDERER_COMMAND"]
    assert stress["HARN_GIBSON_RENDERER_TIMEOUT_MS"] == "2222"

    perception = {}
    assert (
        cli._apply_run_renderer_env(
            perception,
            renderer="examples/renderers/perception.json",
            renderer_command=None,
            renderer_timeout_ms="1",
        )
        is True
    )
    assert perception == {"HARN_GIBSON_RENDERER": "examples/renderers/perception.json"}


def test_cli_import_codex_auth_command(monkeypatch: Any, capsys: Any) -> None:
    class Result:
        available = True
        message = "imported"

    calls: list[tuple[str, str]] = []

    def fake_import(source: str, target: str) -> Result:
        calls.append((source, target))
        return Result()

    monkeypatch.setattr(cli, "import_codex_auth", fake_import)

    assert cli.run(["import-codex-auth", "--codex-auth", "codex.json", "--harn-auth", "harn.json"]) == 0
    assert calls == [("codex.json", "harn.json")]
    assert capsys.readouterr().out.strip() == "imported"


def test_cli_dogfood_imports_auth_and_publishes_diagnostics(monkeypatch: Any, capsys: Any) -> None:
    harn_calls: list[list[str]] = []
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9888)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    class Result:
        available = True
        message = "auth ready"

    def fake_call(command: list[str], env: dict[str, str]) -> int:
        harn_calls.append(command)
        assert env["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9888/events"
        return 0

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda _env=None: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.setattr(cli, "import_codex_auth", lambda environ=None: Result())

    assert cli.run_dogfood(harn_args=["--", "-p", "hello"], hold_on_error=False) == 0
    assert harn_calls == [["harn", "-p", "hello"]]
    assert state.scene.state.log[-1]["eventType"] == "auth_import"
    assert "auth ready" in capsys.readouterr().err


def test_cli_dogfood_holds_display_on_harn_error(monkeypatch: Any) -> None:
    state = GibsonServerState()
    held: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 9890)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda _env=None: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", lambda _command, env: 2)
    monkeypatch.setattr(cli, "_hold_display_on_error", lambda url: held.append(url))

    assert cli.run_dogfood(codex_auth_import=False) == 2
    assert held == ["http://127.0.0.1:9890"]
    assert state.scene.state.log[-1]["summary"] == "error: harn exited with code 2"


def test_cli_dogfood_holds_display_on_missing_harn(monkeypatch: Any) -> None:
    state = GibsonServerState()
    held: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 9891)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def missing(_command: list[str], env: dict[str, str]) -> int:
        raise FileNotFoundError

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda _env=None: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", missing)
    monkeypatch.setattr(cli, "_hold_display_on_error", lambda url: held.append(url))

    assert cli.run_dogfood(harn_bin="missing", codex_auth_import=False) == 127
    assert held == ["http://127.0.0.1:9891"]
    assert state.scene.state.log[-1]["eventType"] == "harn_exit"
