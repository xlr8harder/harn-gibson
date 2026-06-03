from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RendererContext,
    RendererContextBuilder,
    RendererContextConfig,
    RenderInputBatch,
    RenderPipeline,
    RenderPlan,
    RenderRequest,
    RenderStep,
    RenderSubmitResult,
    coerce_batch_window_ms,
    coerce_render_mode,
    decisions_from_payload,
    render_accept_payload,
    render_intent_from_plan,
    render_update_payload,
)
from harn_gibson.scene import SceneAnimation, SceneEngine, SceneMutation, ScenePrimitive
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


def test_renderer_context_builder_compaction_rolling_and_history(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    for directory in (
        repo_root / "docs",
        repo_root / "empty",
        repo_root / "src" / "harn_gibson",
        repo_root / "tests",
        repo_root / ".harn",
    ):
        directory.mkdir(parents=True)
    for path in (
        repo_root / "README.md",
        repo_root / "pyproject.toml",
        repo_root / "docs" / "renderer-agent.md",
        repo_root / "src" / "harn_gibson" / "rendering.py",
        repo_root / "tests" / "test_rendering.py",
        repo_root / ".harn" / "settings.json",
        repo_root / "auth.json",
    ):
        path.write_text("x", encoding="utf-8")
    (repo_root / "README-link.md").symlink_to("README.md")
    builder = RendererContextBuilder(
        RendererContextConfig(
            project_root=str(repo_root),
            compaction_interval_events=2,
            max_recent_plans=1,
            max_recent_log_entries=1,
            max_prop_preview_chars=8,
            max_repo_children_per_dir=1,
            max_touched_files=2,
        )
    )
    scene = SceneEngine()
    scene.apply(
        (
            SceneMutation("patch", target_id="status", props={"text": "connecting-to-gibson"}),
            SceneMutation("append_log", entry={"eventType": "old"}),
            SceneMutation("append_log", entry={"eventType": "new"}),
            SceneMutation(
                "upsert",
                primitive=ScenePrimitive(
                    "assistant-stream",
                    "text_stream",
                    "stage",
                    {
                        "text": ["abcdefghijklmnopqrstuvwxyz", {"nested": "zyxwvutsrqponmlk"}],
                        "isStreaming": True,
                    },
                ),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation("fly-1", "scan-grid", "flythrough", 100, 900),
            ),
        )
    )
    context_event = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "bash",
            "input": {
                "path": "src/harn_gibson/rendering.py",
                "command": "uv run pytest tests/test_rendering.py docs/renderer-agent.md",
                "ignoredUrl": "https://example.com/src/nope.py",
            },
        },
        9,
        timestamp_ms=900,
        recent_context=("agent saw tool call",),
        visualization_context=("grid was pulsing", "agent saw tool call"),
    )
    batch = RenderInputBatch.from_requests((RenderRequest(context_event),))

    compaction = builder.build(batch, scene.state, pipeline_catalog())
    assert isinstance(compaction, RendererContext)
    assert compaction.mode == "compaction"
    assert compaction.to_dict()["schema"] == "harn-gibson.renderer-context.v1"
    assert compaction.project["schemas"]["rendererContext"] == "harn-gibson.renderer-context.v1"
    assert compaction.project["schemas"]["repoTopology"] == "harn-gibson.repo-topology.v1"
    assert compaction.project["repoTopology"]["rootName"] == "repo"
    assert compaction.project["repoTopology"]["available"] is True
    assert compaction.project["repoTopology"]["truncated"] is False
    assert {entry["path"] for entry in compaction.project["repoTopology"]["entries"]} == {
        "docs",
        "empty",
        "src",
        "tests",
        "README.md",
        "README-link.md",
        "pyproject.toml",
    }
    empty_entry = next(entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "empty")
    assert "children" not in empty_entry
    src_entry = next(entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "src")
    assert src_entry["children"] == [{"path": "src/harn_gibson", "name": "harn_gibson", "kind": "dir"}]
    link_entry = next(
        entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "README-link.md"
    )
    assert link_entry["kind"] == "symlink"
    assert "auth.json" not in {entry["path"] for entry in compaction.project["repoTopology"]["entries"]}
    assert compaction.project["touchedFiles"] == {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": "src/harn_gibson/rendering.py",
                "operation": "bash:before",
                "firstSequence": 9,
                "lastSequence": 9,
                "phases": ["before"],
                "sources": ["input.path"],
            },
            {
                "path": "tests/test_rendering.py",
                "operation": "bash:before",
                "firstSequence": 9,
                "lastSequence": 9,
                "phases": ["before"],
                "sources": ["input.command"],
            },
        ],
        "count": 3,
        "truncated": True,
    }
    assert compaction.catalog["schema"] == "harn-gibson.visual-catalog.v1"
    assert compaction.scene["schema"] == "harn-gibson.scene.v1"
    assert compaction.recent_agent_context == ("agent saw tool call", "grid was pulsing")

    truncated_context = RendererContextBuilder(
        RendererContextConfig(project_root=str(repo_root), max_repo_entries=2)
    ).build(batch, scene.state, pipeline_catalog())
    assert truncated_context.project["repoTopology"]["truncated"] is True
    assert truncated_context.project["repoTopology"]["entryCount"] == 2

    builder.record_plan(
        RenderPlan(
            batch.requests,
            (RenderStep((SceneMutation("append_log", entry={"plan": 1}),),),),
            {"renderer": "first"},
        )
    )
    rolling = builder.build(batch, scene.state, pipeline_catalog())
    assert rolling.mode == "rolling"
    assert rolling.project["touchedFiles"]["truncated"] is True
    assert rolling.catalog["mode"] == "summary"
    assert rolling.scene["schema"] == "harn-gibson.scene-summary.v1"
    assert rolling.scene["animationCount"] == 1
    assert rolling.scene["recentLog"] == [{"eventType": "new"}]
    stream_summary = next(item for item in rolling.scene["primitives"] if item["id"] == "assistant-stream")
    assert stream_summary["propsPreview"]["text"] == ["abcde...", {"nested": "zyxwv..."}]
    assert stream_summary["propsPreview"]["isStreaming"] is True
    assert rolling.visualization_context[0]["renderer"] == "first"
    assert rolling.visualization_context[0]["intent"] == "visualize tool_call"
    assert rolling.visualization_context[0]["renderIntent"]["renderer"] == "first"
    assert rolling.visualization_context[0]["mutationCount"] == 1

    builder.record_plan(RenderPlan(batch.requests, (), {"renderer": "second"}))
    interval_compaction = builder.build(batch, scene.state, pipeline_catalog())
    assert interval_compaction.mode == "compaction"
    assert builder.snapshot_history() == (
        {
            "renderer": "second",
            "intent": "visualize tool_call",
            "requestCount": 1,
            "stepCount": 0,
            "mutationCount": 0,
            "eventTypes": ["tool_call"],
            "routes": ["renderer_agent"],
            "renderIntent": {
                "schema": "harn-gibson.render-intent.v1",
                "renderer": "second",
                "intent": "visualize tool_call",
                "requestCount": 1,
                "stepCount": 0,
                "mutationCount": 0,
                "eventTypes": ["tool_call"],
                "routes": ["renderer_agent"],
                "timeline": {"startMs": 900, "endMs": 900, "durationMs": 0},
                "effects": [],
                "targets": [],
                "metadata": {"renderer": "second"},
            },
            "metadata": {"renderer": "second"},
        },
    )


def test_renderer_context_repo_topology_handles_unavailable_root_and_duplicate_touches(tmp_path: Path) -> None:
    event_batch = RenderInputBatch.from_requests(
        (
            RenderRequest(
                GibsonEvent.from_raw(
                    {
                        "type": "tool_result",
                        "toolName": "write",
                        "result": {"filePath": str(tmp_path / "outside.py")},
                    },
                    1,
                    timestamp_ms=10,
                )
            ),
            RenderRequest(
                GibsonEvent.from_raw(
                    {
                        "type": "tool_result",
                        "toolName": "write",
                        "result": {
                            "destinationPath": 123,
                            "filePath": "src/new_scene.py",
                            "outputPath": ["tests/test_rendering.py", ".env"],
                            "path": {"filePath": "docs/renderer-agent.md"},
                            "sourcePath": "https://example.com/src/nope.py",
                            "targetPath": "src/new_scene.py",
                        },
                    },
                    2,
                    timestamp_ms=20,
                )
            ),
            RenderRequest(
                GibsonEvent.from_raw(
                    {
                        "type": "runtime_error",
                        "path": "README.md",
                        "command": "cat ../outside.py src/new_scene.py",
                        "events": [{"path": "docs/renderer-agent.md"}],
                    },
                    3,
                    timestamp_ms=30,
                )
            ),
        )
    )
    builder = RendererContextBuilder(
        RendererContextConfig(project_root=str(tmp_path / "missing"), max_repo_entries=0, max_touched_files=4)
    )

    context = builder.build(event_batch, SceneEngine().state, pipeline_catalog())

    assert context.project["repoTopology"] == {
        "schema": "harn-gibson.repo-topology.v1",
        "rootName": "missing",
        "maxEntries": 0,
        "maxChildrenPerDir": 8,
        "available": False,
        "reason": "project root is not a directory",
        "entries": [],
    }
    assert context.project["touchedFiles"] == {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": "src/new_scene.py",
                "operation": "write:after",
                "firstSequence": 2,
                "lastSequence": 3,
                "phases": ["after"],
                "sources": ["result.filePath", "result.targetPath", "command"],
            },
            {
                "path": "tests/test_rendering.py",
                "operation": "write:after",
                "firstSequence": 2,
                "lastSequence": 2,
                "phases": ["after"],
                "sources": ["result.outputPath.0"],
            },
            {
                "path": "docs/renderer-agent.md",
                "operation": "write:after",
                "firstSequence": 2,
                "lastSequence": 3,
                "phases": ["after"],
                "sources": ["result.path.filePath", "events.0.path"],
            },
            {
                "path": "README.md",
                "operation": "runtime_error:after",
                "firstSequence": 3,
                "lastSequence": 3,
                "phases": ["after"],
                "sources": ["path"],
            },
        ],
        "count": 4,
        "truncated": False,
    }


def test_render_intent_from_plan_summarizes_effects_targets_and_defaults() -> None:
    request = RenderRequest(event(1, "tool_call"))
    duplicate_request = RenderRequest(event(2, "tool_call"), route="direct_scene")
    plan = RenderPlan(
        (request, duplicate_request),
        (
            RenderStep(
                (
                    SceneMutation("upsert", primitive=ScenePrimitive("city", "city_block", "stage")),
                    SceneMutation(
                        "start_animation",
                        animation=SceneAnimation("burst", "city", "packet_burst", 10, 200),
                    ),
                    SceneMutation("patch", target_id="status", props={"text": "running"}),
                    SceneMutation("patch", target_id="status", props={"tone": "cyan"}),
                    SceneMutation("append_log", entry={"summary": "rendered"}),
                ),
                event_index=0,
            ),
        ),
        {"renderer": "fixture", "intent": "map command path"},
    )

    intent = render_intent_from_plan(plan)

    assert intent == {
        "schema": "harn-gibson.render-intent.v1",
        "renderer": "fixture",
        "intent": "map command path",
        "requestCount": 2,
        "stepCount": 1,
        "mutationCount": 5,
        "eventTypes": ["tool_call", "tool_call"],
        "routes": ["direct_scene", "renderer_agent"],
        "timeline": {"startMs": 10, "endMs": 20, "durationMs": 10},
        "effects": ["primitive:city_block", "animation:packet_burst", "patch", "append_log"],
        "targets": ["city", "status"],
        "metadata": {"renderer": "fixture", "intent": "map command path"},
    }

    empty = render_intent_from_plan(RenderPlan((), (), {"intent": ""}))
    assert empty["intent"] == "render idle scene"
    assert empty["renderer"] == "unknown"
    assert empty["timeline"] == {"startMs": 0, "endMs": 0, "durationMs": 0}

    generated = render_intent_from_plan(RenderPlan((request, duplicate_request), (), {"renderer": "blank"}))
    assert generated["intent"] == "visualize tool_call"


def pipeline_catalog():
    return RenderPipeline(scene=SceneEngine(), buffer=EventBuffer()).catalog


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
    assert update["renderPlan"]["intent"]["renderer"] == "deterministic"
    assert update["renderIntent"] == update["renderPlan"]["intent"]
    assert update["scene"]["metadata"]["renderIntents"] == [update["renderIntent"]]
    assert pipeline.scene.state.metadata["lastRenderIntent"]["intent"] == "visualize input"
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
    assert updates[0]["renderPlan"]["timeline"] == {"startMs": 10, "endMs": 20, "durationMs": 10}
    assert updates[0]["renderInput"]["timeline"] == {"startMs": 10, "endMs": 20, "durationMs": 10}
    assert updates[0]["renderInput"]["requests"][0]["coalescedCount"] == 2
    assert updates[0]["renderInput"]["requests"][1]["timelineOffsetMs"] == 10
    assert updates[0]["renderRequests"][0]["metadata"]["renderBatch"]["size"] == 2
    assert updates[1]["event"]["eventType"] == "tool_call"


def test_pipeline_passes_timed_batch_to_renderer() -> None:
    captured: list[RenderRequest] = []

    class CapturingRenderer:
        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            captured.extend(requests)
            return RenderPlan(
                tuple(requests),
                (RenderStep((SceneMutation("append_log", entry={"captured": True}),), event_index=1),),
                {"renderer": "capture"},
            )

    pipeline = RenderPipeline(
        scene=SceneEngine(),
        buffer=EventBuffer(),
        renderer=CapturingRenderer(),
        mode="blocking",
    )

    updates = pipeline._render_and_publish((RenderRequest(event(1)), RenderRequest(event(4))))

    assert captured[0].timeline_offset_ms == 0
    assert captured[1].timeline_offset_ms == 30
    assert captured[0].coalesced_count == 2
    assert captured[1].metadata["renderBatch"] == {
        "index": 1,
        "size": 2,
        "route": "renderer_agent",
        "timeline": {"startMs": 10, "endMs": 40, "durationMs": 30},
    }
    assert updates[0]["renderInput"]["timeline"] == {"startMs": 10, "endMs": 40, "durationMs": 30}
    assert updates[0]["event"]["sequence"] == 4


def test_pipeline_calls_contextual_renderer_with_renderer_context() -> None:
    contexts: list[RendererContext] = []

    class ContextRenderer:
        def render_with_context(
            self,
            requests: tuple[RenderRequest, ...],
            _scene: object,
            context: RendererContext,
        ) -> RenderPlan:
            contexts.append(context)
            return RenderPlan(
                tuple(requests),
                (RenderStep((SceneMutation("append_log", entry={"context": context.mode}),), event_index=0),),
                {"renderer": "contextual", "contextMode": context.mode},
            )

    pipeline = RenderPipeline(
        scene=SceneEngine(),
        buffer=EventBuffer(),
        renderer=ContextRenderer(),  # type: ignore[arg-type]
        mode="blocking",
    )

    result = pipeline.submit(RenderRequest(event(1, "tool_call")))

    assert contexts[0].mode == "compaction"
    assert contexts[0].render_input["requests"][0]["event"]["eventType"] == "tool_call"
    assert result.updates[0]["renderPlan"]["metadata"] == {"renderer": "contextual", "contextMode": "compaction"}
    assert pipeline.context_builder.snapshot_history()[0]["renderer"] == "contextual"


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
    assert result.updates[0]["renderPlan"]["intent"]["intent"] == "visualize input"
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
    assert pipeline.scene.state.metadata["renderIntents"][-1]["renderer"] == "saved"


def test_render_update_and_coercion_helpers() -> None:
    request = RenderRequest(event(1))
    step = RenderStep((SceneMutation("append_log", entry={"x": 1}),))
    scene = SceneEngine().apply(step.mutations)
    update = render_update_payload(RenderPlan((request,), (step,)), step, 0, request, scene)

    assert "decisions" not in update
    assert update["renderPlan"]["stepCount"] == 1
    assert update["renderIntent"]["effects"] == ["append_log"]
    assert RenderSubmitResult("blocking", 0, ({"scene": {"revision": "bad"}},)).scene_revision is None
    assert coerce_render_mode("async") == "async"
    assert coerce_render_mode("blocking") == "blocking"
    assert coerce_render_mode(None) == "blocking"
    assert coerce_batch_window_ms(None) == 40
    assert coerce_batch_window_ms("12") == 12
    assert coerce_batch_window_ms("-1") == 0
    assert coerce_batch_window_ms("bad") == 40
    assert decisions_from_payload({"decisions": [{"a": 1}, "bad", {"b": 2}]}) == ({"a": 1}, {"b": 2})
