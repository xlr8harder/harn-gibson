from __future__ import annotations

import threading
import time

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RenderPipeline,
    RenderPlan,
    RenderRequest,
    RenderStep,
    RenderSubmitResult,
    coerce_batch_window_ms,
    coerce_render_mode,
    decisions_from_payload,
    render_accept_payload,
    render_update_payload,
)
from harn_gibson.scene import SceneEngine, SceneMutation
from harn_gibson.sinks import EventBuffer


def event(sequence: int = 1, event_type: str = "input") -> GibsonEvent:
    return GibsonEvent.from_raw(
        {"type": event_type, "text": "hello", "source": "test"},
        sequence,
        timestamp_ms=sequence * 10,
    )


def test_render_request_step_and_plan_helpers() -> None:
    request = RenderRequest(event(1), ({"block": False},))
    empty_request = RenderRequest(
        event(2),
        route="stream_buffer",
        timeline_offset_ms=25,
        coalesced_count=3,
        metadata={"streamId": "assistant-main"},
    )
    step = RenderStep((SceneMutation("append_log", entry={"x": 1}),), delay_ms=5, start_offset_ms=10, event_index=0)
    no_index = RenderStep(())
    plan = RenderPlan((request, empty_request), (step, no_index), {"agent": "test"})

    assert request.to_dict()["decisions"] == [{"block": False}]
    assert "decisions" not in empty_request.to_dict()
    assert empty_request.to_dict()["route"] == "stream_buffer"
    assert empty_request.to_dict()["timelineOffsetMs"] == 25
    assert empty_request.to_dict()["coalescedCount"] == 3
    assert empty_request.to_dict()["metadata"] == {"streamId": "assistant-main"}
    assert step.to_dict() == {
        "delayMs": 5,
        "startOffsetMs": 10,
        "mutations": [{"op": "append_log", "entry": {"x": 1}}],
        "eventIndex": 0,
    }
    assert no_index.to_dict() == {"delayMs": 0, "mutations": []}
    assert plan.primary_request == empty_request
    assert plan.request_for_step(step) == request
    assert plan.request_for_step(RenderStep((), event_index=-1)) == empty_request
    assert plan.request_for_step(RenderStep((), event_index=9)) == empty_request
    with pytest.raises(ValueError, match="no requests"):
        _ = RenderPlan((), ()).primary_request


def test_deterministic_renderer_creates_one_step_per_request() -> None:
    requests = (RenderRequest(event(1), ({"reason": "x"},)), RenderRequest(event(2, "tool_result")))
    plan = DeterministicSceneRenderer().render(requests, SceneEngine().state)

    assert plan.metadata == {"renderer": "deterministic"}
    assert len(plan.steps) == 2
    assert plan.steps[0].event_index == 0
    assert plan.steps[1].event_index == 1
    assert plan.steps[0].mutations[0].target_id == "status"


def test_blocking_pipeline_applies_and_publishes_updates() -> None:
    buffer = EventBuffer()
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=buffer, mode="blocking")
    pipeline.start()
    result = pipeline.submit(RenderRequest(event(1), ({"block": True},)))

    assert pipeline._worker is None
    assert result.mode == "blocking"
    assert result.queued == 0
    assert result.scene_revision == 1
    assert len(result.updates) == 1
    update = result.updates[0]
    assert update["decisions"] == [{"block": True}]
    assert update["renderPlan"]["batchSize"] == 1
    assert update["events"][0]["eventType"] == "input"
    assert update["renderRequests"][0]["event"]["eventType"] == "input"
    assert buffer.snapshot() == [update]
    assert render_accept_payload(result, 0) == {"ok": True, "renderMode": "blocking", "sceneRevision": 1}


def test_pipeline_async_batch_collection_and_worker_loop() -> None:
    sleeps: list[float] = []
    buffer = EventBuffer()
    pipeline = RenderPipeline(
        scene=SceneEngine(),
        buffer=buffer,
        mode="async",
        batch_window_ms=25,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    first = RenderRequest(event(1))
    second = RenderRequest(event(2, "tool_call"))

    pipeline._queue.put(second)
    requests, stop = pipeline._collect_batch(first)
    assert requests == (first, second)
    assert stop is False
    assert sleeps == [0.025]

    pipeline._queue.put(first)
    pipeline._queue.put(second)
    pipeline._queue.put(None)
    pipeline._worker_loop()
    pipeline._queue.put(None)
    pipeline._worker_loop()
    updates = buffer.snapshot()

    assert len(updates) == 2
    assert updates[0]["renderPlan"]["batchSize"] == 2
    assert updates[1]["event"]["eventType"] == "tool_call"


def test_pipeline_async_worker_continues_until_stop_sentinel() -> None:
    buffer = EventBuffer()
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=buffer, mode="async", batch_window_ms=0)
    worker = threading.Thread(target=pipeline._worker_loop, daemon=True)

    pipeline._queue.put(RenderRequest(event(1)))
    worker.start()
    deadline = time.monotonic() + 1
    while not buffer.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    pipeline._queue.put(None)
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert buffer.snapshot()[0]["event"]["eventType"] == "input"


def test_pipeline_async_submit_start_stop() -> None:
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), mode="async", batch_window_ms=0)

    result = pipeline.submit(RenderRequest(event(1)))
    assert result.mode == "async"
    assert result.queued >= 0
    assert result.scene_revision is None
    assert "pendingRenderJobs" in render_accept_payload(result, 0)

    worker = pipeline._worker
    assert worker is not None
    pipeline.start()
    assert pipeline._worker is worker
    pipeline.stop()
    assert pipeline._worker is None
    pipeline.stop()


def test_pipeline_validation_empty_and_delayed_steps() -> None:
    sleeps: list[float] = []

    class CustomRenderer:
        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            return RenderPlan(
                requests,
                (
                    RenderStep(
                        (SceneMutation("append_log", entry={"delayed": True}),),
                        delay_ms=10,
                        event_index=99,
                    ),
                    RenderStep(()),
                ),
                {"custom": True},
            )

    buffer = EventBuffer()
    pipeline = RenderPipeline(
        scene=SceneEngine(),
        buffer=buffer,
        renderer=CustomRenderer(),
        mode="blocking",
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert pipeline._render_and_publish(()) == []
    result = pipeline.submit(RenderRequest(event(5)))
    assert sleeps == [0.01]
    assert result.updates[0]["renderPlan"]["metadata"] == {"custom": True}
    assert result.updates[1]["scene"]["revision"] == 1
    with pytest.raises(ValueError, match="render mode"):
        RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), mode="later")  # type: ignore[arg-type]


def test_pipeline_direct_apply_bypasses_renderer_queue() -> None:
    buffer = EventBuffer()
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=buffer, mode="async", batch_window_ms=0)
    request = RenderRequest(event(9, "message_update"), route="stream_buffer")

    result = pipeline.apply_direct(
        request,
        (SceneMutation("append_log", entry={"direct": True}),),
        metadata={"route": {"route": "stream_buffer"}},
    )

    assert result.mode == "async"
    assert result.scene_revision == 1
    assert result.updates[0]["renderPlan"]["metadata"] == {
        "renderer": "direct",
        "route": {"route": "stream_buffer"},
    }
    assert buffer.snapshot() == list(result.updates)

    plan_result = pipeline.apply_plan(
        RenderPlan(
            requests=(request,),
            steps=(RenderStep((SceneMutation("append_log", entry={"plan": True}),), event_index=0),),
            metadata={"renderer": "saved"},
        )
    )
    assert plan_result.updates[0]["renderPlan"]["metadata"] == {"renderer": "saved"}
    assert buffer.snapshot()[-1] == plan_result.updates[0]


def test_render_update_and_coercion_helpers() -> None:
    request = RenderRequest(event(1))
    step = RenderStep((SceneMutation("append_log", entry={"x": 1}),))
    scene = SceneEngine().apply(step.mutations)
    update = render_update_payload(RenderPlan((request,), (step,)), step, 0, request, scene)

    assert "decisions" not in update
    assert update["renderPlan"]["stepCount"] == 1
    assert RenderSubmitResult("blocking", 0, ({"scene": {"revision": "bad"}},)).scene_revision is None
    assert coerce_render_mode("async") == "async"
    assert coerce_render_mode("blocking") == "blocking"
    assert coerce_render_mode(None) == "blocking"
    assert coerce_batch_window_ms(None) == 40
    assert coerce_batch_window_ms("12") == 12
    assert coerce_batch_window_ms("-1") == 0
    assert coerce_batch_window_ms("bad") == 40
    assert decisions_from_payload({"decisions": [{"a": 1}, "bad", {"b": 2}]}) == ({"a": 1}, {"b": 2})
