"""Render pipeline for blocking and asynchronous scene updates."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from harn_gibson.catalog import VisualCatalog, default_visual_catalog
from harn_gibson.events import GibsonEvent
from harn_gibson.scene import (
    SceneAnimation,
    SceneEngine,
    SceneMutation,
    ScenePrimitive,
    SceneState,
    default_mutations_for_event,
    scene_update_payload,
)
from harn_gibson.sinks import EventBuffer
from harn_gibson.styles import style_pack_from_name

RenderMode = Literal["blocking", "async"]
RenderTimingMode = Literal["immediate", "scheduled"]
RenderPlanValidationSeverity = Literal["warning", "error"]

DEFAULT_RENDER_PLAN_MAX_STEPS = 64
DEFAULT_RENDER_PLAN_MAX_MUTATIONS = 256
RENDER_PLAN_DIAGNOSTICS_SCHEMA = "harn-gibson.render-plan-diagnostics.v1"
_INITIAL_PRIMITIVE_KINDS = {
    "stage": "viewport",
    "status": "status",
    "event-feed": "feed",
    "trace-log": "code",
    "decision-log": "code",
    "scan-grid": "grid",
}
_BROWSER_PRIMITIVE_KINDS = frozenset(
    {
        "viewport",
        "status",
        "feed",
        "code",
        "grid",
        "text_stream",
        "mesh",
        "city_block",
        "hologram",
        "signal_scope",
        "tunnel_grid",
        "svg_layer",
        "node_graph",
        "trace_route",
        "ribbon",
        "glyph_layer",
        "data_rain",
        "particle_field",
    }
)
_BROWSER_ANIMATION_KINDS = frozenset(
    {
        "pulse",
        "phase-pulse",
        "stream-pulse",
        "packet_burst",
        "timeline_cue",
        "scan",
        "glitch",
        "breach_wave",
        "camera_jolt",
        "camera_path",
        "flythrough",
        "extrude",
        "hold",
    }
)
_REGION_IDS = frozenset({"root", "mast", "stage", "side"})
_SVG_RAW_MARKUP_KEYS = frozenset(
    {
        "externalHref",
        "foreignObject",
        "foreign_object",
        "href",
        "html",
        "innerHTML",
        "markup",
        "rawSvg",
        "raw_svg",
        "src",
        "svg",
    }
)
_SVG_SYMBOL_KINDS = frozenset(
    {
        "core",
        "data_tunnel",
        "filesystem_gate",
        "gate",
        "globe",
        "ice",
        "ice_wall",
        "mainframe_core",
        "reticle",
        "spinning_globe",
        "target",
        "tunnel",
    }
)
_SVG_FILTER_KINDS = frozenset(
    {
        "bloom",
        "chromatic",
        "chromatic_split",
        "echo",
        "ghost",
        "glow",
        "haze",
        "rgb_split",
        "scanline",
        "scanlines",
        "soft_glow",
    }
)
_SVG_FILTER_NUMERIC_KEYS = frozenset({"alpha", "blur", "intensity", "offset", "spacing", "speed", "width", "x", "y"})
_SVG_CLIP_KINDS = frozenset({"circle", "iris", "rect", "scan", "scanline", "wipe"})
_SVG_CLIP_NUMERIC_KEYS = frozenset(
    {
        "alpha",
        "delayMs",
        "durationMs",
        "h",
        "height",
        "progress",
        "r",
        "radius",
        "size",
        "speed",
        "w",
        "width",
        "x",
        "y",
    }
)
_SVG_MAX_KEYFRAMES_PER_SOURCE = 64
_SVG_MAX_MORPHS_PER_PATH = 64
_SVG_KEYFRAME_NUMERIC_KEYS = frozenset(
    {
        "at",
        "offset",
        "progress",
        "timeMs",
        "ms",
        "x",
        "y",
        "scale",
        "rotation",
        "opacity",
    }
)
_SVG_KEYFRAME_ALLOWED_KEYS = _SVG_KEYFRAME_NUMERIC_KEYS | {"transform"}
_SVG_PATH_MORPH_ALLOWED_KEYS = frozenset({"at", "offset", "progress", "timeMs", "ms", "d"})
_SVG_PATH_MORPH_NUMERIC_KEYS = frozenset({"at", "offset", "progress", "timeMs", "ms"})
_SVG_KEYFRAME_PLAYBACK_NUMERIC_KEYS = frozenset({"durationMs", "delayMs"})
_SVG_KEYFRAME_PLAYBACK_BOOLEAN_KEYS = frozenset({"loop", "yoyo"})


@dataclass(frozen=True, slots=True)
class RenderRequest:
    event: GibsonEvent
    decisions: tuple[dict[str, Any], ...] = ()
    route: str = "renderer_agent"
    timeline_offset_ms: int = 0
    coalesced_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"event": self.event.to_dict()}
        if self.decisions:
            payload["decisions"] = list(self.decisions)
        if self.route != "renderer_agent":
            payload["route"] = self.route
        if self.timeline_offset_ms:
            payload["timelineOffsetMs"] = self.timeline_offset_ms
        if self.coalesced_count != 1:
            payload["coalescedCount"] = self.coalesced_count
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    start_ms: int
    end_ms: int

    @classmethod
    def from_events(cls, events: Sequence[GibsonEvent]) -> TimelineWindow:
        if not events:
            return cls(0, 0)
        timestamps = [event.timestamp_ms for event in events]
        return cls(min(timestamps), max(timestamps))

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def offset_for(self, event: GibsonEvent) -> int:
        return max(0, event.timestamp_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class RenderInputBatch:
    requests: tuple[RenderRequest, ...]
    timeline: TimelineWindow
    route: str = "renderer_agent"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_requests(
        cls,
        requests: Sequence[RenderRequest],
        *,
        route: str = "renderer_agent",
        metadata: Mapping[str, Any] | None = None,
    ) -> RenderInputBatch:
        window = TimelineWindow.from_events([request.event for request in requests])
        size = len(requests)
        adjusted = tuple(
            RenderRequest(
                event=request.event,
                decisions=request.decisions,
                route=request.route,
                timeline_offset_ms=window.offset_for(request.event),
                coalesced_count=max(request.coalesced_count, size),
                metadata={
                    **request.metadata,
                    "renderBatch": {
                        "index": index,
                        "size": size,
                        "route": route,
                        "timeline": window.to_dict(),
                    },
                },
            )
            for index, request in enumerate(requests)
        )
        return cls(adjusted, window, route, dict(metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.render-input.v1",
            "route": self.route,
            "timeline": self.timeline.to_dict(),
            "requests": [request.to_dict() for request in self.requests],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RendererContextConfig:
    project_name: str = "harn-gibson"
    project_root: str | None = None
    display_style: str = "gibson"
    style_pack: dict[str, Any] = field(default_factory=dict)
    compaction_interval_events: int = 40
    max_recent_plans: int = 6
    max_recent_log_entries: int = 12
    max_prop_preview_chars: int = 240
    max_visual_anchors: int = 16
    max_visual_recent_items: int = 16
    max_repo_entries: int = 64
    max_repo_children_per_dir: int = 8
    max_touched_files: int = 24
    max_touched_path_chars: int = 160


@dataclass(frozen=True, slots=True)
class RendererContext:
    mode: Literal["rolling", "compaction"]
    project: dict[str, Any]
    catalog: dict[str, Any]
    scene: dict[str, Any]
    render_input: dict[str, Any]
    recent_agent_context: tuple[str, ...] = ()
    visualization_context: tuple[dict[str, Any], ...] = ()
    visual_continuity: dict[str, Any] = field(default_factory=dict)
    compaction: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.renderer-context.v1",
            "mode": self.mode,
            "project": self.project,
            "catalog": self.catalog,
            "scene": self.scene,
            "renderInput": self.render_input,
            "recentAgentContext": list(self.recent_agent_context),
            "visualizationContext": list(self.visualization_context),
            "visualContinuity": self.visual_continuity,
            "compaction": self.compaction,
        }


class RendererContextBuilder:
    """Builds compact renderer-agent context without replaying the whole session."""

    def __init__(self, config: RendererContextConfig | None = None) -> None:
        self.config = config or RendererContextConfig()
        self.events_since_compaction = 0
        self._history: list[dict[str, Any]] = []
        self._last_context_mode: Literal["rolling", "compaction"] | None = None

    def build(
        self,
        batch: RenderInputBatch,
        scene: SceneState,
        catalog: VisualCatalog,
        *,
        force_compaction: bool = False,
    ) -> RendererContext:
        mode: Literal["rolling", "compaction"] = (
            "compaction" if force_compaction or self._should_compact() else "rolling"
        )
        self._last_context_mode = mode
        visualization_context = tuple(self._history[-self.config.max_recent_plans :])
        return RendererContext(
            mode=mode,
            project=self._project_metadata(batch),
            catalog=_catalog_context(catalog, full=mode == "compaction"),
            scene=_scene_context(scene, full=mode == "compaction", config=self.config),
            render_input=batch.to_dict(),
            recent_agent_context=_recent_agent_context(batch),
            visualization_context=visualization_context,
            visual_continuity=_visual_continuity_context(scene, visualization_context, mode, self.config),
            compaction={
                "eventsSinceCompaction": self.events_since_compaction,
                "intervalEvents": max(1, self.config.compaction_interval_events),
                "reason": "initial or interval compaction" if mode == "compaction" else "rolling update",
            },
        )

    def record_plan(self, plan: RenderPlan) -> None:
        self._history.append(_render_plan_summary(plan))
        if len(self._history) > self.config.max_recent_plans:
            del self._history[: len(self._history) - self.config.max_recent_plans]
        event_count = len(plan.requests)
        if self._last_context_mode == "compaction":
            self.events_since_compaction = event_count
        else:
            self.events_since_compaction += event_count
        self._last_context_mode = None

    def snapshot_history(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._history)

    def _should_compact(self) -> bool:
        return self.events_since_compaction == 0 or self.events_since_compaction >= max(
            1, self.config.compaction_interval_events
        )

    def _project_metadata(self, batch: RenderInputBatch) -> dict[str, Any]:
        style_pack = self.config.style_pack or style_pack_from_name(self.config.display_style).to_dict()
        return {
            "name": self.config.project_name,
            "displayStyle": self.config.display_style,
            "stylePack": style_pack,
            "schemas": {
                "catalog": "harn-gibson.visual-catalog.v1",
                "rendererContext": "harn-gibson.renderer-context.v1",
                "renderInput": "harn-gibson.render-input.v1",
                "renderPlan": "harn-gibson.render-plan.v1",
                "repoTopology": "harn-gibson.repo-topology.v1",
                "scene": "harn-gibson.scene.v1",
                "touchedFiles": "harn-gibson.touched-files.v1",
            },
            "repoTopology": _repo_topology_context(self.config),
            "touchedFiles": _touched_files_context(batch, self.config),
        }


@dataclass(frozen=True, slots=True)
class RenderStep:
    mutations: tuple[SceneMutation, ...]
    delay_ms: int = 0
    start_offset_ms: int = 0
    event_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "delayMs": self.delay_ms,
            "mutations": [mutation.to_dict() for mutation in self.mutations],
        }
        if self.start_offset_ms:
            payload["startOffsetMs"] = self.start_offset_ms
        if self.event_index is not None:
            payload["eventIndex"] = self.event_index
        return payload


@dataclass(frozen=True, slots=True)
class RenderPlan:
    requests: tuple[RenderRequest, ...]
    steps: tuple[RenderStep, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_request(self) -> RenderRequest:
        if not self.requests:
            raise ValueError("render plan has no requests")
        return self.requests[-1]

    def request_for_step(self, step: RenderStep) -> RenderRequest:
        if step.event_index is None:
            return self.primary_request
        if step.event_index < 0 or step.event_index >= len(self.requests):
            return self.primary_request
        return self.requests[step.event_index]


@dataclass(frozen=True, slots=True)
class RenderPlanValidationIssue:
    severity: RenderPlanValidationSeverity
    code: str
    message: str
    step_index: int | None = None
    mutation_index: int | None = None
    target_id: str | None = None
    value: str | int | float | bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.step_index is not None:
            payload["stepIndex"] = self.step_index
        if self.mutation_index is not None:
            payload["mutationIndex"] = self.mutation_index
        if self.target_id is not None:
            payload["targetId"] = self.target_id
        if self.value is not None:
            payload["value"] = self.value
        return payload


def validate_render_plan(
    plan: RenderPlan,
    scene: SceneState,
    catalog: VisualCatalog | None = None,
    *,
    max_steps: int = DEFAULT_RENDER_PLAN_MAX_STEPS,
    max_mutations: int = DEFAULT_RENDER_PLAN_MAX_MUTATIONS,
) -> tuple[RenderPlanValidationIssue, ...]:
    """Check whether a renderer plan can be safely applied to the current scene."""

    visual_catalog = catalog or default_visual_catalog()
    known_primitives = {entry.id for entry in visual_catalog.primitives} | _BROWSER_PRIMITIVE_KINDS
    issues: list[RenderPlanValidationIssue] = []
    working_primitives = {primitive_id: primitive.kind for primitive_id, primitive in scene.primitives.items()}
    working_animations = set(scene.animations)
    max_steps = max(0, max_steps)
    max_mutations = max(0, max_mutations)

    if len(plan.steps) > max_steps:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "plan_too_many_steps",
                f"render plan has {len(plan.steps)} steps, limit is {max_steps}",
                value=len(plan.steps),
            )
        )
    mutation_count = sum(len(step.mutations) for step in plan.steps)
    if mutation_count > max_mutations:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "plan_too_many_mutations",
                f"render plan has {mutation_count} mutations, limit is {max_mutations}",
                value=mutation_count,
            )
        )

    for step_index, step in enumerate(plan.steps):
        if step.event_index is not None and (step.event_index < 0 or step.event_index >= len(plan.requests)):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "event_index_out_of_range",
                    "step eventIndex is outside the current request batch and will use the primary request",
                    step_index=step_index,
                    value=step.event_index,
                )
            )
        if step.delay_ms < 0 or step.start_offset_ms < 0:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "negative_timing",
                    "negative step timing will be clamped during playback",
                    step_index=step_index,
                    value=min(step.delay_ms, step.start_offset_ms),
                )
            )
        for mutation_index, mutation in enumerate(step.mutations):
            _validate_mutation(
                mutation,
                known_primitives,
                working_primitives,
                working_animations,
                issues,
                step_index,
                mutation_index,
            )
    return tuple(issues)


def render_plan_diagnostics_payload(
    issues: Sequence[RenderPlanValidationIssue],
    *,
    status: str | None = None,
) -> dict[str, Any]:
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    if status is None:
        if errors:
            status = "rejected"
        elif warnings:
            status = "accepted_with_warnings"
        else:
            status = "accepted"
    return {
        "schema": RENDER_PLAN_DIAGNOSTICS_SCHEMA,
        "status": status,
        "errorCount": errors,
        "warningCount": warnings,
        "issues": [issue.to_dict() for issue in issues],
    }


def render_plan_has_validation_errors(issues: Sequence[RenderPlanValidationIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def _validate_mutation(
    mutation: SceneMutation,
    known_primitives: set[str],
    working_primitives: dict[str, str],
    working_animations: set[str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    if mutation.op == "reset_scene":
        working_primitives.clear()
        working_primitives.update(_INITIAL_PRIMITIVE_KINDS)
        working_animations.clear()
        return
    if mutation.op == "upsert":
        _validate_upsert(mutation, known_primitives, working_primitives, issues, step_index, mutation_index)
        return
    if mutation.op == "patch":
        _validate_patch(mutation, working_primitives, issues, step_index, mutation_index)
        return
    if mutation.op == "remove":
        _validate_remove(mutation, working_primitives, issues, step_index, mutation_index)
        return
    if mutation.op == "append_log":
        return
    if mutation.op == "start_animation":
        _validate_start_animation(mutation, working_primitives, working_animations, issues, step_index, mutation_index)
        return
    _validate_stop_animation(mutation, working_animations, issues, step_index, mutation_index)


def _validate_upsert(
    mutation: SceneMutation,
    known_primitives: set[str],
    working_primitives: dict[str, str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    primitive = mutation.primitive
    if primitive is None:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_upsert_primitive",
                "upsert mutation requires a primitive",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
        return
    if not primitive.id:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_primitive_id",
                "upsert primitive requires an id",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
    if primitive.kind not in known_primitives:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "unsupported_primitive_kind",
                "primitive kind is not in the browser/catalog render set",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=primitive.id,
                value=primitive.kind,
            )
        )
    if primitive.region not in _REGION_IDS:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "unknown_region",
                "primitive region is outside the current browser layout regions",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=primitive.id,
                value=primitive.region,
            )
        )
    if primitive.kind == "svg_layer":
        _validate_svg_layer(primitive, issues, step_index, mutation_index)
    working_primitives[primitive.id] = primitive.kind


def _validate_patch(
    mutation: SceneMutation,
    working_primitives: Mapping[str, str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    if mutation.target_id is None:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_patch_target",
                "patch mutation requires targetId",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
        return
    if mutation.target_id not in working_primitives:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "patch_target_missing",
                "patch target does not exist in current or planned scene state",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=mutation.target_id,
            )
        )
        return
    if working_primitives[mutation.target_id] == "svg_layer":
        _validate_svg_layer_props(mutation.target_id, mutation.props, issues, step_index, mutation_index)


def _validate_remove(
    mutation: SceneMutation,
    working_primitives: dict[str, str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    if mutation.target_id is None:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_remove_target",
                "remove mutation requires targetId",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
        return
    working_primitives.pop(mutation.target_id, None)


def _validate_start_animation(
    mutation: SceneMutation,
    working_primitives: Mapping[str, str],
    working_animations: set[str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    animation = mutation.animation
    if animation is None:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_animation",
                "start_animation mutation requires an animation object",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
        return
    if not animation.id or animation.id == "None":
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_animation_id",
                "animation requires an id",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=animation.target_id,
            )
        )
    if not animation.target_id or animation.target_id == "None":
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_animation_target",
                "animation requires targetId",
                step_index=step_index,
                mutation_index=mutation_index,
                value=animation.kind,
            )
        )
    elif animation.target_id not in working_primitives and animation.target_id != "scan-grid":
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "animation_target_missing",
                "animation target does not exist in current or planned scene state",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=animation.target_id,
                value=animation.kind,
            )
        )
    if animation.kind not in _BROWSER_ANIMATION_KINDS:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "unsupported_animation_kind",
                "animation kind is not in the persistent browser effect set and will render as a pulse fallback",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=animation.target_id,
                value=animation.kind,
            )
        )
    if animation.duration_ms <= 0:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "nonpositive_animation_duration",
                "animation duration should be positive; browser playback will clamp it",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=animation.target_id,
                value=animation.duration_ms,
            )
        )
    working_animations.add(animation.id)


def _validate_stop_animation(
    mutation: SceneMutation,
    working_animations: set[str],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    if mutation.target_id is None:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "missing_stop_animation_target",
                "stop_animation mutation requires targetId containing the animation id",
                step_index=step_index,
                mutation_index=mutation_index,
            )
        )
        return
    working_animations.discard(mutation.target_id)


def _validate_svg_layer(
    primitive: ScenePrimitive,
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    _validate_svg_layer_props(primitive.id, primitive.props, issues, step_index, mutation_index)


def _validate_svg_layer_props(
    target_id: str,
    props: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
) -> None:
    for key in sorted(_SVG_RAW_MARKUP_KEYS & set(props)):
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "raw_svg_markup",
                "svg_layer accepts structured vector data only, not raw markup or external references",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=key,
            )
        )
    _validate_svg_keyframe_source(target_id, props, issues, step_index, mutation_index, "props", 0)
    symbols = props.get("symbols")
    if not isinstance(symbols, list):
        return
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, Mapping):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_symbol",
                    "svg_layer symbols should be objects",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=index,
                )
            )
            continue
        kind = str(symbol.get("kind") or symbol.get("type") or "reticle")
        if kind not in _SVG_SYMBOL_KINDS:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "unsupported_svg_symbol",
                    "svg_layer symbol kind is not in the curated browser symbol set",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=kind,
                )
            )


def _validate_svg_keyframe_source(
    target_id: str,
    source: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
    depth: int,
) -> None:
    _validate_svg_layer_effects(target_id, source, issues, step_index, mutation_index, path)
    _validate_svg_keyframe_playback(target_id, source, issues, step_index, mutation_index, path)
    animation = source.get("animation")
    if isinstance(animation, Mapping):
        _validate_svg_keyframe_playback(target_id, animation, issues, step_index, mutation_index, f"{path}.animation")
    elif animation is not None:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_keyframe_animation",
                "svg_layer animation config should be an object",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=f"{path}.animation",
            )
        )
    _validate_svg_keyframes(target_id, source, issues, step_index, mutation_index, path)
    _validate_svg_paths(target_id, source, issues, step_index, mutation_index, path)
    groups = source.get("groups")
    if not isinstance(groups, list) or depth >= 3:
        return
    for index, group in enumerate(groups):
        if isinstance(group, Mapping):
            _validate_svg_keyframe_source(
                target_id,
                group,
                issues,
                step_index,
                mutation_index,
                f"{path}.groups[{index}]",
                depth + 1,
            )


def _validate_svg_paths(
    target_id: str,
    source: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if "paths" not in source:
        return
    paths = source["paths"]
    path_list_path = f"{path}.paths"
    if not isinstance(paths, list):
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_paths",
                "svg_layer paths should be a bounded list of path objects",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=path_list_path,
            )
        )
        return
    for index, path_spec in enumerate(paths[:256]):
        path_spec_path = f"{path_list_path}[{index}]"
        if not isinstance(path_spec, Mapping):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_path",
                    "svg_layer path entries should be objects",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=path_spec_path,
                )
            )
            continue
        _validate_svg_keyframe_playback(
            target_id,
            path_spec,
            issues,
            step_index,
            mutation_index,
            path_spec_path,
        )
        _validate_svg_path_morphs(target_id, path_spec, issues, step_index, mutation_index, path_spec_path)


def _validate_svg_path_morphs(
    target_id: str,
    path_spec: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if "morphs" not in path_spec:
        return
    morphs = path_spec["morphs"]
    morphs_path = f"{path}.morphs"
    if not isinstance(morphs, list):
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_path_morphs",
                "svg_layer path morphs should be a bounded list of path frames",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=morphs_path,
            )
        )
        return
    if len(morphs) > _SVG_MAX_MORPHS_PER_PATH:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "too_many_svg_path_morphs",
                f"svg_layer path morphs should have at most {_SVG_MAX_MORPHS_PER_PATH} frames per path",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=len(morphs),
            )
        )
    for index, morph in enumerate(morphs[:_SVG_MAX_MORPHS_PER_PATH]):
        morph_path = f"{morphs_path}[{index}]"
        if not isinstance(morph, Mapping):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_path_morph",
                    "svg_layer path morph frames should be objects",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=morph_path,
                )
            )
            continue
        if not isinstance(morph.get("d"), str) or not morph.get("d"):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_path_morph_d",
                    "svg_layer path morph frames should include a nonempty d string",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=f"{morph_path}.d",
                )
            )
        for key, value in morph.items():
            field_path = f"{morph_path}.{key}"
            if key not in _SVG_PATH_MORPH_ALLOWED_KEYS:
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "unsupported_svg_path_morph_field",
                        "svg_layer path morph frames support timing fields and d only",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=field_path,
                    )
                )
                continue
            if key in _SVG_PATH_MORPH_NUMERIC_KEYS and not _is_finite_number(value):
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "invalid_svg_keyframe_value",
                        "svg_layer path morph timing values should be finite JSON numbers",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=field_path,
                    )
                )


def _validate_svg_layer_effects(
    target_id: str,
    source: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if "filter" in source:
        _validate_svg_filter_spec(
            target_id,
            source["filter"],
            issues,
            step_index,
            mutation_index,
            f"{path}.filter",
        )
    if "filters" in source:
        filters = source["filters"]
        if not isinstance(filters, list):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_filters",
                    "svg_layer filters should be a bounded list of preset names or objects",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=f"{path}.filters",
                )
            )
        else:
            for index, filter_spec in enumerate(filters[:16]):
                _validate_svg_filter_spec(
                    target_id,
                    filter_spec,
                    issues,
                    step_index,
                    mutation_index,
                    f"{path}.filters[{index}]",
                )
    if "clip" in source:
        _validate_svg_clip_spec(target_id, source["clip"], issues, step_index, mutation_index, f"{path}.clip")


def _validate_svg_filter_spec(
    target_id: str,
    spec: Any,
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if isinstance(spec, str):
        kind = spec
    elif isinstance(spec, Mapping):
        kind_value = spec.get("kind") or spec.get("type") or spec.get("preset")
        if not isinstance(kind_value, str) or not kind_value:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_filter",
                    "svg_layer filter presets should include a string kind/type/preset",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=path,
                )
            )
            return
        kind = kind_value
        for key in sorted(_SVG_FILTER_NUMERIC_KEYS & set(spec)):
            if not _is_finite_number(spec[key]):
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "invalid_svg_filter_value",
                        "svg_layer filter preset values should be finite JSON numbers",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=f"{path}.{key}",
                    )
                )
    else:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_filter",
                "svg_layer filter presets should be names or objects",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=path,
            )
        )
        return
    if kind not in _SVG_FILTER_KINDS:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "unsupported_svg_filter",
                "svg_layer filter preset is not in the bounded browser filter set",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=kind,
            )
        )


def _validate_svg_clip_spec(
    target_id: str,
    spec: Any,
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if isinstance(spec, str):
        kind = spec
    elif isinstance(spec, Mapping):
        kind_value = spec.get("kind") or spec.get("type") or spec.get("shape")
        if not isinstance(kind_value, str) or not kind_value:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_clip",
                    "svg_layer clip should include a string kind/type/shape",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=path,
                )
            )
            return
        kind = kind_value
        for key in sorted(_SVG_CLIP_NUMERIC_KEYS & set(spec)):
            if not _is_finite_number(spec[key]):
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "invalid_svg_clip_value",
                        "svg_layer clip values should be finite JSON numbers",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=f"{path}.{key}",
                    )
                )
        for key in _SVG_KEYFRAME_PLAYBACK_BOOLEAN_KEYS:
            if key in spec and not isinstance(spec[key], bool):
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "invalid_svg_clip_boolean",
                        "svg_layer clip playback flags should be booleans",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=f"{path}.{key}",
                    )
                )
    else:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_clip",
                "svg_layer clip should be a preset name or object",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=path,
            )
        )
        return
    if kind not in _SVG_CLIP_KINDS:
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "unsupported_svg_clip",
                "svg_layer clip kind is not in the bounded browser clip set",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=kind,
            )
        )


def _validate_svg_keyframe_playback(
    target_id: str,
    source: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    for key in _SVG_KEYFRAME_PLAYBACK_NUMERIC_KEYS:
        if key not in source:
            continue
        value = source[key]
        if not _is_finite_number(value):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_keyframe_value",
                    "svg_layer keyframe timing values should be finite JSON numbers",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=f"{path}.{key}",
                )
            )
        elif key == "durationMs" and float(value) <= 0:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "nonpositive_svg_keyframe_duration",
                    "svg_layer keyframe duration should be positive; browser playback will clamp it",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=f"{path}.{key}",
                )
            )
    for key in _SVG_KEYFRAME_PLAYBACK_BOOLEAN_KEYS:
        if key in source and not isinstance(source[key], bool):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_keyframe_boolean",
                    "svg_layer keyframe playback flags should be booleans",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=f"{path}.{key}",
                )
            )


def _validate_svg_keyframes(
    target_id: str,
    source: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    if "keyframes" not in source:
        return
    keyframes = source["keyframes"]
    keyframe_path = f"{path}.keyframes"
    if not isinstance(keyframes, list):
        issues.append(
            RenderPlanValidationIssue(
                "warning",
                "invalid_svg_keyframes",
                "svg_layer keyframes should be a bounded list of objects",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=keyframe_path,
            )
        )
        return
    if len(keyframes) > _SVG_MAX_KEYFRAMES_PER_SOURCE:
        issues.append(
            RenderPlanValidationIssue(
                "error",
                "too_many_svg_keyframes",
                f"svg_layer keyframes should have at most {_SVG_MAX_KEYFRAMES_PER_SOURCE} frames per layer or group",
                step_index=step_index,
                mutation_index=mutation_index,
                target_id=target_id,
                value=len(keyframes),
            )
        )
    for index, frame in enumerate(keyframes[:_SVG_MAX_KEYFRAMES_PER_SOURCE]):
        frame_path = f"{keyframe_path}[{index}]"
        if not isinstance(frame, Mapping):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_keyframe",
                    "svg_layer keyframe entries should be objects",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=frame_path,
                )
            )
            continue
        _validate_svg_keyframe(target_id, frame, issues, step_index, mutation_index, frame_path)


def _validate_svg_keyframe(
    target_id: str,
    frame: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    for key, value in frame.items():
        field_path = f"{path}.{key}"
        if key == "transform":
            if isinstance(value, Mapping):
                _validate_svg_keyframe_transform(target_id, value, issues, step_index, mutation_index, field_path)
            else:
                issues.append(
                    RenderPlanValidationIssue(
                        "warning",
                        "invalid_svg_keyframe_transform",
                        "svg_layer keyframe transform should be an object",
                        step_index=step_index,
                        mutation_index=mutation_index,
                        target_id=target_id,
                        value=field_path,
                    )
                )
            continue
        if key not in _SVG_KEYFRAME_ALLOWED_KEYS:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "unsupported_svg_keyframe_field",
                    "svg_layer keyframes support numeric timing and transform fields only",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=field_path,
                )
            )
            continue
        if not _is_finite_number(value):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_keyframe_value",
                    "svg_layer keyframe values should be finite JSON numbers",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=field_path,
                )
            )


def _validate_svg_keyframe_transform(
    target_id: str,
    transform: Mapping[str, Any],
    issues: list[RenderPlanValidationIssue],
    step_index: int,
    mutation_index: int,
    path: str,
) -> None:
    for key, value in transform.items():
        field_path = f"{path}.{key}"
        if key not in _SVG_KEYFRAME_NUMERIC_KEYS:
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "unsupported_svg_keyframe_field",
                    "svg_layer keyframe transforms support numeric transform fields only",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=field_path,
                )
            )
            continue
        if not _is_finite_number(value):
            issues.append(
                RenderPlanValidationIssue(
                    "warning",
                    "invalid_svg_keyframe_value",
                    "svg_layer keyframe transform values should be finite JSON numbers",
                    step_index=step_index,
                    mutation_index=mutation_index,
                    target_id=target_id,
                    value=field_path,
                )
            )


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(float(value))


class SceneRenderer(Protocol):
    def render(self, requests: Sequence[RenderRequest], scene: SceneState) -> RenderPlan: ...


class ContextualSceneRenderer(Protocol):
    def render_with_context(
        self,
        requests: Sequence[RenderRequest],
        scene: SceneState,
        context: RendererContext,
    ) -> RenderPlan: ...


@dataclass(slots=True)
class DeterministicSceneRenderer:
    """Default renderer-agent: convert each event to deterministic scene mutations."""

    def render(self, requests: Sequence[RenderRequest], _scene: SceneState) -> RenderPlan:
        steps = []
        for index, request in enumerate(requests):
            steps.append(
                RenderStep(
                    mutations=tuple(default_mutations_for_event(request.event, request.decisions)),
                    event_index=index,
                )
            )
        return RenderPlan(requests=tuple(requests), steps=tuple(steps), metadata={"renderer": "deterministic"})

    def render_with_context(
        self,
        requests: Sequence[RenderRequest],
        scene: SceneState,
        context: RendererContext,
    ) -> RenderPlan:
        base_plan = self.render(requests, scene)
        if not base_plan.steps:
            return base_plan
        repo_mutations = _repo_visual_mutations(context, base_plan.primary_request.event)
        if not repo_mutations:
            return base_plan
        steps = list(base_plan.steps)
        final_step = steps[-1]
        steps[-1] = RenderStep(
            mutations=(*final_step.mutations, *repo_mutations),
            delay_ms=final_step.delay_ms,
            start_offset_ms=final_step.start_offset_ms,
            event_index=final_step.event_index,
        )
        return RenderPlan(requests=base_plan.requests, steps=tuple(steps), metadata=base_plan.metadata)


@dataclass(frozen=True, slots=True)
class RenderSubmitResult:
    mode: RenderMode
    queued: int
    updates: tuple[dict[str, Any], ...] = ()

    @property
    def scene_revision(self) -> int | None:
        if not self.updates:
            return None
        scene = self.updates[-1].get("scene", {})
        revision = scene.get("revision") if isinstance(scene, dict) else None
        return revision if isinstance(revision, int) else None


class RenderPipeline:
    """Submit render jobs in blocking mode or through an async batch queue."""

    def __init__(
        self,
        *,
        scene: SceneEngine,
        buffer: EventBuffer,
        renderer: SceneRenderer | None = None,
        catalog: VisualCatalog | None = None,
        context_builder: RendererContextBuilder | None = None,
        mode: RenderMode = "blocking",
        batch_window_ms: int = 40,
        timing_mode: RenderTimingMode = "immediate",
        sleep_fn: Callable[[float], None] = time.sleep,
        context_recorder: Callable[[RendererContext], None] | None = None,
    ) -> None:
        if mode not in {"blocking", "async"}:
            raise ValueError("render mode must be blocking or async")
        if timing_mode not in {"immediate", "scheduled"}:
            raise ValueError("render timing mode must be immediate or scheduled")
        self.scene = scene
        self.buffer = buffer
        self.renderer = renderer or DeterministicSceneRenderer()
        self.catalog = catalog or default_visual_catalog()
        self.context_builder = context_builder or RendererContextBuilder()
        self.mode = mode
        self.batch_window_ms = max(0, batch_window_ms)
        self.timing_mode = timing_mode
        self._sleep = sleep_fn
        self.context_recorder = context_recorder
        self._queue: queue.Queue[RenderRequest | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, request: RenderRequest) -> RenderSubmitResult:
        if self.mode == "blocking":
            return RenderSubmitResult(mode=self.mode, queued=0, updates=tuple(self._render_and_publish((request,))))
        self.start()
        self._queue.put(request)
        return RenderSubmitResult(mode=self.mode, queued=self.pending_count())

    def apply_direct(
        self,
        request: RenderRequest,
        mutations: Sequence[SceneMutation],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RenderSubmitResult:
        plan = RenderPlan(
            requests=(request,),
            steps=(RenderStep(tuple(mutations), event_index=0),),
            metadata={"renderer": "direct", **dict(metadata or {})},
        )
        with self._lock:
            updates = tuple(self._apply_plan(plan))
        return RenderSubmitResult(mode=self.mode, queued=self.pending_count(), updates=updates)

    def apply_plan(self, plan: RenderPlan) -> RenderSubmitResult:
        with self._lock:
            updates = tuple(self._apply_plan(plan))
        return RenderSubmitResult(mode=self.mode, queued=self.pending_count(), updates=updates)

    def start(self) -> None:
        if self.mode != "async":
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._worker_loop, name="harn-gibson-renderer", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        if self._worker is None:
            return
        self._queue.put(None)
        self._worker.join(timeout=1)
        self._worker = None

    def pending_count(self) -> int:
        return self._queue.qsize()

    def _worker_loop(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                return
            requests, stop_after_batch = self._collect_batch(first)
            self._render_and_publish(requests)
            if stop_after_batch:
                return

    def _collect_batch(self, first: RenderRequest) -> tuple[tuple[RenderRequest, ...], bool]:
        requests = [first]
        stop_after_batch = False
        if self.batch_window_ms:
            self._sleep(self.batch_window_ms / 1000)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stop_after_batch = True
                break
            requests.append(item)
        return tuple(requests), stop_after_batch

    def _render_and_publish(self, requests: tuple[RenderRequest, ...]) -> list[dict[str, Any]]:
        if not requests:
            return []
        with self._lock:
            batch = RenderInputBatch.from_requests(requests, route=requests[-1].route)
            context = self.context_builder.build(batch, self.scene.state, self.catalog)
            if self.context_recorder is not None:
                self.context_recorder(context)
            render_with_context = getattr(self.renderer, "render_with_context", None)
            if callable(render_with_context):
                plan = render_with_context(batch.requests, self.scene.state, context)
            else:
                plan = self.renderer.render(batch.requests, self.scene.state)
            return self._apply_plan(plan)

    def _apply_plan(self, plan: RenderPlan) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        render_input = RenderInputBatch.from_requests(
            plan.requests,
            route=plan.requests[-1].route if plan.requests else "renderer_agent",
        )
        render_intent = render_intent_from_plan(plan, render_input)
        self.scene.record_render_intent(render_intent)
        applied_offset_ms = 0
        for index, step in enumerate(plan.steps):
            wait_ms, applied_offset_ms = step_schedule(step, applied_offset_ms, self.timing_mode)
            if wait_ms > 0:
                self._sleep(wait_ms / 1000)
            request = plan.request_for_step(step)
            scene = self.scene.apply(step.mutations)
            update = render_update_payload(
                plan,
                step,
                index,
                request,
                scene,
                render_input,
                render_intent,
                step_schedule=step_schedule_payload(step, self.timing_mode, wait_ms, applied_offset_ms),
            )
            self.buffer.publish(update)
            updates.append(update)
        self.context_builder.record_plan(plan)
        return updates


def step_schedule(step: RenderStep, current_offset_ms: int, timing_mode: RenderTimingMode) -> tuple[int, int]:
    delay_ms = max(0, step.delay_ms)
    start_offset_ms = max(0, step.start_offset_ms)
    scheduled_start_ms = max(current_offset_ms, start_offset_ms) if timing_mode == "scheduled" else current_offset_ms
    applied_offset_ms = scheduled_start_ms + delay_ms
    return max(0, applied_offset_ms - current_offset_ms), applied_offset_ms


def step_schedule_payload(
    step: RenderStep,
    timing_mode: RenderTimingMode,
    wait_ms: int,
    applied_offset_ms: int,
) -> dict[str, Any]:
    return {
        "timingMode": timing_mode,
        "startOffsetMs": step.start_offset_ms,
        "delayMs": step.delay_ms,
        "scheduledWaitMs": wait_ms,
        "appliedOffsetMs": applied_offset_ms,
    }


def _catalog_context(catalog: VisualCatalog, *, full: bool) -> dict[str, Any]:
    if full:
        return catalog.to_dict()
    return {
        "schema": "harn-gibson.visual-catalog.v1",
        "mode": "summary",
        "primitives": [_catalog_entry_summary(entry) for entry in catalog.primitives],
        "effects": [_catalog_entry_summary(entry) for entry in catalog.effects],
    }


def _catalog_entry_summary(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "tags": list(entry.tags),
    }


def _scene_context(scene: SceneState, *, full: bool, config: RendererContextConfig) -> dict[str, Any]:
    if full:
        return scene.to_dict()
    return {
        "schema": "harn-gibson.scene-summary.v1",
        "revision": scene.revision,
        "primitiveCount": len(scene.primitives),
        "animationCount": len(scene.animations),
        "primitives": [
            _primitive_summary(primitive, config.max_prop_preview_chars)
            for primitive in sorted(scene.primitives.values(), key=lambda item: item.id)
        ],
        "activeAnimations": [
            _animation_summary(animation) for animation in sorted(scene.animations.values(), key=lambda item: item.id)
        ],
        "recentLog": list(scene.log[-config.max_recent_log_entries :]),
    }


def _primitive_summary(primitive: Any, max_chars: int) -> dict[str, Any]:
    props_preview = {
        key: _clip_preview(value, max_chars)
        for key, value in sorted(primitive.props.items())
        if key in {"text", "title", "phase", "tone", "streamId", "isStreaming"}
    }
    return {
        "id": primitive.id,
        "kind": primitive.kind,
        "region": primitive.region,
        "propKeys": sorted(primitive.props),
        "propsPreview": props_preview,
        "children": list(primitive.children),
    }


def _animation_summary(animation: Any) -> dict[str, Any]:
    return {
        "id": animation.id,
        "targetId": animation.target_id,
        "kind": animation.kind,
        "startedAtMs": animation.started_at_ms,
        "durationMs": animation.duration_ms,
        "loop": animation.loop,
    }


def _visual_continuity_context(
    scene: SceneState,
    history: Sequence[Mapping[str, Any]],
    mode: str,
    config: RendererContextConfig,
) -> dict[str, Any]:
    style_pack = config.style_pack or style_pack_from_name(config.display_style).to_dict()
    active_targets = {animation.target_id for animation in scene.animations.values()}
    anchors = [
        _visual_anchor_summary(primitive, active_targets, config.max_prop_preview_chars)
        for primitive in sorted(scene.primitives.values(), key=lambda item: item.id)
        if primitive.region == "stage"
    ][: max(0, config.max_visual_anchors)]
    recent_effects: list[str] = []
    recent_targets: list[str] = []
    recent_renderers: list[str] = []
    for item in history[-config.max_recent_plans :]:
        _append_unique(recent_renderers, str(item.get("renderer") or "unknown"))
        intent = item.get("renderIntent") if isinstance(item.get("renderIntent"), Mapping) else {}
        effects = intent.get("effects") if isinstance(intent, Mapping) else None
        targets = intent.get("targets") if isinstance(intent, Mapping) else None
        for effect in (effects if isinstance(effects, list) else ()):
            _append_unique(recent_effects, str(effect))
        for target in (targets if isinstance(targets, list) else ()):
            _append_unique(recent_targets, str(target))
    return {
        "schema": "harn-gibson.visual-continuity.v1",
        "mode": mode,
        "sceneRevision": scene.revision,
        "style": {
            "id": config.display_style,
            "motifs": list(style_pack.get("motifs", ())),
        },
        "anchorCount": len(anchors),
        "anchors": anchors,
        "activeAnimationCount": len(scene.animations),
        "activeAnimations": [
            _visual_animation_summary(animation, config.max_prop_preview_chars)
            for animation in sorted(scene.animations.values(), key=lambda item: item.id)[
                : max(0, config.max_visual_anchors)
            ]
        ],
        "recentEffects": recent_effects[: max(0, config.max_visual_recent_items)],
        "recentTargets": recent_targets[: max(0, config.max_visual_recent_items)],
        "recentRenderers": recent_renderers[: max(0, config.max_visual_recent_items)],
    }


def _visual_anchor_summary(
    primitive: ScenePrimitive,
    active_targets: set[str],
    max_chars: int,
) -> dict[str, Any]:
    props = primitive.props
    summary: dict[str, Any] = {
        "id": primitive.id,
        "kind": primitive.kind,
        "region": primitive.region,
        "animated": primitive.id in active_targets,
    }
    tone = _visual_tone(props)
    if tone is not None:
        summary["tone"] = tone
    label = _visual_label(props)
    if label is not None:
        summary["label"] = _clip_preview(label, max_chars)
    focus = _visual_focus(props)
    if focus is not None:
        summary["focus"] = _clip_preview(focus, max_chars)
    if props.get("isStreaming") is not None:
        summary["isStreaming"] = bool(props.get("isStreaming"))
    return summary


def _visual_animation_summary(animation: SceneAnimation, max_chars: int) -> dict[str, Any]:
    summary = _animation_summary(animation)
    props = animation.props
    if props:
        summary["propsPreview"] = {
            key: _clip_preview(props[key], max_chars)
            for key in ("phase", "tone", "accentTone", "label")
            if key in props
        }
    cues = props.get("cues")
    if isinstance(cues, list):
        summary["cueCount"] = len(cues)
        labels = [cue.get("label") for cue in cues if isinstance(cue, Mapping) and cue.get("label")]
        if labels:
            summary["cueLabels"] = [str(label)[:32] for label in labels[:6]]
    return summary


def _visual_tone(props: Mapping[str, Any]) -> str | None:
    for key in ("tone", "accentTone", "color", "material", "palette", "phase"):
        value = props.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _visual_label(props: Mapping[str, Any]) -> Any:
    for key in ("label", "title", "streamId", "text"):
        value = props.get(key)
        if value:
            return value
    return None


def _visual_focus(props: Mapping[str, Any]) -> Any:
    for key in ("focusNodeId", "focusBlockId", "focusHopId", "targetId"):
        value = props.get(key)
        if value:
            return value
    return None


def _clip_preview(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else f"{value[: max(0, max_chars - 3)]}..."
    if isinstance(value, list):
        return [_clip_preview(item, max_chars) for item in value[:3]]
    if isinstance(value, dict):
        return {str(key): _clip_preview(child, max_chars) for key, child in list(value.items())[:5]}
    return value


def _recent_agent_context(batch: RenderInputBatch) -> tuple[str, ...]:
    seen: set[str] = set()
    recent = []
    for request in batch.requests:
        for item in (*request.event.recent_context, *request.event.visualization_context):
            if item not in seen:
                seen.add(item)
                recent.append(item)
    return tuple(recent)


def _repo_visual_mutations(context: RendererContext, event: GibsonEvent) -> tuple[SceneMutation, ...]:
    topology = context.project.get("repoTopology")
    touched = context.project.get("touchedFiles")
    repo_entries = _repo_visual_entries(topology)
    touched_files = _repo_visual_touched_files(touched)
    if not repo_entries and not touched_files:
        return ()
    graph_props = _repo_graph_props(topology, repo_entries, touched_files, event)
    city_props = _repo_city_props(topology, repo_entries, touched_files, event)
    mutations = [
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="repo-map",
                kind="node_graph",
                region="stage",
                props=graph_props,
            ),
        ),
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="repo-city",
                kind="city_block",
                region="stage",
                props=city_props,
            ),
        )
    ]
    if touched_files:
        touched_paths = [str(item.get("path", "")) for item in touched_files if item.get("path")]
        mutations.extend(
            [
                SceneMutation(
                    op="upsert",
                    primitive=ScenePrimitive(
                        id="repo-touch-field",
                        kind="particle_field",
                        region="stage",
                        props={
                            "count": min(72, 14 + len(touched_paths) * 6),
                            "velocity": 0.34,
                            "emitter": {"x": 0.58, "y": 0.34},
                            "color": "magenta",
                            "blend": "screen",
                            "seed": event.sequence + len(touched_paths),
                            "paths": touched_paths,
                        },
                    ),
                ),
                SceneMutation(
                    op="start_animation",
                    animation=SceneAnimation(
                        id=f"repo-touch-{event.sequence}",
                        target_id="repo-map",
                        kind="packet_burst",
                        started_at_ms=event.timestamp_ms,
                        duration_ms=2200,
                        props={
                            "phase": event.phase,
                            "tone": "magenta",
                            "sequence": event.sequence,
                            "paths": touched_paths,
                        },
                    ),
                ),
                SceneMutation(
                    op="start_animation",
                    animation=SceneAnimation(
                        id=f"repo-city-touch-{event.sequence}",
                        target_id="repo-city",
                        kind="extrude",
                        started_at_ms=event.timestamp_ms,
                        duration_ms=2600,
                        props={
                            "phase": event.phase,
                            "tone": "magenta",
                            "sequence": event.sequence,
                            "paths": touched_paths,
                        },
                    ),
                ),
            ]
        )
    return tuple(mutations)


def _repo_visual_entries(topology: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(topology, Mapping):
        return ()
    entries = topology.get("entries")
    if not isinstance(entries, list):
        return ()
    return tuple(dict(entry) for entry in entries[:8] if isinstance(entry, Mapping))


def _repo_visual_touched_files(touched: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(touched, Mapping):
        return ()
    files = touched.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(dict(item) for item in files[:6] if isinstance(item, Mapping) and item.get("path"))


def _repo_graph_props(
    topology: Any,
    repo_entries: Sequence[Mapping[str, Any]],
    touched_files: Sequence[Mapping[str, Any]],
    event: GibsonEvent,
) -> dict[str, Any]:
    root_name = str(topology.get("rootName") if isinstance(topology, Mapping) else "repo") or "repo"
    nodes = [{"id": "repo-root", "label": root_name[:18], "x": 0.12, "y": 0.52, "tone": "amber"}]
    edges: list[dict[str, str]] = []
    entry_node_ids: set[str] = set()
    for index, entry in enumerate(repo_entries):
        path = str(entry.get("path") or entry.get("name") or f"entry-{index}")
        node_id = f"repo:{path}"
        entry_node_ids.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "label": _repo_node_label(path),
                "x": round(0.18 + (index % 4) * 0.095, 3),
                "y": round(0.28 + (index // 4) * 0.14, 3),
                "tone": _repo_entry_tone(str(entry.get("kind") or "")),
            }
        )
        edges.append({"source": "repo-root", "target": node_id, "label": str(entry.get("kind") or "entry")})
    for index, item in enumerate(touched_files):
        path = str(item.get("path") or f"touch-{index}")
        node_id = f"touch:{index}"
        source = f"repo:{_repo_top_level(path)}"
        if source not in entry_node_ids:
            source = "repo-root"
        nodes.append(
            {
                "id": node_id,
                "label": _repo_node_label(path),
                "x": round(0.56 + (index % 3) * 0.12, 3),
                "y": round(0.26 + (index // 3) * 0.16, 3),
                "tone": "magenta",
            }
        )
        edges.append({"source": source, "target": node_id, "label": str(item.get("operation") or "touched")[:16]})
    return {
        "layout": "repo-topology",
        "focusNodeId": "touch:0" if touched_files else "repo-root",
        "rootName": root_name,
        "nodes": nodes,
        "edges": edges,
        "touchedFiles": [dict(item) for item in touched_files],
        "eventSequence": event.sequence,
        "labels": [root_name, f"{len(touched_files)} touched"],
    }


def _repo_city_props(
    topology: Any,
    repo_entries: Sequence[Mapping[str, Any]],
    touched_files: Sequence[Mapping[str, Any]],
    event: GibsonEvent,
) -> dict[str, Any]:
    root_name = str(topology.get("rootName") if isinstance(topology, Mapping) else "repo") or "repo"
    touched_paths = [str(item.get("path")) for item in touched_files if item.get("path")]
    blocks: list[dict[str, Any]] = [
        {
            "id": "repo-city-root",
            "path": ".",
            "x": 0.04,
            "y": 0.70,
            "w": 0.055,
            "d": 0.065,
            "h": 0.16,
            "tone": "amber",
            "label": root_name[:10],
            "kind": "root",
            "files": 0,
            "dirs": 0,
            "touched": 0,
        }
    ]
    block_paths: list[tuple[str, str]] = [(".", "repo-city-root")]
    for index, entry in enumerate(repo_entries):
        path = str(entry.get("path") or entry.get("name") or f"entry-{index}")
        children = _repo_entry_children(entry)
        touched_count = _repo_touch_count(path, touched_paths)
        file_count = _repo_visible_file_count(entry, children)
        dir_count = _repo_visible_dir_count(entry, children)
        line_count = _repo_visible_line_count(entry, children)
        block_id = _repo_city_block_id(path)
        x = round(0.08 + (index % 5) * 0.082, 3)
        y = round(0.68 - (index // 5) * 0.10, 3)
        blocks.append(
            {
                "id": block_id,
                "path": path,
                "x": x,
                "y": y,
                "w": 0.058,
                "d": 0.066,
                "h": _repo_city_height(file_count, dir_count, touched_count, line_count),
                "tone": "magenta" if touched_count else _repo_entry_tone(str(entry.get("kind") or "")),
                "label": _repo_node_label(path),
                "kind": str(entry.get("kind") or "entry"),
                "files": file_count,
                "dirs": dir_count,
                "lines": line_count,
                "touched": touched_count,
            }
        )
        block_paths.append((path, block_id))
        blocks.extend(_repo_child_city_blocks(path, children, touched_paths, x, y, block_paths))
    return {
        "focusBlockId": _repo_city_focus_block_id(block_paths, touched_paths),
        "heightScale": 1.12,
        "layout": "repo-bfs-depth-2",
        "rootName": root_name,
        "blocks": blocks,
        "labels": [root_name, f"{len(touched_paths)} touched", f"seq {event.sequence}"],
        "touchedFiles": [dict(item) for item in touched_files],
        "eventSequence": event.sequence,
        "cameraPath": _repo_city_camera_path(event, len(touched_paths)),
    }


def _repo_city_camera_path(event: GibsonEvent, touched_count: int) -> dict[str, Any]:
    direction = -1 if event.sequence % 2 else 1
    touch_push = min(0.018, touched_count * 0.006)
    return {
        "durationMs": 7600,
        "loop": True,
        "yoyo": True,
        "keyframes": [
            {"at": 0, "x": round(-0.010 * direction, 3), "y": 0.006, "scale": 0.99},
            {
                "at": 0.46,
                "x": round((0.018 + touch_push) * direction, 3),
                "y": round(-0.012 - touch_push, 3),
                "scale": round(1.025 + touch_push, 3),
                "rotation": round(0.010 * direction, 3),
            },
            {"at": 1, "x": round(0.004 * direction, 3), "y": 0.004, "scale": 1.0},
        ],
    }


def _repo_entry_children(entry: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    children = entry.get("children")
    if not isinstance(children, list):
        return ()
    return tuple(dict(child) for child in children[:4] if isinstance(child, Mapping))


def _repo_child_city_blocks(
    parent_path: str,
    children: Sequence[Mapping[str, Any]],
    touched_paths: Sequence[str],
    parent_x: float,
    parent_y: float,
    block_paths: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, child in enumerate(children):
        path = str(child.get("path") or f"{parent_path}/child-{index}")
        touched_count = _repo_touch_count(path, touched_paths)
        file_count = 1 if child.get("kind") in {"file", "symlink"} else _repo_entry_int(child.get("visibleFileCount"))
        dir_count = 1 if child.get("kind") == "dir" else 0
        line_count = _repo_entry_line_count(child)
        block_id = _repo_city_block_id(path)
        block_paths.append((path, block_id))
        blocks.append(
            {
                "id": block_id,
                "path": path,
                "x": round(parent_x + 0.012 + (index % 2) * 0.031, 3),
                "y": round(parent_y + 0.035 + (index // 2) * 0.028, 3),
                "w": 0.024,
                "d": 0.030,
                "h": _repo_child_city_height(path, touched_count, line_count),
                "tone": "magenta" if touched_count else _repo_entry_tone(str(child.get("kind") or "")),
                "label": _repo_node_label(path),
                "kind": str(child.get("kind") or "entry"),
                "files": file_count,
                "dirs": dir_count,
                "lines": line_count,
                "touched": touched_count,
            }
        )
    return blocks


def _repo_visible_file_count(entry: Mapping[str, Any], children: Sequence[Mapping[str, Any]]) -> int:
    if entry.get("kind") in {"file", "symlink"}:
        return 1
    file_count = _repo_entry_int(entry.get("visibleFileCount"))
    if file_count:
        return file_count
    return sum(1 for child in children if child.get("kind") in {"file", "symlink"})


def _repo_visible_dir_count(entry: Mapping[str, Any], children: Sequence[Mapping[str, Any]]) -> int:
    if entry.get("kind") == "dir":
        return max(1, _repo_entry_int(entry.get("visibleDirCount")))
    return 0


def _repo_visible_line_count(entry: Mapping[str, Any], children: Sequence[Mapping[str, Any]]) -> int:
    line_count = _repo_entry_line_count(entry)
    if line_count:
        return line_count
    return sum(_repo_entry_line_count(child) for child in children)


def _repo_entry_line_count(entry: Mapping[str, Any]) -> int:
    line_count = _repo_entry_int(entry.get("lineCount"))
    if line_count:
        return line_count
    return _repo_entry_int(entry.get("visibleLineCount"))


def _repo_entry_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _repo_city_height(file_count: int, dir_count: int, touched_count: int, line_count: int) -> float:
    visible_units = max(1, file_count + dir_count)
    line_boost = min(0.24, line_count * 0.006)
    return round(0.12 + min(0.36, visible_units * 0.050) + line_boost + min(0.24, touched_count * 0.08), 3)


def _repo_child_city_height(path: str, touched_count: int, line_count: int) -> float:
    path_boost = min(0.10, len(path) * 0.002)
    line_boost = min(0.16, line_count * 0.005)
    return round(0.075 + path_boost + line_boost + touched_count * 0.055, 3)


def _repo_touch_count(path: str, touched_paths: Sequence[str]) -> int:
    return sum(1 for touched_path in touched_paths if touched_path == path or touched_path.startswith(f"{path}/"))


def _repo_city_focus_block_id(block_paths: Sequence[tuple[str, str]], touched_paths: Sequence[str]) -> str:
    for touched_path in touched_paths:
        matches = [
            (path, block_id)
            for path, block_id in block_paths
            if path != "." and (touched_path == path or touched_path.startswith(f"{path}/"))
        ]
        if matches:
            return max(matches, key=lambda item: len(item[0]))[1]
    return "repo-city-root"


def _repo_city_block_id(path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", path).strip("-")[:56]
    return f"repo-city-{slug or 'root'}"


def _repo_node_label(path: str) -> str:
    return path.rsplit("/", 1)[-1][:18] or path[:18]


def _repo_entry_tone(kind: str) -> str:
    if kind == "dir":
        return "green"
    if kind == "symlink":
        return "amber"
    return "cyan"


def _repo_top_level(path: str) -> str:
    return path.split("/", 1)[0]


_REPO_EXCLUDED_NAMES = {
    ".coverage",
    ".git",
    ".harn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "test-artifacts",
}
_SENSITIVE_PATH_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "auth.json",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "tokens",
    "token",
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_PATH_KEYS = {
    "destinationPath",
    "file",
    "fileName",
    "filePath",
    "filename",
    "filepath",
    "output",
    "outputPath",
    "path",
    "sourcePath",
    "targetPath",
}
_COMMAND_KEYS = {"cmd", "command", "shellCommand"}
_COMMAND_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_./-])(?:\.{0,2}/)?[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+")
_MAX_REPO_LINE_COUNT_BYTES = 256_000


def _repo_topology_context(config: RendererContextConfig) -> dict[str, Any]:
    root = _project_root(config)
    payload: dict[str, Any] = {
        "schema": "harn-gibson.repo-topology.v1",
        "rootName": root.name or root.as_posix(),
        "maxEntries": max(0, config.max_repo_entries),
        "maxChildrenPerDir": max(0, config.max_repo_children_per_dir),
    }
    if not root.is_dir():
        return {**payload, "available": False, "reason": "project root is not a directory", "entries": []}
    entries, truncated = _repo_entries(root, config)
    return {
        **payload,
        "available": True,
        "entries": entries,
        "entryCount": len(entries),
        "truncated": truncated,
    }


def _project_root(config: RendererContextConfig) -> Path:
    if config.project_root:
        return Path(config.project_root).expanduser().resolve()
    return Path.cwd().resolve()


def _repo_entries(root: Path, config: RendererContextConfig) -> tuple[list[dict[str, Any]], bool]:
    max_entries = max(0, config.max_repo_entries)
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in sorted(root.iterdir(), key=_repo_sort_key):
        if _skip_repo_path(child.name):
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        entries.append(_repo_entry(child, root, config))
    return entries, truncated


def _repo_entry(path: Path, root: Path, config: RendererContextConfig) -> dict[str, Any]:
    kind = _repo_path_kind(path)
    entry: dict[str, Any] = {"path": _relative_repo_path(path, root), "name": path.name, "kind": kind}
    if kind == "file" and path.suffix:
        entry["extension"] = path.suffix
    if kind == "file":
        entry["lineCount"] = _repo_file_line_count(path)
    if kind == "dir":
        children, truncated = _repo_child_entries(path, root, config)
        file_count, dir_count, line_count, summary_truncated = _repo_directory_visible_counts(path, config)
        entry["visibleFileCount"] = file_count
        entry["visibleDirCount"] = dir_count
        entry["visibleLineCount"] = line_count
        if children:
            entry["children"] = children
        if truncated:
            entry["childrenTruncated"] = True
        if summary_truncated:
            entry["summaryTruncated"] = True
    return entry


def _repo_child_entries(path: Path, root: Path, config: RendererContextConfig) -> tuple[list[dict[str, Any]], bool]:
    max_children = max(0, config.max_repo_children_per_dir)
    children: list[dict[str, Any]] = []
    truncated = False
    for child in sorted(path.iterdir(), key=_repo_sort_key):
        if _skip_repo_path(child.name):
            continue
        if len(children) >= max_children:
            truncated = True
            break
        child_kind = _repo_path_kind(child)
        child_entry = {"path": _relative_repo_path(child, root), "name": child.name, "kind": child_kind}
        if child_kind == "file" and child.suffix:
            child_entry["extension"] = child.suffix
        if child_kind == "file":
            child_entry["lineCount"] = _repo_file_line_count(child)
        if child_kind == "dir":
            file_count, dir_count, line_count, summary_truncated = _repo_directory_visible_counts(child, config)
            child_entry["visibleFileCount"] = file_count
            child_entry["visibleDirCount"] = dir_count
            child_entry["visibleLineCount"] = line_count
            if summary_truncated:
                child_entry["summaryTruncated"] = True
        children.append(child_entry)
    return children, truncated


def _repo_directory_visible_counts(path: Path, config: RendererContextConfig) -> tuple[int, int, int, bool]:
    max_children = max(0, config.max_repo_children_per_dir)
    file_count = 0
    dir_count = 0
    line_count = 0
    visited = 0
    truncated = False
    for child in sorted(path.iterdir(), key=_repo_sort_key):
        if _skip_repo_path(child.name):
            continue
        if visited >= max_children:
            truncated = True
            break
        visited += 1
        child_kind = _repo_path_kind(child)
        if child_kind == "dir":
            dir_count += 1
            continue
        file_count += 1
        if child_kind == "file":
            line_count += _repo_file_line_count(child) or 0
    return file_count, dir_count, line_count, truncated


def _repo_file_line_count(path: Path) -> int | None:
    if path.is_symlink():
        return None
    if path.stat().st_size > _MAX_REPO_LINE_COUNT_BYTES:
        return None
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _relative_repo_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _repo_path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    return "dir" if path.is_dir() else "file"


def _repo_sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() and not path.is_symlink() else 1, path.name.lower())


def _skip_repo_path(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _REPO_EXCLUDED_NAMES
        or lowered in _SENSITIVE_PATH_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(_SENSITIVE_SUFFIXES)
    )


def _touched_files_context(batch: RenderInputBatch, config: RendererContextConfig) -> dict[str, Any]:
    touched: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    max_files = max(0, config.max_touched_files)
    for request in batch.requests:
        for path, source in _event_touched_paths(request.event, config):
            current = by_path.get(path)
            if current is None:
                current = {
                    "path": path,
                    "operation": _operation_for_event(request.event),
                    "firstSequence": request.event.sequence,
                    "lastSequence": request.event.sequence,
                    "phases": [],
                    "sources": [],
                }
                by_path[path] = current
                touched.append(current)
            current["lastSequence"] = request.event.sequence
            _append_unique(current["phases"], request.event.phase)
            _append_unique(current["sources"], source)
    files = touched[:max_files]
    return {
        "schema": "harn-gibson.touched-files.v1",
        "files": files,
        "count": len(touched),
        "truncated": len(touched) > max_files,
    }


def _event_touched_paths(event: GibsonEvent, config: RendererContextConfig) -> tuple[tuple[str, str], ...]:
    paths: list[tuple[str, str]] = []
    _collect_touched_paths(event.payload, config, paths, ())
    return tuple(paths)


def _collect_touched_paths(
    value: Any,
    config: RendererContextConfig,
    paths: list[tuple[str, str]],
    key_path: tuple[str, ...],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered_key = str(key)
            child_path = (*key_path, rendered_key)
            if rendered_key in _PATH_KEYS:
                _collect_path_values(child, config, paths, ".".join(child_path))
            elif rendered_key in _COMMAND_KEYS and isinstance(child, str):
                _collect_command_paths(child, config, paths, ".".join(child_path))
            else:
                _collect_touched_paths(child, config, paths, child_path)
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_touched_paths(child, config, paths, (*key_path, str(index)))


def _collect_path_values(value: Any, config: RendererContextConfig, paths: list[tuple[str, str]], source: str) -> None:
    if isinstance(value, str):
        normalized = _normalize_repo_path(value, config)
        if normalized is not None:
            paths.append((normalized, source))
        return
    if isinstance(value, Mapping):
        _collect_touched_paths(value, config, paths, (source,))
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_path_values(child, config, paths, f"{source}.{index}")


def _collect_command_paths(
    command: str,
    config: RendererContextConfig,
    paths: list[tuple[str, str]],
    source: str,
) -> None:
    for match in _COMMAND_PATH_PATTERN.finditer(command):
        normalized = _normalize_repo_path(match.group(0), config)
        if normalized is not None:
            paths.append((normalized, source))


def _normalize_repo_path(value: str, config: RendererContextConfig) -> str | None:
    text = value.strip().strip("'\"`")
    if not text or "\n" in text or "://" in text:
        return None
    root = _project_root(config)
    if Path(text).is_absolute():
        try:
            path = Path(text).expanduser().resolve().relative_to(root)
        except ValueError:
            return None
    else:
        path = PurePosixPath(text)
    if any(part in {"", ".", ".."} or _skip_repo_path(part) for part in path.parts):
        return None
    rendered = path.as_posix()
    return rendered[: config.max_touched_path_chars]


def _operation_for_event(event: GibsonEvent) -> str:
    tool_name = event.payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        return f"{tool_name}:{event.phase}"
    return f"{event.event_type}:{event.phase}"


def _render_plan_summary(plan: RenderPlan) -> dict[str, Any]:
    mutation_count = sum(len(step.mutations) for step in plan.steps)
    render_intent = render_intent_from_plan(plan)
    return {
        "renderer": plan.metadata.get("renderer", "unknown"),
        "intent": render_intent["intent"],
        "requestCount": len(plan.requests),
        "stepCount": len(plan.steps),
        "mutationCount": mutation_count,
        "eventTypes": [request.event.event_type for request in plan.requests],
        "routes": sorted({request.route for request in plan.requests}),
        "renderIntent": render_intent,
        "metadata": plan.metadata,
    }


def render_intent_from_plan(
    plan: RenderPlan,
    render_input: RenderInputBatch | None = None,
) -> dict[str, Any]:
    input_batch = render_input or RenderInputBatch.from_requests(
        plan.requests,
        route=plan.requests[-1].route if plan.requests else "renderer_agent",
    )
    mutation_count = sum(len(step.mutations) for step in plan.steps)
    effects: list[str] = []
    targets: list[str] = []
    for step in plan.steps:
        for mutation in step.mutations:
            _append_unique(effects, _mutation_effect_label(mutation))
            target = _mutation_target_id(mutation)
            if target is not None:
                _append_unique(targets, target)
    metadata = dict(plan.metadata)
    intent = metadata.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        intent = _default_plan_intent(plan)
    return {
        "schema": "harn-gibson.render-intent.v1",
        "renderer": str(metadata.get("renderer") or "unknown"),
        "intent": intent,
        "requestCount": len(plan.requests),
        "stepCount": len(plan.steps),
        "mutationCount": mutation_count,
        "eventTypes": [request.event.event_type for request in plan.requests],
        "routes": sorted({request.route for request in plan.requests}),
        "timeline": input_batch.timeline.to_dict(),
        "effects": effects,
        "targets": targets,
        "metadata": metadata,
    }


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _mutation_effect_label(mutation: SceneMutation) -> str:
    if mutation.animation is not None:
        return f"animation:{mutation.animation.kind}"
    if mutation.primitive is not None:
        return f"primitive:{mutation.primitive.kind}"
    return mutation.op


def _mutation_target_id(mutation: SceneMutation) -> str | None:
    if mutation.target_id is not None:
        return mutation.target_id
    if mutation.animation is not None:
        return mutation.animation.target_id
    if mutation.primitive is not None:
        return mutation.primitive.id
    return None


def _default_plan_intent(plan: RenderPlan) -> str:
    event_types = []
    for request in plan.requests:
        if request.event.event_type not in event_types:
            event_types.append(request.event.event_type)
    if not event_types:
        return "render idle scene"
    return f"visualize {' + '.join(event_types)}"


def render_update_payload(
    plan: RenderPlan,
    step: RenderStep,
    step_index: int,
    request: RenderRequest,
    scene: SceneState,
    render_input: RenderInputBatch | None = None,
    render_intent: dict[str, Any] | None = None,
    step_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    update = scene_update_payload(request.event, step.mutations, scene)
    render_input = render_input or RenderInputBatch.from_requests(plan.requests, route=request.route)
    render_intent = render_intent or render_intent_from_plan(plan, render_input)
    update["renderPlan"] = {
        "stepIndex": step_index,
        "stepCount": len(plan.steps),
        "batchSize": len(plan.requests),
        "timeline": render_input.timeline.to_dict(),
        "intent": render_intent,
        "metadata": plan.metadata,
    }
    if step_schedule is not None:
        update["renderPlan"]["stepSchedule"] = step_schedule
    update["renderIntent"] = render_intent
    update["events"] = [current.event.to_dict() for current in plan.requests]
    update["renderInput"] = render_input.to_dict()
    update["renderRequests"] = [current.to_dict() for current in plan.requests]
    if request.decisions:
        update["decisions"] = list(request.decisions)
    return update


def render_accept_payload(result: RenderSubmitResult, current_revision: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "renderMode": result.mode,
        "sceneRevision": result.scene_revision if result.scene_revision is not None else current_revision,
    }
    if result.mode == "async":
        payload["pendingRenderJobs"] = result.queued
    return payload


def decisions_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(decision for decision in payload.get("decisions", ()) if isinstance(decision, dict))


def coerce_render_mode(value: str | None) -> RenderMode:
    return "async" if value == "async" else "blocking"


def coerce_render_timing_mode(value: str | None) -> RenderTimingMode:
    return "scheduled" if value == "scheduled" else "immediate"


def coerce_batch_window_ms(value: str | None) -> int:
    if value is None:
        return 40
    try:
        return max(0, int(value))
    except ValueError:
        return 40
