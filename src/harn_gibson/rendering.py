"""Render pipeline for blocking and asynchronous scene updates."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
    project_root: str | None = None
    display_style: str = "gibson"
    compaction_interval_events: int = 40
    max_recent_plans: int = 6
    max_recent_log_entries: int = 12
    max_prop_preview_chars: int = 240
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
            project=self._project_metadata(batch),
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

    def _project_metadata(self, batch: RenderInputBatch) -> dict[str, Any]:
        return {
            "name": self.config.project_name,
            "displayStyle": self.config.display_style,
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
        render_input = RenderInputBatch.from_requests(
            plan.requests,
            route=plan.requests[-1].route if plan.requests else "renderer_agent",
        )
        render_intent = render_intent_from_plan(plan, render_input)
        self.scene.record_render_intent(render_intent)
        for index, step in enumerate(plan.steps):
            if step.delay_ms > 0:
                self._sleep(step.delay_ms / 1000)
            request = plan.request_for_step(step)
            scene = self.scene.apply(step.mutations)
            update = render_update_payload(plan, step, index, request, scene, render_input, render_intent)
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


def _repo_visual_mutations(context: RendererContext, event: GibsonEvent) -> tuple[SceneMutation, ...]:
    topology = context.project.get("repoTopology")
    touched = context.project.get("touchedFiles")
    repo_entries = _repo_visual_entries(topology)
    touched_files = _repo_visual_touched_files(touched)
    if not repo_entries and not touched_files:
        return ()
    graph_props = _repo_graph_props(topology, repo_entries, touched_files, event)
    mutations = [
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id="repo-map",
                kind="node_graph",
                region="stage",
                props=graph_props,
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
    if kind == "dir":
        children, truncated = _repo_child_entries(path, root, config)
        if children:
            entry["children"] = children
        if truncated:
            entry["childrenTruncated"] = True
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
        child_entry = {"path": _relative_repo_path(child, root), "name": child.name, "kind": _repo_path_kind(child)}
        if child_entry["kind"] == "file" and child.suffix:
            child_entry["extension"] = child.suffix
        children.append(child_entry)
    return children, truncated


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


def coerce_batch_window_ms(value: str | None) -> int:
    if value is None:
        return 40
    try:
        return max(0, int(value))
    except ValueError:
        return 40
