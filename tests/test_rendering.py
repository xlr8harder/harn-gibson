from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.external_renderer import (
    ExternalRenderer,
    external_renderer_from_env,
    external_renderer_payload,
    parse_renderer_command,
    render_plan_from_external_response,
    renderer_timeout_seconds_from_env,
)
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
    coerce_render_timing_mode,
    decisions_from_payload,
    render_accept_payload,
    render_intent_from_plan,
    render_plan_diagnostics_payload,
    render_plan_has_validation_errors,
    render_update_payload,
    step_schedule,
    step_schedule_payload,
    validate_render_plan,
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


def test_external_renderer_command_env_and_response_parser() -> None:
    request = RenderRequest(event(1, "tool_call"))
    response = {
        "plan": {
            "steps": [
                {
                    "eventIndex": 0,
                    "delayMs": 5,
                    "startOffsetMs": 2,
                    "mutations": [{"op": "patch", "targetId": "status", "props": {"text": "external"}}],
                }
            ],
            "metadata": {"intent": "external patch"},
        }
    }

    plan = render_plan_from_external_response(response, (request,), renderer_id="fixture-renderer")
    renderer_metadata_plan = render_plan_from_external_response(
        {"steps": [{"mutations": []}], "metadata": {"renderer": "named-renderer"}},
        (request,),
    )

    assert plan.requests == (request,)
    assert plan.metadata == {"intent": "external patch", "renderer": "fixture-renderer"}
    assert plan.steps[0].delay_ms == 5
    assert plan.steps[0].start_offset_ms == 2
    assert plan.steps[0].mutations[0].target_id == "status"
    assert renderer_metadata_plan.metadata == {"renderer": "named-renderer"}
    assert renderer_metadata_plan.steps[0].event_index is None
    assert parse_renderer_command('["python", "renderer.py"]') == ("python", "renderer.py")
    assert parse_renderer_command("python renderer.py") == ("python", "renderer.py")
    assert renderer_timeout_seconds_from_env(None) == 30.0
    assert renderer_timeout_seconds_from_env("") == 30.0
    assert renderer_timeout_seconds_from_env("2500") == 2.5
    assert external_renderer_from_env(None) is None
    assert external_renderer_from_env('["python", "renderer.py"]', "100").command == ("python", "renderer.py")
    with pytest.raises(ValueError, match="cannot be empty"):
        ExternalRenderer(())

    for command_value, message in (
        ("", "cannot be empty"),
        ('""', "cannot be empty"),
        ("[", "JSON must be an array"),
        ("[]", "non-empty array"),
        ("[1]", "non-empty strings"),
    ):
        with pytest.raises(ValueError, match=message):
            parse_renderer_command(command_value)
    for timeout_value in ("nope", "0"):
        with pytest.raises(ValueError, match="positive number"):
            renderer_timeout_seconds_from_env(timeout_value)
    for bad_response, message in (
        ([], "JSON object"),
        ({}, "steps list"),
        ({"steps": [[]]}, "step 0"),
        ({"steps": [{"mutations": {}}]}, "mutations must be a list"),
        ({"steps": [{"mutations": [None]}]}, "mutation must be an object"),
    ):
        with pytest.raises(ValueError, match=message):
            render_plan_from_external_response(bad_response, (request,))


def test_external_renderer_subprocess_receives_context_and_returns_plan(tmp_path: Path) -> None:
    script = tmp_path / "renderer.py"
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
request = payload["requests"][-1]["event"]
assert payload["schema"] == "harn-gibson.external-renderer-request.v1"
assert payload["context"]["schema"] == "harn-gibson.renderer-context.v1"
assert payload["scene"]["schema"] == "harn-gibson.scene.v1"
json.dump(
    {
        "metadata": {"intent": "external renderer patch"},
        "steps": [
            {
                "eventIndex": 0,
                "mutations": [
                    {
                        "op": "patch",
                        "targetId": "status",
                        "props": {
                            "text": "external:" + request["eventType"],
                            "phase": "lifecycle",
                            "tone": "cyan",
                        },
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
    renderer = ExternalRenderer((sys.executable, str(script)), timeout_seconds=2, renderer_id="fixture-external")
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), renderer=renderer)

    result = pipeline.submit(RenderRequest(event(3, "tool_call")))
    direct_plan = renderer.render((RenderRequest(event(4, "browser_input")),), SceneEngine().state)
    payload = external_renderer_payload(
        (RenderRequest(event(5)),),
        SceneEngine().state,
        RendererContext("rolling", {}, {}, {}, {}),
    )

    assert result.scene_revision == 1
    assert result.updates[0]["renderPlan"]["metadata"] == {
        "intent": "external renderer patch",
        "renderer": "fixture-external",
    }
    assert pipeline.scene.state.primitives["status"].props["text"] == "external:tool_call"
    assert direct_plan.steps[0].mutations[0].props["text"] == "external:browser_input"
    assert payload["schema"] == "harn-gibson.external-renderer-request.v1"
    assert payload["requests"][0]["event"]["eventType"] == "input"


def test_render_plan_validation_reports_scene_and_catalog_issues() -> None:
    request = RenderRequest(event(1, "tool_call"))
    plan = RenderPlan(
        (request,),
        (
            RenderStep(
                (
                    SceneMutation("patch", target_id="missing-panel", props={"text": "bad"}),
                    SceneMutation(
                        "upsert",
                        primitive=ScenePrimitive(
                            "unknown-primitive",
                            "hologram",
                            "void",
                            {"density": 2},
                        ),
                    ),
                    SceneMutation(
                        "upsert",
                        primitive=ScenePrimitive(
                            "unsafe-vector",
                            "svg_layer",
                            "stage",
                            {
                                "rawSvg": "<svg><script></script></svg>",
                                "symbols": [{"kind": "dragon"}, []],
                            },
                        ),
                    ),
                    SceneMutation(
                        "start_animation",
                        animation=SceneAnimation("wormhole-1", "missing-vector", "wormhole", 10, 0),
                    ),
                ),
                delay_ms=-2,
                start_offset_ms=-5,
                event_index=4,
            ),
        ),
        {"renderer": "fixture"},
    )

    issues = validate_render_plan(plan, SceneEngine().state, pipeline_catalog())
    limited = validate_render_plan(plan, SceneEngine().state, pipeline_catalog(), max_steps=0, max_mutations=1)
    codes = {issue.code for issue in issues}
    payload = render_plan_diagnostics_payload(issues)

    assert {
        "animation_target_missing",
        "event_index_out_of_range",
        "invalid_svg_symbol",
        "negative_timing",
        "nonpositive_animation_duration",
        "patch_target_missing",
        "raw_svg_markup",
        "unknown_region",
        "unsupported_animation_kind",
        "unsupported_primitive_kind",
        "unsupported_svg_symbol",
    } <= codes
    assert {issue.code for issue in limited} >= {"plan_too_many_steps", "plan_too_many_mutations"}
    assert render_plan_has_validation_errors(issues) is True
    assert payload["schema"] == "harn-gibson.render-plan-diagnostics.v1"
    assert payload["status"] == "rejected"
    assert payload["errorCount"] == 2
    assert payload["warningCount"] == len(issues) - 2
    assert payload["issues"][0]["stepIndex"] == 0


def test_render_plan_validation_covers_safe_and_missing_payload_branches() -> None:
    request = RenderRequest(event(1, "tool_call"))
    plan = RenderPlan(
        (request,),
        (
            RenderStep(
                (
                    SceneMutation("reset_scene"),
                    SceneMutation("append_log", entry={"summary": "ok"}),
                    SceneMutation("remove", target_id="event-feed"),
                    SceneMutation("stop_animation", target_id="done-animation"),
                    SceneMutation("upsert"),
                    SceneMutation("upsert", primitive=ScenePrimitive("", "mesh", "stage")),
                    SceneMutation("patch"),
                    SceneMutation("remove"),
                    SceneMutation("start_animation"),
                    SceneMutation("start_animation", animation=SceneAnimation("", "None", "pulse", 10, 100)),
                    SceneMutation(
                        "upsert",
                        primitive=ScenePrimitive(
                            "safe-vector",
                            "svg_layer",
                            "stage",
                            {
                                "animation": {"durationMs": 1200, "loop": False},
                                "durationMs": 2400,
                                "loop": True,
                                "symbols": [{"kind": "globe"}],
                                "keyframes": [
                                    {"at": 0, "x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 0.8},
                                    {"timeMs": 1200, "transform": {"x": 4, "y": -2, "scale": 1.1}},
                                ],
                                "groups": [
                                    {
                                        "groups": [
                                            {
                                                "groups": [
                                                    {
                                                        "groups": [
                                                            {
                                                                "keyframes": [
                                                                    {"at": 0, "scale": 1},
                                                                ],
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ],
                            },
                        ),
                    ),
                    SceneMutation(
                        "upsert",
                        primitive=ScenePrimitive(
                            "symbol-less-vector",
                            "svg_layer",
                            "stage",
                            {"paths": []},
                        ),
                    ),
                    SceneMutation("start_animation", animation=SceneAnimation("pulse-1", "status", "pulse", 10, 100)),
                    SceneMutation("stop_animation"),
                ),
                event_index=0,
            ),
        ),
        {"renderer": "fixture"},
    )

    issues = validate_render_plan(plan, SceneEngine().state, None)
    limited = validate_render_plan(plan, SceneEngine().state, pipeline_catalog(), max_steps=0, max_mutations=1)
    codes = {issue.code for issue in issues}
    limited_payload = render_plan_diagnostics_payload(limited)

    assert {
        "missing_animation",
        "missing_animation_id",
        "missing_animation_target",
        "missing_patch_target",
        "missing_primitive_id",
        "missing_remove_target",
        "missing_stop_animation_target",
        "missing_upsert_primitive",
    } <= codes
    assert "stepIndex" not in limited_payload["issues"][0]
    assert render_plan_diagnostics_payload(()) == {
        "schema": "harn-gibson.render-plan-diagnostics.v1",
        "status": "accepted",
        "errorCount": 0,
        "warningCount": 0,
        "issues": [],
    }


def test_render_plan_validation_checks_svg_patch_keyframes() -> None:
    request = RenderRequest(event(1, "tool_call"))
    scene = SceneEngine()
    scene.apply(
        (
            SceneMutation(
                "upsert",
                primitive=ScenePrimitive(
                    "vector",
                    "svg_layer",
                    "stage",
                    {"symbols": [{"kind": "globe"}]},
                ),
            ),
        )
    )
    too_many_keyframes = [{"at": index / 70, "x": index} for index in range(70)]
    plan = RenderPlan(
        (request,),
        (
            RenderStep(
                (
                    SceneMutation(
                        "patch",
                        target_id="vector",
                        props={
                            "rawSvg": "<svg><script></script></svg>",
                            "animation": "spin fast",
                            "durationMs": -1,
                            "delayMs": "soon",
                            "loop": "forever",
                            "yoyo": 1,
                            "keyframes": too_many_keyframes,
                            "groups": [
                                {
                                    "durationMs": "fast",
                                    "loop": False,
                                    "keyframes": [
                                        {
                                            "at": "start",
                                            "transform": {
                                                "x": "far",
                                                "opacity": 0.8,
                                                "skew": 3,
                                            },
                                            "morph": "circle",
                                        },
                                        [],
                                        {"transform": "bad"},
                                    ],
                                },
                                {"keyframes": "bad"},
                                "ignored-group",
                            ],
                        },
                    ),
                ),
                event_index=0,
            ),
        ),
        {"renderer": "fixture"},
    )

    issues = validate_render_plan(plan, scene.state, pipeline_catalog())
    codes = {issue.code for issue in issues}
    payload = render_plan_diagnostics_payload(issues)

    assert {
        "invalid_svg_keyframe",
        "invalid_svg_keyframe_animation",
        "invalid_svg_keyframe_boolean",
        "invalid_svg_keyframe_transform",
        "invalid_svg_keyframe_value",
        "invalid_svg_keyframes",
        "nonpositive_svg_keyframe_duration",
        "raw_svg_markup",
        "too_many_svg_keyframes",
        "unsupported_svg_keyframe_field",
    } <= codes
    assert render_plan_has_validation_errors(issues) is True
    assert payload["status"] == "rejected"
    assert payload["errorCount"] == 2
    assert any(issue.target_id == "vector" and issue.value == "props.groups[0].keyframes[0].morph" for issue in issues)


def test_external_renderer_validation_rejects_unsafe_plan_without_crashing(tmp_path: Path) -> None:
    script = tmp_path / "unsafe_renderer.py"
    script.write_text(
        """
import json
import sys

json.dump(
    {
        "metadata": {"intent": "break scene"},
        "steps": [
            {
                "mutations": [
                    {"op": "patch", "targetId": "missing-panel", "props": {"text": "boom"}}
                ]
            }
        ],
    },
    sys.stdout,
)
""".lstrip(),
        encoding="utf-8",
    )
    renderer = ExternalRenderer((sys.executable, str(script)), timeout_seconds=2, renderer_id="fixture-external")
    pipeline = RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), renderer=renderer)

    result = pipeline.submit(RenderRequest(event(6, "tool_result")))

    metadata = result.updates[0]["renderIntent"]["metadata"]
    diagnostics = metadata["renderPlanDiagnostics"]
    trace = pipeline.scene.state.primitives["trace-log"].props["text"][0]
    assert result.scene_revision == 1
    assert metadata["fallbackRenderer"] == "deterministic"
    assert metadata["rendererError"]["message"] == "external renderer returned unsafe render plan"
    assert diagnostics["status"] == "rejected"
    assert diagnostics["errorCount"] == 1
    assert diagnostics["issues"][0]["code"] == "patch_target_missing"
    assert pipeline.scene.state.primitives["status"].props["text"] == "renderer:error"
    assert "patch_target_missing" in trace["details"]


def test_external_renderer_validation_preserves_safe_warning_plans(tmp_path: Path) -> None:
    script = tmp_path / "warning_renderer.py"
    script.write_text(
        """
import json
import sys

json.dump(
    {
        "metadata": {"intent": "invent visual toy"},
        "steps": [
            {
                "mutations": [
                    {
                        "op": "upsert",
                        "primitive": {
                            "id": "unknown-toy",
                            "kind": "hologram",
                            "region": "stage",
                            "props": {"tone": "cyan"},
                        },
                    }
                ]
            }
        ],
    },
    sys.stdout,
)
""".lstrip(),
        encoding="utf-8",
    )
    scene = SceneEngine()
    context = RendererContext("compaction", {}, {}, scene.state.to_dict(), {})
    request = RenderRequest(event(7, "tool_call"))
    renderer = ExternalRenderer((sys.executable, str(script)), timeout_seconds=2, renderer_id="fixture-external")

    plan = renderer.render_with_context((request,), scene.state, context)

    diagnostics = plan.metadata["renderPlanDiagnostics"]
    assert plan.metadata["renderer"] == "fixture-external"
    assert diagnostics["status"] == "accepted_with_warnings"
    assert diagnostics["errorCount"] == 0
    assert diagnostics["warningCount"] == 1
    assert diagnostics["issues"][0]["code"] == "unsupported_primitive_kind"
    assert plan.steps[0].mutations[0].primitive is not None
    assert plan.steps[0].mutations[0].primitive.kind == "hologram"


def test_external_renderer_failures_become_trace_state(tmp_path: Path) -> None:
    failing = tmp_path / "failing_renderer.py"
    failing.write_text(
        "import sys\nsys.stderr.write('renderer exploded ' + 'x' * 5000)\nsys.exit(7)\n",
        encoding="utf-8",
    )
    stdout_only = tmp_path / "stdout_renderer.py"
    stdout_only.write_text(
        "import sys\nsys.stdout.write('stdout-only failure')\nsys.exit(8)\n",
        encoding="utf-8",
    )
    invalid_json = tmp_path / "invalid_renderer.py"
    invalid_json.write_text("print('not json')\n", encoding="utf-8")
    scene = SceneEngine()
    context = RendererContext("compaction", {}, {}, scene.state.to_dict(), {})
    request = RenderRequest(event(6, "tool_result"))
    renderer = ExternalRenderer((sys.executable, str(failing)), timeout_seconds=2, renderer_id="fixture-external")

    class EmptyFallback:
        def render_with_context(
            self,
            requests: tuple[RenderRequest, ...],
            _scene: object,
            _context: RendererContext,
        ) -> RenderPlan:
            return RenderPlan(requests, (), {"renderer": "empty"})

    result = RenderPipeline(scene=scene, buffer=EventBuffer(), renderer=renderer).submit(request)
    empty_fallback_plan = ExternalRenderer(
        (sys.executable, str(failing)),
        timeout_seconds=2,
        renderer_id="fixture-external",
        fallback=EmptyFallback(),  # type: ignore[arg-type]
    ).render_with_context((request,), scene.state, context)
    stdout_plan = ExternalRenderer(
        (sys.executable, str(stdout_only)),
        timeout_seconds=2,
        renderer_id="fixture-external",
    ).render_with_context((request,), scene.state, context)
    invalid_plan = ExternalRenderer(
        (sys.executable, str(invalid_json)),
        timeout_seconds=2,
        renderer_id="fixture-external",
    ).render_with_context((request,), scene.state, context)
    empty_plan = renderer.render_with_context((), scene.state, context)

    trace = scene.state.primitives["trace-log"].props["text"][0]
    assert result.scene_revision == 1
    assert scene.state.primitives["gibson-city"].kind == "city_block"
    assert scene.state.primitives["status"].props["text"] == "renderer:error"
    assert trace["eventType"] == "renderer_error"
    assert "exited with code 7" in trace["message"]
    assert "renderer exploded" in trace["details"]
    assert len(trace["details"]) == 4000
    assert trace["details"].endswith("...")
    assert empty_fallback_plan.steps[0].event_index == 0
    assert empty_fallback_plan.steps[0].mutations[-1].target_id == "trace-log"
    assert result.updates[0]["renderIntent"]["renderer"] == "fixture-external"
    assert result.updates[0]["renderIntent"]["metadata"]["fallbackRenderer"] == "deterministic"
    assert "stdout-only failure" in stdout_plan.metadata["rendererError"]["details"]
    assert invalid_plan.metadata["rendererError"]["message"].startswith("external renderer failed")
    assert "not json" in invalid_plan.metadata["rendererError"]["message"]
    assert empty_plan.steps == ()


def test_deterministic_renderer_adds_repo_graph_from_context(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    for directory in (repo_root / "src" / "harn_gibson", repo_root / "docs"):
        directory.mkdir(parents=True)
    for path in (
        repo_root / "README.md",
        repo_root / "src" / "harn_gibson" / "rendering.py",
        repo_root / "docs" / "renderer-agent.md",
    ):
        path.write_text("x", encoding="utf-8")
    (repo_root / "README-link.md").symlink_to("README.md")
    context_event = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "bash",
            "input": {
                "command": (
                    "uv run pytest src/harn_gibson/rendering.py docs/renderer-agent.md tests/test_rendering.py"
                )
            },
        },
        8,
        timestamp_ms=800,
    )
    scene = SceneEngine()
    batch = RenderInputBatch.from_requests((RenderRequest(context_event),))
    context = RendererContextBuilder(RendererContextConfig(project_root=str(repo_root))).build(
        batch,
        scene.state,
        pipeline_catalog(),
    )

    plan = DeterministicSceneRenderer().render_with_context(batch.requests, scene.state, context)
    mutations = plan.steps[-1].mutations
    repo_map = next(
        mutation.primitive for mutation in mutations if mutation.primitive and mutation.primitive.id == "repo-map"
    )
    repo_city = next(
        mutation.primitive for mutation in mutations if mutation.primitive and mutation.primitive.id == "repo-city"
    )
    touch_field = next(
        mutation.primitive
        for mutation in mutations
        if mutation.primitive and mutation.primitive.id == "repo-touch-field"
    )
    repo_animation = next(
        mutation.animation for mutation in mutations if mutation.animation and mutation.animation.id == "repo-touch-8"
    )
    repo_city_animation = next(
        mutation.animation
        for mutation in mutations
        if mutation.animation and mutation.animation.id == "repo-city-touch-8"
    )
    intent = render_intent_from_plan(plan)

    assert plan.metadata == {"renderer": "deterministic"}
    assert repo_map is not None
    assert repo_map.kind == "node_graph"
    assert repo_map.props["focusNodeId"] == "touch:0"
    assert {node["id"] for node in repo_map.props["nodes"]} >= {"repo-root", "repo:src", "repo:docs", "touch:0"}
    assert next(node for node in repo_map.props["nodes"] if node["id"] == "repo:README-link.md")["tone"] == "amber"
    assert repo_map.props["touchedFiles"][0]["path"] == "src/harn_gibson/rendering.py"
    fallback_edge = next(edge for edge in repo_map.props["edges"] if edge["target"] == "touch:2")
    assert fallback_edge["source"] == "repo-root"
    assert repo_city is not None
    assert repo_city.kind == "city_block"
    assert repo_city.props["layout"] == "repo-bfs-depth-2"
    assert repo_city.props["focusBlockId"] == "repo-city-src-harn_gibson"
    city_blocks = {block["id"]: block for block in repo_city.props["blocks"]}
    assert city_blocks["repo-city-root"]["label"] == "repo"
    assert city_blocks["repo-city-src"]["dirs"] == 1
    assert city_blocks["repo-city-src"]["touched"] == 1
    assert city_blocks["repo-city-src"]["h"] == 0.255
    assert city_blocks["repo-city-src-harn_gibson"]["tone"] == "magenta"
    assert city_blocks["repo-city-docs-renderer-agent-md"]["files"] == 1
    assert city_blocks["repo-city-README-link-md"]["tone"] == "amber"
    assert touch_field is not None
    assert touch_field.props["paths"] == [
        "src/harn_gibson/rendering.py",
        "docs/renderer-agent.md",
        "tests/test_rendering.py",
    ]
    assert repo_animation is not None
    assert repo_animation.target_id == "repo-map"
    assert repo_city_animation is not None
    assert repo_city_animation.target_id == "repo-city"
    assert repo_city_animation.kind == "extrude"
    assert "repo-map" in intent["targets"]
    assert "repo-city" in intent["targets"]
    assert "repo-touch-field" in intent["targets"]
    assert "animation:packet_burst" in intent["effects"]
    assert "animation:extrude" in intent["effects"]

    missing_context = RendererContextBuilder(RendererContextConfig(project_root=str(tmp_path / "missing"))).build(
        RenderInputBatch.from_requests((RenderRequest(event(4)),)),
        scene.state,
        pipeline_catalog(),
    )
    fallback = DeterministicSceneRenderer().render_with_context(
        (RenderRequest(event(4)),),
        scene.state,
        missing_context,
    )
    assert all(
        mutation.primitive is None or mutation.primitive.id != "repo-map" for mutation in fallback.steps[0].mutations
    )
    assert all(
        mutation.primitive is None or mutation.primitive.id != "repo-city" for mutation in fallback.steps[0].mutations
    )
    bad_context = RendererContext(
        "compaction",
        {"repoTopology": "bad", "touchedFiles": "bad"},
        {},
        {},
        {},
    )
    assert DeterministicSceneRenderer().render_with_context((RenderRequest(event(5)),), scene.state, bad_context).steps[
        0
    ].mutations == DeterministicSceneRenderer().render((RenderRequest(event(5)),), scene.state).steps[0].mutations
    bad_shape_context = RendererContext(
        "compaction",
        {"repoTopology": {"entries": "bad"}, "touchedFiles": {"files": "bad"}},
        {},
        {},
        {},
    )
    assert all(
        mutation.primitive is None or mutation.primitive.id != "repo-map"
        for mutation in DeterministicSceneRenderer()
        .render_with_context((RenderRequest(event(6)),), scene.state, bad_shape_context)
        .steps[0]
        .mutations
    )
    assert all(
        mutation.primitive is None or mutation.primitive.id != "repo-city"
        for mutation in DeterministicSceneRenderer()
        .render_with_context((RenderRequest(event(6)),), scene.state, bad_shape_context)
        .steps[0]
        .mutations
    )
    assert DeterministicSceneRenderer().render_with_context((), scene.state, context).steps == ()

    unknown_touch_event = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "bash",
            "input": {"command": "cat unknown/path.py"},
        },
        10,
        timestamp_ms=1000,
    )
    unknown_context = RendererContextBuilder(RendererContextConfig(project_root=str(repo_root))).build(
        RenderInputBatch.from_requests((RenderRequest(unknown_touch_event),)),
        scene.state,
        pipeline_catalog(),
    )
    unknown_plan = DeterministicSceneRenderer().render_with_context(
        (RenderRequest(unknown_touch_event),),
        scene.state,
        unknown_context,
    )
    unknown_city = next(
        mutation.primitive
        for mutation in unknown_plan.steps[0].mutations
        if mutation.primitive and mutation.primitive.id == "repo-city"
    )
    assert unknown_city.props["focusBlockId"] == "repo-city-root"


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
            display_style="mainframe",
            style_pack={"id": "mainframe", "motifs": ["phosphor-grid"]},
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
    assert compaction.project["displayStyle"] == "mainframe"
    assert compaction.project["stylePack"] == {"id": "mainframe", "motifs": ["phosphor-grid"]}
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
    recorded_contexts: list[RendererContext] = []

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
        context_recorder=recorded_contexts.append,
    )

    result = pipeline.submit(RenderRequest(event(1, "tool_call")))

    assert contexts[0].mode == "compaction"
    assert recorded_contexts[0] is contexts[0]
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
    assert result.updates[0]["renderPlan"]["stepSchedule"] == {
        "timingMode": "immediate",
        "startOffsetMs": 0,
        "delayMs": 10,
        "scheduledWaitMs": 10,
        "appliedOffsetMs": 10,
    }
    assert result.updates[1]["renderPlan"]["stepSchedule"]["appliedOffsetMs"] == 10
    assert result.updates[1]["scene"]["revision"] == 1
    with pytest.raises(ValueError, match="render mode"):
        RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), mode="later")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="render timing mode"):
        RenderPipeline(scene=SceneEngine(), buffer=EventBuffer(), timing_mode="later")  # type: ignore[arg-type]


def test_pipeline_scheduled_timing_honors_start_offsets() -> None:
    sleeps: list[float] = []

    class TimelineRenderer:
        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            return RenderPlan(
                requests,
                (
                    RenderStep((SceneMutation("append_log", entry={"step": 0}),), event_index=0),
                    RenderStep(
                        (SceneMutation("append_log", entry={"step": 1}),),
                        start_offset_ms=30,
                        event_index=0,
                    ),
                    RenderStep(
                        (SceneMutation("append_log", entry={"step": 2}),),
                        delay_ms=5,
                        start_offset_ms=20,
                        event_index=0,
                    ),
                ),
                {"renderer": "timeline"},
            )

    pipeline = RenderPipeline(
        scene=SceneEngine(),
        buffer=EventBuffer(),
        renderer=TimelineRenderer(),
        timing_mode="scheduled",
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    result = pipeline.submit(RenderRequest(event(7)))

    assert sleeps == [0.03, 0.005]
    assert [entry["step"] for entry in pipeline.scene.state.log] == [0, 1, 2]
    assert [update["renderPlan"]["stepSchedule"] for update in result.updates] == [
        {
            "timingMode": "scheduled",
            "startOffsetMs": 0,
            "delayMs": 0,
            "scheduledWaitMs": 0,
            "appliedOffsetMs": 0,
        },
        {
            "timingMode": "scheduled",
            "startOffsetMs": 30,
            "delayMs": 0,
            "scheduledWaitMs": 30,
            "appliedOffsetMs": 30,
        },
        {
            "timingMode": "scheduled",
            "startOffsetMs": 20,
            "delayMs": 5,
            "scheduledWaitMs": 5,
            "appliedOffsetMs": 35,
        },
    ]
    assert step_schedule(RenderStep((), delay_ms=-1, start_offset_ms=-1), 4, "scheduled") == (0, 4)
    assert step_schedule_payload(RenderStep((), delay_ms=2, start_offset_ms=3), "scheduled", 1, 5) == {
        "timingMode": "scheduled",
        "startOffsetMs": 3,
        "delayMs": 2,
        "scheduledWaitMs": 1,
        "appliedOffsetMs": 5,
    }


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
    assert coerce_render_timing_mode("scheduled") == "scheduled"
    assert coerce_render_timing_mode("immediate") == "immediate"
    assert coerce_render_timing_mode(None) == "immediate"
    assert coerce_batch_window_ms(None) == 40
    assert coerce_batch_window_ms("12") == 12
    assert coerce_batch_window_ms("-1") == 0
    assert coerce_batch_window_ms("bad") == 40
    assert decisions_from_payload({"decisions": [{"a": 1}, "bad", {"b": 2}]}) == ({"a": 1}, {"b": 2})
