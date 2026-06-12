"""Tests for the projection engine (harn-gibson.projection.v1)."""

from __future__ import annotations

import json
from pathlib import Path

from harn_gibson.catalog import default_visual_catalog
from harn_gibson.events import GibsonEvent
from harn_gibson.projection import (
    DEFAULT_PROJECTION,
    PROJECTION_SCENE_ID,
    PROJECTION_SCENE_SCHEMA,
    ProjectionEngine,
    ProjectionSceneRenderer,
    load_projection_spec,
)
from harn_gibson.rendering import RenderRequest, validate_render_plan
from harn_gibson.scene import SceneEngine


def _perception(
    *,
    entities: list[dict] | None = None,
    relations: list[dict] | None = None,
    events: list[dict] | None = None,
    latest_seq: int = 9,
) -> dict:
    return {
        "schema": "harn-gibson.perception-model.v1",
        "latestSequence": latest_seq,
        "latestSeenMs": latest_seq * 1000,
        "workspace": {
            "rootName": "repo",
            "git": {"available": True, "branch": "main", "headSha": "abc123def456",
                    "dirtyPathCount": 1, "untrackedPathCount": 0},
            "fileCount": 3,
            "basis": "git",
        },
        "entities": entities if entities is not None else _default_entities(),
        "relations": relations if relations is not None else _default_relations(),
        "events": events or [],
    }


def _default_entities() -> list[dict]:
    return [
        {"id": "agent", "type": "agent", "attrs": {}},
        {"id": "dir:.", "type": "dir", "attrs": {"fileCount": 3, "root": True}},
        {"id": "dir:src", "type": "dir", "attrs": {"fileCount": 2}},
        {"id": "file:src/app.py", "type": "file",
         "attrs": {"touchCount": 4, "lastTouchedSeq": 9, "dirty": True}},
        {"id": "file:src/util.py", "type": "file", "attrs": {"touchCount": 0}},
        {"id": "file:README.md", "type": "file", "attrs": {"touchCount": 1, "exists": False}},
        {"id": "command:5", "type": "command",
         "attrs": {"preview": "python -m pytest", "status": "error", "startSeq": 5, "endSeq": 6}},
        {"id": "check:test:6", "type": "check", "attrs": {"category": "test", "status": "error", "seq": 6}},
    ]


def _default_relations() -> list[dict]:
    return [
        {"type": "contains", "from": "dir:.", "to": "dir:src"},
        {"type": "contains", "from": "dir:src", "to": "file:src/app.py"},
        {"type": "contains", "from": "dir:src", "to": "file:src/util.py"},
        {"type": "contains", "from": "dir:.", "to": "file:README.md"},
        {"type": "touched", "from": "command:5", "to": "file:src/app.py", "lastSeq": 9},
        {"type": "touched", "from": "command:5", "to": "file:README.md", "lastSeq": 5},
        {"type": "produced", "from": "command:5", "to": "check:test:6"},
        {"type": "focused_on", "from": "agent", "to": "file:src/app.py"},
    ]


def test_default_projection_resolves_a_complete_scene() -> None:
    engine = ProjectionEngine()
    scene = engine.resolve(_perception(), project_name="repo", now_ms=9000)

    assert scene["schema"] == PROJECTION_SCENE_SCHEMA
    assert scene["theme"] == "gibson"
    assert scene["title"] == "repo"
    assert scene["mood"]["name"] == "alert"

    nodes = {node["id"]: node for node in scene["nodes"]}
    # files, dirs, and the agent cursor are all placed
    assert {"dir:.", "dir:src", "file:src/app.py", "file:src/util.py", "file:README.md", "agent"} <= set(nodes)
    root = nodes["dir:."]
    assert (root["x"], root["y"]) == (0.5, 0.5)
    # encodings: touched file is bigger and brighter than the dormant one
    assert nodes["file:src/app.py"]["size"] > nodes["file:src/util.py"]["size"]
    assert nodes["file:src/util.py"]["opacity"] == 0.35
    assert nodes["file:src/util.py"]["tone"] == "ghost"
    # blast membership under alert turns the implicated file alarm-toned
    assert nodes["file:src/app.py"]["tone"] == "alarm"
    # missing files are ghosted regardless of other facts
    assert nodes["file:README.md"]["tone"] == "ghost"
    assert nodes["file:README.md"]["opacity"] <= 0.4
    assert nodes["agent"]["kind"] == "agent"
    assert nodes["file:src/app.py"]["focus"] is True

    edge_styles = {(edge["from"], edge["to"], edge["style"]) for edge in scene["edges"]}
    assert ("dir:.", "dir:src", "skeleton") in edge_styles
    # flow edges re-anchor causality on the agent cursor
    assert ("agent", "file:src/app.py", "flow") in edge_styles
    # the stale touched relation is excluded by the recent filter
    assert not any(edge["to"] == "file:README.md" and edge["style"] == "flow" for edge in scene["edges"])
    assert ("agent", "file:src/app.py", "beam") in edge_styles

    assert scene["camera"]["target"] == "file:src/app.py"
    assert scene["hud"]["focus"] == "src/app.py"
    assert "TEST:ERROR" in scene["hud"]["checks"]
    assert "main @ abc123d" in scene["hud"]["workspace"]


def test_event_rules_fire_effects_once_with_blast_and_recovery() -> None:
    engine = ProjectionEngine()
    error_events = [
        {"seq": 6, "ts": 6000, "kind": "command_completed", "entity": "command:5", "status": "error"},
        {"seq": 6, "ts": 6000, "kind": "check_completed", "entity": "check:test:6",
         "category": "test", "status": "error"},
        {"seq": 9, "ts": 9000, "kind": "file_changed", "entity": "file:src/app.py", "churnFraction": 0.4},
    ]
    scene = engine.resolve(_perception(events=error_events), now_ms=9000)
    by_kind = {effect["kind"]: effect for effect in scene["effects"]}
    assert set(by_kind) == {"alarm", "breach", "shake", "pulse"}
    assert by_kind["breach"]["targets"] == ["file:README.md", "file:src/app.py"]
    assert by_kind["pulse"]["magnitude"] == 0.4
    assert by_kind["alarm"]["targets"] == []

    # the same events do not re-fire on the next resolve; expired effects prune
    scene = engine.resolve(_perception(events=error_events), now_ms=30000)
    assert scene["effects"] == []

    # recovery: an ok check for a category that previously erred fires the ring
    recovered_entities = [
        entity for entity in _default_entities()
        if entity["id"] != "check:test:6"
    ] + [{"id": "check:test:9", "type": "check", "attrs": {"category": "test", "status": "ok", "seq": 9}}]
    recovery_events = error_events + [
        {"seq": 9, "ts": 9000, "kind": "check_completed", "entity": "check:test:9",
         "category": "test", "status": "ok"},
    ]
    scene = engine.resolve(
        _perception(entities=recovered_entities, events=recovery_events), now_ms=31000
    )
    rings = [effect for effect in scene["effects"] if effect["kind"] == "ring"]
    assert len(rings) == 1
    assert rings[0]["label"] == "LOCK RELEASED"
    assert rings[0]["tone"] == "good"
    assert scene["mood"]["name"] == "recovery"


def test_effect_target_selectors_and_field_substitution() -> None:
    spec = {
        "on": [
            {"event": "commit_created",
             "effects": [{"kind": "ring", "target": "$root", "label": "$subject", "ttlMs": 9000},
                         {"kind": "pulse", "target": "$focus"},
                         {"kind": "pulse", "target": "file:src/app.py", "magnitude": 0.5},
                         {"kind": "pulse", "target": "file:nonexistent.py"},
                         {"kind": "banner", "label": "MILESTONE"}]},
        ],
    }
    engine = ProjectionEngine(spec)
    events = [{"seq": 9, "ts": 9000, "kind": "commit_created", "entity": "commit:abc", "subject": "ship it"}]
    scene = engine.resolve(_perception(events=events), now_ms=9000)
    effects = scene["effects"]
    ring = next(effect for effect in effects if effect["kind"] == "ring")
    assert ring["targets"] == ["dir:."]
    assert ring["label"] == "ship it"
    assert ring["ttlMs"] == 9000
    pulses = [effect for effect in effects if effect["kind"] == "pulse"]
    assert pulses[0]["targets"] == ["file:src/app.py"]  # $focus
    assert pulses[1]["targets"] == ["file:src/app.py"]  # literal id
    assert pulses[1]["magnitude"] == 0.5
    assert pulses[2]["targets"] == ["dir:."]  # unknown literal falls back to root
    banner = next(effect for effect in effects if effect["kind"] == "banner")
    assert banner["targets"] == []
    assert banner["label"] == "MILESTONE"


def test_rule_when_conditions_must_all_match() -> None:
    spec = {"on": [{"event": "check_completed", "when": {"status": "error", "category": "build"},
                    "effects": [{"kind": "alarm"}]}]}
    engine = ProjectionEngine(spec)
    events = [{"seq": 6, "ts": 6000, "kind": "check_completed", "entity": "check:test:6",
               "category": "test", "status": "error"}]
    scene = engine.resolve(_perception(events=events), now_ms=6000)
    assert scene["effects"] == []


def test_malformed_spec_entries_are_skipped() -> None:
    spec = {
        "layers": ["not-a-layer", {"id": "world", "select": {"types": ["file"]},
                                   "edges": ["bad", {"relation": "touched", "style": "flow"}]}],
        "on": ["bad-rule", {"event": "file_changed", "effects": ["bad-effect", {"kind": "pulse"}]}],
    }
    engine = ProjectionEngine(spec)
    events = [{"seq": 9, "ts": 9000, "kind": "file_changed", "entity": "file:src/app.py"}]
    scene = engine.resolve(_perception(events=events), now_ms=9000)
    assert any(node["id"] == "file:src/app.py" for node in scene["nodes"])
    assert [effect["kind"] for effect in scene["effects"]] == ["pulse"]


def test_grid_ring_and_force_layouts() -> None:
    grid_engine = ProjectionEngine({"layers": [
        {"id": "g", "select": {"types": ["file"]}, "layout": {"kind": "grid", "sort": "touchCount"}},
    ]})
    scene = grid_engine.resolve(_perception(), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    # most-touched file sorts first (top-left)
    app = nodes["file:src/app.py"]
    assert (app["y"], app["x"]) <= min((n["y"], n["x"]) for n in nodes.values())

    ring_engine = ProjectionEngine({"layers": [
        {"id": "r", "select": {"types": ["file"]}, "layout": {"kind": "ring"}},
    ]})
    ring_scene = ring_engine.resolve(_perception(), now_ms=1000)
    assert len(ring_scene["nodes"]) == 3

    force_engine = ProjectionEngine({"layers": [
        {"id": "f", "select": {"types": ["file", "command"]},
         "layout": {"kind": "force", "relations": ["touched"]}},
    ]})
    first = force_engine.resolve(_perception(), now_ms=1000)
    positions_a = {node["id"]: (node["x"], node["y"]) for node in first["nodes"]}
    # determinism: a fresh engine reproduces the same equilibrium
    fresh = ProjectionEngine({"layers": [
        {"id": "f", "select": {"types": ["file", "command"]},
         "layout": {"kind": "force", "relations": ["touched"]}},
    ]})
    positions_b = {node["id"]: (node["x"], node["y"]) for node in fresh.resolve(_perception(), now_ms=1000)["nodes"]}
    assert positions_a == positions_b
    # warm start: a second resolve stays near the settled equilibrium
    second = force_engine.resolve(_perception(), now_ms=2000)
    for node in second["nodes"]:
        ax, ay = positions_a[node["id"]]
        assert abs(node["x"] - ax) < 0.08
        assert abs(node["y"] - ay) < 0.08
    # springs pull related nodes together: command near its touched file
    cmd_x, cmd_y = positions_a["command:5"]
    app_x, app_y = positions_a["file:src/app.py"]
    util_x, util_y = positions_a["file:src/util.py"]
    spring_dist = ((cmd_x - app_x) ** 2 + (cmd_y - app_y) ** 2) ** 0.5
    loose_dist = ((cmd_x - util_x) ** 2 + (cmd_y - util_y) ** 2) ** 0.5
    assert spring_dist < loose_dist

    # force layout with no relation filter uses every relation type
    open_engine = ProjectionEngine({"layers": [
        {"id": "f", "select": {"types": ["file"]}, "layout": {"kind": "force"}},
    ]})
    assert open_engine.resolve(_perception(), now_ms=1000)["nodes"]


def test_radial_tree_handles_missing_root_and_interior_nodes() -> None:
    # no explicit root and no dir entities: root inferred from parent links
    entities = [
        {"id": "file:a.py", "type": "file", "attrs": {"touchCount": 1}},
        {"id": "file:b.py", "type": "file", "attrs": {"touchCount": 1}},
    ]
    relations = [{"type": "contains", "from": "file:a.py", "to": "file:b.py"}]
    engine = ProjectionEngine({"layers": [
        {"id": "w", "select": {"types": ["file"]}, "layout": {"kind": "radial-tree", "relation": "contains"}},
    ]})
    scene = engine.resolve(_perception(entities=entities, relations=relations), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    assert (nodes["file:a.py"]["x"], nodes["file:a.py"]["y"]) == (0.5, 0.5)

    # a parent chain that leaves the selection collapses to the nearest selected hub
    deep_entities = [
        {"id": "dir:.", "type": "dir", "attrs": {"root": True}},
        {"id": "file:src/deep/x.py", "type": "file", "attrs": {"touchCount": 1}},
    ]
    deep_relations = [
        {"type": "contains", "from": "dir:src", "to": "dir:src/deep"},
        {"type": "contains", "from": "dir:src/deep", "to": "file:src/deep/x.py"},
    ]
    deep_engine = ProjectionEngine({"layers": [
        {"id": "w", "select": {"types": ["dir", "file"]},
         "layout": {"kind": "radial-tree", "relation": "contains", "root": "dir:."}},
    ]})
    deep_scene = deep_engine.resolve(
        _perception(entities=deep_entities, relations=deep_relations), now_ms=1000
    )
    assert {node["id"] for node in deep_scene["nodes"]} >= {"dir:.", "file:src/deep/x.py"}

    # empty selection yields no nodes and no crash
    empty_engine = ProjectionEngine({"layers": [
        {"id": "w", "select": {"types": ["nothing"]}, "layout": {"kind": "radial-tree"}},
    ]})
    assert empty_engine.resolve(_perception(), now_ms=1000)["nodes"] == []


def test_pinned_layers_track_focus_or_named_anchor() -> None:
    engine = ProjectionEngine()  # default cursor layer pins near $focus
    scene = engine.resolve(_perception(), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    focus = nodes["file:src/app.py"]
    agent = nodes["agent"]
    assert abs(agent["x"] - (focus["x"] + (0.5 - focus["x"]) * 0.3)) < 1e-3

    named = ProjectionEngine({"layers": [
        {"id": "world", "select": {"types": ["dir", "file"]},
         "layout": {"kind": "radial-tree", "relation": "contains", "root": "dir:."}},
        {"id": "cursor", "select": {"ids": ["agent"]}, "place": {"near": "dir:."}},
    ]})
    nodes = {node["id"]: node for node in named.resolve(_perception(), now_ms=1000)["nodes"]}
    assert abs(nodes["agent"]["x"] - 0.5) < 1e-3

    # no focus anywhere: the pinned layer parks at the default perch
    no_focus_relations = [r for r in _default_relations() if r["type"] != "focused_on"]
    parked = ProjectionEngine()
    nodes = {
        node["id"]: node
        for node in parked.resolve(_perception(relations=no_focus_relations), now_ms=1000)["nodes"]
    }
    assert (nodes["agent"]["x"], nodes["agent"]["y"]) == (0.5, 0.3)


def test_encodings_normalize_against_session_maximum() -> None:
    engine = ProjectionEngine()
    scene = engine.resolve(_perception(), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    assert nodes["file:src/app.py"]["size"] == 1.0  # touchCount 4 == session max

    # when the max grows, earlier values renormalize against the larger max
    grown = [dict(entity) for entity in _default_entities()]
    for entity in grown:
        if entity["id"] == "file:src/util.py":
            entity["attrs"] = {"touchCount": 8}
    scene = engine.resolve(_perception(entities=grown), now_ms=2000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    assert nodes["file:src/util.py"]["size"] == 1.0
    assert nodes["file:src/app.py"]["size"] < 1.0


def test_tone_and_label_encode_rules_override_defaults() -> None:
    spec = {"layers": [
        {"id": "w", "select": {"types": ["check"]}, "layout": {"kind": "grid"},
         "encode": {"tone": {"attr": "status", "map": {"error": "alarm"}, "default": "good"},
                    "label": {"attr": "category"}}},
    ]}
    engine = ProjectionEngine(spec)
    scene = engine.resolve(_perception(), now_ms=1000)
    node = scene["nodes"][0]
    assert node["tone"] == "alarm"
    assert node["label"] == "TEST"

    # unmapped values fall to the rule default; invalid tones fall through to smart defaults
    ok_entities = [{"id": "check:build:2", "type": "check",
                    "attrs": {"category": "build", "status": "ok", "seq": 2}}]
    scene = engine.resolve(_perception(entities=ok_entities), now_ms=2000)
    assert scene["nodes"][0]["tone"] == "good"

    bad_spec = {"layers": [
        {"id": "w", "select": {"types": ["file"]}, "layout": {"kind": "grid"},
         "encode": {"tone": {"attr": "status", "map": {}, "default": "chartreuse"}}},
    ]}
    scene = ProjectionEngine(bad_spec).resolve(_perception(), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    assert nodes["file:src/util.py"]["tone"] == "ghost"


def test_mood_progression_idle_work_verify() -> None:
    engine = ProjectionEngine()
    assert engine.resolve(_perception(entities=[], relations=[]), now_ms=1)["mood"]["name"] == "idle"
    working = [{"id": "command:1", "type": "command",
                "attrs": {"preview": "ls", "status": "ok", "startSeq": 1}}]
    assert engine.resolve(_perception(entities=working), now_ms=2)["mood"]["name"] == "work"
    running = [{"id": "command:2", "type": "command",
                "attrs": {"preview": "pytest", "status": "running", "startSeq": 2}}]
    assert engine.resolve(_perception(entities=running), now_ms=3)["mood"]["name"] == "verify"


def test_camera_pin_and_fallback() -> None:
    pinned = ProjectionEngine({"camera": {"target": "dir:."}})
    assert pinned.resolve(_perception(), now_ms=1)["camera"] == {"target": "dir:.", "zoom": 1.0}

    no_focus = [r for r in _default_relations() if r["type"] != "focused_on"]
    fallback = ProjectionEngine()
    camera = fallback.resolve(_perception(relations=no_focus), now_ms=1)["camera"]
    assert camera == {"target": "dir:.", "zoom": 1.0}


def test_renderer_adapter_produces_valid_plans() -> None:
    renderer = ProjectionSceneRenderer()
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "ls"}},
        sequence=3,
        timestamp_ms=300,
    )
    request = RenderRequest(event)

    class _Context:
        project = {"name": "repo", "perceptionModel": _perception()}

    plan = renderer.render_with_context((request,), SceneEngine().state, _Context())
    assert plan.metadata["renderer"] == "projection-engine"
    assert plan.metadata["nodes"] > 0
    mutations = plan.steps[0].mutations
    upserts = [m for m in mutations if m.op == "upsert"]
    assert upserts[0].primitive.id == PROJECTION_SCENE_ID
    assert upserts[0].primitive.kind == "projection_scene"
    issues = validate_render_plan(plan, SceneEngine().state, default_visual_catalog())
    assert issues == ()

    # context-free render still produces a coherent empty-world plan
    bare = renderer.render((request,), SceneEngine().state)
    assert bare.metadata["nodes"] == 0
    empty = renderer.render((), SceneEngine().state)
    assert empty.steps[0].mutations[0].props["phase"] == "lifecycle"


def test_load_projection_spec_flag_and_path(tmp_path: Path) -> None:
    assert load_projection_spec("1") == {}
    assert load_projection_spec("default") == {}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"theme": "blueprint"}), encoding="utf-8")
    assert load_projection_spec(str(spec_path)) == {"theme": "blueprint"}


def test_spec_merge_keeps_defaults_for_missing_fields() -> None:
    engine = ProjectionEngine({"theme": "blueprint"})
    assert engine.spec["theme"] == "blueprint"
    assert engine.spec["layers"] == DEFAULT_PROJECTION["layers"]
    assert ProjectionEngine(None).spec["theme"] == "gibson"


def test_server_env_enables_projection_renderer() -> None:
    from harn_gibson.server import projection_renderer_from_env

    assert projection_renderer_from_env(None) is None
    assert projection_renderer_from_env("  ") is None
    renderer = projection_renderer_from_env("1")
    assert isinstance(renderer, ProjectionSceneRenderer)


def test_cli_projection_flag_maps_to_env() -> None:
    import argparse

    from harn_gibson.cli import _explicit_replay_renderer_env_from_args

    args = argparse.Namespace(
        renderer_command=None, renderer_model_command=None,
        renderer_timeout_ms=None, renderer_model_timeout_ms=None,
        projection="examples/projections/gibson-sector.json",
    )
    env = _explicit_replay_renderer_env_from_args(args)
    assert env == {"HARN_GIBSON_PROJECTION": "examples/projections/gibson-sector.json"}


def test_blast_and_focus_fall_back_gracefully() -> None:
    # error check with no produced relation: empty blast, no crash
    relations = [r for r in _default_relations() if r["type"] != "produced"]
    engine = ProjectionEngine()
    events = [{"seq": 6, "ts": 6000, "kind": "check_completed", "entity": "check:test:6",
               "category": "test", "status": "error"}]
    scene = engine.resolve(_perception(relations=relations, events=events), now_ms=6000)
    breach = next(effect for effect in scene["effects"] if effect["kind"] == "breach")
    assert breach["targets"] == ["dir:."]  # fell back to root rather than floating free


def test_recovers_rule_does_not_fire_without_prior_error() -> None:
    engine = ProjectionEngine()
    ok_entities = [{"id": "check:test:3", "type": "check",
                    "attrs": {"category": "test", "status": "ok", "seq": 3}}]
    events = [{"seq": 3, "ts": 3000, "kind": "check_completed", "entity": "check:test:3",
               "category": "test", "status": "ok"}]
    scene = engine.resolve(_perception(entities=ok_entities, events=events), now_ms=3000)
    assert not any(effect["kind"] == "ring" for effect in scene["effects"])


def test_radial_tree_places_childless_interior_directories() -> None:
    engine = ProjectionEngine({"layers": [
        {"id": "w", "select": {"types": ["dir"]},
         "layout": {"kind": "radial-tree", "relation": "contains", "root": "dir:."}},
    ]})
    scene = engine.resolve(_perception(), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    # dir:src parents files that are not selected: it still gets a hub-ring spot
    assert "dir:src" in nodes
    assert (nodes["dir:src"]["x"], nodes["dir:src"]["y"]) != (0.5, 0.5)


def test_label_rule_with_non_string_attr_falls_back_and_untyped_ids_keep_their_name() -> None:
    engine = ProjectionEngine({"layers": [
        {"id": "w", "select": {"types": ["file"], "ids": ["beacon"]}, "layout": {"kind": "grid"},
         "encode": {"label": {"attr": "touchCount"}}},
    ]})
    entities = [
        {"id": "file:src/app.py", "type": "file", "attrs": {"touchCount": 2}},
        {"id": "beacon", "attrs": {}},
    ]
    scene = engine.resolve(_perception(entities=entities, relations=[]), now_ms=1000)
    nodes = {node["id"]: node for node in scene["nodes"]}
    assert nodes["file:src/app.py"]["label"] == "APP.PY"
    assert nodes["beacon"]["kind"] == "beacon"


def test_int_coercion_rejects_bools_and_accepts_integral_floats() -> None:
    from harn_gibson.projection import _int

    assert _int(True, 7) == 7
    assert _int(9.0, 7) == 9
    assert _int(9.5, 7) == 7
    assert _int("9", 7) == 7


def test_engine_redirect_swaps_spec_but_keeps_history() -> None:
    engine = ProjectionEngine()
    error_events = [{"seq": 6, "ts": 6000, "kind": "check_completed", "entity": "check:test:6",
                     "category": "test", "status": "error"}]
    first = engine.resolve(_perception(events=error_events), now_ms=6000)
    assert any(effect["kind"] == "breach" for effect in first["effects"])

    engine.redirect({"theme": "blueprint", "layers": [
        {"id": "w", "select": {"types": ["file"]}, "layout": {"kind": "grid"}},
    ]})
    second = engine.resolve(_perception(events=error_events), now_ms=6500)
    assert second["theme"] == "blueprint"
    # seen-event history survives the redirect: the same events do not re-fire
    assert not any(effect["kind"] == "breach" and effect["startedAtMs"] > 6000 for effect in second["effects"])
    # default rules survive too (redirect merges over defaults)
    assert second["nodes"]


def test_renderer_redirect_is_applied_at_the_next_plan() -> None:
    renderer = ProjectionSceneRenderer()
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "ls"}},
        sequence=1,
        timestamp_ms=100,
    )
    request = RenderRequest(event)

    class _Context:
        project = {"name": "repo", "perceptionModel": _perception()}

    plan = renderer.render_with_context((request,), SceneEngine().state, _Context())
    assert plan.metadata["theme"] == "gibson"
    renderer.redirect({"theme": "blueprint"})
    plan = renderer.render_with_context((request,), SceneEngine().state, _Context())
    assert plan.metadata["theme"] == "blueprint"


def test_projection_http_endpoints_redirect_live_sessions() -> None:
    import threading
    import urllib.request

    from harn_gibson.server import build_state_from_env, create_server

    state = build_state_from_env({"HARN_GIBSON_PROJECTION": "1"})
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"

    def _request(path: str, data: bytes | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{base}{path}", data=data, method="POST" if data is not None else "GET"
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    try:
        status, payload = _request("/projection")
        assert status == 200
        assert payload["active"] is True
        assert payload["spec"]["theme"] == "gibson"

        status, payload = _request("/projection", json.dumps({"theme": "blueprint"}).encode("utf-8"))
        assert status == 202
        assert payload["spec"]["theme"] == "blueprint"
        # the nudge event re-rendered the scene under the new projection
        assert payload["engineRevision"] >= 1

        status, payload = _request("/projection", b"not-json")
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        state.pipeline.stop()


def test_projection_post_conflicts_without_projection_renderer() -> None:
    import threading
    import urllib.error
    import urllib.request

    from harn_gibson.server import GibsonServerState, create_server, projection_status_payload

    state = GibsonServerState()
    assert projection_status_payload(state) == {"active": False, "schema": "harn-gibson.projection.v1"}
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        request = urllib.request.Request(
            f"http://{host}:{port}/projection", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=2)  # noqa: S310
            raise AssertionError("expected HTTP 409")
        except urllib.error.HTTPError as error:
            assert error.code == 409
    finally:
        server.shutdown()
        server.server_close()
        state.pipeline.stop()


def test_example_projection_specs_parse_and_resolve() -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "projections"
    for spec_path in sorted(root.glob("*.json")):
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        engine = ProjectionEngine(spec)
        scene = engine.resolve(_perception(), now_ms=1000)
        assert scene["schema"] == PROJECTION_SCENE_SCHEMA, spec_path.name
        assert scene["nodes"], spec_path.name
