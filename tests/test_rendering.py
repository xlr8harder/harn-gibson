from __future__ import annotations

import json
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
    _repo_file_line_count,
    coerce_batch_window_ms,
    coerce_context_limit,
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
    touched_files_context_from_events,
    validate_render_plan,
)
from harn_gibson.scene import SceneAnimation, SceneEngine, SceneMutation, ScenePrimitive
from harn_gibson.sinks import EventBuffer
from harn_gibson.styles import style_pack_from_name
from harn_gibson.world_bindings import WORLD_BINDING_SCHEMA


def event(sequence: int = 1, event_type: str = "input") -> GibsonEvent:
    return GibsonEvent.from_raw(
        {"type": event_type, "text": "hello", "source": "test"},
        sequence,
        timestamp_ms=sequence * 10,
    )


def test_touched_files_context_from_events_uses_default_config() -> None:
    touched = touched_files_context_from_events(
        (
            GibsonEvent.from_raw(
                {
                    "type": "tool_call",
                    "toolName": "bash",
                    "input": {"command": "python -m pytest tests/test_rendering.py"},
                },
                7,
                timestamp_ms=70,
            ),
        )
    )

    assert touched == {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": "tests/test_rendering.py",
                "operation": "bash:before",
                "firstSequence": 7,
                "lastSequence": 7,
                "phases": ["before"],
                "sources": ["input.command"],
            }
        ],
        "count": 1,
        "truncated": False,
    }


def test_touched_files_context_skips_sed_and_perl_program_fragments() -> None:
    touched = touched_files_context_from_events(
        (
            GibsonEvent.from_raw(
                {
                    "type": "tool_call",
                    "toolName": "bash",
                    "input": {
                        "command": (
                            "sed -i 's/return 2/return 0/' src/repo_map/cli.py && "
                            "perl -pi -e 's/foo/bar/' tests/test_cli.py"
                        )
                    },
                },
                8,
                timestamp_ms=80,
            ),
        )
    )

    assert [item["path"] for item in touched["files"]] == ["src/repo_map/cli.py", "tests/test_cli.py"]
    assert all(item["sources"] == ["input.command"] for item in touched["files"])
    assert "s/return" not in {item["path"] for item in touched["files"]}
    assert "2/return" not in {item["path"] for item in touched["files"]}


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
                            "neural_mist",
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
                                "filters": [
                                    {"kind": "chromatic_split", "intensity": 1.1},
                                    "scanline",
                                ],
                                "clip": {"kind": "iris", "durationMs": 1200, "loop": True},
                                "symbols": [{"kind": "globe"}],
                                "paths": [
                                    {
                                        "d": "M0 0 L10 0 L10 10 Z",
                                        "durationMs": 1200,
                                        "loop": True,
                                        "morphs": [
                                            {"at": 0, "d": "M0 0 L10 0 L10 10 Z"},
                                            {"at": 1, "d": "M1 0 L11 1 L9 11 Z"},
                                        ],
                                    },
                                    {"d": "M0 0 L2 2"},
                                ],
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
    too_many_morphs = [{"at": index / 70, "d": "M0 0 L1 1"} for index in range(70)]
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
                            "filter": {"kind": "raw-css-filter", "intensity": "bright"},
                            "filters": ["drop-shadow(url(https://example.invalid/x))", 42],
                            "clip": {"kind": "scripted", "progress": "done", "loop": "forever"},
                            "keyframes": too_many_keyframes,
                            "paths": [
                                [],
                                {"durationMs": "fast", "loop": "yes", "morphs": "bad"},
                                {
                                    "morphs": [
                                        [],
                                        {"at": "start", "d": 5},
                                        {"at": 0.5, "d": "M0 0 L1 1", "curve": "wild"},
                                    ]
                                },
                                {"morphs": too_many_morphs},
                            ],
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
                                {
                                    "filter": {},
                                    "filters": "glow",
                                    "clip": {"progress": 0.4},
                                },
                                {"paths": "bad"},
                                {
                                    "filters": [{"preset": "bloom", "blur": 0.4}],
                                    "clip": "wipe",
                                },
                                {
                                    "filter": {"type": "glow", "alpha": 0.5},
                                    "clip": 7,
                                },
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
        "invalid_svg_clip_boolean",
        "invalid_svg_clip",
        "invalid_svg_clip_value",
        "invalid_svg_filter",
        "invalid_svg_filter_value",
        "invalid_svg_filters",
        "invalid_svg_paths",
        "invalid_svg_path",
        "invalid_svg_path_morph",
        "invalid_svg_path_morph_d",
        "invalid_svg_path_morphs",
        "nonpositive_svg_keyframe_duration",
        "raw_svg_markup",
        "too_many_svg_keyframes",
        "too_many_svg_path_morphs",
        "unsupported_svg_clip",
        "unsupported_svg_filter",
        "unsupported_svg_keyframe_field",
        "unsupported_svg_path_morph_field",
    } <= codes
    assert render_plan_has_validation_errors(issues) is True
    assert payload["status"] == "rejected"
    assert payload["errorCount"] == 3
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
                            "kind": "neural_mist",
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
    assert plan.steps[0].mutations[0].primitive.kind == "neural_mist"


def test_dogfood_showcase_renderer_returns_valid_event_reactive_plan(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "docs").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo_root / "docs" / "plan.md").write_text("# plan\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    scene = SceneEngine()
    context_event = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "cat src/app.py docs/plan.md"},
            "output": "updated src/app.py and docs/plan.md",
        },
        42,
        timestamp_ms=4200,
        recent_context=("created a tiny project",),
    )
    batch = RenderInputBatch.from_requests((RenderRequest(context_event),))
    context = RendererContextBuilder(RendererContextConfig(project_root=str(repo_root))).build(
        batch,
        scene.state,
        pipeline_catalog(),
    )
    renderer = ExternalRenderer(
        (sys.executable, str(root / "examples" / "renderers" / "gibson_dogfood_renderer.py")),
        timeout_seconds=5,
        renderer_id="fixture-dogfood",
        catalog=pipeline_catalog(),
    )

    plan = renderer.render_with_context(batch.requests, scene.state, context)
    issues = validate_render_plan(plan, scene.state, pipeline_catalog())
    mutations = plan.steps[0].mutations
    primitive_kinds = {mutation.primitive.id: mutation.primitive.kind for mutation in mutations if mutation.primitive}
    animation_kinds = {mutation.animation.id: mutation.animation.kind for mutation in mutations if mutation.animation}

    assert plan.metadata["renderer"] == "gibson-dogfood-showcase"
    assert "renderPlanDiagnostics" not in plan.metadata
    assert issues == ()
    assert plan.steps[0].event_index == 0
    assert primitive_kinds == {
        "dogfood-rain": "data_rain",
        "dogfood-opcodes": "glyph_layer",
        "dogfood-terminal-wall": "terminal_wall",
        "dogfood-access-matrix": "access_matrix",
        "dogfood-orbital-map": "orbital_map",
        "dogfood-tunnel": "tunnel_grid",
        "dogfood-landscape": "wire_landscape",
        "dogfood-vault": "data_vault",
        "dogfood-black-ice": "black_ice",
        "dogfood-ice-mesh": "mesh",
        "dogfood-scope": "signal_scope",
        "dogfood-control-graph": "node_graph",
        "dogfood-route": "trace_route",
        "dogfood-city": "city_block",
        "dogfood-file-sparks": "particle_field",
        "dogfood-hologram": "hologram",
        "dogfood-command-ribbon": "ribbon",
        "dogfood-sigil": "svg_layer",
    }
    assert animation_kinds["dogfood-camera-path"] == "camera_path"
    assert animation_kinds["dogfood-camera-jolt"] == "camera_jolt"
    assert animation_kinds["dogfood-interference"] == "signal_interference"
    assert animation_kinds["dogfood-breach"] == "breach_wave"
    assert animation_kinds["dogfood-city-extrude"] == "extrude"
    assert animation_kinds["dogfood-route-trace"] == "route_trace"
    scene.apply(mutations)
    assert scene.state.primitives["dogfood-city"].props["blocks"][1]["path"] == "docs"
    assert scene.state.primitives["dogfood-city"].props["blocks"][1]["lines"] == 1
    assert scene.state.primitives["dogfood-city"].props["blocks"][1]["h"] == 0.271
    assert scene.state.primitives["dogfood-landscape"].props["rows"] >= 14
    assert scene.state.primitives["dogfood-landscape"].props["focusPeakId"] == "terrain-0"
    assert scene.state.primitives["dogfood-landscape"].props["peaks"][0]["label"] == "docs"
    assert scene.state.primitives["dogfood-landscape"].props["peaks"][0]["touched"] == 1
    assert scene.state.primitives["dogfood-file-sparks"].props["label"] == "3 TOUCHED FILES"
    assert len(scene.state.primitives["dogfood-file-sparks"].props["emitters"]) == 3
    assert scene.state.primitives["dogfood-ice-mesh"].props["label"] == "ICE TOOL RESULT"
    assert scene.state.primitives["dogfood-black-ice"].props["label"] == "BLACK ICE AFTER"
    assert scene.state.primitives["dogfood-black-ice"].props["breach"] == 0.64
    assert scene.state.primitives["dogfood-vault"].props["locks"] == 11
    assert scene.state.primitives["dogfood-vault"].props["packets"] > 32
    assert scene.state.primitives["dogfood-opcodes"].props["density"] == 0.295
    assert scene.state.primitives["dogfood-terminal-wall"].props["panels"][1]["title"] == "COMMAND BUS"
    assert scene.state.primitives["dogfood-terminal-wall"].props["panels"][1]["lines"][0] == (
        "cat src/app.py docs/plan.md"
    )
    assert scene.state.primitives["dogfood-access-matrix"].props["focusCellId"] == "file-0"
    assert scene.state.primitives["dogfood-access-matrix"].props["rows"] == 3
    assert scene.state.primitives["dogfood-access-matrix"].props["columns"] == 5
    assert scene.state.primitives["dogfood-access-matrix"].props["cells"][2]["breached"] is True
    assert scene.state.primitives["dogfood-orbital-map"].props["focusNodeId"] == "gibson"
    assert scene.state.primitives["dogfood-orbital-map"].props["nodes"][3]["label"] == "GIBSON"
    assert scene.state.primitives["dogfood-orbital-map"].props["arcs"][2]["to"] == "gibson"
    assert scene.state.primitives["dogfood-orbital-map"].props["packets"] >= 45
    terminal_file_lines = scene.state.primitives["dogfood-terminal-wall"].props["panels"][2]["lines"]
    assert "docs/plan.md" in terminal_file_lines
    assert "src/app.py" in terminal_file_lines
    assert scene.state.primitives["dogfood-terminal-wall"].props["panels"][3]["lines"] == [
        "updated src/app.py and docs/plan.md"
    ]
    assert scene.state.primitives["dogfood-control-graph"].props["focusNodeId"] == "file-0"
    assert scene.state.primitives["dogfood-command-ribbon"].props["labels"] == ["AFTER", "TOOL_RESULT"]
    assert scene.state.primitives["dogfood-route"].props["focusHopId"] == "target-0"
    assert scene.state.primitives["dogfood-hologram"].props["rings"] == 6
    assert scene.state.animations["dogfood-camera-path"].props["keyframes"][1]["scale"] == 1.038
    assert scene.state.animations["dogfood-interference"].target_id == "scan-grid"
    assert scene.state.animations["dogfood-interference"].props["label"] == "SIGNAL BREAK"
    assert scene.state.animations["dogfood-interference"].props["noise"] == 96
    assert scene.state.animations["dogfood-route-trace"].props["points"][2]["label"] == "TOOL_RESUL"
    assert "displayStyle" not in plan.metadata

    styled_scene = SceneEngine()
    styled_context = RendererContextBuilder(
        RendererContextConfig(
            project_root=str(repo_root),
            display_style="mainframe",
            style_pack=style_pack_from_name("mainframe").to_dict(),
        )
    ).build(batch, styled_scene.state, pipeline_catalog())
    styled_plan = renderer.render_with_context(batch.requests, styled_scene.state, styled_context)
    assert validate_render_plan(styled_plan, styled_scene.state, pipeline_catalog()) == ()
    styled_scene.apply(styled_plan.steps[0].mutations)

    assert styled_plan.metadata["displayStyle"] == "mainframe"
    assert styled_plan.metadata["styleMotifs"] == ["phosphor-grid", "audit-frames", "amber-alerts"]
    assert styled_scene.state.primitives["dogfood-rain"].props["tone"] == "amber"
    assert styled_scene.state.primitives["dogfood-rain"].props["accentTone"] == "green"
    assert styled_scene.state.primitives["dogfood-ice-mesh"].props["material"] == "amber"
    assert styled_scene.state.primitives["dogfood-black-ice"].props["tone"] == "red"

    uplink_scene = SceneEngine()
    uplink_context = RendererContextBuilder(
        RendererContextConfig(
            project_root=str(repo_root),
            display_style="satellite-uplink",
            style_pack=style_pack_from_name("satellite-uplink").to_dict(),
        )
    ).build(batch, uplink_scene.state, pipeline_catalog())
    uplink_plan = renderer.render_with_context(batch.requests, uplink_scene.state, uplink_context)
    assert validate_render_plan(uplink_plan, uplink_scene.state, pipeline_catalog()) == ()
    uplink_scene.apply(uplink_plan.steps[0].mutations)

    assert uplink_plan.metadata["displayStyle"] == "satellite-uplink"
    assert uplink_plan.metadata["styleMotifs"] == ["orbital-grid", "radar-sweeps", "warning-chevrons"]
    assert uplink_scene.state.primitives["dogfood-rain"].props["tone"] == "amber"
    assert uplink_scene.state.primitives["dogfood-rain"].props["accentTone"] == "red"
    assert uplink_scene.state.primitives["dogfood-ice-mesh"].props["material"] == "amber"
    assert uplink_scene.state.primitives["dogfood-black-ice"].props["accentTone"] == "red"


def test_gibson1_renderer_returns_coherent_valid_plan(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "tests").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hi')\nprint('there')\n", encoding="utf-8")
    (repo_root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    scene = SceneEngine()
    context_event = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "uv run pytest"},
            "output": "tests/test_app.py passed",
        },
        51,
        timestamp_ms=5100,
        recent_context=("running project tests",),
    )
    batch = RenderInputBatch.from_requests((RenderRequest(context_event),))
    context = RendererContextBuilder(RendererContextConfig(project_root=str(repo_root))).build(
        batch,
        scene.state,
        pipeline_catalog(),
    )
    renderer = ExternalRenderer(
        (sys.executable, str(root / "examples" / "renderers" / "gibson1_renderer.py")),
        timeout_seconds=5,
        renderer_id="fixture-gibson1",
        catalog=pipeline_catalog(),
    )

    plan = renderer.render_with_context(batch.requests, scene.state, context)
    issues = validate_render_plan(plan, scene.state, pipeline_catalog())
    mutations = plan.steps[0].mutations
    primitive_kinds = {mutation.primitive.id: mutation.primitive.kind for mutation in mutations if mutation.primitive}
    animation_kinds = {mutation.animation.id: mutation.animation.kind for mutation in mutations if mutation.animation}

    assert plan.metadata["renderer"] == "gibson1"
    assert plan.metadata["mode"] == "usable-default"
    assert plan.metadata["visualizer"] == "gibson1"
    assert plan.metadata["touchedFileCount"] == 1
    assert plan.metadata["repoTerrain"] is True
    assert "displayStyle" not in plan.metadata
    assert "renderPlanDiagnostics" not in plan.metadata
    assert issues == ()
    assert primitive_kinds == {
        "gibson1-terminal": "terminal_wall",
        "gibson1-repo-terrain": "wire_landscape",
        "gibson1-repo-city": "city_block",
        "gibson1-scope": "signal_scope",
        "gibson1-route": "trace_route",
        "gibson1-rain": "data_rain",
    }
    assert animation_kinds == {
        "gibson1-cues": "timeline_cue",
        "gibson1-route-trace": "route_trace",
    }
    assert all(not primitive_id.startswith("dogfood-") for primitive_id in primitive_kinds)

    scene.apply(mutations)
    assert scene.state.primitives["status"].props["text"] == "gibson1::tool_result"
    assert scene.state.primitives["gibson1-terminal"].props["title"] == "GIBSON1 EVENT BOARD"
    assert scene.state.primitives["gibson1-terminal"].props["panels"][1]["lines"] == ["uv run pytest"]
    assert scene.state.primitives["gibson1-terminal"].props["panels"][3]["lines"] == [
        "tests/test_app.py passed"
    ]
    repo_city = scene.state.primitives["gibson1-repo-city"]
    assert repo_city.props["focusBlockId"] == "gibson1-block-1"
    assert repo_city.props["heightScale"] == 0.92
    assert repo_city.props["cameraPath"]["keyframes"][0]["scale"] == 0.90
    assert repo_city.props["blocks"][1]["path"] == "tests"
    assert repo_city.props["blocks"][1]["touched"] == 1
    assert 0.45 <= repo_city.props["blocks"][1]["y"] <= 0.65
    assert repo_city.props["blocks"][1]["h"] <= 0.32
    city_blocks = {block["path"]: block for block in repo_city.props["blocks"]}
    assert city_blocks["src/app.py"]["parentId"] == "gibson1-block-0"
    assert city_blocks["src/app.py"]["lines"] == 2
    assert city_blocks["tests/test_app.py"]["parentId"] == "gibson1-block-1"
    repo_terrain = scene.state.primitives["gibson1-repo-terrain"]
    terrain_peaks = {peak["path"]: peak for peak in repo_terrain.props["peaks"]}
    assert repo_terrain.props["focusPeakId"] == "gibson1-terrain-1"
    assert repo_terrain.props["opacity"] == 0.30
    assert terrain_peaks["tests"]["tone"] == "magenta"
    assert terrain_peaks["tests"]["touched"] == 1
    assert terrain_peaks["tests/test_app.py"]["parentId"] == "gibson1-terrain-1"
    assert scene.state.primitives["gibson1-scope"].props["blips"][0]["label"] == "TEST-APP.PY"
    assert scene.state.primitives["gibson1-route"].props["focusHopId"] == "file-0"
    assert scene.state.animations["gibson1-route-trace"].target_id == "gibson1-route"

    styled_scene = SceneEngine()
    styled_context = RendererContextBuilder(
        RendererContextConfig(
            project_root=str(repo_root),
            display_style="mainframe",
            style_pack=style_pack_from_name("mainframe").to_dict(),
        )
    ).build(batch, styled_scene.state, pipeline_catalog())
    styled_plan = renderer.render_with_context(batch.requests, styled_scene.state, styled_context)
    assert validate_render_plan(styled_plan, styled_scene.state, pipeline_catalog()) == ()
    styled_scene.apply(styled_plan.steps[0].mutations)

    assert styled_plan.metadata["displayStyle"] == "mainframe"
    assert styled_plan.metadata["styleMotifs"] == ["phosphor-grid", "audit-frames", "amber-alerts"]
    assert styled_scene.state.primitives["status"].props["tone"] == "amber"
    assert styled_scene.state.primitives["gibson1-rain"].props["tone"] == "amber"
    assert styled_scene.state.primitives["gibson1-rain"].props["accentTone"] == "green"
    assert styled_scene.state.primitives["gibson1-repo-terrain"].props["accentTone"] == "green"


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
    (repo_root / "README.md").write_text("heading", encoding="utf-8")
    (repo_root / "src" / "harn_gibson" / "rendering.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    (repo_root / "docs" / "renderer-agent.md").write_text("title\nbody\n", encoding="utf-8")
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
    assert repo_map.props["worldBindings"][0]["schema"] == WORLD_BINDING_SCHEMA
    assert repo_map.props["worldBindings"][0]["entityId"] == "repo:."
    assert any(
        binding["entityId"] == "file:src/harn_gibson/rendering.py"
        and binding["source"] == "worldModel"
        and binding["relationship"] == "highlights"
        for binding in repo_map.props["worldBindings"]
    )
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
    assert city_blocks["repo-city-src"]["lines"] == 5
    assert city_blocks["repo-city-src"]["h"] == 0.28
    assert city_blocks["repo-city-src-harn_gibson"]["tone"] == "magenta"
    assert city_blocks["repo-city-src-harn_gibson"]["lines"] == 5
    assert city_blocks["repo-city-docs-renderer-agent-md"]["files"] == 1
    assert city_blocks["repo-city-docs-renderer-agent-md"]["lines"] == 2
    assert city_blocks["repo-city-README-link-md"]["tone"] == "amber"
    assert city_blocks["repo-city-README-link-md"]["lines"] == 0
    assert repo_city.props["cameraPath"]["keyframes"][1]["scale"] == 1.043
    assert repo_city.props["cameraPath"]["durationMs"] == 7600
    city_height_binding = next(
        binding for binding in repo_city.props["worldBindings"] if binding["entityId"] == "repo:src/harn_gibson"
    )
    assert city_height_binding["targetProp"].startswith("blocks[")
    assert city_height_binding["targetProp"].endswith("].h")
    assert city_height_binding["source"] == "repoTopology"
    assert any(
        binding["entityId"] == "file:src/harn_gibson/rendering.py"
        and binding["targetProp"] == "focusBlockId"
        and binding["relationship"] == "focuses"
        for binding in repo_city.props["worldBindings"]
    )
    assert touch_field is not None
    assert touch_field.props["paths"] == [
        "src/harn_gibson/rendering.py",
        "docs/renderer-agent.md",
        "tests/test_rendering.py",
    ]
    assert [binding["targetProp"] for binding in touch_field.props["worldBindings"]] == [
        "paths[0]",
        "paths[1]",
        "paths[2]",
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

    boolean_count_context = RendererContext(
        "compaction",
        {
            "repoTopology": {
                "rootName": "bad-counts",
                "entries": [
                    {
                        "path": "bools",
                        "kind": "dir",
                        "visibleFileCount": True,
                        "visibleDirCount": False,
                        "visibleLineCount": True,
                        "children": [{"path": "bools/file.py", "kind": "file", "lineCount": True}],
                    }
                ],
            },
            "touchedFiles": {"files": []},
        },
        {},
        {},
        {},
    )
    boolean_count_plan = DeterministicSceneRenderer().render_with_context(
        (RenderRequest(event(12)),),
        scene.state,
        boolean_count_context,
    )
    boolean_count_city = next(
        mutation.primitive
        for mutation in boolean_count_plan.steps[0].mutations
        if mutation.primitive and mutation.primitive.id == "repo-city"
    )
    boolean_block = next(block for block in boolean_count_city.props["blocks"] if block["path"] == "bools")
    assert boolean_block["files"] == 1
    assert boolean_block["dirs"] == 1
    assert boolean_block["lines"] == 0


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
        repo_root / "src" / "harn_gibson" / "__init__.py",
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
                "upsert",
                primitive=ScenePrimitive(
                    "continuity-graph",
                    "node_graph",
                    "stage",
                    {
                        "tone": "cyan",
                        "focusNodeId": "renderer",
                        "label": "context-map",
                        "worldBindings": [
                            {
                                "entityId": "file:src/harn_gibson/rendering.py",
                                "entityKind": "file",
                                "fieldPath": "activityCount",
                                "targetProp": "nodes[1].tone",
                                "source": "worldModel",
                                "relationship": "highlights",
                            }
                        ],
                    },
                ),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation("fly-1", "scan-grid", "flythrough", 100, 900),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation(
                    "cue-1",
                    "assistant-stream",
                    "timeline_cue",
                    120,
                    1800,
                    props={
                        "tone": "green",
                        "label": "window",
                        "cues": [{"at": 0, "label": "START"}, {"at": 1, "label": "END"}],
                    },
                ),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation(
                    "cue-unlabeled",
                    "continuity-graph",
                    "timeline_cue",
                    140,
                    1200,
                    props={"cues": [{"at": 0.5}]},
                ),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation(
                    "route-empty",
                    "continuity-graph",
                    "route_trace",
                    150,
                    2100,
                    props={"points": [{"x": 0.2, "y": 0.7}, {"x": 0.8, "y": 0.3}]},
                ),
            ),
            SceneMutation(
                "start_animation",
                animation=SceneAnimation(
                    "route-1",
                    "continuity-graph",
                    "route_trace",
                    160,
                    2200,
                    props={
                        "tone": "cyan",
                        "label": "route",
                        "points": [
                            {"id": "queue", "label": "QUEUE", "x": 0.1, "y": 0.8},
                            {"id": "render", "label": "RENDER", "x": 0.5, "y": 0.5},
                            {"id": "scene", "label": "SCENE", "x": 0.9, "y": 0.2},
                        ],
                    },
                ),
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
    assert compaction.project["schemas"]["agentAttention"] == "harn-gibson.agent-attention.v1"
    assert compaction.project["schemas"]["rendererContext"] == "harn-gibson.renderer-context.v1"
    assert compaction.project["schemas"]["repoTopology"] == "harn-gibson.repo-topology.v1"
    assert compaction.project["schemas"]["worldBinding"] == "harn-gibson.world-binding.v1"
    assert compaction.project["schemas"]["worldModel"] == "harn-gibson.world-model.v1"
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
    assert empty_entry["visibleFileCount"] == 0
    assert empty_entry["visibleDirCount"] == 0
    assert empty_entry["visibleLineCount"] == 0
    src_entry = next(entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "src")
    assert src_entry["visibleFileCount"] == 0
    assert src_entry["visibleDirCount"] == 1
    assert src_entry["visibleLineCount"] == 0
    assert src_entry["children"] == [
        {
            "path": "src/harn_gibson",
            "name": "harn_gibson",
            "kind": "dir",
            "visibleFileCount": 1,
            "visibleDirCount": 0,
            "visibleLineCount": 1,
            "summaryTruncated": True,
        }
    ]
    link_entry = next(
        entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "README-link.md"
    )
    assert link_entry["kind"] == "symlink"
    readme_entry = next(
        entry for entry in compaction.project["repoTopology"]["entries"] if entry["path"] == "README.md"
    )
    assert readme_entry["lineCount"] == 1
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
    assert compaction.project["worldModel"]["schema"] == "harn-gibson.world-model.v1"
    assert compaction.project["worldModel"]["revision"] == 1
    assert compaction.project["worldModel"]["entityCount"] == 4
    assert compaction.project["worldModel"]["counts"] == {"files": 2, "commands": 1, "changes": 0, "health": 1}
    assert compaction.project["worldModel"]["truncated"] is False
    assert [item["path"] for item in compaction.project["worldModel"]["entities"]["files"]] == [
        "src/harn_gibson/rendering.py",
        "tests/test_rendering.py",
    ]
    assert compaction.project["worldModel"]["entities"]["files"][0]["provenance"]["source"] == "observed"
    assert compaction.project["worldModel"]["entities"]["commands"][0]["commandPreview"] == (
        "uv run pytest tests/test_rendering.py docs/renderer-agent.md"
    )
    assert compaction.project["worldModel"]["entities"]["health"][0]["category"] == "test"
    assert compaction.project["worldModel"]["entities"]["health"][0]["sourceCommandId"] == "command:9"
    assert compaction.project["agentAttention"]["schema"] == "harn-gibson.agent-attention.v1"
    assert compaction.project["agentAttention"]["action"]["kind"] == "verify"
    assert compaction.project["agentAttention"]["objective"] == {
        "text": "Verify current work: uv run pytest tests/test_rendering.py docs/renderer-agent.md",
        "source": "command",
    }
    assert compaction.project["agentAttention"]["focus"]["primaryPath"] == "src/harn_gibson/rendering.py"
    assert compaction.project["agentAttention"]["focus"]["paths"] == [
        "src/harn_gibson/rendering.py",
        "tests/test_rendering.py",
    ]
    assert compaction.project["agentAttention"]["healthFocus"]["category"] == "test"
    assert compaction.project["agentAttention"]["signals"] == ["currentEvent", "touchedFiles", "worldModel"]
    assert compaction.catalog["schema"] == "harn-gibson.visual-catalog.v1"
    assert compaction.scene["schema"] == "harn-gibson.scene.v1"
    assert compaction.recent_agent_context == ("agent saw tool call", "grid was pulsing")
    assert compaction.visual_continuity["schema"] == "harn-gibson.visual-continuity.v1"
    assert compaction.visual_continuity["mode"] == "compaction"
    assert compaction.visual_continuity["sceneRevision"] == scene.state.revision
    assert compaction.visual_continuity["style"] == {"id": "mainframe", "motifs": ["phosphor-grid"]}
    assert compaction.visual_continuity["worldBindingCount"] == 1
    assert compaction.visual_continuity["activeAnimationCount"] == 5
    cue_summary = next(item for item in compaction.visual_continuity["activeAnimations"] if item["id"] == "cue-1")
    assert cue_summary["kind"] == "timeline_cue"
    assert cue_summary["cueCount"] == 2
    assert cue_summary["cueLabels"] == ["START", "END"]
    assert cue_summary["propsPreview"]["label"] == "window"
    unlabeled_cue = next(
        item for item in compaction.visual_continuity["activeAnimations"] if item["id"] == "cue-unlabeled"
    )
    assert unlabeled_cue["cueCount"] == 1
    assert "cueLabels" not in unlabeled_cue
    unlabeled_route = next(
        item for item in compaction.visual_continuity["activeAnimations"] if item["id"] == "route-empty"
    )
    assert unlabeled_route["pointCount"] == 2
    assert "pointIds" not in unlabeled_route
    assert "pointLabels" not in unlabeled_route
    route_summary = next(item for item in compaction.visual_continuity["activeAnimations"] if item["id"] == "route-1")
    assert route_summary["kind"] == "route_trace"
    assert route_summary["pointCount"] == 3
    assert route_summary["pointIds"] == ["queue", "render", "scene"]
    assert route_summary["pointLabels"] == ["QUEUE", "RENDER", "SCENE"]
    graph_anchor = next(item for item in compaction.visual_continuity["anchors"] if item["id"] == "continuity-graph")
    assert graph_anchor["focus"] == "renderer"
    assert graph_anchor["tone"] == "cyan"
    assert graph_anchor["worldBindingCount"] == 1
    assert graph_anchor["worldBindings"][0]["targetId"] == "continuity-graph"
    assert graph_anchor["worldBindings"][0]["relationship"] == "highlights"
    stream_anchor = next(item for item in compaction.visual_continuity["anchors"] if item["id"] == "assistant-stream")
    assert stream_anchor["animated"] is True
    assert stream_anchor["isStreaming"] is True
    assert compaction.to_dict()["visualContinuity"]["activeAnimationCount"] == 5

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
    assert rolling.project["worldModel"]["revision"] == 1
    assert rolling.project["worldModel"]["entityCount"] == 4
    assert rolling.project["worldModel"]["counts"] == {"files": 2, "commands": 1, "changes": 0, "health": 1}
    assert rolling.catalog["mode"] == "summary"
    assert rolling.scene["schema"] == "harn-gibson.scene-summary.v1"
    assert rolling.scene["animationCount"] == 5
    assert rolling.scene["recentLog"] == [{"eventType": "new"}]
    stream_summary = next(item for item in rolling.scene["primitives"] if item["id"] == "assistant-stream")
    assert stream_summary["propsPreview"]["text"] == ["abcde...", {"nested": "zyxwv..."}]
    assert stream_summary["propsPreview"]["isStreaming"] is True
    graph_summary = next(item for item in rolling.scene["primitives"] if item["id"] == "continuity-graph")
    assert graph_summary["worldBindingCount"] == 1
    assert graph_summary["worldBindings"][0]["entityId"] == "file:src/harn_gibson/rendering.py"
    assert rolling.visualization_context[0]["renderer"] == "first"
    assert rolling.visualization_context[0]["intent"] == "visualize tool_call"
    assert rolling.visualization_context[0]["renderIntent"]["renderer"] == "first"
    assert rolling.visualization_context[0]["mutationCount"] == 1
    assert rolling.visual_continuity["mode"] == "rolling"
    assert rolling.visual_continuity["recentEffects"] == ["append_log"]
    assert rolling.visual_continuity["recentTargets"] == []
    assert rolling.visual_continuity["recentRenderers"] == ["first"]

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


def test_renderer_context_repo_topology_counts_lines_without_contents(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "binary.bin").write_bytes(b"a\x00b")
    (repo_root / "empty.txt").write_text("", encoding="utf-8")
    (repo_root / "large.txt").write_bytes(b"x" * 256_001)
    (repo_root / "newline.py").write_text("a\nb\n", encoding="utf-8")
    (repo_root / "plain.md").write_text("one", encoding="utf-8")
    (repo_root / "plain-link.md").symlink_to("plain.md")
    (repo_root / "links").mkdir()
    (repo_root / "links" / "target.txt").write_text("first\nsecond\n", encoding="utf-8")
    (repo_root / "links" / "target-link.txt").symlink_to("target.txt")

    context = RendererContextBuilder(RendererContextConfig(project_root=str(repo_root))).build(
        RenderInputBatch.from_requests((RenderRequest(event(11)),)),
        SceneEngine().state,
        pipeline_catalog(),
    )
    entries = {entry["path"]: entry for entry in context.project["repoTopology"]["entries"]}

    assert entries["empty.txt"]["lineCount"] == 0
    assert entries["newline.py"]["lineCount"] == 2
    assert entries["plain.md"]["lineCount"] == 1
    assert entries["binary.bin"]["lineCount"] is None
    assert entries["large.txt"]["lineCount"] is None
    assert entries["plain-link.md"]["kind"] == "symlink"
    assert "lineCount" not in entries["plain-link.md"]
    assert entries["links"]["visibleFileCount"] == 2
    assert entries["links"]["visibleLineCount"] == 2
    assert _repo_file_line_count(repo_root / "plain-link.md") is None
    assert "one" not in json.dumps(context.project["repoTopology"])


def test_renderer_context_repo_topology_handles_unavailable_root_and_duplicate_touches(tmp_path: Path) -> None:
    event_batch = RenderInputBatch.from_requests(
        (
            RenderRequest(
                GibsonEvent.from_raw(
                    {
                        "type": "tool_result",
                        "toolName": "write",
                        "input": {"command": "write readme"},
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
                        "input": {"command": "write project files"},
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
    assert context.project["worldModel"]["entityCount"] == 6
    assert context.project["worldModel"]["counts"] == {"files": 4, "commands": 2, "changes": 0, "health": 0}
    world_files = {item["path"]: item for item in context.project["worldModel"]["entities"]["files"]}
    world_commands = {item["id"]: item for item in context.project["worldModel"]["entities"]["commands"]}
    assert world_files["src/new_scene.py"]["activityCount"] == 2
    assert world_files["src/new_scene.py"]["lastOutcome"]["eventType"] == "runtime_error"
    assert world_commands["command:1"]["commandPreview"] == "write readme"
    assert world_commands["command:2"]["lastOutcome"]["status"] == "ok"
    assert context.project["worldModel"]["recentOutcomes"][0]["toolName"] == "write"


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
    request = RenderRequest(
        GibsonEvent.from_raw(
            {"type": "tool_result", "toolName": "write", "isError": False, "filePath": "src/direct.py"},
            9,
            timestamp_ms=90,
        ),
        route="direct_scene",
    )

    result = pipeline.apply_direct(
        request,
        (SceneMutation("append_log", entry={"direct": True}),),
        metadata={"route": {"route": "direct_scene"}},
    )

    assert result.mode == "async"
    assert result.scene_revision == 1
    assert result.updates[0]["renderPlan"]["metadata"] == {
        "renderer": "direct",
        "route": {"route": "direct_scene"},
    }
    assert buffer.snapshot() == list(result.updates)
    context = pipeline.context_builder.build(
        RenderInputBatch.from_requests((RenderRequest(event(10, "input")),)),
        pipeline.scene.state,
        pipeline.catalog,
    )
    world_file = context.project["worldModel"]["entities"]["files"][0]
    assert world_file["path"] == "src/direct.py"
    assert world_file["lastOutcome"]["status"] == "ok"

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
    assert coerce_context_limit(None, 7) == 7
    assert coerce_context_limit("12", 7) == 12
    assert coerce_context_limit("-3", 7) == 0
    assert coerce_context_limit("0", 7, minimum=1) == 1
    assert coerce_context_limit("bad", 7) == 7
    assert decisions_from_payload({"decisions": [{"a": 1}, "bad", {"b": 2}]}) == ({"a": 1}, {"b": 2})
