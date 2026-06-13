"""Persistent scene state and mutation primitives for harn-gibson."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from harn_gibson.events import GibsonEvent

SceneOp = Literal[
    "upsert",
    "patch",
    "remove",
    "append_log",
    "start_animation",
    "stop_animation",
    "reset_scene",
]

SCENE_MUTATION_OPS: tuple[SceneOp, ...] = (
    "upsert",
    "patch",
    "remove",
    "append_log",
    "start_animation",
    "stop_animation",
    "reset_scene",
)


@dataclass(frozen=True, slots=True)
class ScenePrimitive:
    id: str
    kind: str
    region: str
    props: dict[str, Any] = field(default_factory=dict)
    children: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "region": self.region,
            "props": self.props,
            "children": list(self.children),
        }


@dataclass(frozen=True, slots=True)
class SceneAnimation:
    id: str
    target_id: str
    kind: str
    started_at_ms: int
    duration_ms: int
    loop: bool = False
    props: dict[str, Any] = field(default_factory=dict)
    ttl_ms: int | None = None
    expires_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "targetId": self.target_id,
            "kind": self.kind,
            "startedAtMs": self.started_at_ms,
            "durationMs": self.duration_ms,
            "loop": self.loop,
            "props": self.props,
        }
        if self.ttl_ms is not None:
            payload["ttlMs"] = self.ttl_ms
        expiry_ms = self.expiry_ms
        if expiry_ms is not None:
            payload["expiresAtMs"] = expiry_ms
        return payload

    @property
    def expiry_ms(self) -> int | None:
        if self.expires_at_ms is not None:
            return self.expires_at_ms
        if self.ttl_ms is not None:
            return self.started_at_ms + self.ttl_ms
        return None

    def is_expired(self, now_ms: int) -> bool:
        expiry_ms = self.expiry_ms
        return expiry_ms is not None and now_ms >= expiry_ms


@dataclass(frozen=True, slots=True)
class SceneMutation:
    op: SceneOp
    target_id: str | None = None
    primitive: ScenePrimitive | None = None
    props: dict[str, Any] = field(default_factory=dict)
    animation: SceneAnimation | None = None
    entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op}
        if self.target_id is not None:
            payload["targetId"] = self.target_id
        if self.primitive is not None:
            payload["primitive"] = self.primitive.to_dict()
        if self.props:
            payload["props"] = self.props
        if self.animation is not None:
            payload["animation"] = self.animation.to_dict()
        if self.entry:
            payload["entry"] = self.entry
        return payload


@dataclass(slots=True)
class SceneState:
    revision: int = 0
    primitives: dict[str, ScenePrimitive] = field(default_factory=dict)
    animations: dict[str, SceneAnimation] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.scene.v1",
            "revision": self.revision,
            "primitives": {key: primitive.to_dict() for key, primitive in self.primitives.items()},
            "animations": {key: animation.to_dict() for key, animation in self.animations.items()},
            "log": list(self.log),
            "metadata": self.metadata,
        }


class SceneEngine:
    """Applies scene mutations to persistent scene state."""

    def __init__(
        self,
        state: SceneState | None = None,
        *,
        initial_scene_factory: Callable[[], SceneState] | None = None,
        max_log_entries: int = 120,
        max_render_intents: int = 24,
    ) -> None:
        self._initial_scene_factory = initial_scene if initial_scene_factory is None else initial_scene_factory
        self.state = state or self._initial_scene_factory()
        self.max_log_entries = max_log_entries
        self.max_render_intents = max(1, max_render_intents)

    def configure_initial_scene(self, factory: Callable[[], SceneState], *, reset: bool = False) -> None:
        self._initial_scene_factory = factory
        if reset:
            self.state = self._initial_scene_factory()

    def apply(
        self,
        mutations: Iterable[SceneMutation | Mapping[str, Any]],
        *,
        now_ms: int | None = None,
    ) -> SceneState:
        applied = bool(self._prune_expired_animations(now_ms))
        for mutation in mutations:
            self._apply(mutation_from_mapping(mutation) if isinstance(mutation, Mapping) else mutation)
            applied = True
        if self._prune_expired_animations(now_ms):
            applied = True
        if applied:
            self.state.revision += 1
        return self.state

    def prune_expired_animations(self, now_ms: int | None, *, increment_revision: bool = True) -> tuple[str, ...]:
        expired = self._prune_expired_animations(now_ms)
        if expired and increment_revision:
            self.state.revision += 1
        return expired

    def _apply(self, mutation: SceneMutation) -> None:
        if mutation.op == "reset_scene":
            self.state = self._initial_scene_factory()
            return
        if mutation.op == "upsert":
            primitive = _require(mutation.primitive, "upsert requires primitive")
            self.state.primitives[primitive.id] = primitive
            return
        if mutation.op == "patch":
            target_id = _require(mutation.target_id, "patch requires target_id")
            current = _require(self.state.primitives.get(target_id), f"unknown primitive: {target_id}")
            self.state.primitives[target_id] = ScenePrimitive(
                id=current.id,
                kind=current.kind,
                region=current.region,
                props={**current.props, **mutation.props},
                children=current.children,
            )
            return
        if mutation.op == "remove":
            target_id = _require(mutation.target_id, "remove requires target_id")
            self.state.primitives.pop(target_id, None)
            self.state.animations = {
                key: animation for key, animation in self.state.animations.items() if animation.target_id != target_id
            }
            return
        if mutation.op == "append_log":
            self.state.log.append(dict(mutation.entry))
            if len(self.state.log) > self.max_log_entries:
                del self.state.log[: len(self.state.log) - self.max_log_entries]
            return
        if mutation.op == "start_animation":
            animation = _require(mutation.animation, "start_animation requires animation")
            self.state.animations[animation.id] = animation
            return
        if mutation.op == "stop_animation":
            target_id = _require(mutation.target_id, "stop_animation requires target_id")
            self.state.animations.pop(target_id, None)
            return
        raise ValueError(f"unsupported scene mutation op: {mutation.op}")

    def record_render_intent(self, intent: Mapping[str, Any]) -> None:
        rendered_intent = dict(intent)
        current = self.state.metadata.get("renderIntents", ())
        if isinstance(current, list):
            history = [dict(item) for item in current if isinstance(item, Mapping)]
        else:
            history = []
        history.append(rendered_intent)
        if len(history) > self.max_render_intents:
            del history[: len(history) - self.max_render_intents]
        self.state.metadata = {
            **self.state.metadata,
            "lastRenderIntent": rendered_intent,
            "renderIntents": history,
        }

    def _prune_expired_animations(self, now_ms: int | None) -> tuple[str, ...]:
        if now_ms is None:
            return ()
        expired = tuple(
            animation_id
            for animation_id, animation in self.state.animations.items()
            if animation.is_expired(now_ms)
        )
        for animation_id in expired:
            self.state.animations.pop(animation_id, None)
        return expired


def initial_scene(style_pack: Mapping[str, Any] | None = None) -> SceneState:
    state = SceneState()
    style_id = str(style_pack.get("id")) if isinstance(style_pack, Mapping) and style_pack.get("id") else "gibson"
    for primitive in (
        ScenePrimitive(
            id="stage",
            kind="viewport",
            region="root",
            props={"theme": style_id, "title": "GIBSON LINK"},
            children=("status", "event-feed", "trace-log", "scan-grid"),
        ),
        ScenePrimitive(id="status", kind="status", region="mast", props={"text": "awaiting signal", "phase": "idle"}),
        ScenePrimitive(id="event-feed", kind="feed", region="side", props={"maxItems": 80}),
        ScenePrimitive(id="trace-log", kind="code", region="side", props={"language": "text", "text": []}),
        ScenePrimitive(id="scan-grid", kind="grid", region="stage", props={"intensity": 0.2}),
    ):
        state.primitives[primitive.id] = primitive
    if isinstance(style_pack, Mapping):
        apply_style_to_scene(state, style_pack)
    return state


def apply_style_to_scene(state: SceneState, style_pack: Mapping[str, Any]) -> None:
    style_id = str(style_pack.get("id") or "gibson")
    stage = state.primitives.get("stage")
    if stage is not None:
        state.primitives["stage"] = ScenePrimitive(
            id=stage.id,
            kind=stage.kind,
            region=stage.region,
            props={**stage.props, "theme": style_id, "stylePack": dict(style_pack)},
            children=stage.children,
        )
    state.metadata = {
        **state.metadata,
        "displayStyle": style_id,
        "stylePack": dict(style_pack),
    }


def mutation_from_mapping(value: Mapping[str, Any]) -> SceneMutation:
    op = value.get("op")
    if op not in SCENE_MUTATION_OPS:
        raise ValueError(f"unsupported scene mutation op: {op}")
    return SceneMutation(
        op=op,
        target_id=_optional_str(value.get("targetId", value.get("target_id"))),
        primitive=_primitive_from_mapping(value["primitive"]) if isinstance(value.get("primitive"), Mapping) else None,
        props=dict(value.get("props") or {}),
        animation=_animation_from_mapping(value["animation"]) if isinstance(value.get("animation"), Mapping) else None,
        entry=dict(value.get("entry") or {}),
    )


def scene_state_from_mapping(value: Mapping[str, Any]) -> SceneState:
    primitives_value = value.get("primitives")
    animations_value = value.get("animations")
    log_value = value.get("log")
    metadata_value = value.get("metadata")
    primitives = (
        {
            primitive.id: primitive
            for primitive in (
                _primitive_from_mapping(item) for item in primitives_value.values() if isinstance(item, Mapping)
            )
        }
        if isinstance(primitives_value, Mapping)
        else {}
    )
    animations = (
        {
            animation.id: animation
            for animation in (
                _animation_from_mapping(item) for item in animations_value.values() if isinstance(item, Mapping)
            )
        }
        if isinstance(animations_value, Mapping)
        else {}
    )
    return SceneState(
        revision=int(value.get("revision") or 0),
        primitives=primitives,
        animations=animations,
        log=[dict(item) for item in log_value if isinstance(item, Mapping)] if isinstance(log_value, list) else [],
        metadata=dict(metadata_value) if isinstance(metadata_value, Mapping) else {},
    )


def default_mutations_for_event(event: GibsonEvent) -> list[SceneMutation]:
    tone = _tone_for_phase(event.phase)
    mutations = [
        SceneMutation(
            op="patch",
            target_id="status",
            props={"text": f"{event.phase}:{event.event_type}", "phase": event.phase, "tone": tone},
        ),
        SceneMutation(
            op="append_log",
            entry={
                "sequence": event.sequence,
                "phase": event.phase,
                "eventType": event.event_type,
                "title": event.title,
                "summary": event.summary,
            },
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="gibson-city",
                kind="city_block",
                region="stage",
                props={
                    "blocks": _city_blocks_for_event(event, tone),
                    "heightScale": 1.0,
                    "labels": [event.phase, event.event_type],
                    "focusBlockId": f"district-{event.sequence % 7}",
                    "cameraPath": _city_camera_path_for_event(event),
                },
            ),
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="signal-graph",
                kind="node_graph",
                region="stage",
                props=_node_graph_for_event(event, tone),
            ),
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="data-ribbon",
                kind="ribbon",
                region="stage",
                props={
                    "points": _ribbon_points_for_event(event),
                    "width": 3,
                    "material": tone,
                    "direction": "forward",
                    "labels": [event.event_type],
                },
            ),
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="glyph-layer",
                kind="glyph_layer",
                region="stage",
                props={
                    "text": _glyph_text_for_event(event),
                    "density": 0.72,
                    "motion": event.phase,
                    "palette": tone,
                    "seed": event.sequence,
                },
            ),
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="packet-field",
                kind="particle_field",
                region="stage",
                props={
                    "count": 18 + (event.sequence % 11),
                    "velocity": 0.24 + (event.sequence % 5) * 0.04,
                    "emitter": {"x": 0.18, "y": 0.82},
                    "color": tone,
                    "blend": "screen",
                    "seed": event.sequence,
                },
            ),
        ),
        SceneMutation(
            op="start_animation",
            animation=SceneAnimation(
                id=f"pulse-{event.sequence}",
                target_id="scan-grid",
                kind="phase-pulse",
                started_at_ms=event.timestamp_ms,
                duration_ms=1600,
                props={"phase": event.phase, "tone": tone, "sequence": event.sequence},
                ttl_ms=2600,
            ),
        ),
    ]
    trace_entry = _trace_entry_for_event(event)
    if trace_entry is not None:
        mutations.append(
            SceneMutation(
                op="patch",
                target_id="trace-log",
                props={"text": [trace_entry]},
            )
        )
    return mutations


def scene_update_payload(
    event: GibsonEvent,
    mutations: Iterable[SceneMutation],
    scene: SceneState,
) -> dict[str, Any]:
    return {
        "schema": "harn-gibson.scene-update.v1",
        "event": event.to_dict(),
        "mutations": [mutation.to_dict() for mutation in mutations],
        "scene": scene.to_dict(),
    }


def _primitive_from_mapping(value: Mapping[str, Any]) -> ScenePrimitive:
    return ScenePrimitive(
        id=str(value["id"]),
        kind=str(value["kind"]),
        region=str(value["region"]),
        props=dict(value.get("props") or {}),
        children=tuple(str(child) for child in value.get("children", ())),
    )


def _animation_from_mapping(value: Mapping[str, Any]) -> SceneAnimation:
    started_at_ms = int(value.get("startedAtMs", value.get("started_at_ms", 0)))
    return SceneAnimation(
        id=str(value["id"]),
        target_id=str(value.get("targetId", value.get("target_id"))),
        kind=str(value["kind"]),
        started_at_ms=started_at_ms,
        duration_ms=int(value.get("durationMs", value.get("duration_ms", 0))),
        loop=bool(value.get("loop", False)),
        props=dict(value.get("props") or {}),
        ttl_ms=_optional_int(value.get("ttlMs", value.get("ttl_ms", value.get("removeAfterMs")))),
        expires_at_ms=_optional_int(value.get("expiresAtMs", value.get("expires_at_ms"))),
    )


def _tone_for_phase(phase: str) -> str:
    return {
        "before": "green",
        "during": "cyan",
        "after": "magenta",
        "lifecycle": "amber",
    }.get(phase, "green")


def _city_blocks_for_event(event: GibsonEvent, tone: str) -> list[dict[str, Any]]:
    focus = event.sequence % 7
    return [
        {
            "id": f"district-{index}",
            "x": round(0.08 + index * 0.11, 3),
            "y": round(0.18 + ((index * 3 + event.sequence) % 5) * 0.11, 3),
            "w": 0.07,
            "d": 0.08,
            "h": round(0.14 + ((event.sequence + index * 2) % 6) * 0.055, 3),
            "tone": tone if index == focus else "cyan",
            "label": event.event_type if index == focus else f"{index:02x}",
        }
        for index in range(7)
    ]


def _city_camera_path_for_event(event: GibsonEvent) -> dict[str, Any]:
    direction = -1 if event.sequence % 2 else 1
    phase = (event.sequence % 5) * 0.004
    return {
        "durationMs": 6200,
        "loop": True,
        "yoyo": True,
        "keyframes": [
            {"at": 0, "x": round(-0.012 * direction, 3), "y": round(0.010 + phase, 3), "scale": 0.985},
            {
                "at": 0.52,
                "x": round(0.020 * direction, 3),
                "y": round(-0.014 - phase, 3),
                "scale": 1.035,
                "rotation": round(0.012 * direction, 3),
            },
            {"at": 1, "x": round(0.004 * direction, 3), "y": 0.006, "scale": 1.0},
        ],
    }


def _node_graph_for_event(event: GibsonEvent, tone: str) -> dict[str, Any]:
    return {
        "focusNodeId": "event",
        "layout": "triad",
        "nodes": [
            {"id": "harn", "label": "harn", "x": 0.18, "y": 0.74, "tone": "green"},
            {"id": "event", "label": event.event_type, "x": 0.50, "y": 0.42, "tone": tone},
            {"id": "scene", "label": "scene", "x": 0.82, "y": 0.70, "tone": "cyan"},
        ],
        "edges": [
            {"source": "harn", "target": "event", "label": event.phase},
            {"source": "event", "target": "scene", "label": "mutate"},
        ],
    }


def _ribbon_points_for_event(event: GibsonEvent) -> list[dict[str, float]]:
    wobble = (event.sequence % 5) * 0.025
    return [
        {"x": 0.10, "y": round(0.82 - wobble, 3)},
        {"x": 0.32, "y": round(0.62 + wobble, 3)},
        {"x": 0.56, "y": round(0.50 - wobble, 3)},
        {"x": 0.78, "y": round(0.28 + wobble, 3)},
        {"x": 0.92, "y": round(0.20 - wobble, 3)},
    ]


def _glyph_text_for_event(event: GibsonEvent) -> str:
    summary = event.summary.replace(" ", "_")[:36]
    return f"{event.phase.upper()}::{event.event_type.upper()}::{summary}::{event.sequence:04x}"


def _trace_entry_for_event(event: GibsonEvent) -> dict[str, Any] | None:
    traceback_text = event.payload.get("traceback")
    details = event.payload.get("details")
    if not traceback_text and not details:
        return None
    return {
        "sequence": event.sequence,
        "eventType": event.event_type,
        "title": event.title,
        "message": event.payload.get("message") or event.summary,
        "details": details,
        "traceback": traceback_text,
    }


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _require[T](value: T | None, message: str) -> T:
    if value is None:
        raise ValueError(message)
    return value
