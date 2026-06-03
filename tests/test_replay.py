from __future__ import annotations

import json
from pathlib import Path

import pytest

from harn_gibson import (
    BrowserScreenshotResult,
    ReplayExpectationError,
    ReplayFrame,
    ReplayFrameScreenshot,
    ReplayRendererContext,
    ReplayResult,
    ReplayStepResult,
    renderer_prompt_from_context,
    renderer_prompt_messages_payload,
    replay_frame_review_html,
    replay_render_intents_from_result,
    replay_render_intents_review_html,
    replay_review_bundle_index_html,
    replay_review_bundle_manifest,
    replay_timeline_from_result,
    run_replay_data,
    run_replay_file,
)
from harn_gibson.events import GibsonEvent
from harn_gibson.replay import (
    ReplayBaselineResult,
    ReplayExpectationResult,
    ReplayFileResult,
    ReplaySuiteResult,
    capture_replay_frame_screenshots,
    compare_replay_baseline,
    discover_replay_files,
    evaluate_replay_expectations,
    evaluate_screenshot_expectations,
    load_replay_file,
    mutations_from_value,
    render_plan_from_mapping,
    render_request_from_mapping,
    render_step_from_mapping,
    replay_baseline_from_result,
    replay_baseline_scene,
    replay_data_from_event_log,
    replay_frame_screenshot_manifest,
    replay_renderer_chunks_from_result,
    replay_renderer_chunks_review_html,
    replay_renderer_contexts_from_result,
    replay_renderer_prompts_from_result,
    replay_renderer_prompts_review_html,
    run_replay_suite,
    write_replay_baseline,
    write_replay_frame_review_html,
    write_replay_frame_screenshot_manifest,
    write_replay_render_intents,
    write_replay_render_intents_review_html,
    write_replay_renderer_chunks,
    write_replay_renderer_chunks_review_html,
    write_replay_renderer_contexts,
    write_replay_renderer_prompts,
    write_replay_renderer_prompts_review_html,
    write_replay_result,
    write_replay_review_bundle,
    write_replay_timeline,
    write_scene,
)
from harn_gibson.scene import SceneMutation, SceneState
from harn_gibson.server import GibsonServerState

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_REPLAYS = ROOT / "examples" / "replays"


def event_payload(
    sequence: int = 1,
    event_type: str = "tool_call",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    raw = {"type": event_type, **dict(payload or {})}
    return GibsonEvent.from_raw(raw, sequence, source="unit", timestamp_ms=1000 + sequence).to_dict()


def test_renderer_prompt_metadata_handles_sparse_context() -> None:
    prompt = renderer_prompt_from_context(
        {
            "project": "bad",
            "renderInput": {
                "timeline": {"startMs": None, "endMs": "bad", "durationMs": "12"},
                "requests": [
                    {"event": {"eventType": "tool_call"}},
                    {"event": {"eventType": "tool_call"}},
                    {"route": "direct_scene", "event": {"eventType": "browser_input"}},
                ],
            },
        },
        context_index=3,
    )

    assert prompt["contextIndex"] == 3
    assert prompt["metadata"]["eventTypes"] == ["tool_call", "browser_input"]
    assert prompt["metadata"]["routes"] == ["renderer_agent", "direct_scene"]
    assert prompt["metadata"]["timeline"] == {"startMs": 0, "endMs": 0, "durationMs": 12}
    assert prompt["metadata"]["displayStyle"] == "gibson"


def test_replay_event_steps_file_io_and_writers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harn-gibson.replay.v1",
                "name": "event replay",
                "metadata": {"fixture": True},
                "steps": [
                    {
                        "type": "event",
                        "event": event_payload(
                            1,
                            "tool_call",
                            {"toolName": "bash", "input": {"command": "pwd"}},
                        ),
                    },
                    {
                        "kind": "event",
                        "event": event_payload(
                            2,
                            "message_update",
                            {"assistantMessageEvent": {"delta": "loading"}},
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert load_replay_file(path)["name"] == "event replay"
    result = run_replay_file(path)

    assert isinstance(result, ReplayResult)
    assert result.schema == "harn-gibson.replay.v1"
    assert result.name == "event replay"
    assert result.metadata == {"fixture": True}
    assert result.expectations == ()
    assert result.frames == ()
    assert result.steps[0].to_dict() == {
        "index": 0,
        "kind": "event",
        "sceneRevision": 1,
        "updates": 1,
    }
    assert result.steps[1].route == "stream_buffer"
    assert result.scene.primitives["assistant-stream"].props["text"] == "loading"
    assert result.to_dict()["steps"][1]["route"] == "stream_buffer"

    scene_path = tmp_path / "out" / "scene.json"
    result_path = tmp_path / "out" / "result.json"
    write_scene(scene_path, result.scene)
    write_replay_result(result_path, result)

    assert json.loads(scene_path.read_text(encoding="utf-8"))["revision"] == 2
    assert json.loads(result_path.read_text(encoding="utf-8"))["name"] == "event replay"
    assert "frames" not in json.loads(result_path.read_text(encoding="utf-8"))

    context_result = run_replay_file(path, capture_renderer_contexts=True)
    context_path = tmp_path / "out" / "renderer-contexts.json"
    write_replay_renderer_contexts(context_path, context_result)
    context_payload = json.loads(context_path.read_text(encoding="utf-8"))

    assert isinstance(context_result.renderer_contexts[0], ReplayRendererContext)
    assert context_result.renderer_contexts[0].context["schema"] == "harn-gibson.renderer-context.v1"
    assert context_result.renderer_contexts[0].context["mode"] == "compaction"
    captured_event = context_result.renderer_contexts[0].context["renderInput"]["requests"][0]["event"]
    assert captured_event["eventType"] == "tool_call"
    assert replay_renderer_contexts_from_result(context_result)["contextCount"] == 1
    assert context_payload["schema"] == "harn-gibson.replay-renderer-contexts.v1"
    assert context_payload["contexts"][0]["context"]["catalog"]["schema"] == "harn-gibson.visual-catalog.v1"
    assert context_result.to_dict()["rendererContexts"][0]["index"] == 0
    prompts_path = tmp_path / "out" / "renderer-prompts.json"
    prompts_review_path = tmp_path / "out" / "renderer-prompts.html"
    prompts = replay_renderer_prompts_from_result(context_result)
    write_replay_renderer_prompts(prompts_path, context_result)
    write_replay_renderer_prompts_review_html(prompts_review_path, prompts)
    prompts_payload = json.loads(prompts_path.read_text(encoding="utf-8"))
    prompts_review = prompts_review_path.read_text(encoding="utf-8")
    prompt = prompts["prompts"][0]
    prompt_messages = renderer_prompt_messages_payload(context_result.renderer_contexts[0].context)

    assert prompts["schema"] == "harn-gibson.replay-renderer-prompts.v1"
    assert prompts["promptCount"] == 1
    assert prompts_payload["prompts"][0]["schema"] == "harn-gibson.renderer-prompt.v1"
    assert prompt["metadata"]["eventTypes"] == ["tool_call"]
    assert prompt["metadata"]["routes"] == ["renderer_agent"]
    assert prompt["metadata"]["displayStyle"] == "gibson"
    assert prompt["messages"][0]["role"] == "system"
    assert "harn-gibson.render-plan.v1" in prompt["messages"][0]["content"]
    assert "harn-gibson.renderer-context.v1" in prompt["messages"][1]["content"]
    assert "tool_call" in prompt["messages"][1]["content"]
    assert prompt_messages["schema"] == "harn-gibson.renderer-prompt-messages.v1"
    assert prompt_messages["messageCount"] == 2
    assert "event replay renderer prompt review" in prompts_review
    assert "window.__gibsonRendererPrompts" in prompts_review
    assert "tool_call" in prompts_review
    chunks_path = tmp_path / "out" / "renderer-chunks.json"
    second_context = json.loads(json.dumps(context_result.renderer_contexts[0].context))
    second_context["mode"] = "rolling"
    second_context["renderInput"]["timeline"] = {"startMs": 2000, "endMs": 2400, "durationMs": 400}
    multi_context_result = ReplayResult(
        schema="harn-gibson.replay.v1",
        name="chunked context replay",
        steps=context_result.steps,
        scene=context_result.scene,
        metadata={"fixture": "chunks"},
        renderer_contexts=(
            context_result.renderer_contexts[0],
            ReplayRendererContext(1, second_context),
        ),
    )
    chunks = replay_renderer_chunks_from_result(multi_context_result, chunk_size=2)
    write_replay_renderer_chunks(chunks_path, multi_context_result, chunk_size=1)
    chunks_review_path = tmp_path / "out" / "renderer-chunks.html"
    write_replay_renderer_chunks_review_html(chunks_review_path, chunks)
    chunk_file = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks_review = chunks_review_path.read_text(encoding="utf-8")

    assert chunks["schema"] == "harn-gibson.replay-renderer-chunks.v1"
    assert chunks["contextCount"] == 2
    assert chunks["chunkCount"] == 1
    assert chunks["chunkSize"] == 2
    assert chunks["metadata"] == {"fixture": "chunks"}
    assert chunks["chunks"][0]["contextIndexes"] == [0, 1]
    assert chunks["chunks"][0]["modes"] == ["compaction", "rolling"]
    assert chunks["chunks"][0]["eventTypes"] == ["tool_call"]
    assert chunks["chunks"][0]["routes"] == ["renderer_agent"]
    assert chunks["chunks"][0]["requestCount"] == 2
    assert chunks["chunks"][0]["timeline"] == {"startMs": 1001, "endMs": 2400, "durationMs": 1399}
    assert chunks["chunks"][0]["prompts"][1]["mode"] == "rolling"
    assert chunks["chunks"][0]["contexts"][0]["context"]["mode"] == "compaction"
    assert chunks["chunks"][0]["messageChars"] > chunks["chunks"][0]["contextChars"]
    assert chunk_file["chunkCount"] == 2
    assert chunk_file["chunks"][1]["contextStart"] == 1
    assert "chunked context replay renderer chunk review" in chunks_review
    assert "window.__gibsonRendererChunks" in chunks_review
    assert "first prompt user message" in chunks_review
    assert "tool_call" in chunks_review
    assert "<\\/script>" in replay_renderer_chunks_review_html(
        {
            "replayName": "</script>",
            "schema": "test",
            "chunkSize": 1,
            "contextCount": 1,
            "chunks": [
                {
                    "index": 0,
                    "contextStart": 0,
                    "contextEnd": 0,
                    "eventTypes": ["</script>"],
                    "routes": ["renderer_agent"],
                    "modes": ["rolling"],
                    "displayStyles": ["gibson"],
                    "prompts": [{"messages": [{"role": "user", "content": "</script>"}]}],
                }
            ],
        }
    )
    no_prompt_preview = replay_renderer_chunks_review_html(
        {
            "replayName": "no prompt",
            "schema": "test",
            "chunks": [
                {
                    "index": 0,
                    "contextStart": 0,
                    "contextEnd": 0,
                    "prompts": [],
                }
            ],
        }
    )
    assert "chunk #0" in no_prompt_preview
    assert "first prompt user message" not in no_prompt_preview
    assert "No renderer chunks" in replay_renderer_chunks_review_html(
        {"replayName": "empty", "schema": "test", "chunks": []}
    )
    assert replay_renderer_chunks_from_result(result)["chunks"] == []
    with pytest.raises(ValueError, match="render chunk size"):
        replay_renderer_chunks_from_result(context_result, chunk_size=0)
    with pytest.raises(ValueError, match="render chunk size"):
        replay_renderer_chunks_from_result(context_result, chunk_size="bad")  # type: ignore[arg-type]
    assert "<\\/script>" in replay_renderer_prompts_review_html(
        {
            "replayName": "</script>",
            "schema": "test",
            "prompts": [
                {
                    "contextIndex": 0,
                    "mode": "rolling",
                    "metadata": {"eventTypes": ["</script>"], "routes": ["renderer_agent"]},
                    "messages": [{"role": "user", "content": "</script>"}],
                }
            ],
        }
    )
    assert "No renderer prompts" in replay_renderer_prompts_review_html(
        {"replayName": "empty", "schema": "test", "prompts": []}
    )

    forwarded_contexts = []

    def record_forwarded_context(context: object) -> None:
        forwarded_contexts.append(context)

    chained_state = GibsonServerState()
    chained_state.pipeline.context_recorder = record_forwarded_context
    chained_result = run_replay_file(path, chained_state, capture_renderer_contexts=True)
    assert len(forwarded_contexts) == 1
    assert chained_result.renderer_contexts[0].context["mode"] == "compaction"
    assert chained_state.pipeline.context_recorder is record_forwarded_context

    render_intents_path = tmp_path / "out" / "render-intents.json"
    render_intents_review_path = tmp_path / "out" / "render-intents.html"
    render_intents = replay_render_intents_from_result(result)
    write_replay_render_intents(render_intents_path, result)
    write_replay_render_intents_review_html(render_intents_review_path, render_intents)
    render_intents_payload = json.loads(render_intents_path.read_text(encoding="utf-8"))
    render_intents_review = render_intents_review_path.read_text(encoding="utf-8")

    assert render_intents["schema"] == "harn-gibson.replay-render-intents.v1"
    assert render_intents["intentCount"] == 2
    assert render_intents["intents"][0]["index"] == 0
    assert render_intents["intents"][0]["intent"]["eventTypes"] == ["tool_call"]
    assert render_intents["intents"][0]["intent"]["routes"] == ["renderer_agent"]
    assert render_intents["intents"][0]["intent"]["timeline"] == {"startMs": 1001, "endMs": 1001, "durationMs": 0}
    assert render_intents["intents"][1]["intent"]["routes"] == ["stream_buffer"]
    assert render_intents_payload["intentCount"] == 2
    assert "event replay render intent review" in render_intents_review
    assert "window.__gibsonRenderIntents" in render_intents_review
    assert "stream_buffer" in render_intents_review
    assert "<\\/script>" in replay_render_intents_review_html(
        {
            "replayName": "script",
            "schema": "test",
            "intents": [
                {
                    "index": "bad",
                    "intent": {
                        "renderer": "</script>",
                        "intent": "<b>bold</b>",
                        "eventTypes": ["tool_call"],
                        "metadata": {"payload": "</script>"},
                    },
                }
            ],
        }
    )
    assert "No render intents" in replay_render_intents_review_html(
        {"replayName": "empty", "schema": "test", "intents": []}
    )
    fallback_result = ReplayResult(
        schema="test",
        name="fallback",
        steps=(),
        scene=SceneState(),
    )
    fallback_result.scene.metadata["renderIntents"] = []
    fallback_result.scene.metadata["lastRenderIntent"] = {"renderer": "legacy", "intent": "old scene"}
    assert replay_render_intents_from_result(fallback_result)["intents"] == [
        {"index": 0, "intent": {"renderer": "legacy", "intent": "old scene"}}
    ]
    fallback_result.scene.metadata.clear()
    assert replay_render_intents_from_result(fallback_result)["intentCount"] == 0

    framed = run_replay_file(path, capture_frames=True)
    timeline_path = tmp_path / "out" / "timeline.json"
    write_replay_timeline(timeline_path, framed)
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))

    assert isinstance(framed.frames[0], ReplayFrame)
    assert framed.frames[0].step == framed.steps[0]
    assert framed.frames[0].scene["revision"] == 1
    assert framed.frames[1].scene["primitives"]["assistant-stream"]["props"]["text"] == "loading"
    assert framed.to_dict()["frames"][1]["step"]["route"] == "stream_buffer"
    assert replay_timeline_from_result(framed)["frameCount"] == 2
    assert timeline["schema"] == "harn-gibson.replay-timeline.v1"
    assert timeline["replayName"] == "event replay"
    assert timeline["stepCount"] == 2
    assert timeline["frames"][0]["scene"]["schema"] == "harn-gibson.scene.v1"

    captures: list[tuple[int, Path, int, int]] = []

    def fake_capture(state: GibsonServerState, path: str | Path, *, width: int, height: int) -> BrowserScreenshotResult:
        captures.append((state.scene.state.revision, Path(path), width, height))
        return BrowserScreenshotResult(
            Path(path),
            "http://127.0.0.1:1",
            state.scene.state.revision,
            width,
            height,
            {"nonblank": True},
        )

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)
    frame_screenshots = capture_replay_frame_screenshots(framed, tmp_path / "frames", width=640, height=480)
    manifest_path = tmp_path / "frames" / "manifest.json"
    write_replay_frame_screenshot_manifest(manifest_path, framed, frame_screenshots)
    html_path = tmp_path / "frames" / "index.html"
    write_replay_frame_review_html(html_path, replay_frame_screenshot_manifest(framed, frame_screenshots))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    review_html = html_path.read_text(encoding="utf-8")

    assert isinstance(frame_screenshots[0], ReplayFrameScreenshot)
    assert captures == [
        (1, tmp_path / "frames" / "frame-0000.png", 640, 480),
        (2, tmp_path / "frames" / "frame-0001.png", 640, 480),
    ]
    assert replay_frame_screenshot_manifest(framed, frame_screenshots)["screenshotCount"] == 2
    bundle_result = run_replay_file(path, capture_frames=True, capture_renderer_contexts=True)
    bundle_path = tmp_path / "bundle"
    bundle_manifest = write_replay_review_bundle(bundle_path, bundle_result, frame_screenshots)
    bundle_index = (bundle_path / "index.html").read_text(encoding="utf-8")
    bundle_manifest_file = json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8"))
    assert bundle_manifest["schema"] == "harn-gibson.replay-review-bundle.v1"
    assert bundle_manifest["contextCount"] == 1
    assert bundle_manifest["intentCount"] == 2
    assert bundle_manifest["promptCount"] == 1
    assert bundle_manifest["chunkCount"] == 1
    assert bundle_manifest["renderChunkSize"] == 4
    assert bundle_manifest["screenshotCount"] == 2
    assert bundle_manifest["artifacts"]["frameReview"] == "frames/index.html"
    assert bundle_manifest["artifacts"]["rendererChunks"] == "renderer-chunks.json"
    assert bundle_manifest["artifacts"]["rendererChunkReview"] == "renderer-chunks.html"
    assert bundle_manifest["artifacts"]["rendererPromptReview"] == "renderer-prompts.html"
    assert bundle_manifest_file == bundle_manifest
    assert json.loads((bundle_path / "renderer-contexts.json").read_text(encoding="utf-8"))["contextCount"] == 1
    assert json.loads((bundle_path / "renderer-prompts.json").read_text(encoding="utf-8"))["promptCount"] == 1
    assert json.loads((bundle_path / "renderer-chunks.json").read_text(encoding="utf-8"))["chunkCount"] == 1
    assert "renderer chunk review" in (bundle_path / "renderer-chunks.html").read_text(encoding="utf-8")
    assert json.loads((bundle_path / "render-intents.json").read_text(encoding="utf-8"))["intentCount"] == 2
    assert json.loads((bundle_path / "frames" / "manifest.json").read_text(encoding="utf-8"))["screenshotCount"] == 2
    assert "renderer prompt review" in (bundle_path / "renderer-prompts.html").read_text(encoding="utf-8")
    assert "event replay replay review" in bundle_index
    assert 'href="frames/index.html"' in bundle_index
    assert "Renderer Chunk Review" in bundle_index
    assert "Renderer Prompt Review" in bundle_index
    assert "window.__gibsonReplayReview" in bundle_index
    assert "<\\/script>" in replay_review_bundle_index_html(
        replay_review_bundle_manifest(
            bundle_result,
            (),
            {"scene": "scene.json", "result": "</script>"},
        )
    )
    assert "Replay Result JSON" not in replay_review_bundle_index_html({"artifacts": "bad"})
    assert replay_review_bundle_manifest(bundle_result, (), {})["screenshotCount"] == 0
    assert 'src="frame-0000.png"' in review_html
    assert 'id="timelineScrubber"' in review_html
    assert 'data-frame-select="1"' in review_html
    assert "window.__gibsonReplayFrames" in review_html
    assert "<\\/script>" in replay_frame_review_html(
        {
            "replayName": "script",
            "schema": "test",
            "frames": [{"index": 0, "step": {"kind": "</script>"}, "screenshot": {"path": "frame.png"}}],
        }
    )
    assert "event replay timeline review" in replay_frame_review_html(
        replay_frame_screenshot_manifest(framed, frame_screenshots)
    )
    relative_html = replay_frame_review_html(
        {
            "replayName": "relative",
            "schema": "test",
            "frames": [
                {"index": 0, "step": {}, "screenshot": {"path": "relative/frame.png"}},
                {"index": 1, "step": {}, "screenshot": {}},
            ],
        },
        output_path=tmp_path / "relative" / "index.html",
    )
    assert 'relative/frame.png"' in relative_html
    assert 'src=""' in relative_html
    assert manifest["schema"] == "harn-gibson.replay-frame-screenshots.v1"
    assert manifest["frames"][1]["screenshot"]["canvasMetrics"] == {"nonblank": True}


def test_replay_data_from_event_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                "",
                json.dumps(event_payload(1, "tool_call", {"toolName": "bash"})),
                json.dumps(event_payload(2, "message_update", {"assistantMessageEvent": {"delta": "ok"}})),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fixture = replay_data_from_event_log(path)
    result = run_replay_data(fixture)

    assert fixture["schema"] == "harn-gibson.replay.v1"
    assert fixture["name"] == "event log: events.jsonl"
    assert fixture["metadata"] == {"sourceEventLog": path.as_posix(), "eventCount": 2}
    assert fixture["steps"][0]["type"] == "event"
    assert result.name == "event log: events.jsonl"
    assert result.steps[0].kind == "event"

    bad = tmp_path / "bad.jsonl"
    bad.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="event log line 1 must contain a JSON object"):
        replay_data_from_event_log(bad)


def test_replay_baseline_write_compare_and_validation(tmp_path: Path) -> None:
    result = run_replay_data(
        {
            "name": "baseline replay",
            "metadata": {"fixture": True},
            "steps": [{"type": "event", "event": event_payload(1, "tool_call", {"toolName": "bash"})}],
        }
    )
    baseline_path = tmp_path / "baselines" / "fixture.json"

    baseline = replay_baseline_from_result(result)
    write_replay_baseline(baseline_path, result)
    compared = compare_replay_baseline(baseline_path, result)

    assert baseline["schema"] == "harn-gibson.replay-baseline.v1"
    assert baseline["replayName"] == "baseline replay"
    assert baseline["metadata"] == {"fixture": True}
    assert baseline["scene"] == replay_baseline_scene(result.scene)
    assert baseline["scene"]["metadata"]["lastRenderIntent"]["timeline"] == {"durationMs": 0}
    assert compared == ReplayBaselineResult(path=baseline_path.as_posix(), ok=True)
    assert compared.to_dict() == {
        "path": baseline_path.as_posix(),
        "ok": True,
        "updated": False,
        "checked": ["scene"],
    }

    missing = compare_replay_baseline(tmp_path / "missing.json", result)
    assert missing.ok is False
    assert missing.error.startswith("baseline missing:")
    assert missing.to_dict()["error"] == missing.error

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    assert compare_replay_baseline(invalid, result).error == (
        "baseline invalid: baseline file must contain a JSON object"
    )

    no_scene = tmp_path / "no-scene.json"
    no_scene.write_text("{}", encoding="utf-8")
    assert compare_replay_baseline(no_scene, result).error == "baseline invalid: baseline scene must be a JSON object"

    broken_json = tmp_path / "broken.json"
    broken_json.write_text("{", encoding="utf-8")
    assert compare_replay_baseline(broken_json, result).error.startswith("baseline invalid: Expecting")

    changed = json.loads(baseline_path.read_text(encoding="utf-8"))
    changed["scene"]["primitives"]["status"]["props"]["text"] = "wrong"
    baseline_path.write_text(json.dumps(changed), encoding="utf-8")
    mismatch = compare_replay_baseline(baseline_path, result)
    assert mismatch.ok is False
    assert "baseline scene mismatch" in mismatch.error
    assert '"text": "wrong"' in mismatch.error
    assert '"text": "before:tool_call"' in mismatch.error

    result.scene.metadata = "not a dict"  # type: ignore[assignment]
    assert replay_baseline_scene(result.scene)["metadata"] == "not a dict"
    result.scene.metadata = {"lastRenderIntent": "not an intent", "renderIntents": "not a list"}
    assert replay_baseline_scene(result.scene)["metadata"]["renderIntents"] == "not a list"
    result.scene.metadata = {"lastRenderIntent": {"timeline": "not a dict"}, "renderIntents": [{"timeline": {}}]}
    normalized = replay_baseline_scene(result.scene)["metadata"]
    assert normalized["lastRenderIntent"]["timeline"] == "not a dict"
    assert normalized["renderIntents"][0]["timeline"] == {"durationMs": 0}


def test_checked_in_replay_fixtures_cover_agent_and_renderer_sides() -> None:
    agent_result = run_replay_file(EXAMPLE_REPLAYS / "stream-and-diagnostic.json")
    renderer_result = run_replay_file(EXAMPLE_REPLAYS / "renderer-plan.json")
    gallery_result = run_replay_file(EXAMPLE_REPLAYS / "primitive-gallery.json")
    animation_result = run_replay_file(EXAMPLE_REPLAYS / "animation-gallery.json")

    assert [step.kind for step in agent_result.steps] == ["event", "event", "mutations"]
    assert len(agent_result.expectations) == 5
    assert agent_result.steps[1].route == "stream_buffer"
    assert agent_result.scene.primitives["assistant-stream"].props["text"] == "collecting event telemetry..."
    assert agent_result.scene.primitives["trace-log"].props["text"][0]["eventType"] == "runtime_error"

    assert [step.kind for step in renderer_result.steps] == ["render_plan"]
    assert len(renderer_result.expectations) == 5
    assert renderer_result.steps[0].route == "saved_renderer_plan"
    assert renderer_result.steps[0].updates == 2
    assert renderer_result.scene.primitives["status"].props["text"] == "renderer:coverage locked"
    assert renderer_result.scene.primitives["decision-log"].props["text"][0]["renderer"] == "fixture"

    assert [step.kind for step in gallery_result.steps] == ["mutations"]
    assert len(gallery_result.expectations) == 36
    assert gallery_result.scene.primitives["gallery-mesh"].kind == "mesh"
    assert gallery_result.scene.primitives["gallery-vector"].kind == "svg_layer"
    assert gallery_result.scene.primitives["gallery-vector"].props["gradients"][0]["id"] == "ice-gradient"
    assert gallery_result.scene.primitives["gallery-vector"].props["rects"][0]["rx"] == 5
    assert gallery_result.scene.primitives["gallery-vector"].props["lines"][0]["from"]["x"] == 18
    assert gallery_result.scene.primitives["gallery-vector"].props["polylines"][0]["points"][2][0] == 45
    assert gallery_result.scene.primitives["gallery-vector"].props["polygons"][0]["fill"] == "magenta"
    assert gallery_result.scene.primitives["gallery-vector"].props["groups"][0]["labels"][0]["text"] == "CORE"
    assert gallery_result.scene.primitives["gallery-vector"].props["keyframes"][1]["scale"] == 1.08
    assert gallery_result.scene.primitives["gallery-vector"].props["groups"][0]["keyframes"][2]["rotation"] == -0.12
    assert gallery_result.scene.primitives["gallery-vector"].props["traces"][0]["count"] == 9
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][0]["kind"] == "globe"
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][1]["kind"] == "filesystem_gate"
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][2]["kind"] == "reticle"
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][3]["kind"] == "data_tunnel"
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][4]["kind"] == "ice_wall"
    assert gallery_result.scene.primitives["gallery-vector"].props["symbols"][5]["kind"] == "mainframe_core"
    assert gallery_result.scene.primitives["gallery-vector"].props["filters"][0]["kind"] == "chromatic_split"
    assert gallery_result.scene.primitives["gallery-vector"].props["filters"][1]["kind"] == "scanline"
    assert gallery_result.scene.primitives["gallery-vector"].props["clip"]["kind"] == "iris"
    assert gallery_result.scene.primitives["gallery-hologram"].kind == "hologram"
    assert gallery_result.scene.primitives["gallery-hologram"].props["rings"] == 6
    assert gallery_result.scene.primitives["gallery-hologram"].props["panels"] == 4
    assert gallery_result.scene.primitives["gallery-city"].kind == "city_block"
    assert gallery_result.scene.primitives["gallery-trace"].kind == "trace_route"
    assert gallery_result.scene.primitives["gallery-trace"].props["hops"][2]["label"] == "ICE"
    assert gallery_result.scene.primitives["gallery-trace"].props["focusHopId"] == "gibson"
    assert gallery_result.scene.primitives["gallery-rain"].kind == "data_rain"
    assert gallery_result.scene.primitives["gallery-rain"].props["columns"] == 42
    assert gallery_result.scene.primitives["gallery-rain"].props["bands"] == 3
    assert gallery_result.scene.primitives["assistant-stream"].props["title"] == "CATALOG STREAM"

    assert [step.kind for step in animation_result.steps] == ["mutations"]
    assert len(animation_result.expectations) == 12
    assert animation_result.scene.primitives["animation-vector"].kind == "svg_layer"
    assert animation_result.scene.primitives["animation-vector"].props["gradients"][0]["id"] == "fx-gradient"
    assert animation_result.scene.primitives["animation-vector"].props["traces"][0]["direction"] == "reverse"
    assert animation_result.scene.animations["gallery-packets"].kind == "packet_burst"
    assert sorted(animation.kind for animation in animation_result.scene.animations.values()) == [
        "extrude",
        "flythrough",
        "glitch",
        "hold",
        "packet_burst",
        "phase-pulse",
        "scan",
    ]


def test_replay_raw_events_render_plans_and_mutations() -> None:
    explicit_event = event_payload(12, "browser_input", {"id": "input-1", "message": "go"})
    state = GibsonServerState()

    result = run_replay_data(
        {
            "name": "mixed replay",
            "steps": [
                {
                    "type": "raw_event",
                    "raw": {"type": "input", "text": "raw input", "source": "replay"},
                    "sequence": 10,
                    "source": "raw-fixture",
                    "timestampMs": 2222,
                    "recentContext": ["user asked for status"],
                    "visualizationContext": "ignored",
                    "decisions": [{"block": False}, "not a decision"],
                },
                {
                    "type": "render_plan",
                    "plan": {
                        "requests": [
                            {
                                "event": event_payload(11, "tool_result", {"toolName": "bash"}),
                                "route": "direct_scene",
                                "timelineOffsetMs": 5,
                                "coalescedCount": 2,
                                "decisions": [{"reviewed": True}],
                                "metadata": {"source": "fixture"},
                            }
                        ],
                        "steps": [
                            {
                                "startOffsetMs": 5,
                                "eventIndex": 0,
                                "mutations": [
                                    {"op": "patch", "targetId": "status", "props": {"text": "saved plan"}},
                                    {"op": "append_log", "entry": {"sequence": 11, "eventType": "saved_plan"}},
                                ],
                            }
                        ],
                        "metadata": {"renderer": "saved", "route": {"route": "saved_plan"}},
                    },
                },
                {
                    "type": "mutations",
                    "event": explicit_event,
                    "mutations": [SceneMutation("patch", target_id="status", props={"text": "manual event"})],
                },
                {
                    "type": "mutations",
                    "summary": "manual diagnostic",
                    "timestamp_ms": 3333,
                    "mutations": [{"op": "append_log", "entry": {"eventType": "manual"}}],
                },
            ],
        },
        state,
    )

    assert [step.kind for step in result.steps] == ["raw_event", "render_plan", "mutations", "mutations"]
    assert result.steps[1].route == "saved_plan"
    assert result.steps[2].scene_revision == 3
    assert result.scene.revision == 4
    assert result.scene.primitives["status"].props["text"] == "manual event"
    assert result.scene.log[-1]["eventType"] == "manual"


def test_replay_expectations_pass_fail_and_serialize() -> None:
    result = run_replay_data(
        {
            "steps": [
                {
                    "type": "event",
                    "event": event_payload(
                        1,
                        "message_update",
                        {"assistantMessageEvent": {"delta": "signal"}},
                    ),
                }
            ],
            "expect": {
                "sceneRevision": 1,
                "checks": [
                    {"path": "primitives.assistant-stream.props.text", "equals": "signal"},
                    {"path": "primitives.assistant-stream.props.text", "contains": "ign"},
                    {"path": "primitives.assistant-stream", "exists": True},
                    {"path": "primitives.missing", "exists": False},
                    {"path": "primitives.assistant-stream.props", "contains": {"isStreaming": True}},
                    {"path": "animations.stream-pulse-1", "contains": {"targetId": "scan-grid"}},
                ],
            },
        }
    )

    assert all(expectation.passed for expectation in result.expectations)
    assert evaluate_replay_expectations(result.scene, None) == ()
    assert result.to_dict()["expectations"][0] == {
        "path": "revision",
        "op": "equals",
        "passed": True,
        "expected": 1,
        "actual": 1,
    }
    serialized = ReplayExpectationResult("x", "exists", False, False, message="missing").to_dict()
    assert serialized == {
        "path": "x",
        "op": "exists",
        "passed": False,
        "expected": False,
        "message": "missing",
    }
    assert ReplayExpectationResult("x", "exists", True).to_dict() == {"path": "x", "op": "exists", "passed": True}
    branch_checks = evaluate_replay_expectations(
        result.scene,
        {
            "checks": [
                {"path": "log.9", "exists": False},
                {"path": "primitives.assistant-stream.props.text.9", "exists": False},
                {"path": "revision", "contains": 1},
            ]
        },
    )
    assert [check.passed for check in branch_checks] == [True, True, False]

    with pytest.raises(ReplayExpectationError, match="replay expectations failed") as error:
        run_replay_data(
            {
                "steps": [{"type": "event", "event": event_payload()}],
                "expect": {"checks": [{"path": "primitives.status.props.text", "equals": "wrong"}]},
            }
        )
    assert error.value.failures[0].path == "primitives.status.props.text"
    assert "expected to equals" in error.value.failures[0].message


def test_replay_suite_discovers_runs_and_serializes(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    nested = fixture_dir / "nested"
    nested.mkdir(parents=True)
    first = fixture_dir / "first.json"
    second = nested / "second.json"
    ignored = nested / "ignored.txt"
    event = event_payload(1, "message_update", {"assistantMessageEvent": {"delta": "ok"}})
    first.write_text(
        json.dumps({"steps": [{"type": "event", "event": event}], "expect": {"sceneRevision": 1}}),
        "utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "steps": [{"type": "event", "event": event}],
                "expect": {"checks": [{"path": "primitives.status.props.text", "equals": "wrong"}]},
            }
        ),
        "utf-8",
    )
    ignored.write_text("not json", "utf-8")

    assert [path.name for path in discover_replay_files(fixture_dir)] == ["first.json", "second.json"]
    assert discover_replay_files(first) == (first,)
    suite = run_replay_suite(fixture_dir)

    assert isinstance(suite, ReplaySuiteResult)
    assert suite.total == 2
    assert suite.failed == 1
    assert suite.ok is False
    assert suite.files[0] == ReplayFileResult("first.json", True, steps=1, scene_revision=1, expectations=1)
    assert ReplayFileResult("unrun.json", True).to_dict() == {
        "path": "unrun.json",
        "ok": True,
        "steps": 0,
        "expectations": 0,
    }
    assert suite.files[1].path == "nested/second.json"
    assert suite.files[1].expectation_failures[0].path == "primitives.status.props.text"
    assert suite.to_dict()["files"][1]["expectationFailures"][0]["passed"] is False
    assert run_replay_suite(first).to_dict()["files"][0]["path"] == first.as_posix()


def test_replay_suite_can_run_with_style_pack(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "styled.json").write_text(
        json.dumps(
            {
                "steps": [{"type": "mutations", "mutations": []}],
                "expect": {
                    "sceneRevision": 0,
                    "checks": [
                        {"path": "metadata.displayStyle", "equals": "mainframe"},
                        {"path": "primitives.stage.props.theme", "equals": "mainframe"},
                    ],
                },
            }
        ),
        "utf-8",
    )

    suite = run_replay_suite(fixture_dir, style="mainframe")

    assert suite.ok is True
    assert suite.files[0].scene_revision == 0
    assert suite.files[0].expectations == 3


def test_replay_suite_captures_screenshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_dir = tmp_path / "fixtures"
    nested = fixture_dir / "nested"
    screenshot_dir = tmp_path / "screenshots"
    nested.mkdir(parents=True)
    first = fixture_dir / "first.json"
    second = nested / "second.json"
    event = event_payload(1, "message_update", {"assistantMessageEvent": {"delta": "ok"}})
    for replay_file in (first, second):
        replay_file.write_text(
            json.dumps(
                {
                    "steps": [{"type": "event", "event": event}],
                    "expect": {"sceneRevision": 1},
                    "screenshotExpect": {
                        "nonblank": True,
                        "checks": [
                            {"path": "canvasMetrics.litRatio", "min": 0.05},
                            {"path": "canvasMetrics.maxChannelTotal", "max": 600},
                            {"path": "canvasMetrics.sampledPixels", "exists": True},
                        ],
                    },
                }
            ),
            "utf-8",
        )
    captures: list[tuple[str, int, int, int]] = []

    def fake_capture(state: GibsonServerState, path: str | Path, *, width: int, height: int) -> BrowserScreenshotResult:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake screenshot")
        try:
            label = output.relative_to(screenshot_dir).as_posix()
        except ValueError:
            label = output.name
        captures.append((label, state.scene.state.revision, width, height))
        return BrowserScreenshotResult(
            output,
            "http://127.0.0.1:1",
            state.scene.state.revision,
            width,
            height,
            {
                "nonblank": True,
                "litRatio": 0.12,
                "maxChannelTotal": 255,
                "sampledPixels": 100,
            },
        )

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    suite = run_replay_suite(fixture_dir, screenshot_dir=screenshot_dir, screenshot_width=640, screenshot_height=480)

    assert suite.ok is True
    assert captures == [
        ("first.png", 1, 640, 480),
        ("nested/second.png", 1, 640, 480),
    ]
    assert suite.files[0].screenshot == {
        "path": str(screenshot_dir / "first.png"),
        "url": "http://127.0.0.1:1",
        "sceneRevision": 1,
        "width": 640,
        "height": 480,
        "canvasMetrics": {
            "nonblank": True,
            "litRatio": 0.12,
            "maxChannelTotal": 255,
            "sampledPixels": 100,
        },
    }
    assert suite.files[0].screenshot_expectations == 4
    assert suite.to_dict()["files"][0]["screenshotExpectations"] == 4
    assert suite.to_dict()["files"][1]["screenshot"]["path"] == str(screenshot_dir / "nested" / "second.png")
    assert (screenshot_dir / "nested" / "second.png").read_bytes() == b"fake screenshot"

    single = run_replay_suite(first, screenshot_dir=tmp_path / "single-screenshot")
    assert single.files[0].screenshot["path"] == str(tmp_path / "single-screenshot" / "first.png")


def test_replay_suite_checks_screenshot_expectations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replay_path = tmp_path / "fixture.json"
    event = event_payload(1, "message_update", {"assistantMessageEvent": {"delta": "ok"}})
    replay_path.write_text(
        json.dumps(
            {
                "steps": [{"type": "event", "event": event}],
                "screenshotExpect": {
                    "checks": [
                        {"path": "canvasMetrics.nonblank", "equals": True},
                        {"path": "canvasMetrics.litRatio", "min": 0.5},
                        {"path": "canvasMetrics.maxChannelTotal", "max": 100},
                    ],
                },
            }
        ),
        "utf-8",
    )

    def fake_capture(state: GibsonServerState, path: str | Path, *, width: int, height: int) -> BrowserScreenshotResult:
        return BrowserScreenshotResult(
            Path(path),
            "http://127.0.0.1:1",
            state.scene.state.revision,
            width,
            height,
            {"nonblank": True, "litRatio": 0.02, "maxChannelTotal": 240},
        )

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    suite = run_replay_suite(replay_path, screenshot_dir=tmp_path / "screenshots")

    assert suite.ok is False
    assert suite.files[0].screenshot_expectations == 3
    assert [failure.path for failure in suite.files[0].screenshot_expectation_failures] == [
        "canvasMetrics.litRatio",
        "canvasMetrics.maxChannelTotal",
    ]
    assert suite.files[0].error.startswith("replay screenshot expectations failed:")
    assert suite.files[0].screenshot is not None
    payload = suite.to_dict()["files"][0]
    assert payload["screenshotExpectations"] == 3
    assert payload["screenshotExpectationFailures"][0]["op"] == "min"

    direct = evaluate_screenshot_expectations(
        {"canvasMetrics": {"litRatio": 0.2, "nonblank": True}},
        {
            "nonblank": True,
            "checks": [
                {"path": "canvasMetrics.litRatio", "max": 0.3},
                {"path": "canvasMetrics.missing", "exists": False},
            ],
        },
    )
    assert [result.passed for result in direct] == [True, True, True]

    with pytest.raises(ValueError, match="replay screenshotExpect must be an object"):
        evaluate_screenshot_expectations({}, [])
    with pytest.raises(ValueError, match="replay screenshotExpect checks must be a list"):
        evaluate_screenshot_expectations({}, {"checks": "bad"})
    with pytest.raises(ValueError, match="replay screenshotExpect check 0 must be an object"):
        evaluate_screenshot_expectations({}, {"checks": ["bad"]})


def test_replay_suite_updates_checks_and_fails_baselines(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    nested = fixture_dir / "nested"
    baseline_dir = tmp_path / "baselines"
    nested.mkdir(parents=True)
    first = fixture_dir / "first.json"
    second = nested / "second.json"
    event = event_payload(1, "message_update", {"assistantMessageEvent": {"delta": "ok"}})
    for replay_file in (first, second):
        replay_file.write_text(
            json.dumps({"steps": [{"type": "event", "event": event}], "expect": {"sceneRevision": 1}}),
            "utf-8",
        )

    with pytest.raises(ValueError, match="update_baselines requires baseline_dir"):
        run_replay_suite(fixture_dir, update_baselines=True)

    missing = run_replay_suite(fixture_dir, baseline_dir=baseline_dir)
    assert missing.ok is False
    assert missing.files[0].baseline == ReplayBaselineResult(
        path=(baseline_dir / "first.json").as_posix(),
        ok=False,
        error=f"baseline missing: {baseline_dir / 'first.json'}",
    )

    updated = run_replay_suite(fixture_dir, baseline_dir=baseline_dir, update_baselines=True)
    assert updated.ok is True
    assert updated.files[0].baseline == ReplayBaselineResult(
        path=(baseline_dir / "first.json").as_posix(),
        ok=True,
        updated=True,
    )
    assert updated.files[1].baseline.path == (baseline_dir / "nested" / "second.json").as_posix()
    assert json.loads((baseline_dir / "nested" / "second.json").read_text(encoding="utf-8"))["scene"]["revision"] == 1
    assert updated.to_dict()["files"][0]["baseline"]["updated"] is True

    single = run_replay_suite(first, baseline_dir=tmp_path / "single-baseline", update_baselines=True)
    assert single.files[0].baseline.path == (tmp_path / "single-baseline" / "first.json").as_posix()

    checked = run_replay_suite(fixture_dir, baseline_dir=baseline_dir)
    assert checked.ok is True
    assert checked.files[0].baseline.ok is True
    assert checked.files[0].baseline.updated is False

    corrupted = json.loads((baseline_dir / "first.json").read_text(encoding="utf-8"))
    corrupted["scene"]["revision"] = 99
    (baseline_dir / "first.json").write_text(json.dumps(corrupted), encoding="utf-8")
    failed = run_replay_suite(fixture_dir, baseline_dir=baseline_dir)
    assert failed.ok is False
    assert failed.files[0].steps == 1
    assert failed.files[0].scene_revision == 1
    assert failed.files[0].expectations == 1
    assert failed.files[0].baseline.ok is False
    assert failed.files[0].error.startswith("baseline scene mismatch")


def test_replay_suite_reports_screenshot_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replay_path = tmp_path / "fixture.json"
    event = event_payload(1, "message_update", {"assistantMessageEvent": {"delta": "ok"}})
    replay_path.write_text(
        json.dumps({"steps": [{"type": "event", "event": event}], "expect": {"sceneRevision": 1}}),
        "utf-8",
    )

    def fake_capture(state: GibsonServerState, path: str | Path, *, width: int, height: int) -> BrowserScreenshotResult:
        raise RuntimeError(f"browser unavailable for {Path(path).name} at {width}x{height}")

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    suite = run_replay_suite(replay_path, screenshot_dir=tmp_path / "screenshots", screenshot_width=320)

    assert suite.ok is False
    assert suite.failed == 1
    assert suite.files[0].path == replay_path.as_posix()
    assert suite.files[0].steps == 1
    assert suite.files[0].scene_revision == 1
    assert suite.files[0].expectations == 1
    assert "browser unavailable for fixture.png at 320x900" == suite.files[0].error


def test_replay_suite_validation(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="replay path not found"):
        discover_replay_files(tmp_path / "missing")
    with pytest.raises(ValueError, match="no replay JSON files"):
        discover_replay_files(tmp_path)

    bad = tmp_path / "bad.json"
    bad.write_text("[not-json", "utf-8")
    suite = run_replay_suite(bad)
    assert suite.failed == 1
    assert suite.files[0].ok is False
    assert "Expecting value" in suite.files[0].error


def test_replay_raw_event_without_decisions_and_empty_plan() -> None:
    result = run_replay_data(
        {
            "steps": [
                {"type": "raw_event", "raw": {"type": "input", "text": "raw input"}},
                {
                    "type": "render_plan",
                    "requests": [{"event": event_payload(20, "tool_call", {"toolName": "bash"})}],
                    "steps": [],
                },
            ]
        }
    )

    assert result.steps[0].updates == 1
    assert result.steps[1].updates == 0
    assert result.steps[1].scene_revision == result.scene.revision


def test_render_plan_parsers_accept_direct_mappings() -> None:
    request_event = event_payload(7, "tool_result", {"toolName": "bash"})
    request = render_request_from_mapping(
        {
            "event": request_event,
            "route": "direct_scene",
            "timeline_offset_ms": 12,
            "coalesced_count": 3,
            "metadata": {"x": 1},
        }
    )
    step = render_step_from_mapping(
        {
            "delay_ms": 4,
            "start_offset_ms": 8,
            "event_index": 0,
            "mutations": [{"op": "append_log", "entry": {"ok": True}}],
        }
    )
    plan = render_plan_from_mapping(
        {
            "requests": [request.to_dict()],
            "steps": [step.to_dict()],
            "metadata": {"renderer": "direct-test"},
        }
    )

    assert request.timeline_offset_ms == 12
    assert request.coalesced_count == 3
    assert step.delay_ms == 4
    assert plan.metadata == {"renderer": "direct-test"}
    assert plan.steps[0].mutations[0].entry == {"ok": True}

    no_index_step = render_step_from_mapping({"mutations": []})
    assert no_index_step.event_index is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "replay file must contain a JSON object"),
    ],
)
def test_load_replay_file_validation(tmp_path: Path, payload: object, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_replay_file(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "replay must contain a steps list"),
        ({"steps": [{}]}, "unsupported replay step type"),
        ({"steps": ["bad"]}, "replay step 0 must be an object"),
        ({"steps": [{"type": "event"}]}, "event replay step 0 must include event object"),
        ({"steps": [{"type": "raw_event"}]}, "raw_event replay step 0 must include raw"),
        (
            {"steps": [{"type": "render_plan", "requests": [], "steps": []}]},
            "render_plan replay step must include non-empty requests",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": [{"event": event_payload()}]}]},
            "render_plan replay step must include steps list",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": ["bad"], "steps": []}]},
            "render request must be an object",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": [{"event": event_payload()}], "steps": ["bad"]}]},
            "render step must be an object",
        ),
        (
            {"steps": [{"type": "mutations", "mutations": "bad"}]},
            "replay step 0 mutations must be a list",
        ),
        (
            {"steps": [{"type": "mutations", "mutations": ["bad"]}]},
            "replay step 0 mutation must be an object",
        ),
        ({"steps": [], "expect": []}, "replay expect must be an object"),
        ({"steps": [], "expect": {"checks": {}}}, "replay expect checks must be a list"),
        (
            {"steps": [], "expect": {"checks": ["bad"]}},
            "replay expect check 0 must be an object",
        ),
        (
            {"steps": [], "expect": {"checks": [{"equals": 1}]}},
            "replay expect check 0 must include path",
        ),
        (
            {"steps": [], "expect": {"checks": [{"path": "revision"}]}},
            "replay expect check 0 must include exactly one operation",
        ),
        (
            {"steps": [], "expect": {"checks": [{"path": "revision", "equals": 0, "exists": True}]}},
            "replay expect check 0 must include exactly one operation",
        ),
    ],
)
def test_run_replay_data_validation(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_replay_data(payload)


def test_parser_validation_helpers() -> None:
    with pytest.raises(ValueError, match="render request must include event object"):
        render_request_from_mapping({})
    with pytest.raises(ValueError, match="replay step -1 mutations must be a list"):
        render_step_from_mapping({"mutations": None})
    with pytest.raises(ValueError, match="replay step 3 mutation must be an object"):
        mutations_from_value([object()], 3)

    step = ReplayStepResult(1, "event", 5, 0)
    assert step.to_dict() == {"index": 1, "kind": "event", "sceneRevision": 5, "updates": 0}
