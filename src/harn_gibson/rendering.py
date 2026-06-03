"""Render pipeline for blocking and asynchronous scene updates."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from harn_gibson.catalog import VisualCatalog, default_visual_catalog
from harn_gibson.events import GibsonEvent
from harn_gibson.scene import SceneEngine, SceneMutation, SceneState, default_mutations_for_event, scene_update_payload
from harn_gibson.sinks import EventBuffer

RenderMode = Literal["blocking", "async"]


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
    display_style: str = "gibson"
    compaction_interval_events: int = 40
    max_recent_plans: int = 6
    max_recent_log_entries: int = 12
    max_prop_preview_chars: int = 240


@dataclass(frozen=True, slots=True)
class RendererContext:
    mode: Literal["rolling", "compaction"]
    project: dict[str, Any]
    catalog: dict[str, Any]
    scene: dict[str, Any]
    render_input: dict[str, Any]
    recent_agent_context: tuple[str, ...] = ()
    visualization_context: tuple[dict[str, Any], ...] = ()
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
        return RendererContext(
            mode=mode,
            project=self._project_metadata(),
            catalog=_catalog_context(catalog, full=mode == "compaction"),
            scene=_scene_context(scene, full=mode == "compaction", config=self.config),
            render_input=batch.to_dict(),
            recent_agent_context=_recent_agent_context(batch),
            visualization_context=tuple(self._history[-self.config.max_recent_plans :]),
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

    def _project_metadata(self) -> dict[str, Any]:
        return {
            "name": self.config.project_name,
            "displayStyle": self.config.display_style,
            "schemas": {
                "catalog": "harn-gibson.visual-catalog.v1",
                "rendererContext": "harn-gibson.renderer-context.v1",
                "renderInput": "harn-gibson.render-input.v1",
                "renderPlan": "harn-gibson.render-plan.v1",
                "scene": "harn-gibson.scene.v1",
            },
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
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in {"blocking", "async"}:
            raise ValueError("render mode must be blocking or async")
        self.scene = scene
        self.buffer = buffer
        self.renderer = renderer or DeterministicSceneRenderer()
        self.catalog = catalog or default_visual_catalog()
        self.context_builder = context_builder or RendererContextBuilder()
        self.mode = mode
        self.batch_window_ms = max(0, batch_window_ms)
        self._sleep = sleep_fn
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
            render_with_context = getattr(self.renderer, "render_with_context", None)
            if callable(render_with_context):
                plan = render_with_context(batch.requests, self.scene.state, context)
            else:
                plan = self.renderer.render(batch.requests, self.scene.state)
            return self._apply_plan(plan)

    def _apply_plan(self, plan: RenderPlan) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for index, step in enumerate(plan.steps):
            if step.delay_ms > 0:
                self._sleep(step.delay_ms / 1000)
            request = plan.request_for_step(step)
            scene = self.scene.apply(step.mutations)
            update = render_update_payload(plan, step, index, request, scene)
            self.buffer.publish(update)
            updates.append(update)
        self.context_builder.record_plan(plan)
        return updates


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


def _render_plan_summary(plan: RenderPlan) -> dict[str, Any]:
    mutation_count = sum(len(step.mutations) for step in plan.steps)
    return {
        "renderer": plan.metadata.get("renderer", "unknown"),
        "requestCount": len(plan.requests),
        "stepCount": len(plan.steps),
        "mutationCount": mutation_count,
        "eventTypes": [request.event.event_type for request in plan.requests],
        "routes": sorted({request.route for request in plan.requests}),
        "metadata": plan.metadata,
    }


def render_update_payload(
    plan: RenderPlan,
    step: RenderStep,
    step_index: int,
    request: RenderRequest,
    scene: SceneState,
) -> dict[str, Any]:
    update = scene_update_payload(request.event, step.mutations, scene)
    render_input = RenderInputBatch.from_requests(plan.requests, route=request.route)
    update["renderPlan"] = {
        "stepIndex": step_index,
        "stepCount": len(plan.steps),
        "batchSize": len(plan.requests),
        "timeline": render_input.timeline.to_dict(),
        "metadata": plan.metadata,
    }
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


def coerce_batch_window_ms(value: str | None) -> int:
    if value is None:
        return 40
    try:
        return max(0, int(value))
    except ValueError:
        return 40
