from __future__ import annotations

import pytest

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.scene import (
    SCENE_MUTATION_OPS,
    SceneAnimation,
    SceneEngine,
    SceneMutation,
    ScenePrimitive,
    SceneState,
    apply_style_to_scene,
    default_mutations_for_event,
    initial_scene,
    mutation_from_mapping,
    scene_state_from_mapping,
    scene_update_payload,
)


def test_scene_primitive_animation_mutation_and_state_to_dict() -> None:
    primitive = ScenePrimitive("node", "meter", "side", {"value": 1}, ("child",))
    animation = SceneAnimation("anim", "node", "blink", 10, 20, True, {"color": "green"})
    expiring_animation = SceneAnimation("ttl", "node", "blink", 100, 300, ttl_ms=900)
    mutation = SceneMutation("upsert", primitive=primitive)
    state = initial_scene()

    assert primitive.to_dict() == {
        "id": "node",
        "kind": "meter",
        "region": "side",
        "props": {"value": 1},
        "children": ["child"],
    }
    assert animation.to_dict() == {
        "id": "anim",
        "targetId": "node",
        "kind": "blink",
        "startedAtMs": 10,
        "durationMs": 20,
        "loop": True,
        "props": {"color": "green"},
    }
    assert expiring_animation.expiry_ms == 1000
    assert expiring_animation.is_expired(999) is False
    assert expiring_animation.is_expired(1000) is True
    assert expiring_animation.to_dict() == {
        "id": "ttl",
        "targetId": "node",
        "kind": "blink",
        "startedAtMs": 100,
        "durationMs": 300,
        "loop": False,
        "props": {},
        "ttlMs": 900,
        "expiresAtMs": 1000,
    }
    assert mutation.to_dict()["primitive"] == primitive.to_dict()
    assert state.to_dict()["schema"] == "harn-gibson.scene.v1"
    assert state.to_dict()["metadata"] == {}
    assert "status" in state.primitives


def test_scene_mutation_ops_are_the_public_parser_vocabulary() -> None:
    assert SCENE_MUTATION_OPS == (
        "upsert",
        "patch",
        "remove",
        "append_log",
        "start_animation",
        "stop_animation",
        "reset_scene",
    )
    assert mutation_from_mapping({"op": "append_log", "entry": {"ok": True}}).op in SCENE_MUTATION_OPS


def test_scene_state_from_mapping_round_trips_scene_payload() -> None:
    payload = {
        "revision": 7,
        "primitives": {
            "node": {
                "id": "node",
                "kind": "panel",
                "region": "stage",
                "props": {"text": "ready"},
                "children": ["leaf"],
            },
            "bad": "ignored",
        },
        "animations": {
            "anim": {
                "id": "anim",
                "targetId": "node",
                "kind": "pulse",
                "startedAtMs": 10,
                "durationMs": 20,
                "loop": True,
                "ttlMs": 200,
                "props": {"tone": "cyan"},
            },
            "explicit-expiry": {
                "id": "explicit-expiry",
                "targetId": "node",
                "kind": "pulse",
                "startedAtMs": 30,
                "durationMs": 20,
                "expiresAtMs": 1250,
            },
            "bad": None,
        },
        "log": [{"eventType": "x"}, "ignored"],
        "metadata": {"displayStyle": "mainframe"},
    }

    state = scene_state_from_mapping(payload)
    empty = scene_state_from_mapping({"revision": "", "primitives": [], "animations": [], "log": {}, "metadata": []})

    assert state.revision == 7
    assert state.primitives["node"] == ScenePrimitive("node", "panel", "stage", {"text": "ready"}, ("leaf",))
    assert state.animations["anim"] == SceneAnimation("anim", "node", "pulse", 10, 20, True, {"tone": "cyan"}, 200)
    assert state.animations["anim"].expiry_ms == 210
    assert state.animations["explicit-expiry"].expiry_ms == 1250
    assert state.log == [{"eventType": "x"}]
    assert state.metadata == {"displayStyle": "mainframe"}
    assert empty == SceneState()


def test_scene_engine_records_bounded_render_intents() -> None:
    engine = SceneEngine(max_render_intents=2)

    engine.record_render_intent({"intent": "one", "renderer": "test"})
    engine.record_render_intent({"intent": "two", "renderer": "test"})
    engine.record_render_intent({"intent": "three", "renderer": "test"})

    assert engine.state.revision == 0
    assert engine.state.metadata["lastRenderIntent"]["intent"] == "three"
    assert engine.state.metadata["renderIntents"] == [
        {"intent": "two", "renderer": "test"},
        {"intent": "three", "renderer": "test"},
    ]

    engine.state.metadata["renderIntents"] = "bad"
    engine.record_render_intent({"intent": "fresh"})
    assert engine.state.metadata["renderIntents"] == [{"intent": "fresh"}]

    engine.apply([SceneMutation("reset_scene")])
    assert engine.state.metadata == {}


def test_initial_scene_can_carry_style_pack_and_reset_factory() -> None:
    style_pack = {
        "schema": "harn-gibson.style-pack.v1",
        "id": "mainframe",
        "tones": {"green": [117, 255, 127]},
    }
    scene = initial_scene(style_pack)
    engine = SceneEngine()

    assert scene.primitives["stage"].props["theme"] == "mainframe"
    assert scene.primitives["stage"].props["stylePack"] == style_pack
    assert scene.metadata["displayStyle"] == "mainframe"

    apply_style_to_scene(engine.state, style_pack)
    assert engine.state.metadata["stylePack"] == style_pack
    empty_scene = SceneState()
    apply_style_to_scene(empty_scene, {"id": ""})
    assert empty_scene.metadata["displayStyle"] == "gibson"
    engine.configure_initial_scene(lambda: initial_scene(style_pack), reset=True)
    assert engine.state.primitives["stage"].props["theme"] == "mainframe"
    engine.apply([SceneMutation("patch", target_id="status", props={"text": "changed"})])
    engine.apply([SceneMutation("reset_scene")])
    assert engine.state.primitives["stage"].props["theme"] == "mainframe"
    assert engine.state.primitives["status"].props["text"] == "awaiting signal"


def test_scene_engine_applies_all_mutations_and_trims_log() -> None:
    engine = SceneEngine(max_log_entries=2)
    primitive = ScenePrimitive("node", "panel", "side", {"text": "a"})
    animation = SceneAnimation("anim", "node", "pulse", 1, 100)

    state = engine.apply(
        [
            SceneMutation("upsert", primitive=primitive),
            SceneMutation("patch", target_id="node", props={"text": "b", "level": 2}),
            SceneMutation("append_log", entry={"sequence": 1}),
            SceneMutation("append_log", entry={"sequence": 2}),
            SceneMutation("append_log", entry={"sequence": 3}),
            SceneMutation("start_animation", animation=animation),
        ]
    )

    assert state.revision == 1
    assert state.primitives["node"].props == {"text": "b", "level": 2}
    assert state.log == [{"sequence": 2}, {"sequence": 3}]
    assert state.animations["anim"] == animation
    assert engine.apply([]).revision == 1

    state = engine.apply([SceneMutation("remove", target_id="node")])
    assert state.revision == 2
    assert "node" not in state.primitives
    assert "anim" not in state.animations

    engine.apply([SceneMutation("start_animation", animation=SceneAnimation("anim2", "status", "pulse", 1, 2))])
    engine.apply([SceneMutation("stop_animation", target_id="anim2")])
    assert "anim2" not in engine.state.animations

    engine.apply([SceneMutation("reset_scene")])
    assert engine.state.revision == 1
    assert set(engine.state.primitives) >= {"stage", "status", "event-feed", "trace-log"}


def test_scene_engine_prunes_expired_animations_by_scene_time() -> None:
    engine = SceneEngine()
    expiring = SceneAnimation("ttl", "status", "pulse", 100, 200, ttl_ms=500)
    explicit = SceneAnimation("explicit", "status", "pulse", 100, 200, expires_at_ms=1000)
    persistent = SceneAnimation("persistent", "status", "pulse", 100, 200)

    state = engine.apply(
        [
            SceneMutation("start_animation", animation=expiring),
            SceneMutation("start_animation", animation=explicit),
            SceneMutation("start_animation", animation=persistent),
        ],
        now_ms=599,
    )

    assert state.revision == 1
    assert set(state.animations) == {"ttl", "explicit", "persistent"}
    assert engine.prune_expired_animations(600) == ("ttl",)
    assert engine.state.revision == 2
    assert set(engine.state.animations) == {"explicit", "persistent"}

    state = engine.apply([SceneMutation("patch", target_id="status", props={"text": "fresh"})], now_ms=1000)

    assert state.revision == 3
    assert set(state.animations) == {"persistent"}
    assert engine.apply([], now_ms=5000).revision == 3

    state = engine.apply(
        [
            SceneMutation(
                "start_animation",
                animation=SceneAnimation("already-expired", "status", "pulse", 2000, 100, ttl_ms=1),
            )
        ],
        now_ms=2001,
    )

    assert state.revision == 4
    assert set(state.animations) == {"persistent"}


def test_scene_engine_validation_errors() -> None:
    engine = SceneEngine()

    with pytest.raises(ValueError, match="upsert requires primitive"):
        engine.apply([SceneMutation("upsert")])
    with pytest.raises(ValueError, match="patch requires target_id"):
        engine.apply([SceneMutation("patch")])
    with pytest.raises(ValueError, match="unknown primitive"):
        engine.apply([SceneMutation("patch", target_id="missing")])
    with pytest.raises(ValueError, match="remove requires target_id"):
        engine.apply([SceneMutation("remove")])
    with pytest.raises(ValueError, match="start_animation requires animation"):
        engine.apply([SceneMutation("start_animation")])
    with pytest.raises(ValueError, match="stop_animation requires target_id"):
        engine.apply([SceneMutation("stop_animation")])
    with pytest.raises(ValueError, match="unsupported"):
        engine._apply(SceneMutation("bogus"))  # type: ignore[arg-type]


def test_mutation_from_mapping() -> None:
    mutation = mutation_from_mapping(
        {
            "op": "upsert",
            "primitive": {"id": "x", "kind": "panel", "region": "main", "props": {"a": 1}, "children": ["y"]},
        }
    )
    assert mutation.primitive == ScenePrimitive("x", "panel", "main", {"a": 1}, ("y",))

    animation = mutation_from_mapping(
        {
            "op": "start_animation",
            "animation": {
                "id": "a",
                "target_id": "x",
                "kind": "blink",
                "started_at_ms": 10,
                "duration_ms": 20,
                "loop": True,
                "props": {"b": 2},
            },
        }
    )
    assert animation.animation == SceneAnimation("a", "x", "blink", 10, 20, True, {"b": 2})

    assert mutation_from_mapping({"op": "patch", "target_id": "status", "props": {"x": 1}}).target_id == "status"
    with pytest.raises(ValueError, match="unsupported"):
        mutation_from_mapping({"op": "nope"})


def test_default_mutations_and_scene_update_payload() -> None:
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "content": [{"type": "text", "text": "ok"}]},
        4,
        timestamp_ms=100,
    )
    decisions = [{"block": False, "metadata": {"reviewed": True}}]
    mutations = default_mutations_for_event(event, decisions)
    engine = SceneEngine()
    scene = engine.apply(mutations)
    payload = scene_update_payload(event, mutations, scene)

    assert [mutation.op for mutation in mutations] == [
        "patch",
        "append_log",
        "patch",
        "upsert",
        "upsert",
        "upsert",
        "upsert",
        "upsert",
        "start_animation",
    ]
    assert scene.primitives["status"].props["text"] == "after:tool_result"
    assert scene.primitives["decision-log"].props["text"] == decisions
    assert scene.primitives["trace-log"].props["text"] == []
    assert scene.primitives["gibson-city"].kind == "city_block"
    assert len(scene.primitives["gibson-city"].props["blocks"]) == 7
    assert scene.primitives["gibson-city"].props["focusBlockId"] == "district-4"
    assert scene.primitives["gibson-city"].props["cameraPath"]["keyframes"][1]["scale"] == 1.035
    assert scene.primitives["signal-graph"].kind == "node_graph"
    assert scene.primitives["signal-graph"].props["focusNodeId"] == "event"
    assert scene.primitives["data-ribbon"].kind == "ribbon"
    assert len(scene.primitives["data-ribbon"].props["points"]) == 5
    assert scene.primitives["glyph-layer"].kind == "glyph_layer"
    assert "TOOL_RESULT" in scene.primitives["glyph-layer"].props["text"]
    assert scene.primitives["packet-field"].kind == "particle_field"
    assert scene.primitives["packet-field"].props["count"] == 22
    assert scene.animations["pulse-4"].props["tone"] == "magenta"
    assert payload["event"]["eventType"] == "tool_result"
    assert payload["mutations"][8]["animation"]["targetId"] == "scan-grid"
    assert payload["mutations"][8]["animation"]["ttlMs"] == 2600
    assert payload["mutations"][8]["animation"]["expiresAtMs"] == 2700


def test_default_mutations_capture_tracebacks() -> None:
    event = diagnostic_event(
        5,
        event_type="runtime_error",
        severity="error",
        message="failed",
        details="during delivery",
        traceback_text="Traceback...",
        timestamp_ms=500,
    )
    mutations = default_mutations_for_event(event)
    scene = SceneEngine().apply(mutations)

    assert [mutation.op for mutation in mutations] == [
        "patch",
        "append_log",
        "patch",
        "upsert",
        "upsert",
        "upsert",
        "upsert",
        "upsert",
        "start_animation",
        "patch",
    ]
    trace = scene.primitives["trace-log"].props["text"]
    assert trace == [
        {
            "sequence": 5,
            "eventType": "runtime_error",
            "title": "Runtime error",
            "message": "failed",
            "details": "during delivery",
            "traceback": "Traceback...",
        }
    ]
