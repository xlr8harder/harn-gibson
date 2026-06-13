"""Local browser display server for harn-gibson."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from harn_gibson.catalog import VisualCatalog, default_visual_catalog
from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.external_renderer import external_renderer_from_env
from harn_gibson.model_renderer import model_renderer_from_env
from harn_gibson.projection import PROJECTION_SCHEMA, ProjectionSceneRenderer, load_projection_spec
from harn_gibson.renderers import direct_renderer_command, normalize_renderer
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RendererContextBuilder,
    RendererContextConfig,
    RenderMode,
    RenderPipeline,
    RenderSubmitResult,
    RenderTimingMode,
    SceneRenderer,
    coerce_batch_window_ms,
    coerce_context_flag,
    coerce_context_limit,
    coerce_render_mode,
    coerce_render_timing_mode,
    render_accept_payload,
)
from harn_gibson.routing import (
    EventRouter,
    EventRouteRule,
    RendererEventInterest,
    event_route_rules_from_value,
    renderer_event_interest_from_renderer,
    renderer_event_interest_from_value,
)
from harn_gibson.scene import (
    SCENE_MUTATION_OPS,
    SceneEngine,
    SceneMutation,
    apply_style_to_scene,
    initial_scene,
)
from harn_gibson.sinks import EventBuffer
from harn_gibson.styles import DEFAULT_STYLE_ID, STYLE_PACKS, StylePack, default_style_pack, style_pack_from_name

CORE_PRIMITIVE_KINDS = ("viewport", "status", "feed", "code", "grid")
INPUT_DELIVERY_KINDS = ("followUp", "steer")
SUPPORTED_RENDER_MODES = ("blocking", "async")
SUPPORTED_RENDER_TIMING_MODES = ("immediate", "scheduled")


class GibsonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@dataclass(slots=True)
class GibsonServerState:
    buffer: EventBuffer = field(default_factory=EventBuffer)
    scene: SceneEngine = field(default_factory=SceneEngine)
    catalog: VisualCatalog = field(default_factory=default_visual_catalog)
    style_pack: StylePack = field(default_factory=default_style_pack)
    inputs: BrowserInputQueue = field(default_factory=lambda: BrowserInputQueue())
    input_bridge: HarnBridgeState = field(default_factory=lambda: HarnBridgeState())
    router: EventRouter = field(default_factory=EventRouter)
    renderer_interest: RendererEventInterest | None = None
    render_mode: RenderMode = "blocking"
    render_batch_window_ms: int = 40
    render_timing_mode: RenderTimingMode = "immediate"
    renderer: SceneRenderer = field(default_factory=DeterministicSceneRenderer)
    project_name: str = "harn-gibson"
    project_root: str | None = None
    renderer_context_config: RendererContextConfig = field(default_factory=RendererContextConfig)
    replay_control: ReplayControl | None = None
    pipeline: RenderPipeline = field(init=False)

    def __post_init__(self) -> None:
        style_payload = self.style_pack.to_dict()
        if self.style_pack.id != DEFAULT_STYLE_ID:
            self.scene.configure_initial_scene(lambda: initial_scene(style_payload))
            apply_style_to_scene(self.scene.state, style_payload)
        renderer_interest = self.renderer_interest or renderer_event_interest_from_renderer(self.renderer)
        if renderer_interest is not None and self.router.renderer_interest is None:
            self.router.renderer_interest = renderer_interest
        context_config = replace(
            self.renderer_context_config,
            project_name=self.project_name,
            project_root=self.project_root,
            display_style=self.style_pack.id,
            style_pack=style_payload,
        )
        self.pipeline = RenderPipeline(
            scene=self.scene,
            buffer=self.buffer,
            renderer=self.renderer,
            catalog=self.catalog,
            context_builder=RendererContextBuilder(context_config),
            mode=self.render_mode,
            batch_window_ms=self.render_batch_window_ms,
            timing_mode=self.render_timing_mode,
        )


@dataclass(slots=True)
class ReplayControl:
    """Registered by watch-replay: lets the browser (or anything else) re-run
    the loaded replay against a freshly reset session."""

    description: str
    runner: Callable[[], None]
    runs: int = 0
    _thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def restart(self) -> bool:
        if self.running:
            return False
        self.runs += 1
        self._thread = threading.Thread(target=self.runner, name="harn-gibson-replay-restart", daemon=True)
        self._thread.start()
        return True


def reset_session(state: GibsonServerState) -> None:
    """Fresh perception/world models and scene under the same config, so a
    replay restart actually replays (instead of every event deduping away)."""
    pipeline = state.pipeline
    pipeline.context_builder = RendererContextBuilder(pipeline.context_builder.config)
    reset = getattr(pipeline.renderer, "reset", None)
    if callable(reset):
        reset()
    # stream buffers accumulate assistant text per stream id; without this a
    # replayed session re-appends every chunk onto the previous run's text
    state.router.stream_buffers.clear()
    state.scene.apply((SceneMutation(op="reset_scene"),), now_ms=0)


def replay_status_payload(state: GibsonServerState) -> dict[str, Any]:
    control = state.replay_control
    if control is None:
        return {"available": False}
    return {
        "available": True,
        "description": control.description,
        "runs": control.runs,
        "running": control.running,
    }


@dataclass(frozen=True, slots=True)
class BrowserInput:
    id: str
    sequence: int
    message: str
    deliver_as: str = "followUp"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "message": self.message,
            "deliverAs": self.deliver_as,
        }


@dataclass(slots=True)
class BrowserInputQueue:
    _items: queue.Queue[BrowserInput] = field(default_factory=queue.Queue)
    _sequence: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def enqueue(self, message: str, deliver_as: str = "followUp") -> BrowserInput:
        text = message.strip()
        if not text:
            raise ValueError("message cannot be empty")
        if deliver_as not in INPUT_DELIVERY_KINDS:
            raise ValueError("deliverAs must be followUp or steer")
        with self._lock:
            self._sequence += 1
            item = BrowserInput(
                id=f"input-{self._sequence}",
                sequence=self._sequence,
                message=text,
                deliver_as=deliver_as,
            )
        self._items.put(item)
        return item

    def pop(self) -> BrowserInput | None:
        try:
            return self._items.get_nowait()
        except queue.Empty:
            return None

    def pending_count(self) -> int:
        return self._items.qsize()


@dataclass(slots=True)
class HarnBridgeState:
    connected_window_ms: int = 3000
    poll_count: int = 0
    delivered_inputs: int = 0
    last_input_poll_ms: int | None = None
    last_input_delivery_ms: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_input_poll(self, *, delivered: bool, timestamp_ms: int | None = None) -> None:
        current_ms = _now_ms() if timestamp_ms is None else timestamp_ms
        with self._lock:
            self.poll_count += 1
            self.last_input_poll_ms = current_ms
            if delivered:
                self.delivered_inputs += 1
                self.last_input_delivery_ms = current_ms

    def snapshot(self, *, pending_inputs: int, timestamp_ms: int | None = None) -> dict[str, Any]:
        current_ms = _now_ms() if timestamp_ms is None else timestamp_ms
        with self._lock:
            last_poll = self.last_input_poll_ms
            last_delivery = self.last_input_delivery_ms
            poll_count = self.poll_count
            delivered_inputs = self.delivered_inputs
        poll_age = None if last_poll is None else max(0, current_ms - last_poll)
        return {
            "pendingInputs": pending_inputs,
            "inputPollerSeen": last_poll is not None,
            "inputPollerConnected": poll_age is not None and poll_age <= self.connected_window_ms,
            "lastInputPollMs": last_poll,
            "lastInputPollAgeMs": poll_age,
            "lastInputDeliveryMs": last_delivery,
            "pollCount": poll_count,
            "deliveredInputs": delivered_inputs,
        }


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    state: GibsonServerState | None = None,
) -> ThreadingHTTPServer:
    return GibsonHTTPServer((host, port), make_handler(state or build_state_from_env()))


def run_server(host: str = "127.0.0.1", port: int = 8765, *, style: str | None = None) -> None:  # pragma: no cover
    if style is None:
        state = build_state_from_env()
    else:
        state = build_state_from_env({**dict(environ), "HARN_GIBSON_STYLE": style})
    server = create_server(host, port, state)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        state.pipeline.stop()
        server.server_close()


def build_state_from_env(env: dict[str, str] | None = None) -> GibsonServerState:
    source = environ if env is None else env
    project_root = project_root_from_env(source.get("HARN_GIBSON_PROJECT_ROOT"))
    renderer_interest = renderer_interest_from_env(source.get("HARN_GIBSON_RENDERER_INTEREST"))
    selected_renderer = selected_renderer_from_env(
        source.get("HARN_GIBSON_RENDERER"),
        source.get("HARN_GIBSON_RENDERER_TIMEOUT_MS"),
    )
    model_renderer = model_renderer_from_env(
        source.get("HARN_GIBSON_RENDERER_MODEL_COMMAND"),
        source.get("HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS") or source.get("HARN_GIBSON_RENDERER_TIMEOUT_MS"),
    )
    renderer = external_renderer_from_env(
        source.get("HARN_GIBSON_RENDERER_COMMAND"),
        source.get("HARN_GIBSON_RENDERER_TIMEOUT_MS"),
    )
    return GibsonServerState(
        router=EventRouter(route_rules=route_rules_from_env(source.get("HARN_GIBSON_ROUTE_RULES"))),
        renderer_interest=renderer_interest,
        render_mode=coerce_render_mode(source.get("HARN_GIBSON_RENDER_MODE")),
        render_batch_window_ms=coerce_batch_window_ms(source.get("HARN_GIBSON_RENDER_BATCH_MS")),
        render_timing_mode=coerce_render_timing_mode(source.get("HARN_GIBSON_RENDER_TIMING")),
        renderer=selected_renderer or model_renderer or renderer or DeterministicSceneRenderer(),
        style_pack=style_pack_from_name(source.get("HARN_GIBSON_STYLE")),
        project_name=project_name_from_env(source.get("HARN_GIBSON_PROJECT_NAME"), project_root),
        project_root=project_root,
        renderer_context_config=renderer_context_config_from_env(source),
    )


def selected_renderer_from_env(value: str | None, timeout_ms: str | None = None) -> SceneRenderer | None:
    renderer = normalize_renderer(value, default=None)
    if renderer is None:
        return None
    if renderer == "default":
        return ProjectionSceneRenderer({})
    command = direct_renderer_command(renderer)
    if command is not None:
        return external_renderer_from_env(command, timeout_ms)
    return ProjectionSceneRenderer(load_projection_spec(renderer))


def project_root_from_env(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value


def project_name_from_env(value: str | None, project_root: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    if project_root:
        return str(Path(project_root).expanduser().resolve().name or "workspace")
    return "harn-gibson"


def renderer_context_config_from_env(source: Mapping[str, str]) -> RendererContextConfig:
    defaults = RendererContextConfig()
    return RendererContextConfig(
        compaction_interval_events=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_COMPACTION_EVENTS"),
            defaults.compaction_interval_events,
            minimum=1,
        ),
        max_recent_plans=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_RECENT_PLANS"),
            defaults.max_recent_plans,
        ),
        max_recent_log_entries=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_RECENT_LOG_ENTRIES"),
            defaults.max_recent_log_entries,
        ),
        max_prop_preview_chars=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS"),
            defaults.max_prop_preview_chars,
        ),
        max_visual_anchors=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_VISUAL_ANCHORS"),
            defaults.max_visual_anchors,
        ),
        max_visual_objects_per_anchor=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_VISUAL_OBJECTS_PER_ANCHOR"),
            defaults.max_visual_objects_per_anchor,
        ),
        max_visual_recent_items=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_VISUAL_RECENT_ITEMS"),
            defaults.max_visual_recent_items,
        ),
        max_repo_entries=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES"),
            defaults.max_repo_entries,
        ),
        max_repo_children_per_dir=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_REPO_CHILDREN"),
            defaults.max_repo_children_per_dir,
        ),
        max_touched_files=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_TOUCHED_FILES"),
            defaults.max_touched_files,
        ),
        max_touched_path_chars=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_TOUCHED_PATH_CHARS"),
            defaults.max_touched_path_chars,
        ),
        max_world_entities=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_WORLD_ENTITIES"),
            defaults.max_world_entities,
        ),
        perception_discovery=(
            "stream"
            if (source.get("HARN_GIBSON_PERCEPTION_DISCOVERY") or "").strip().lower() == "stream"
            else "workspace"
        ),
        include_semantic_graph=coerce_context_flag(
            source.get("HARN_GIBSON_RENDERER_SEMANTIC_GRAPH"),
            defaults.include_semantic_graph,
        ),
        max_semantic_files=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_SEMANTIC_FILES"),
            defaults.max_semantic_files,
        ),
        max_semantic_edges=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_SEMANTIC_EDGES"),
            defaults.max_semantic_edges,
        ),
        max_semantic_symbols=coerce_context_limit(
            source.get("HARN_GIBSON_RENDERER_MAX_SEMANTIC_SYMBOLS"),
            defaults.max_semantic_symbols,
        ),
    )


def route_rules_from_env(value: str | None) -> tuple[EventRouteRule, ...]:
    if not value:
        return ()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("HARN_GIBSON_ROUTE_RULES must be a JSON list") from error
    try:
        return event_route_rules_from_value(payload)
    except ValueError as error:
        raise ValueError(f"HARN_GIBSON_ROUTE_RULES invalid: {error}") from error


def renderer_interest_from_env(value: str | None) -> RendererEventInterest | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("HARN_GIBSON_RENDERER_INTEREST must be a JSON object") from error
    try:
        return renderer_event_interest_from_value(payload)
    except ValueError as error:
        raise ValueError(f"HARN_GIBSON_RENDERER_INTEREST invalid: {error}") from error


def make_handler(state: GibsonServerState) -> type[BaseHTTPRequestHandler]:
    class GibsonRequestHandler(BaseHTTPRequestHandler):
        server_version = "harn-gibson/0.1"

        def do_GET(self) -> None:
            request_path = urlsplit(self.path).path
            if request_path in {"/", "/index.html"}:
                kicker = _escape_html(state.project_name.upper() or "HARN DISPLAY")
                self._write(
                    HTTPStatus.OK,
                    HTML.replace("__HARN_GIBSON_KICKER__", kicker),
                    "text/html; charset=utf-8",
                )
                return
            if request_path == "/assets/app.css":
                self._write(HTTPStatus.OK, CSS, "text/css; charset=utf-8")
                return
            if request_path == "/assets/app.js":
                self._write(HTTPStatus.OK, JS, "application/javascript; charset=utf-8")
                return
            if request_path in {"/health", "/healthz"}:
                self._json(HTTPStatus.OK, health_payload(state))
                return
            if request_path == "/scene":
                self._json(HTTPStatus.OK, state.scene.state.to_dict())
                return
            if request_path == "/catalog":
                self._json(HTTPStatus.OK, state.catalog.to_dict())
                return
            if request_path == "/backend-contract":
                self._json(HTTPStatus.OK, backend_contract_payload(state))
                return
            if request_path == "/projection":
                self._json(HTTPStatus.OK, projection_status_payload(state))
                return
            if request_path == "/replay":
                self._json(HTTPStatus.OK, replay_status_payload(state))
                return
            if request_path == "/input/next":
                item = state.inputs.pop()
                state.input_bridge.record_input_poll(delivered=item is not None)
                if item is None:
                    self._empty(HTTPStatus.NO_CONTENT)
                    return
                self._json(HTTPStatus.OK, item.to_dict())
                return
            if request_path in {"/events", "/events/stream"}:  # pragma: no cover
                self._stream_events()  # pragma: no cover
                return  # pragma: no cover
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            request_path = urlsplit(self.path).path
            if request_path == "/events":
                self._handle_event_post()
                return
            if request_path == "/input":
                self._handle_input_post()
                return
            if request_path == "/projection":
                self._handle_projection_post()
                return
            if request_path == "/replay/restart":
                control = state.replay_control
                if control is None:
                    self._json(HTTPStatus.CONFLICT, {"error": "no replay registered on this display"})
                    return
                restarted = control.restart()
                payload = replay_status_payload(state)
                payload["restarted"] = restarted
                self._json(HTTPStatus.ACCEPTED, payload)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _handle_projection_post(self) -> None:
            spec = self._read_json_payload("projection spec must be an object")
            if spec is None:
                return
            renderer = state.pipeline.renderer
            if not isinstance(renderer, ProjectionSceneRenderer):
                self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "active renderer is not projection-driven; start with HARN_GIBSON_RENDERER=default"},
                )
                return
            renderer.redirect(spec)
            # nudge the pipeline so the new projection lands without waiting
            # for the next agent event
            publish_diagnostic_event(
                state,
                state.scene.state.revision + 1,
                message=f"projection redirected (theme {spec.get('theme') or 'default'})",
                event_type="projection_update",
            )
            self._json(HTTPStatus.ACCEPTED, projection_status_payload(state))

        def _handle_event_post(self) -> None:
            payload = self._read_json_payload("event payload must be an object")
            if payload is None:
                return
            try:
                result = submit_event_to_renderer(payload, state)
            except (KeyError, TypeError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(HTTPStatus.ACCEPTED, render_accept_payload(result, state.scene.state.revision))

        def _handle_input_post(self) -> None:
            payload = self._read_json_payload("input payload must be an object")
            if payload is None:
                return
            try:
                item = enqueue_browser_input(payload, state)
            except (TypeError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            result = submit_event_to_renderer(browser_input_event_payload(item), state)
            response = render_accept_payload(result, state.scene.state.revision)
            response.update(
                {
                    "input": item.to_dict(),
                    "pendingInputs": state.inputs.pending_count(),
                    "inputBridge": state.input_bridge.snapshot(pending_inputs=state.inputs.pending_count()),
                }
            )
            self._json(
                HTTPStatus.ACCEPTED,
                response,
            )

        def _read_json_payload(self, object_error: str) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return None
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": object_error})
                return None
            return payload

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def _stream_events(self) -> None:  # pragma: no cover
            subscriber, unsubscribe = state.buffer.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    event = subscriber.get()
                    self.wfile.write(format_sse(event).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                unsubscribe()

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            self._write(status, json.dumps(payload, separators=(",", ":")), "application/json")

        def _empty(self, status: HTTPStatus) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _write(self, status: HTTPStatus, body: str, content_type: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return GibsonRequestHandler


def enqueue_browser_input(payload: dict[str, Any], state: GibsonServerState) -> BrowserInput:
    message = payload.get("message")
    if not isinstance(message, str):
        raise TypeError("message must be a string")
    deliver_as = payload.get("deliverAs", "followUp")
    if not isinstance(deliver_as, str):
        raise TypeError("deliverAs must be a string")
    return state.inputs.enqueue(message, deliver_as)


def health_payload(state: GibsonServerState) -> dict[str, Any]:
    return {
        "ok": True,
        "projectName": state.project_name,
        "events": len(state.buffer.snapshot()),
        "sceneRevision": state.scene.state.revision,
        "renderMode": state.pipeline.mode,
        "renderTiming": state.pipeline.timing_mode,
        "displayStyle": state.style_pack.id,
        "stylePack": state.style_pack.to_dict(),
        "pendingRenderJobs": state.pipeline.pending_count(),
        "inputBridge": state.input_bridge.snapshot(pending_inputs=state.inputs.pending_count()),
        "streams": state.router.stream_snapshot(),
    }


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def projection_status_payload(state: GibsonServerState) -> dict[str, Any]:
    renderer = state.pipeline.renderer
    if not isinstance(renderer, ProjectionSceneRenderer):
        return {"active": False, "schema": PROJECTION_SCHEMA}
    return {
        "active": True,
        "schema": PROJECTION_SCHEMA,
        "spec": renderer.engine.spec,
        "engineRevision": renderer.engine.revision,
        "sceneRevision": state.scene.state.revision,
    }


def backend_contract_payload(state: GibsonServerState) -> dict[str, Any]:
    catalog = state.catalog.to_dict()
    catalog_primitive_kinds = [str(entry["id"]) for entry in catalog["primitives"]]
    effect_kinds = [str(entry["id"]) for entry in catalog["effects"]]
    supported_primitives = tuple(dict.fromkeys((*CORE_PRIMITIVE_KINDS, *catalog_primitive_kinds)))
    supported_style_packs = [style.to_dict() for style in STYLE_PACKS]
    supported_style_pack_ids = [style["id"] for style in supported_style_packs]
    supported_mutation_ops = list(SCENE_MUTATION_OPS)
    supported_input_deliver_as = list(INPUT_DELIVERY_KINDS)
    supported_render_modes = list(SUPPORTED_RENDER_MODES)
    supported_render_timing_modes = list(SUPPORTED_RENDER_TIMING_MODES)
    supported_primitive_kinds = list(supported_primitives)
    display_backend = {
        "id": "browser-canvas",
        "primary": True,
        "renderTarget": "html-canvas",
        "catalogSupport": "full",
        "styleSupport": "style-pack-v1",
    }
    return {
        "schema": "harn-gibson.display-backend-contract.v1",
        "transport": "http+sse",
        "sceneSchema": "harn-gibson.scene.v1",
        "sceneUpdateSchema": "harn-gibson.scene-update.v1",
        "catalogSchema": catalog["schema"],
        "mutationSchema": "harn-gibson.scene-mutation.v1",
        "inputSchema": "harn-gibson.browser-input.v1",
        "stylePackSchema": "harn-gibson.style-pack.v1",
        "renderInputSchema": "harn-gibson.render-input.v1",
        "renderIntentSchema": "harn-gibson.render-intent.v1",
        "endpoints": {
            "display": {"method": "GET", "path": "/", "contentType": "text/html; charset=utf-8"},
            "health": {"method": "GET", "path": "/health", "payload": "display health JSON"},
            "scene": {"method": "GET", "path": "/scene", "schema": "harn-gibson.scene.v1"},
            "catalog": {"method": "GET", "path": "/catalog", "schema": catalog["schema"]},
            "events": {"method": "POST", "path": "/events", "accepts": "harn-gibson.event.v1"},
            "sceneStream": {
                "method": "GET",
                "path": "/events/stream",
                "contentType": "text/event-stream",
                "data": "JSON scene-update payload per SSE message",
                "schema": "harn-gibson.scene-update.v1",
            },
            "input": {"method": "POST", "path": "/input", "schema": "harn-gibson.browser-input.v1"},
            "inputNext": {"method": "GET", "path": "/input/next", "schema": "harn-gibson.browser-input.v1"},
        },
        "displayBackend": display_backend,
        "corePrimitiveKinds": list(CORE_PRIMITIVE_KINDS),
        "catalogPrimitiveKinds": catalog_primitive_kinds,
        "supportedPrimitiveKinds": supported_primitive_kinds,
        "supportedEffectKinds": effect_kinds,
        "supportedMutationOps": supported_mutation_ops,
        "supportedInputDeliverAs": supported_input_deliver_as,
        "supportedRenderModes": supported_render_modes,
        "supportedRenderTimingModes": supported_render_timing_modes,
        "activeStylePack": state.style_pack.to_dict(),
        "supportedStylePackIds": supported_style_pack_ids,
        "supportedStylePacks": supported_style_packs,
        "capabilityProfile": {
            "schema": "harn-gibson.backend-capability-profile.v1",
            "backendId": display_backend["id"],
            "primitiveLayer": {
                "contract": catalog["schema"],
                "catalogSupport": display_backend["catalogSupport"],
                "supportsCustomPrimitiveLayer": True,
                "customPrimitivePolicy": (
                    "Implement the advertised catalog directly, translate it to a backend-native vocabulary, "
                    "or pair a custom vocabulary with a renderer that targets that vocabulary."
                ),
                "unknownPrimitivePolicy": "preserve-scene-state-render-noop",
                "supportedPrimitiveKinds": supported_primitive_kinds,
                "supportedEffectKinds": effect_kinds,
            },
            "mutationLayer": {
                "schema": "harn-gibson.scene-mutation.v1",
                "supportedOps": supported_mutation_ops,
                "patchSemantics": "shallow-props-merge",
                "sceneSnapshotAuthority": True,
            },
            "timing": {
                "renderModes": supported_render_modes,
                "renderTimingModes": supported_render_timing_modes,
                "supportsRenderStepDelayMs": True,
                "supportsRenderStepStartOffsetMs": True,
                "coalescedBatchTimeline": True,
            },
            "input": {
                "schema": "harn-gibson.browser-input.v1",
                "deliverAs": supported_input_deliver_as,
                "queueEndpoint": "/input",
                "pollEndpoint": "/input/next",
            },
            "style": {
                "schema": "harn-gibson.style-pack.v1",
                "support": display_backend["styleSupport"],
                "activeStylePackId": state.style_pack.id,
                "supportedStylePackIds": supported_style_pack_ids,
            },
        },
        "contracts": {
            "scene": "A full scene snapshot is authoritative for backend state.",
            "sceneUpdate": "Scene updates include the triggering event, mutations, full scene, and render metadata.",
            "mutation": "Scene mutations are state deltas; display backends own drawing and animation loops.",
            "stylePack": (
                "Style packs are presentation hints for tones, canvas backdrop behavior, CSS variables, and motifs; "
                "the selected pack is mirrored in scene metadata."
            ),
            "backend": (
                "A non-web backend may render the full supported primitive set or advertise a subset, "
                "but should preserve SceneState and SceneMutation semantics."
            ),
        },
    }


def browser_input_event_payload(item: BrowserInput) -> dict[str, Any]:
    summary = f"gibson input queued: {_clip(item.message, 96)}"
    return {
        "sequence": item.sequence,
        "timestampMs": int(time.time() * 1000),
        "source": "gibson",
        "eventType": "browser_input",
        "phase": "before",
        "title": "Browser input",
        "summary": summary,
        "payload": item.to_dict(),
    }


def diagnostic_event_payload(
    sequence: int,
    *,
    message: str,
    event_type: str = "launcher_diagnostic",
    source: str = "harn-gibson",
    severity: str = "info",
    title: str | None = None,
    details: str | None = None,
    traceback_text: str | None = None,
) -> dict[str, Any]:
    return diagnostic_event(
        sequence,
        message=message,
        event_type=event_type,
        source=source,
        severity=severity,
        title=title,
        details=details,
        traceback_text=traceback_text,
    ).to_dict()


def publish_diagnostic_event(
    state: GibsonServerState,
    sequence: int,
    *,
    message: str,
    event_type: str = "launcher_diagnostic",
    source: str = "harn-gibson",
    severity: str = "info",
    title: str | None = None,
    details: str | None = None,
    traceback_text: str | None = None,
) -> RenderSubmitResult:
    return submit_event_to_renderer(
        diagnostic_event_payload(
            sequence,
            message=message,
            event_type=event_type,
            source=source,
            severity=severity,
            title=title,
            details=details,
            traceback_text=traceback_text,
        ),
        state,
    )


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _now_ms() -> int:
    return int(time.time() * 1000)


def format_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def submit_event_to_renderer(payload: dict[str, Any], state: GibsonServerState) -> RenderSubmitResult:
    event = event_from_payload(payload)
    route = state.router.route(event)
    if route.dropped:
        return RenderSubmitResult(mode=state.pipeline.mode, queued=state.pipeline.pending_count())
    if not route.uses_renderer:
        renderer = state.pipeline.renderer
        if isinstance(renderer, ProjectionSceneRenderer):
            # the projection owns the stage entirely: streamed chunks feed
            # perception without publishing (a chunk flood must not drown the
            # browser in scene snapshots), and every Nth chunk runs a full
            # resolve as a narration heartbeat so the voice keeps streaming
            renderer.stream_chunk_count = getattr(renderer, "stream_chunk_count", 0) + 1
            if renderer.stream_chunk_count % 8 == 0:
                return state.pipeline.submit(route.request)
            return state.pipeline.apply_direct(
                route.request,
                (),
                metadata={"route": route.decision.to_dict()},
                publish_empty=False,
            )
        return state.pipeline.apply_direct(
            route.request,
            route.direct_mutations,
            metadata={"route": route.decision.to_dict(), "renderInput": route.batch.to_dict()},
        )
    return state.pipeline.submit(route.request)


def apply_event_to_scene(payload: dict[str, Any], state: GibsonServerState) -> dict[str, Any]:
    result = submit_event_to_renderer(payload, state)
    if result.updates:
        return result.updates[-1]
    return render_accept_payload(result, state.scene.state.revision)


def event_from_payload(payload: dict[str, Any]) -> GibsonEvent:
    event_type = payload.get("eventType", payload.get("event_type"))
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event payload missing eventType")
    phase = payload.get("phase")
    if phase not in {"before", "during", "after", "lifecycle"}:
        raise ValueError("event payload has invalid phase")
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        raise ValueError("event payload missing payload object")
    return GibsonEvent(
        sequence=int(payload.get("sequence", 0)),
        timestamp_ms=int(payload.get("timestampMs", payload.get("timestamp_ms", 0))),
        source=str(payload.get("source", "harn")),
        event_type=event_type,
        phase=phase,
        title=str(payload.get("title", event_type)),
        summary=str(payload.get("summary", "")),
        payload=event_payload,
        recent_context=tuple(str(item) for item in payload.get("recentContext", ())),
        visualization_context=tuple(str(item) for item in payload.get("visualizationContext", ())),
    )


HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>harn-gibson</title>
    <link rel="stylesheet" href="/assets/app.css">
  </head>
  <body>
    <main class="shell">
      <section class="stage" aria-label="Gibson visualization">
        <canvas id="grid" width="1400" height="780"></canvas>
        <div class="mast">
          <div>
            <p class="kicker" id="kicker">__HARN_GIBSON_KICKER__</p>
            <h1>GIBSON LINK</h1>
          </div>
          <div class="topbar">
            <button id="replayButton" class="debug-toggle" type="button" hidden>&#8635; REPLAY</button>
            <button id="debugToggle" class="debug-toggle" type="button" aria-expanded="false">DEBUG</button>
            <div id="bridgeStatus" class="status bridge-status">harn bridge idle</div>
            <div id="status" class="status">awaiting signal</div>
          </div>
        </div>
        <div class="signal-copy">
          <span id="signalTitle">CHANNEL IDLE</span>
          <strong id="signalSummary">awaiting harn stream</strong>
        </div>
        <section id="streamPanel" class="stream-panel" aria-label="Stream buffer" hidden>
          <span id="streamTitle">ASSISTANT STREAM</span>
          <pre id="streamText"></pre>
        </section>
        <form id="inputForm" class="composer" autocomplete="off">
          <textarea id="promptInput" rows="2" placeholder="route input to harn"></textarea>
          <div class="composer-actions">
            <select id="deliverAs" aria-label="Input delivery mode">
              <option value="followUp">queue</option>
              <option value="steer">steer</option>
            </select>
            <button type="submit">SEND</button>
          </div>
          <p id="inputStatus" class="input-status">ready</p>
        </form>
      </section>
      <aside id="debugPanel" class="debug-panel" aria-label="Debug stream">
        <div class="debug-drawer-header">
          <span>DEBUG STREAM</span>
          <button id="debugClose" class="debug-toggle" type="button">CLOSE</button>
        </div>
        <div class="panel debug-details">
          <h2>Event Details</h2>
          <dl>
            <div>
              <dt>Phase</dt>
              <dd id="phase">idle</dd>
            </div>
            <div>
              <dt>Event</dt>
              <dd id="eventType">none</dd>
            </div>
            <div>
              <dt>Sequence</dt>
              <dd id="sequence">0</dd>
            </div>
          </dl>
        </div>
        <div class="panel">
          <h2>Event Feed</h2>
          <ol id="feed"></ol>
        </div>
        <div class="panel">
          <h2>Render Intents</h2>
          <pre id="intentLog">[]</pre>
        </div>
        <div class="panel">
          <h2>Tracebacks</h2>
          <pre id="traceLog">[]</pre>
        </div>
      </aside>
    </main>
    <script src="/assets/app.js"></script>
  </body>
</html>
"""

CSS = """
:root {
  color-scheme: dark;
  --bg: #05060a;
  --stage-bg: #070b0f;
  --panel: rgba(13, 18, 25, 0.86);
  --line: rgba(110, 255, 207, 0.22);
  --green: #69ffb8;
  --cyan: #58d7ff;
  --amber: #ffcc66;
  --magenta: #ff5bc8;
  --text: #e8fff8;
  --muted: #8aa69f;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
  font-family: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
}
.shell {
  position: relative;
  min-height: 100vh;
  padding: 18px;
}
.stage {
  position: relative;
  overflow: hidden;
  min-height: calc(100vh - 36px);
  border: 1px solid var(--line);
  background: var(--stage-bg);
  transition: margin-right 160ms ease-out;
}
#grid {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
.mast {
  position: relative;
  z-index: 1;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 24px;
  gap: 16px;
}
.kicker {
  margin: 0 0 8px;
  color: var(--amber);
  font-size: 12px;
}
h1 {
  margin: 0;
  font-size: 42px;
  font-weight: 700;
  letter-spacing: 0;
}
.topbar {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.debug-toggle,
.composer button,
.composer select {
  min-height: 38px;
  border: 1px solid rgba(105, 255, 184, 0.36);
  background: rgba(5, 10, 13, 0.82);
  color: var(--green);
  font: inherit;
}
.debug-toggle,
.composer button {
  padding: 0 12px;
  cursor: pointer;
}
.debug-toggle[aria-expanded="true"] {
  border-color: rgba(255, 204, 102, 0.65);
  color: var(--amber);
}
.debug-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 10px 12px;
}
.debug-drawer-header span {
  color: var(--amber);
  font-size: 12px;
}
.status {
  min-width: 0;
  max-width: 220px;
  padding: 8px 10px;
  border: 1px solid rgba(255, 204, 102, 0.35);
  color: var(--amber);
  text-align: right;
  overflow-wrap: anywhere;
}
.bridge-status {
  color: var(--green);
}
.bridge-status.waiting {
  color: var(--magenta);
}
.signal-copy {
  position: absolute;
  left: 24px;
  right: 24px;
  z-index: 1;
  bottom: 176px;
  max-width: 780px;
  max-height: 94px;
  overflow: hidden;
  pointer-events: none;
}
.stream-panel {
  position: absolute;
  top: 150px;
  right: 24px;
  z-index: 1;
  width: min(430px, calc(100% - 48px));
  max-height: 260px;
  overflow: hidden;
  border: 1px solid rgba(88, 215, 255, 0.32);
  background: rgba(4, 9, 12, 0.78);
  padding: 12px;
}
.stream-panel[hidden] {
  display: none;
}
.stream-panel span {
  display: block;
  margin-bottom: 8px;
  color: var(--amber);
  font-size: 12px;
  text-transform: uppercase;
}
.stream-panel pre {
  margin: 0;
  max-height: 210px;
  overflow: hidden;
  color: var(--green);
  font: inherit;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.signal-copy span,
.panel h2 {
  display: block;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  text-transform: uppercase;
}
.signal-copy strong {
  display: block;
  margin-top: 8px;
  color: var(--cyan);
  font-size: 22px;
  line-height: 1.25;
  display: -webkit-box;
  overflow-wrap: anywhere;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.panel {
  border: 1px solid var(--line);
  background: var(--panel);
}
.composer {
  position: absolute;
  left: 24px;
  right: 24px;
  bottom: 24px;
  z-index: 2;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: stretch;
  border: 1px solid var(--line);
  background: rgba(3, 6, 8, 0.9);
  padding: 10px;
}
.composer textarea {
  min-height: 68px;
  max-height: 160px;
  resize: vertical;
  border: 1px solid rgba(88, 215, 255, 0.28);
  background: rgba(7, 13, 16, 0.88);
  color: var(--text);
  font: inherit;
  line-height: 1.4;
  padding: 10px;
  outline: none;
}
.composer textarea:focus {
  border-color: rgba(88, 215, 255, 0.72);
}
.composer-actions {
  display: grid;
  grid-template-rows: 38px 1fr;
  gap: 8px;
  min-width: 110px;
}
.composer select {
  padding: 0 8px;
}
.input-status {
  grid-column: 1 / -1;
  min-height: 16px;
  margin: -2px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.debug-panel {
  position: fixed;
  top: 18px;
  right: 18px;
  bottom: 18px;
  z-index: 5;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) minmax(96px, 0.38fr) minmax(96px, 0.38fr) minmax(96px, 0.38fr);
  gap: 12px;
  width: min(420px, calc(100vw - 36px));
  transform: translateX(calc(100% + 24px));
  transition: transform 160ms ease-out;
  pointer-events: none;
}
body.debug-open .debug-panel {
  transform: translateX(0);
  pointer-events: auto;
}
@media (min-width: 901px) {
  body.debug-open .stage {
    margin-right: 438px;
  }
}
.panel {
  min-width: 0;
  overflow: hidden;
  padding: 14px;
}
.panel h2 {
  margin: 0 0 12px;
}
.debug-details dl {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}
.debug-details div {
  min-width: 0;
}
.debug-details dt {
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
}
.debug-details dd {
  margin: 5px 0 0;
  color: var(--cyan);
  font-size: 13px;
  overflow-wrap: anywhere;
}
#feed {
  display: flex;
  flex-direction: column-reverse;
  gap: 8px;
  height: calc(100% - 32px);
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}
#feed li {
  border-left: 3px solid var(--green);
  padding: 8px 10px;
  background: rgba(105, 255, 184, 0.08);
}
#feed li.after { border-left-color: var(--magenta); }
#feed li.during { border-left-color: var(--cyan); }
#feed li.lifecycle { border-left-color: var(--amber); }
#feed b {
  display: block;
  margin-bottom: 4px;
  font-size: 12px;
}
#feed span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
#intentLog {
  height: calc(100% - 32px);
  margin: 0;
  overflow: auto;
  color: var(--cyan);
  white-space: pre-wrap;
}
#traceLog {
  height: calc(100% - 32px);
  margin: 0;
  overflow: auto;
  color: var(--magenta);
  white-space: pre-wrap;
}
@media (max-width: 900px) {
  .shell { padding: 10px; }
  .stage { min-height: calc(100vh - 20px); }
  .mast { flex-direction: column; padding: 16px; }
  .topbar {
    width: 100%;
    flex-wrap: wrap;
    justify-content: flex-start;
    align-items: stretch;
  }
  .topbar .status {
    flex: 1 1 120px;
    max-width: none;
    text-align: left;
  }
  .signal-copy {
    left: 16px;
    right: 16px;
    bottom: 214px;
    max-height: 84px;
  }
  .stream-panel {
    top: 208px;
    left: 16px;
    right: 16px;
    width: auto;
    max-height: 180px;
  }
  .stream-panel pre {
    max-height: 132px;
  }
  .signal-copy strong { font-size: 18px; }
  .composer {
    left: 16px;
    right: 16px;
    grid-template-columns: 1fr;
  }
  .composer-actions { grid-template-columns: 1fr 1fr; grid-template-rows: 38px; }
}
"""

JS = """
const canvas = document.getElementById("grid");
const ctx = canvas.getContext("2d");
const feed = document.getElementById("feed");
const statusEl = document.getElementById("status");
const bridgeStatus = document.getElementById("bridgeStatus");
const phaseEl = document.getElementById("phase");
const eventEl = document.getElementById("eventType");
const sequenceEl = document.getElementById("sequence");
const signalTitle = document.getElementById("signalTitle");
const signalSummary = document.getElementById("signalSummary");
const streamPanel = document.getElementById("streamPanel");
const streamTitle = document.getElementById("streamTitle");
const streamText = document.getElementById("streamText");
const intentLog = document.getElementById("intentLog");
const traceLog = document.getElementById("traceLog");
const debugToggle = document.getElementById("debugToggle");
const debugClose = document.getElementById("debugClose");
const inputForm = document.getElementById("inputForm");
const promptInput = document.getElementById("promptInput");
const deliverAs = document.getElementById("deliverAs");
const inputStatus = document.getElementById("inputStatus");
const pulses = [];
const animationClocks = new Map();
const DEFAULT_STYLE_PACK = {
  id: "gibson",
  tones: {
    amber: [255, 204, 102],
    cyan: [88, 215, 255],
    green: [105, 255, 184],
    magenta: [255, 91, 200],
    red: [255, 89, 89],
    white: [230, 255, 248],
  },
  canvas: {
    background: "#05070b",
    gridTone: "cyan",
    gridAlpha: 0.12,
    gridPerspective: 0.32,
    horizonTone: "cyan",
    horizonAlpha: 0,
  },
  cssVars: {},
  motifs: ["city-grid"],
};
let lastQueuedInputId = null;
let currentScene = null;
let currentStylePack = DEFAULT_STYLE_PACK;
const queryParams = new URLSearchParams(location.search);
const captureMode = queryParams.get("capture") === "1";
let drawScheduled = false;
let captureDrawsRemaining = 0;
if (captureMode) window.__gibsonCaptureReady = false;

debugToggle.addEventListener("click", () => {
  const expanded = document.body.classList.toggle("debug-open");
  debugToggle.setAttribute("aria-expanded", String(expanded));
});

const replayButton = document.getElementById("replayButton");
async function initReplayControl() {
  try {
    const response = await fetch("/replay");
    const payload = await response.json();
    replayButton.hidden = !(payload && payload.available);
  } catch (error) {
    /* display is not replay-driven */
  }
}
replayButton.addEventListener("click", async () => {
  replayButton.disabled = true;
  try {
    const response = await fetch("/replay/restart", {method: "POST"});
    if (response.status === 202) {
      const payload = await response.json();
      statusEl.textContent = payload.restarted ? "replay restarting" : "replay already running";
    } else {
      statusEl.textContent = "replay unavailable - reload page";
    }
  } catch (error) {
    statusEl.textContent = "replay failed - server unreachable";
  }
  setTimeout(() => {
    replayButton.disabled = false;
  }, 4000);
});
if (!captureMode) initReplayControl();

debugClose.addEventListener("click", () => {
  document.body.classList.remove("debug-open");
  debugToggle.setAttribute("aria-expanded", "false");
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    inputForm.requestSubmit();
  }
});

inputForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = promptInput.value.trim();
  if (!message) {
    inputStatus.textContent = "empty input";
    return;
  }
  inputStatus.textContent = "queueing";
  try {
    const response = await fetch("/input", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message, deliverAs: deliverAs.value}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "queue failed");
    promptInput.value = "";
    lastQueuedInputId = payload.input.id;
    updateBridgeStatus(payload.inputBridge);
  } catch (error) {
    inputStatus.textContent = error instanceof Error ? error.message : "queue failed";
  }
});

function resize() {
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(320, Math.floor(rect.width * devicePixelRatio));
  canvas.height = Math.max(240, Math.floor(rect.height * devicePixelRatio));
}
addEventListener("resize", resize);
resize();

function scheduleDraw() {
  if (drawScheduled) return;
  drawScheduled = true;
  requestAnimationFrame(draw);
}

function markSceneDirty(drawCount = 1) {
  if (captureMode) {
    window.__gibsonCaptureReady = false;
    captureDrawsRemaining = Math.max(captureDrawsRemaining, drawCount);
  }
  scheduleDraw();
}

function draw() {
  drawScheduled = false;
  const w = canvas.width;
  const h = canvas.height;
  const now = performance.now();
  syncAnimationClocks(currentScene, now);
  const camera = sceneCameraState(currentScene, w, h, now);
  drawBackdrop(w, h, now);
  ctx.save();
  applySceneCamera(camera, w, h);
  drawScenePrimitives(currentScene, w, h, now);
  ctx.restore();
  drawSceneAnimations(currentScene, w, h, now);
  drawPulses(w, h);
  if (captureMode) {
    if (captureDrawsRemaining > 0) captureDrawsRemaining -= 1;
    if (captureDrawsRemaining > 0) {
      scheduleDraw();
    } else if (currentScene) {
      window.__gibsonCaptureReady = true;
    }
  } else {
    scheduleDraw();
  }
}
markSceneDirty();

function drawBackdrop(w, h, now) {
  const canvasStyle = currentStylePack.canvas || DEFAULT_STYLE_PACK.canvas;
  const motifs = new Set(currentStylePack.motifs || []);
  let motifEffectCount = 0;
  ctx.fillStyle = canvasStyle.background || DEFAULT_STYLE_PACK.canvas.background;
  ctx.fillRect(0, 0, w, h);
  const horizonAlpha = Number(canvasStyle.horizonAlpha || 0);
  if (horizonAlpha > 0) {
    const gradient = ctx.createRadialGradient(w * 0.5, h * 0.62, h * 0.05, w * 0.5, h * 0.72, h * 0.62);
    gradient.addColorStop(0, toneColor(canvasStyle.horizonTone || "amber", horizonAlpha));
    gradient.addColorStop(1, toneColor(canvasStyle.horizonTone || "amber", 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);
  }
  ctx.lineWidth = 1;
  ctx.strokeStyle = toneColor(canvasStyle.gridTone || "cyan", Number(canvasStyle.gridAlpha ?? 0.12));
  const step = 42 * devicePixelRatio;
  const perspective = Number(canvasStyle.gridPerspective ?? 0.32);
  for (let x = -step; x < w + step; x += step) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x + w * perspective, h);
    ctx.stroke();
  }
  for (let y = 0; y < h; y += step) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y + h * 0.08);
    ctx.stroke();
  }
  if (motifs.has("packet-routes")) motifEffectCount += drawPacketRouteMotif(w, h, now);
  if (motifs.has("vector-ice")) motifEffectCount += drawVectorIceMotif(w, h, now);
  if (motifs.has("horizon-glow")) motifEffectCount += drawHotlineGridMotif(w, h, now);
  if (motifs.has("chrome-decals")) motifEffectCount += drawChromeDecalMotif(w, h, now);
  if (motifs.has("phosphor-grid")) motifEffectCount += drawPhosphorGridMotif(w, h, now);
  if (motifs.has("audit-frames")) motifEffectCount += drawAuditFrameMotif(w, h, now);
  if (motifs.has("amber-alerts")) motifEffectCount += drawAmberAlertMotif(w, h, now);
  if (motifs.has("orbital-grid")) motifEffectCount += drawOrbitalGridMotif(w, h, now);
  if (motifs.has("radar-sweeps")) motifEffectCount += drawRadarSweepMotif(w, h, now);
  if (motifs.has("warning-chevrons")) motifEffectCount += drawWarningChevronMotif(w, h, now);
  window.__gibsonBackdropState = {
    styleId: currentStylePack.id || "gibson",
    motifs: Array.from(motifs),
    gridTone: canvasStyle.gridTone || "cyan",
    horizonAlpha,
    motifEffectCount,
  };
}

function drawPacketRouteMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("cyan", 0.10);
  ctx.fillStyle = toneColor("amber", 0.34);
  ctx.lineWidth = 1 * devicePixelRatio;
  for (let route = 0; route < 5; route++) {
    const y = h * (0.18 + route * 0.13);
    const startX = w * (0.08 + route * 0.06);
    const endX = w * (0.78 + route * 0.03);
    ctx.beginPath();
    ctx.moveTo(startX, y);
    ctx.bezierCurveTo(w * 0.32, y - h * 0.08, w * 0.55, y + h * 0.06, endX, y - h * 0.03);
    ctx.stroke();
    const progress = (now * 0.00016 + route * 0.21) % 1;
    const packetX = startX + (endX - startX) * progress;
    const packetY = y + Math.sin(progress * Math.PI * 2 + route) * h * 0.035;
    ctx.beginPath();
    ctx.arc(packetX, packetY, 1.8 * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
  return 1;
}

function drawVectorIceMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("white", 0.08);
  ctx.fillStyle = toneColor("cyan", 0.025 + Math.sin(now * 0.002) * 0.006);
  for (let shard = 0; shard < 8; shard++) {
    const side = shard % 2;
    const x = side ? w * (0.82 + (shard % 3) * 0.035) : w * (0.04 + (shard % 3) * 0.025);
    const y = h * (0.16 + shard * 0.09);
    const size = Math.min(w, h) * (0.045 + (shard % 3) * 0.01);
    ctx.beginPath();
    ctx.moveTo(x, y - size);
    ctx.lineTo(x + (side ? size : -size) * 0.72, y + size * 0.15);
    ctx.lineTo(x + (side ? size : -size) * 0.22, y + size);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
  return 1;
}

function drawHotlineGridMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("magenta", 0.08 + Math.sin(now * 0.003) * 0.015);
  ctx.lineWidth = 1.2 * devicePixelRatio;
  for (let line = -4; line < 10; line++) {
    const x = line * w * 0.12 + (now * 0.018) % (w * 0.12);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x + w * 0.24, h);
    ctx.stroke();
  }
  ctx.restore();
  return 1;
}

function drawChromeDecalMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("white", 0.12 + Math.sin(now * 0.004) * 0.02);
  ctx.lineWidth = 1.4 * devicePixelRatio;
  const inset = 18 * devicePixelRatio;
  const length = Math.min(w, h) * 0.12;
  for (const corner of [
    [inset, inset, 1, 1],
    [w - inset, inset, -1, 1],
    [inset, h - inset, 1, -1],
    [w - inset, h - inset, -1, -1],
  ]) {
    const [x, y, dx, dy] = corner;
    ctx.beginPath();
    ctx.moveTo(x, y + dy * length);
    ctx.lineTo(x, y);
    ctx.lineTo(x + dx * length, y);
    ctx.stroke();
  }
  ctx.restore();
  return 1;
}

function drawPhosphorGridMotif(w, h, now) {
  ctx.save();
  ctx.fillStyle = toneColor("green", 0.035 + Math.sin(now * 0.004) * 0.01);
  for (let y = 0; y < h; y += 6 * devicePixelRatio) ctx.fillRect(0, y, w, devicePixelRatio);
  ctx.restore();
  return 1;
}

function drawAuditFrameMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("green", 0.10);
  ctx.lineWidth = 1 * devicePixelRatio;
  const pulse = 0.5 + Math.sin(now * 0.003) * 0.5;
  for (let frame = 0; frame < 3; frame++) {
    const inset = (24 + frame * 28 + pulse * 4) * devicePixelRatio;
    ctx.strokeRect(inset, inset, w - inset * 2, h - inset * 2);
  }
  ctx.restore();
  return 1;
}

function drawAmberAlertMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.fillStyle = toneColor("amber", 0.04 + Math.max(0, Math.sin(now * 0.006)) * 0.025);
  const barHeight = 10 * devicePixelRatio;
  for (let band = 0; band < 3; band++) {
    const y = h * (0.18 + band * 0.23);
    ctx.fillRect(0, y, w, barHeight);
  }
  ctx.restore();
  return 1;
}

function drawOrbitalGridMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  const cx = w * 0.72;
  const cy = h * 0.34;
  const radius = Math.min(w, h) * 0.28;
  ctx.strokeStyle = toneColor("cyan", 0.13);
  ctx.lineWidth = 1 * devicePixelRatio;
  for (let ring = 0; ring < 4; ring++) {
    ctx.beginPath();
    ctx.ellipse(cx, cy, radius * (0.42 + ring * 0.18), radius * (0.12 + ring * 0.05), 0.32, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.strokeStyle = toneColor("white", 0.10);
  for (let spoke = 0; spoke < 8; spoke++) {
    const angle = spoke * Math.PI * 0.25 + now * 0.00018;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius * 0.42);
    ctx.stroke();
  }
  ctx.restore();
  return 1;
}

function drawRadarSweepMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  const cx = w * 0.72;
  const cy = h * 0.34;
  const radius = Math.min(w, h) * 0.34;
  const angle = now * 0.0012;
  ctx.fillStyle = toneColor("green", 0.055);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, radius, angle, angle + 0.42);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = toneColor("green", 0.28);
  ctx.lineWidth = 1.2 * devicePixelRatio;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(angle + 0.42) * radius, cy + Math.sin(angle + 0.42) * radius);
  ctx.stroke();
  ctx.restore();
  return 1;
}

function drawWarningChevronMotif(w, h, now) {
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor("red", 0.15 + Math.max(0, Math.sin(now * 0.005)) * 0.08);
  ctx.lineWidth = 1.4 * devicePixelRatio;
  const size = 14 * devicePixelRatio;
  for (let index = 0; index < 12; index++) {
    const x = w * 0.05 + index * size * 2.8;
    const y = h * 0.90;
    ctx.beginPath();
    ctx.moveTo(x, y - size);
    ctx.lineTo(x + size, y);
    ctx.lineTo(x, y + size);
    ctx.stroke();
  }
  ctx.restore();
  return 1;
}

function drawPulses(w, h) {
  for (let i = pulses.length - 1; i >= 0; i--) {
    const pulse = pulses[i];
    pulse.age += 0.018;
    const radius = pulse.age * 280 * devicePixelRatio;
    ctx.strokeStyle = pulse.color.replace("1)", `${Math.max(0, 1 - pulse.age)})`);
    ctx.lineWidth = 2 * devicePixelRatio;
    ctx.beginPath();
    ctx.arc(pulse.x * w, pulse.y * h, radius, 0, Math.PI * 2);
    ctx.stroke();
    if (pulse.age >= 1) pulses.splice(i, 1);
  }
}

function colorFor(phase) {
  if (phase === "after") return toneColor("magenta", 1);
  if (phase === "during") return toneColor("cyan", 1);
  if (phase === "lifecycle") return toneColor("amber", 1);
  return toneColor("green", 1);
}

function toneColor(tone, alpha = 1) {
  const colors = {...DEFAULT_STYLE_PACK.tones, ...(currentStylePack.tones || {})};
  const [r, g, b] = colors[tone] || colors.cyan;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function normalizedPoint(point, w, h) {
  return {
    x: Number(point?.x || 0) * w,
    y: Number(point?.y || 0) * h,
  };
}

function drawPolygon(points, fill, stroke) {
  if (!points.length) return;
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = stroke;
  ctx.stroke();
}

function drawScenePrimitives(scene, w, h, now) {
  if (!scene?.primitives) return;
  const primitives = Object.values(scene.primitives).filter((primitive) => primitive.region === "stage");
  const orderedKinds = [
    "data_rain",
    "tunnel_grid",
    "wire_landscape",
    "particle_field",
    "data_vault",
    "black_ice",
    "access_matrix",
    "orbital_map",
    "mesh",
    "city_block",
    "hologram",
    "signal_scope",
    "svg_layer",
    "ribbon",
    "trace_route",
    "spatial_map",
    "projection_scene",
    "node_graph",
    "terminal_wall",
    "glyph_layer",
  ];
  for (const kind of orderedKinds) {
    for (const primitive of primitives) {
      if (primitive.kind === kind) drawPrimitive(primitive, w, h, now);
    }
  }
}

function drawPrimitive(primitive, w, h, now) {
  if (primitive.kind === "mesh") drawMesh(primitive, w, h, now);
  if (primitive.kind === "city_block") drawCityBlock(primitive, w, h, now);
  if (primitive.kind === "hologram") drawHologram(primitive, w, h, now);
  if (primitive.kind === "signal_scope") drawSignalScope(primitive, w, h, now);
  if (primitive.kind === "tunnel_grid") drawTunnelGrid(primitive, w, h, now);
  if (primitive.kind === "wire_landscape") drawWireLandscape(primitive, w, h, now);
  if (primitive.kind === "data_vault") drawDataVault(primitive, w, h, now);
  if (primitive.kind === "black_ice") drawBlackIce(primitive, w, h, now);
  if (primitive.kind === "access_matrix") drawAccessMatrix(primitive, w, h, now);
  if (primitive.kind === "orbital_map") drawOrbitalMap(primitive, w, h, now);
  if (primitive.kind === "svg_layer") drawSvgLayer(primitive, w, h, now);
  if (primitive.kind === "node_graph") drawNodeGraph(primitive, w, h);
  if (primitive.kind === "spatial_map") drawSpatialMap(primitive, w, h, now);
  if (primitive.kind === "projection_scene") drawProjectionScene(primitive, w, h, now);
  if (primitive.kind === "trace_route") drawTraceRoute(primitive, w, h, now);
  if (primitive.kind === "ribbon") drawRibbon(primitive, w, h, now);
  if (primitive.kind === "terminal_wall") drawTerminalWall(primitive, w, h, now);
  if (primitive.kind === "glyph_layer") drawGlyphLayer(primitive, w, h, now);
  if (primitive.kind === "data_rain") drawDataRain(primitive, w, h, now);
  if (primitive.kind === "particle_field") drawParticleField(primitive, w, h, now);
}

function syncAnimationClocks(scene, now) {
  const animations = scene?.animations || {};
  const ids = new Set(Object.keys(animations));
  for (const id of ids) {
    if (!animationClocks.has(id)) animationClocks.set(id, now);
  }
  for (const id of Array.from(animationClocks.keys())) {
    if (!ids.has(id)) animationClocks.delete(id);
  }
  window.__gibsonAnimationState = {
    ids: Array.from(ids),
    kinds: Object.values(animations).map((animation) => animation.kind),
  };
}

function animationProgress(animation, now) {
  const duration = Math.max(1, Number(animation.durationMs || 1000));
  const start = animationClocks.get(animation.id) ?? now;
  const elapsed = Math.max(0, now - start);
  return animation.loop ? (elapsed % duration) / duration : Math.min(1, elapsed / duration);
}

function animationTone(animation) {
  return animation.props?.tone || colorPhaseTone(animation.props?.phase) || "cyan";
}

function sceneCameraState(scene, w, h, now) {
  const animations = Object.values(scene?.animations || {});
  const active = [];
  for (const animation of animations) {
    if (animation.kind === "camera_jolt") {
      const item = cameraJoltState(animation, scene, w, h, now);
      if (item) active.push(item);
    } else if (animation.kind === "camera_path") {
      const item = cameraPathState(animation, scene, w, h, now);
      if (item) active.push(item);
    }
  }
  const state = active.reduce(
    (camera, item) => ({
      activeCount: camera.activeCount + 1,
      animationIds: [...camera.animationIds, item.id],
      targetIds: [...camera.targetIds, item.targetId],
      anchorRefs: [...camera.anchorRefs, item.anchorRef],
      kinds: [...camera.kinds, item.kind],
      pathKeyframeCount: camera.pathKeyframeCount + item.keyframeCount,
      anchorX: camera.anchorX + item.anchor.x,
      anchorY: camera.anchorY + item.anchor.y,
      x: camera.x + item.x,
      y: camera.y + item.y,
      scale: camera.scale * item.scale,
      rotation: camera.rotation + item.rotation,
    }),
    {
      activeCount: 0,
      animationIds: [],
      targetIds: [],
      anchorRefs: [],
      kinds: [],
      pathKeyframeCount: 0,
      anchorX: 0,
      anchorY: 0,
      x: 0,
      y: 0,
      scale: 1,
      rotation: 0,
    },
  );
  if (state.activeCount > 0) {
    state.anchorX /= state.activeCount;
    state.anchorY /= state.activeCount;
  } else {
    state.anchorX = w * 0.5;
    state.anchorY = h * 0.5;
  }
  const rounded = {
    activeCount: state.activeCount,
    animationIds: state.animationIds,
    targetIds: state.targetIds,
    anchorRefs: state.anchorRefs,
    kinds: state.kinds,
    pathKeyframeCount: state.pathKeyframeCount,
    anchorX: vectorRounded(state.anchorX),
    anchorY: vectorRounded(state.anchorY),
    x: vectorRounded(state.x),
    y: vectorRounded(state.y),
    scale: vectorRounded(state.scale),
    rotation: vectorRounded(state.rotation),
  };
  if (typeof window !== "undefined") window.__gibsonCameraState = rounded;
  return state;
}

function cameraJoltState(animation, scene, w, h, now) {
  const progress = animationProgress(animation, now);
  if (!animation.loop && progress >= 1) return null;
  const props = animation.props || {};
  const intensity = clamp(finiteNumber(props.intensity, 0.72), 0.02, 2.5);
  const seed = finiteNumber(props.seed, animation.id.length);
  const envelope = Math.sin(progress * Math.PI);
  const tremor = Math.sin(now * 0.049 + seed * 1.37) * Math.cos(now * 0.029 + seed * 0.73);
  const anchor = cameraAnimationAnchor(animation, scene, w, h);
  return {
    id: animation.id,
    targetId: animation.targetId,
    kind: animation.kind,
    progress,
    anchor,
    anchorRef: anchor.ref || null,
    keyframeCount: 0,
    x: Math.sin(now * 0.041 + seed) * Math.min(w, h) * 0.012 * intensity * envelope,
    y: Math.cos(now * 0.052 + seed * 0.43) * Math.min(w, h) * 0.009 * intensity * envelope,
    scale: 1 + envelope * finiteNumber(props.zoom, 0.028) * intensity + tremor * 0.003 * intensity,
    rotation:
      Math.sin(now * 0.035 + seed * 0.31)
      * finiteNumber(props.roll ?? props.rotation, 0.018)
      * intensity
      * envelope,
  };
}

function cameraPathState(animation, scene, w, h, now) {
  const props = animation.props || {};
  const frames = cameraPathKeyframes(animation);
  if (!frames.length) return null;
  const progress = cameraPathProgress(animation, now);
  if (!animation.loop && progress >= 1) return null;
  const transform = cameraPathTransform(frames, progress);
  const anchor = cameraAnimationAnchor(animation, scene, w, h);
  return {
    id: animation.id,
    targetId: animation.targetId,
    kind: animation.kind,
    progress,
    anchor,
    anchorRef: anchor.ref || null,
    keyframeCount: transform.keyframeCount,
    x: cameraPathMeasure(transform.x, w),
    y: cameraPathMeasure(transform.y, h),
    scale: Math.max(0.05, transform.scale),
    rotation: transform.rotation,
  };
}

function cameraPathProgress(animation, now) {
  const duration = Math.max(1, Number(animation.durationMs || 1000));
  const start = animationClocks.get(animation.id) ?? now;
  const elapsed = Math.max(0, now - start);
  if (!animation.loop) return clamp(elapsed / duration, 0, 1);
  const cycle = Math.floor(elapsed / duration);
  let progress = (elapsed % duration) / duration;
  if (animation.props?.yoyo === true && cycle % 2 === 1) progress = 1 - progress;
  return clamp(progress, 0, 1);
}

function cameraPathKeyframes(animation) {
  const rawFrames = Array.isArray(animation.props?.keyframes) ? animation.props.keyframes : [];
  if (!rawFrames.length) return [];
  const duration = Math.max(1, Number(animation.durationMs || 1000));
  return rawFrames
    .filter((frame) => frame && typeof frame === "object")
    .slice(0, 64)
    .map((frame, index) => ({
      ...frame,
      at: cameraPathKeyframeOffset(frame, index, rawFrames.length, duration),
    }))
    .sort((left, right) => left.at - right.at);
}

function cameraPathKeyframeOffset(frame, index, count, durationMs) {
  const direct = Number(frame.at ?? frame.offset ?? frame.progress);
  if (Number.isFinite(direct)) return clamp(direct, 0, 1);
  const timeMs = Number(frame.timeMs ?? frame.ms);
  if (Number.isFinite(timeMs)) return clamp(timeMs / durationMs, 0, 1);
  return count <= 1 ? 0 : index / (count - 1);
}

function cameraPathFrameNumber(frame, key) {
  const transform = frame?.transform && typeof frame.transform === "object" ? frame.transform : {};
  const numeric = Number(frame?.[key] ?? transform[key]);
  return Number.isFinite(numeric) ? numeric : null;
}

function cameraPathFrameLerp(left, right, key, fallback, progress) {
  const start = cameraPathFrameNumber(left, key);
  const end = cameraPathFrameNumber(right, key);
  if (start === null && end === null) return fallback;
  if (start === null) return end;
  if (end === null) return start;
  return start + (end - start) * progress;
}

function cameraPathTransform(frames, progress) {
  let left = frames[0];
  let right = frames[frames.length - 1];
  for (let index = 1; index < frames.length; index++) {
    if (progress <= frames[index].at) {
      left = frames[index - 1];
      right = frames[index];
      break;
    }
  }
  const span = Math.max(0.0001, right.at - left.at);
  const localProgress = clamp((progress - left.at) / span, 0, 1);
  return {
    x: cameraPathFrameLerp(left, right, "x", 0, localProgress),
    y: cameraPathFrameLerp(left, right, "y", 0, localProgress),
    scale: cameraPathFrameLerp(left, right, "scale", 1, localProgress),
    rotation: cameraPathFrameLerp(left, right, "rotation", 0, localProgress),
    keyframeCount: frames.length,
  };
}

function cameraPathMeasure(value, extent) {
  const numeric = finiteNumber(value, 0);
  return Math.abs(numeric) <= 1 ? numeric * extent : numeric * devicePixelRatio;
}

function applySceneCamera(camera, w, h) {
  if (!camera || camera.activeCount <= 0) return;
  const anchorX = Number.isFinite(camera.anchorX) ? camera.anchorX : w * 0.5;
  const anchorY = Number.isFinite(camera.anchorY) ? camera.anchorY : h * 0.5;
  ctx.translate(anchorX + camera.x, anchorY + camera.y);
  ctx.rotate(camera.rotation);
  ctx.scale(camera.scale, camera.scale);
  ctx.translate(-anchorX, -anchorY);
}

function colorPhaseTone(phase) {
  if (phase === "after") return "magenta";
  if (phase === "during") return "cyan";
  if (phase === "lifecycle") return "amber";
  if (phase === "before") return "green";
  return null;
}

function cameraAnchor(point, ref) {
  return {x: point.x, y: point.y, ref: ref || null};
}

function cameraAnimationAnchor(animation, scene, w, h) {
  const props = animation.props || {};
  if (props.position) {
    return cameraAnchor(normalizedPoint(props.position, w, h), {
      source: "position",
      primitiveId: animation.targetId,
    });
  }
  return animationAnchor(animation, scene, w, h);
}

function primitiveAnchor(primitive, w, h) {
  const props = primitive?.props || {};
  if (props.position) {
    return cameraAnchor(normalizedPoint(props.position, w, h), {
      source: "primitive",
      primitiveId: primitive?.id || null,
    });
  }
  if (primitive?.kind === "node_graph") {
    const nodes = Array.isArray(props.nodes) ? props.nodes : [];
    const focus = nodes.find((node) => node.id === props.focusNodeId) || nodes[0];
    if (focus) {
      return cameraAnchor(normalizedPoint(focus, w, h), objectAnchorRef(primitive, focus, "node", null, "primitive"));
    }
  }
  if (primitive?.kind === "spatial_map") {
    const objects = spatialMapObjects(props);
    const focus = objects.find((object) => object.id === props.focusObjectId || object.entityId === props.focusObjectId)
      || objects[0];
    if (focus) {
      return cameraAnchor(
        spatialMapObjectPoint(props, focus, w, h),
        objectAnchorRef(primitive, focus, "object", null, "primitive"),
      );
    }
  }
  if (primitive?.kind === "city_block") {
    const blocks = Array.isArray(props.blocks) ? props.blocks : [];
    const focus = blocks.find((block) => block.id === props.focusBlockId) || blocks[0];
    if (focus) {
      return cameraAnchor(
        cityBlockAnchorPoint(focus, w, h),
        objectAnchorRef(primitive, focus, "block", null, "primitive"),
      );
    }
  }
  if (primitive?.kind === "ribbon") {
    const points = Array.isArray(props.points) ? props.points : [];
    if (points.length) {
      const index = Math.floor(points.length / 2);
      return cameraAnchor(
        normalizedPoint(points[index], w, h),
        objectAnchorRef(primitive, points[index], "point", index, "primitive"),
      );
    }
  }
  if (primitive?.kind === "particle_field") {
    const emitters = particleFieldEmitters(props, w, h);
    if (emitters.length) {
      return cameraAnchor(emitters[0].point, objectAnchorRef(primitive, emitters[0].config, "emitter", 0, "primitive"));
    }
  }
  return cameraAnchor({x: w * 0.5, y: h * 0.48}, {
    source: "fallback",
    primitiveId: primitive?.id || null,
  });
}

function animationAnchor(animation, scene, w, h) {
  const primitive = scene?.primitives?.[animation.targetId];
  const ref = cameraTargetRef(animation);
  if (ref && primitive) {
    const objectAnchor = primitiveObjectAnchor(primitive, ref, w, h);
    if (objectAnchor) return objectAnchor;
  }
  if (animation.targetId === "scan-grid") {
    return cameraAnchor({x: w * 0.5, y: h * 0.52}, {
      source: "primitive",
      primitiveId: "scan-grid",
    });
  }
  return primitiveAnchor(primitive, w, h);
}

function cameraTargetRef(animation) {
  const props = animation.props || {};
  const ref = props.targetRef || props.anchorRef;
  return ref && typeof ref === "object" ? ref : null;
}

function primitiveObjectAnchor(primitive, ref, w, h) {
  const props = primitive.props || {};
  if (primitive.kind === "node_graph") {
    const node = sceneObjectByRef(Array.isArray(props.nodes) ? props.nodes : [], ref);
    if (node) return cameraAnchor(normalizedPoint(node, w, h), objectAnchorRef(primitive, node, "node"));
  }
  if (primitive.kind === "city_block") {
    const block = sceneObjectByRef(Array.isArray(props.blocks) ? props.blocks : [], ref);
    if (block) return cameraAnchor(cityBlockAnchorPoint(block, w, h), objectAnchorRef(primitive, block, "block"));
  }
  if (primitive.kind === "spatial_map") {
    const object = sceneObjectByRef(spatialMapObjects(props), ref);
    if (object) {
      return cameraAnchor(
        spatialMapObjectPoint(props, object, w, h),
        objectAnchorRef(primitive, object, "object"),
      );
    }
  }
  if (primitive.kind === "trace_route") {
    const hop = sceneObjectByRef(traceRouteHops(props, w, h), ref);
    if (hop) return cameraAnchor(hop.point, objectAnchorRef(primitive, hop, "hop"));
  }
  if (primitive.kind === "ribbon") {
    const points = Array.isArray(props.points) ? props.points : [];
    const point = sceneObjectByRef(points, ref);
    if (point) {
      return cameraAnchor(
        normalizedPoint(point, w, h),
        objectAnchorRef(primitive, point, "point", points.indexOf(point)),
      );
    }
  }
  if (primitive.kind === "wire_landscape") {
    const peak = sceneObjectByRef(wireLandscapePeaks(props), ref);
    if (peak) {
      const rect = wireLandscapeRect(props, w, h);
      const point = wireLandscapePoint(rect, props, peak.x, peak.z, peak.height, 0);
      return cameraAnchor(point, objectAnchorRef(primitive, peak, "peak"));
    }
  }
  return null;
}

function sceneObjectByRef(items, ref) {
  if (!Array.isArray(items) || !items.length) return null;
  const index = Number(ref.index);
  if (Number.isInteger(index) && index >= 0 && index < items.length) return items[index];
  const id = firstString(ref.id, ref.objectId, ref.blockId, ref.nodeId, ref.hopId, ref.peakId, ref.pointId);
  if (id) {
    const found = items.find((item) => item && typeof item === "object" && String(item.id || "") === id);
    if (found) return found;
  }
  const entityId = firstString(ref.entityId, ref.entity_id);
  if (entityId) {
    const found = items.find(
      (item) => item && typeof item === "object" && String(item.entityId || item.entity_id || "") === entityId,
    );
    if (found) return found;
  }
  const path = firstString(ref.path, ref.objectPath);
  if (path) {
    const found = items.find((item) => item && typeof item === "object" && String(item.path || "") === path);
    if (found) return found;
  }
  const label = firstString(ref.label, ref.name);
  if (label) {
    const found = items.find(
      (item) => item && typeof item === "object" && String(item.label || item.name || "") === label,
    );
    if (found) return found;
  }
  return null;
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value) return value;
  }
  return "";
}

function objectAnchorRef(primitive, object, kind, index = null, source = "targetRef") {
  const ref = {
    source,
    primitiveId: primitive?.id || null,
    kind,
  };
  if (object && typeof object === "object") {
    if (object.id) ref.objectId = String(object.id);
    if (object.entityId || object.entity_id) ref.entityId = String(object.entityId || object.entity_id);
    if (object.entityKind || object.entity_kind) ref.entityKind = String(object.entityKind || object.entity_kind);
    if (object.path) ref.path = String(object.path);
    if (object.label) ref.label = String(object.label).slice(0, 48);
  }
  if (index !== null && Number.isFinite(index)) ref.index = index;
  return ref;
}

function cityBlockAnchorPoint(block, w, h) {
  const x = finiteNumber(block?.x, 0.5) + finiteNumber(block?.w, 0) * 0.5;
  const y = finiteNumber(block?.y, 0.5) - finiteNumber(block?.h, 0) * 0.35;
  return {x: x * w, y: y * h};
}

function drawSceneAnimations(scene, w, h, now) {
  if (!scene?.animations) return;
  syncAnimationClocks(scene, now);
  // a projection scene carries its own entity-anchored effect channel; the
  // legacy animation overlays (stream pulses on the backdrop grid, etc.) are
  // noise on top of it and stand down entirely
  if (scene?.primitives?.["projection-scene"]) return;
  for (const animation of Object.values(scene.animations)) {
    const progress = animationProgress(animation, now);
    if (!animation.loop && progress >= 1) continue;
    drawSceneAnimation(animation, scene, w, h, now, progress);
  }
}

function drawSceneAnimation(animation, scene, w, h, now, progress) {
  if (animation.kind === "packet_burst") drawPacketBurstAnimation(animation, scene, w, h, now, progress);
  else if (animation.kind === "timeline_cue") drawTimelineCueAnimation(animation, scene, w, h, progress);
  else if (animation.kind === "scan") drawScanAnimation(animation, w, h, progress);
  else if (animation.kind === "glitch") drawGlitchAnimation(animation, scene, w, h, now, progress);
  else if (animation.kind === "signal_interference") drawSignalInterferenceAnimation(animation, w, h, now, progress);
  else if (animation.kind === "breach_wave") drawBreachWaveAnimation(animation, scene, w, h, now, progress);
  else if (animation.kind === "route_trace") drawRouteTraceAnimation(animation, scene, w, h, now, progress);
  else if (animation.kind === "flythrough") drawFlythroughAnimation(animation, w, h, progress);
  else if (animation.kind === "extrude") drawExtrudeAnimation(animation, scene, w, h, progress);
  else if (animation.kind === "hold") drawHoldAnimation(animation, scene, w, h, progress);
  else drawPulseAnimation(animation, scene, w, h, progress);
}

function drawPulseAnimation(animation, scene, w, h, progress) {
  const anchor = animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  const alpha = Math.max(0, 1 - progress);
  const radius = (0.035 + progress * 0.19) * Math.min(w, h);
  ctx.save();
  ctx.lineWidth = (2.2 + progress * 3) * devicePixelRatio;
  ctx.shadowColor = toneColor(tone, alpha * 0.9);
  ctx.shadowBlur = 24 * devicePixelRatio;
  ctx.strokeStyle = toneColor(tone, alpha * 0.78);
  ctx.beginPath();
  ctx.arc(anchor.x, anchor.y, radius, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

function drawPacketBurstAnimation(animation, scene, w, h, now, progress) {
  const anchor = animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  const paths = Array.isArray(animation.props?.paths) ? animation.props.paths : [];
  const count = Math.min(80, 22 + paths.length * 8);
  const maxDistance = Math.min(w, h) * 0.26;
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.lineCap = "round";
  for (let index = 0; index < count; index++) {
    const angle = index * 2.399 + progress * 5.8 + Number(animation.props?.sequence || 0) * 0.03;
    const distance = (0.08 + progress) * maxDistance * (0.55 + ((index % 7) / 10));
    const x = anchor.x + Math.cos(angle) * distance;
    const y = anchor.y + Math.sin(angle) * distance * 0.72;
    const alpha = Math.max(0, 1 - progress) * (0.35 + (index % 5) * 0.11);
    ctx.strokeStyle = toneColor(tone, alpha * 0.74);
    ctx.lineWidth = 1.4 * devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(anchor.x, anchor.y);
    ctx.lineTo(x, y);
    ctx.stroke();
    ctx.fillStyle = toneColor(index % 3 === 0 ? "white" : tone, alpha);
    ctx.beginPath();
    ctx.arc(x, y, (1.4 + (index % 3)) * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

function timelineCueItems(animation) {
  const raw = Array.isArray(animation.props?.cues) ? animation.props.cues : [];
  const duration = Math.max(1, Number(animation.durationMs || 1000));
  return raw
    .filter((cue) => cue && typeof cue === "object")
    .slice(0, 32)
    .map((cue, index) => {
      let at = Number(cue.at ?? cue.progress ?? cue.offset);
      if (!Number.isFinite(at)) {
        const timeMs = Number(cue.timeMs ?? cue.ms ?? cue.delayMs);
        at = Number.isFinite(timeMs) ? timeMs / duration : (raw.length <= 1 ? 0 : index / (raw.length - 1));
      }
      return {...cue, at: clamp(at, 0, 1), index};
    })
    .sort((left, right) => left.at - right.at);
}

function animationMeasure(value, fallback, extent) {
  const numeric = finiteNumber(value, fallback);
  return Math.abs(numeric) <= 1 ? numeric * extent : numeric * devicePixelRatio;
}

function drawTimelineCueAnimation(animation, scene, w, h, progress) {
  const props = animation.props || {};
  const cues = timelineCueItems(animation);
  const base = props.position ? normalizedPoint(props.position, w, h) : animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  const accentTone = props.accentTone || props.accent || "magenta";
  const width = Math.max(120 * devicePixelRatio, animationMeasure(props.width, 0.34, w));
  const markerHeight = Math.max(18 * devicePixelRatio, animationMeasure(props.height, 0.045, h));
  const x = base.x + animationMeasure(props.offsetX, 0, w);
  const y = base.y + animationMeasure(props.offsetY, 0.085, h);
  const windowSize = clamp(finiteNumber(props.window ?? props.beatWindow, 0.12), 0.02, 0.5);
  let activeCue = null;
  let activeDistance = Infinity;
  for (const cue of cues) {
    const distance = Math.abs(progress - cue.at);
    if (distance < activeDistance) {
      activeCue = cue;
      activeDistance = distance;
    }
  }

  if (typeof window !== "undefined") {
    window.__gibsonTimelineCueState = window.__gibsonTimelineCueState || {};
    window.__gibsonTimelineCueState[animation.id] = {
      targetId: animation.targetId,
      cueCount: cues.length,
      activeCueIndex: activeCue ? activeCue.index : -1,
      activeLabel: activeCue?.label || null,
      progress: vectorRounded(progress),
      hasLabels: cues.some((cue) => Boolean(cue.label)),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.translate(x, y);
  ctx.shadowColor = toneColor(tone, 0.72);
  ctx.shadowBlur = 18 * devicePixelRatio;
  ctx.lineWidth = Math.max(1, 1.25 * devicePixelRatio);
  ctx.strokeStyle = toneColor(tone, 0.35);
  ctx.beginPath();
  ctx.moveTo(-width * 0.5, 0);
  ctx.lineTo(width * 0.5, 0);
  ctx.stroke();
  const progressX = -width * 0.5 + width * progress;
  const gradient = ctx.createLinearGradient(-width * 0.5, 0, progressX, 0);
  gradient.addColorStop(0, toneColor(tone, 0.08));
  gradient.addColorStop(1, toneColor(tone, 0.76));
  ctx.strokeStyle = gradient;
  ctx.lineWidth = Math.max(2, 2.2 * devicePixelRatio);
  ctx.beginPath();
  ctx.moveTo(-width * 0.5, 0);
  ctx.lineTo(progressX, 0);
  ctx.stroke();
  ctx.strokeStyle = toneColor("white", 0.82);
  ctx.beginPath();
  ctx.moveTo(progressX, -markerHeight * 0.62);
  ctx.lineTo(progressX, markerHeight * 0.62);
  ctx.stroke();
  ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  for (const cue of cues) {
    const cueTone = cue.tone || (cue.index % 2 ? accentTone : tone);
    const cueX = -width * 0.5 + width * cue.at;
    const distance = Math.abs(progress - cue.at);
    const activity = clamp(1 - distance / windowSize, 0, 1);
    const radius = (2.4 + activity * 6.2) * devicePixelRatio;
    ctx.shadowColor = toneColor(cueTone, 0.45 + activity * 0.45);
    ctx.shadowBlur = (7 + activity * 14) * devicePixelRatio;
    ctx.strokeStyle = toneColor(cueTone, 0.42 + activity * 0.42);
    ctx.lineWidth = Math.max(0.8, (1 + activity * 1.4) * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(cueX, -markerHeight * (0.34 + activity * 0.42));
    ctx.lineTo(cueX, markerHeight * (0.34 + activity * 0.42));
    ctx.stroke();
    ctx.fillStyle = toneColor(cueTone, 0.48 + activity * 0.38);
    ctx.beginPath();
    ctx.arc(cueX, 0, radius, 0, Math.PI * 2);
    ctx.fill();
    if (cue.label && (activity > 0.15 || cue.showLabel === true)) {
      ctx.fillStyle = toneColor("white", 0.58 + activity * 0.34);
      ctx.fillText(String(cue.label).slice(0, 14), cueX, -markerHeight * (0.78 + activity * 0.3));
    }
  }
  if (props.label) {
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.76);
    ctx.fillText(String(props.label).slice(0, 22), -width * 0.5, markerHeight * 0.95);
  }
  ctx.restore();
}

function drawScanAnimation(animation, w, h, progress) {
  const tone = animationTone(animation);
  const direction = animation.props?.direction || "down";
  const position = 0.08 + progress * 0.84;
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.shadowColor = toneColor(tone, 0.85);
  ctx.shadowBlur = 24 * devicePixelRatio;
  const gradient = ctx.createLinearGradient(0, 0, direction === "right" ? w : 0, direction === "right" ? 0 : h);
  gradient.addColorStop(0, toneColor(tone, 0));
  gradient.addColorStop(0.5, toneColor(tone, 0.58));
  gradient.addColorStop(1, toneColor(tone, 0));
  ctx.strokeStyle = gradient;
  ctx.lineWidth = 3 * devicePixelRatio;
  ctx.beginPath();
  if (direction === "right") {
    const x = position * w;
    ctx.moveTo(x - 90 * devicePixelRatio, 0);
    ctx.lineTo(x + 90 * devicePixelRatio, h);
  } else {
    const y = position * h;
    ctx.moveTo(0, y);
    ctx.lineTo(w, y - h * 0.08);
  }
  ctx.stroke();
  ctx.restore();
}

function drawGlitchAnimation(animation, scene, w, h, now, progress) {
  const anchor = animationAnchor(animation, scene, w, h);
  const amount = Math.max(0.2, Number(animation.props?.amount || 0.7));
  const seed = Number(animation.props?.seed || animation.id.length);
  const alpha = animation.loop ? 0.42 : Math.max(0, 1 - progress) * 0.56;
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.lineWidth = 1 * devicePixelRatio;
  for (let index = 0; index < 12; index++) {
    const jitter = Math.sin(now * 0.035 + seed + index * 1.7);
    const x = anchor.x + jitter * 44 * amount * devicePixelRatio;
    const y = anchor.y + (index - 6) * 8 * devicePixelRatio;
    const width = (28 + (index % 4) * 18) * devicePixelRatio;
    ctx.strokeStyle = toneColor(index % 2 ? "magenta" : "cyan", alpha);
    ctx.strokeRect(x - width * 0.5, y, width, 3 * devicePixelRatio);
  }
  ctx.restore();
}

function drawSignalInterferenceAnimation(animation, w, h, now, progress) {
  const props = animation.props || {};
  const tone = animationTone(animation);
  const accentTone = props.accentTone || props.accent || "magenta";
  const intensity = clamp(finiteNumber(props.intensity, 0.74), 0.05, 2.2);
  const bandCount = Math.max(1, Math.min(28, Math.floor(finiteNumber(props.bands, 12))));
  const blockCount = Math.max(0, Math.min(80, Math.floor(finiteNumber(props.blocks, 24))));
  const noiseCount = Math.max(0, Math.min(160, Math.floor(finiteNumber(props.noise, 72))));
  const speed = Math.max(0, finiteNumber(props.speed, 0.84));
  const seed = finiteNumber(props.seed, animation.id.length);
  const wave = animation.loop ? 0.58 + Math.sin(now * 0.005 * speed + seed) * 0.22 : Math.sin(progress * Math.PI);
  const alpha = clamp(wave * intensity, 0, 1.4);

  if (typeof window !== "undefined") {
    window.__gibsonSignalInterferenceState = window.__gibsonSignalInterferenceState || {};
    window.__gibsonSignalInterferenceState[animation.id] = {
      targetId: animation.targetId,
      bandCount,
      blockCount,
      noiseCount,
      tone,
      accentTone,
      hasLabel: Boolean(props.label),
      progress: vectorRounded(progress),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineCap = "butt";
  ctx.lineJoin = "miter";

  const scanGap = Math.max(3, finiteNumber(props.scanGap, 7) * devicePixelRatio);
  ctx.fillStyle = toneColor(tone, 0.018 * alpha);
  for (let y = (now * 0.035 * speed + seed * 13) % scanGap; y < h; y += scanGap) {
    ctx.fillRect(0, y, w, Math.max(1, 1.1 * devicePixelRatio));
  }

  for (let index = 0; index < bandCount; index++) {
    const bandSeed = seed + index * 11.43 + Math.floor(now * 0.008 * speed);
    const y = (seededUnit(bandSeed) * h + progress * h * 0.14 * (index % 2 ? -1 : 1) + h) % h;
    const height = (2 + seededUnit(bandSeed + 1.7) * 11) * devicePixelRatio;
    const jitter = (seededUnit(bandSeed + 2.9) - 0.5) * w * 0.08 * intensity;
    const bandTone = index % 3 === 0 ? accentTone : tone;
    const gradient = ctx.createLinearGradient(0, y, w, y);
    gradient.addColorStop(0, toneColor(bandTone, 0));
    gradient.addColorStop(0.18, toneColor(bandTone, 0.05 * alpha));
    gradient.addColorStop(0.52, toneColor("white", 0.08 * alpha));
    gradient.addColorStop(0.86, toneColor(bandTone, 0.06 * alpha));
    gradient.addColorStop(1, toneColor(bandTone, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(jitter - w * 0.04, y, w * 1.08, height);
  }

  for (let index = 0; index < blockCount; index++) {
    const blockSeed = seed + index * 5.77 + Math.floor(now * 0.010 * speed);
    const width = (8 + seededUnit(blockSeed + 1.4) * 74) * devicePixelRatio;
    const height = (3 + seededUnit(blockSeed + 2.6) * 18) * devicePixelRatio;
    const x = seededUnit(blockSeed + 3.2) * w;
    const y = seededUnit(blockSeed + 4.1) * h;
    const blockTone = index % 4 === 0 ? "white" : (index % 2 ? accentTone : tone);
    ctx.fillStyle = toneColor(blockTone, (0.018 + seededUnit(blockSeed + 5.8) * 0.06) * alpha);
    ctx.fillRect(x, y, width, height);
  }

  ctx.lineWidth = Math.max(1, 1.15 * devicePixelRatio);
  for (let index = 0; index < noiseCount; index++) {
    const noiseSeed = seed + index * 2.31 + Math.floor(now * 0.014 * speed);
    const x = seededUnit(noiseSeed) * w;
    const y = seededUnit(noiseSeed + 0.9) * h;
    const length = (3 + seededUnit(noiseSeed + 2.2) * 28) * devicePixelRatio;
    const noiseTone = index % 5 === 0 ? "white" : (index % 2 ? accentTone : tone);
    ctx.strokeStyle = toneColor(noiseTone, (0.04 + seededUnit(noiseSeed + 3.5) * 0.10) * alpha);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + length, y + (seededUnit(noiseSeed + 4.7) - 0.5) * 5 * devicePixelRatio);
    ctx.stroke();
  }

  if (props.label) {
    const y = h * clamp(finiteNumber(props.labelY, 0.17), 0.04, 0.92);
    const x = w * clamp(finiteNumber(props.labelX, 0.74), 0.05, 0.95);
    const label = String(props.label).slice(0, 24);
    ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = toneColor(accentTone, 0.8 * alpha);
    ctx.shadowBlur = 10 * devicePixelRatio;
    ctx.fillStyle = toneColor("white", 0.52 + 0.30 * alpha);
    ctx.fillText(label, x, y);
    ctx.strokeStyle = toneColor(accentTone, 0.42 * alpha);
    ctx.lineWidth = Math.max(1, 1.2 * devicePixelRatio);
    ctx.strokeRect(
      x - label.length * 4.5 * devicePixelRatio,
      y - 12 * devicePixelRatio,
      label.length * 9 * devicePixelRatio,
      24 * devicePixelRatio
    );
  }

  ctx.restore();
}

function drawBreachWaveAnimation(animation, scene, w, h, now, progress) {
  const props = animation.props || {};
  const anchor = props.position ? normalizedPoint(props.position, w, h) : animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  const accentTone = props.accentTone || props.accent || "white";
  const intensity = clamp(finiteNumber(props.intensity, 0.86), 0.05, 2);
  const ringCount = Math.max(1, Math.min(12, Math.floor(finiteNumber(props.rings, 5))));
  const shardCount = Math.max(0, Math.min(80, Math.floor(finiteNumber(props.shards, 28))));
  const seed = finiteNumber(props.seed, 0);
  const wave = Math.sin(progress * Math.PI);
  const maxRadius = Math.hypot(w, h) * clamp(finiteNumber(props.radius, 0.58), 0.08, 1.4);

  if (typeof window !== "undefined") {
    window.__gibsonBreachWaveState = window.__gibsonBreachWaveState || {};
    window.__gibsonBreachWaveState[animation.id] = {
      targetId: animation.targetId,
      ringCount,
      shardCount,
      tone,
      accentTone,
      hasLabel: Boolean(props.label),
      progress: vectorRounded(progress),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  const flashAlpha = wave * 0.075 * intensity;
  if (flashAlpha > 0) {
    const gradient = ctx.createRadialGradient(anchor.x, anchor.y, 0, anchor.x, anchor.y, maxRadius * 0.78);
    gradient.addColorStop(0, toneColor(accentTone, flashAlpha * 1.6));
    gradient.addColorStop(0.42, toneColor(tone, flashAlpha));
    gradient.addColorStop(1, toneColor(tone, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);
  }

  ctx.shadowBlur = 22 * devicePixelRatio * intensity;
  for (let ring = 0; ring < ringCount; ring++) {
    const offset = ring / ringCount;
    const ringProgress = (progress + offset * 0.36) % 1;
    const alpha = Math.pow(1 - ringProgress, 1.35) * (0.52 + offset * 0.18) * intensity;
    const radius = (0.035 + ringProgress * 0.82) * maxRadius;
    const ringTone = ring % 2 ? accentTone : tone;
    ctx.shadowColor = toneColor(ringTone, alpha * 0.82);
    ctx.strokeStyle = toneColor(ringTone, alpha);
    ctx.lineWidth = Math.max(0.8, (1.1 + offset * 2.6) * devicePixelRatio);
    ctx.setLineDash([
      (18 + ring * 3) * devicePixelRatio,
      (10 + ring * 2) * devicePixelRatio,
    ]);
    ctx.lineDashOffset = -now * 0.035 * (1 + offset);
    ctx.beginPath();
    ctx.arc(anchor.x, anchor.y, radius, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for (let shard = 0; shard < shardCount; shard++) {
    const shardSeed = seed + shard * 9.73;
    const angle = seededUnit(shardSeed) * Math.PI * 2 + progress * 0.55;
    const distance = maxRadius * (0.08 + ((progress + seededUnit(shardSeed + 4.1) * 0.52) % 1) * 0.72);
    const length = maxRadius * (0.018 + seededUnit(shardSeed + 2.3) * 0.045) * intensity;
    const x = anchor.x + Math.cos(angle) * distance;
    const y = anchor.y + Math.sin(angle) * distance * 0.68;
    const alpha = Math.max(0, 1 - distance / maxRadius) * (0.26 + wave * 0.46);
    const shardTone = shard % 4 === 0 ? accentTone : tone;
    ctx.shadowColor = toneColor(shardTone, alpha);
    ctx.strokeStyle = toneColor(shardTone, alpha);
    ctx.lineWidth = Math.max(0.7, (0.8 + seededUnit(shardSeed + 8.2) * 1.4) * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(x - Math.cos(angle) * length, y - Math.sin(angle) * length * 0.68);
    ctx.lineTo(x + Math.cos(angle) * length, y + Math.sin(angle) * length * 0.68);
    ctx.stroke();
  }

  const sliceCount = Math.max(0, Math.min(12, Math.floor(finiteNumber(props.slices, 5))));
  for (let slice = 0; slice < sliceCount; slice++) {
    const y = (seededUnit(seed + slice * 5.1 + Math.floor(now / 140)) * h + progress * h * 0.18) % h;
    const height = (2 + seededUnit(seed + slice * 7.6) * 7) * devicePixelRatio;
    const alpha = wave * (0.10 + seededUnit(seed + slice * 3.4) * 0.18) * intensity;
    const gradient = ctx.createLinearGradient(0, y, w, y);
    gradient.addColorStop(0, toneColor(accentTone, 0));
    gradient.addColorStop(0.5, toneColor(slice % 2 ? accentTone : tone, alpha));
    gradient.addColorStop(1, toneColor(tone, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(0, y, w, height);
  }

  if (props.label) {
    ctx.font = `${13 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = toneColor(accentTone, 0.85);
    ctx.shadowBlur = 12 * devicePixelRatio;
    ctx.fillStyle = toneColor("white", 0.66 + wave * 0.28);
    ctx.fillText(String(props.label).slice(0, 26), anchor.x, anchor.y - Math.min(w, h) * 0.16);
  }

  ctx.restore();
}

function routeTracePoints(animation, scene, w, h) {
  const rawPoints = Array.isArray(animation.props?.points) ? animation.props.points : [];
  const points = rawPoints
    .filter((point) => point && typeof point === "object")
    .slice(0, 24)
    .map((point, index) => ({
      ...point,
      id: String(point.id || `route-${index}`),
      label: point.label || point.id || `HOP ${index + 1}`,
      tone: point.tone || animation.props?.tone || "cyan",
      point: normalizedPoint(point, w, h),
    }));
  if (points.length >= 2) return points;
  const anchor = animationAnchor(animation, scene, w, h);
  const radius = Math.min(w, h) * 0.16;
  return [
    {
      id: "entry",
      label: "ENTRY",
      tone: animation.props?.tone || "green",
      point: {x: anchor.x - radius, y: anchor.y + radius * 0.36},
    },
    {
      id: "core",
      label: "CORE",
      tone: animation.props?.accentTone || "magenta",
      point: {x: anchor.x + radius, y: anchor.y - radius * 0.36},
    },
  ];
}

function routeTraceSegments(points) {
  const segments = [];
  let total = 0;
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1].point;
    const b = points[index].point;
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (length <= 0) continue;
    segments.push({a, b, left: points[index - 1], right: points[index], length, start: total});
    total += length;
  }
  return {segments, total};
}

function routeTracePointOnPath(path, progress) {
  if (!path.segments.length) return null;
  const distance = clamp(progress, 0, 1) * path.total;
  for (const segment of path.segments) {
    if (distance <= segment.start + segment.length) {
      const local = clamp((distance - segment.start) / segment.length, 0, 1);
      return {
        x: segment.a.x + (segment.b.x - segment.a.x) * local,
        y: segment.a.y + (segment.b.y - segment.a.y) * local,
        segment,
      };
    }
  }
  const last = path.segments[path.segments.length - 1];
  return {x: last.b.x, y: last.b.y, segment: last};
}

function drawRouteTraceAnimation(animation, scene, w, h, now, progress) {
  const props = animation.props || {};
  const points = routeTracePoints(animation, scene, w, h);
  const path = routeTraceSegments(points);
  if (!path.total) return;
  const tone = animationTone(animation);
  const accentTone = props.accentTone || props.accent || "magenta";
  const packetCount = Math.max(1, Math.min(80, Math.floor(finiteNumber(props.packets, 18))));
  const tail = clamp(finiteNumber(props.tail, 0.055), 0, 0.35);
  const seed = finiteNumber(props.seed, animation.id.length);
  const activeIndex = Math.min(points.length - 1, Math.floor(progress * points.length));

  if (typeof window !== "undefined") {
    window.__gibsonRouteTraceState = window.__gibsonRouteTraceState || {};
    window.__gibsonRouteTraceState[animation.id] = {
      targetId: animation.targetId,
      pointCount: points.length,
      packetCount,
      activePointId: points[activeIndex]?.id || null,
      hasLabel: Boolean(props.label) || points.some((point) => Boolean(point.label)),
      progress: vectorRounded(progress),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowBlur = 14 * devicePixelRatio;

  for (const [index, segment] of path.segments.entries()) {
    const phase = (progress * Math.max(1, path.segments.length) - index);
    const activity = clamp(phase, 0, 1);
    const segmentTone = segment.right.tone || (index % 2 ? accentTone : tone);
    const width = (2.1 + activity * 2.2) * devicePixelRatio;
    ctx.shadowColor = toneColor(segmentTone, 0.46 + activity * 0.34);
    ctx.strokeStyle = toneColor(segmentTone, 0.22 + activity * 0.42);
    ctx.lineWidth = Math.max(1, width);
    ctx.setLineDash([
      (12 + index * 2) * devicePixelRatio,
      (8 + index) * devicePixelRatio,
    ]);
    ctx.lineDashOffset = -now * 0.030 * (1 + index * 0.08);
    ctx.beginPath();
    ctx.moveTo(segment.a.x, segment.a.y);
    ctx.lineTo(segment.b.x, segment.b.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for (let packet = 0; packet < packetCount; packet++) {
    const rawProgress = (progress + packet / packetCount + seededUnit(seed + packet * 7.3) * 0.045) % 1;
    const head = routeTracePointOnPath(path, rawProgress);
    if (!head) continue;
    const tailPoint = routeTracePointOnPath(path, Math.max(0, rawProgress - tail));
    const packetTone = packet % 5 === 0 ? "white" : (packet % 2 ? accentTone : tone);
    const alpha = 0.30 + seededUnit(seed + packet * 2.9) * 0.48;
    ctx.shadowColor = toneColor(packetTone, alpha);
    ctx.shadowBlur = (8 + (packet % 4) * 3) * devicePixelRatio;
    if (tailPoint && rawProgress - tail >= 0) {
      ctx.strokeStyle = toneColor(packetTone, alpha * 0.56);
      ctx.lineWidth = Math.max(0.8, (1.1 + (packet % 3) * 0.35) * devicePixelRatio);
      ctx.beginPath();
      ctx.moveTo(tailPoint.x, tailPoint.y);
      ctx.lineTo(head.x, head.y);
      ctx.stroke();
    }
    ctx.fillStyle = toneColor(packetTone, alpha);
    ctx.beginPath();
    ctx.arc(head.x, head.y, (1.6 + (packet % 4) * 0.62) * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }

  for (const [index, item] of points.entries()) {
    const pointProgress = points.length <= 1 ? 0 : index / (points.length - 1);
    const distance = Math.abs(progress - pointProgress);
    const active = clamp(1 - distance / 0.18, 0, 1);
    const pointTone = item.tone || (index % 2 ? accentTone : tone);
    const radius = (4.8 + active * 7.4) * devicePixelRatio;
    ctx.shadowColor = toneColor(pointTone, 0.58 + active * 0.34);
    ctx.shadowBlur = (8 + active * 14) * devicePixelRatio;
    ctx.fillStyle = toneColor(pointTone, 0.22 + active * 0.48);
    ctx.strokeStyle = toneColor("white", 0.34 + active * 0.42);
    ctx.lineWidth = Math.max(0.8, (1 + active * 1.4) * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(item.point.x, item.point.y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (item.label && (active > 0.2 || props.showLabels === true)) {
      ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = toneColor("white", 0.62 + active * 0.28);
      ctx.fillText(String(item.label).slice(0, 14), item.point.x, item.point.y - radius - 4 * devicePixelRatio);
    }
  }

  if (props.label) {
    const first = points[0].point;
    ctx.font = `${11.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.shadowColor = toneColor(accentTone, 0.7);
    ctx.shadowBlur = 8 * devicePixelRatio;
    ctx.fillStyle = toneColor("white", 0.78);
    ctx.fillText(String(props.label).slice(0, 28), first.x, first.y - 18 * devicePixelRatio);
  }
  ctx.restore();
}

function drawFlythroughAnimation(animation, w, h, progress) {
  const tone = animationTone(animation);
  const center = {x: w * 0.5, y: h * 0.52};
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.strokeStyle = toneColor(tone, 0.30);
  ctx.lineWidth = 1.2 * devicePixelRatio;
  for (let index = 0; index < 16; index++) {
    const angle = (index / 16) * Math.PI * 2;
    const inner = (0.08 + progress * 0.18) * Math.min(w, h);
    const outer = Math.max(w, h) * (0.72 + progress * 0.28);
    ctx.beginPath();
    ctx.moveTo(center.x + Math.cos(angle) * inner, center.y + Math.sin(angle) * inner * 0.58);
    ctx.lineTo(center.x + Math.cos(angle) * outer, center.y + Math.sin(angle) * outer * 0.58);
    ctx.stroke();
  }
  ctx.restore();
}

function drawExtrudeAnimation(animation, scene, w, h, progress) {
  const anchor = animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  for (let index = 0; index < 5; index++) {
    const size = (34 + index * 20 + progress * 30) * devicePixelRatio;
    const offset = (index * 8 + progress * 24) * devicePixelRatio;
    ctx.strokeStyle = toneColor(tone, Math.max(0, 0.45 - index * 0.07));
    ctx.strokeRect(anchor.x - size * 0.5 + offset, anchor.y - size * 0.5 - offset, size, size * 0.62);
  }
  ctx.restore();
}

function drawHoldAnimation(animation, scene, w, h, progress) {
  const anchor = animationAnchor(animation, scene, w, h);
  const tone = animationTone(animation);
  const size = (34 + Math.sin(progress * Math.PI * 2) * 6) * devicePixelRatio;
  ctx.save();
  ctx.strokeStyle = toneColor(tone, 0.72);
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.beginPath();
  ctx.moveTo(anchor.x - size, anchor.y - size);
  ctx.lineTo(anchor.x - size * 0.45, anchor.y - size);
  ctx.moveTo(anchor.x - size, anchor.y - size);
  ctx.lineTo(anchor.x - size, anchor.y - size * 0.45);
  ctx.moveTo(anchor.x + size, anchor.y + size);
  ctx.lineTo(anchor.x + size * 0.45, anchor.y + size);
  ctx.moveTo(anchor.x + size, anchor.y + size);
  ctx.lineTo(anchor.x + size, anchor.y + size * 0.45);
  ctx.stroke();
  ctx.restore();
}

function cityCameraSource(props) {
  const rawPath = props.cameraPath;
  if (Array.isArray(rawPath)) {
    return {
      keyframes: rawPath,
      durationMs: props.cameraDurationMs ?? props.durationMs,
      delayMs: props.cameraDelayMs,
      loop: props.cameraLoop ?? props.loop,
      yoyo: props.cameraYoyo ?? props.yoyo,
    };
  }
  if (rawPath && typeof rawPath === "object") return rawPath;
  return {};
}

function cityCameraOffset(value, extent) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) return 0;
  return Math.abs(numeric) <= 1 ? numeric * extent : numeric * devicePixelRatio;
}

function cityCameraTransform(props, w, h, now) {
  const source = cityCameraSource(props);
  const transform = vectorKeyframeTransform(source, now);
  return {
    ...transform,
    x: cityCameraOffset(transform.x, w),
    y: cityCameraOffset(transform.y, h),
  };
}

function drawCityBlock(primitive, w, h, now) {
  const props = primitive.props || {};
  const blocks = Array.isArray(props.blocks) ? props.blocks : [];
  const camera = cityCameraTransform(props, w, h, now);
  if (typeof window !== "undefined") {
    window.__gibsonCityState = window.__gibsonCityState || {};
    window.__gibsonCityState[primitive.id] = {
      blockCount: blocks.length,
      focusBlockId: props.focusBlockId || null,
      cameraKeyframeCount: camera.keyframeCount,
      cameraProgress: vectorRounded(camera.progress),
      cameraX: vectorRounded(camera.x),
      cameraY: vectorRounded(camera.y),
      cameraScale: vectorRounded(camera.scale),
      cameraRotation: vectorRounded(camera.rotation),
    };
  }
  ctx.save();
  ctx.translate(camera.x, camera.y);
  ctx.translate(w * 0.5, h * 0.54);
  ctx.rotate(camera.rotation);
  ctx.scale(camera.scale, camera.scale);
  ctx.translate(-w * 0.5, -h * 0.54);
  ctx.lineWidth = 1.4 * devicePixelRatio;
  ctx.font = `${11 * devicePixelRatio}px ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const block of blocks) {
    const x = Number(block.x || 0) * w;
    const y = Number(block.y || 0) * h;
    const bw = Math.max(18 * devicePixelRatio, Number(block.w || 0.05) * w);
    const bd = Math.max(16 * devicePixelRatio, Number(block.d || 0.05) * h);
    const bh = Math.max(16 * devicePixelRatio, Number(block.h || 0.12) * h * Number(props.heightScale || 1));
    const tone = block.tone || "cyan";
    const focus = block.id === props.focusBlockId;
    const top = [
      {x, y: y - bh},
      {x: x + bw * 0.5, y: y - bh - bd * 0.42},
      {x: x + bw, y: y - bh},
      {x: x + bw * 0.5, y: y - bh + bd * 0.42},
    ];
    const left = [
      {x, y: y - bh},
      {x: x + bw * 0.5, y: y - bh + bd * 0.42},
      {x: x + bw * 0.5, y},
      {x, y: y - bd * 0.42},
    ];
    const right = [
      {x: x + bw, y: y - bh},
      {x: x + bw * 0.5, y: y - bh + bd * 0.42},
      {x: x + bw * 0.5, y},
      {x: x + bw, y: y - bd * 0.42},
    ];
    ctx.shadowColor = toneColor(tone, focus ? 0.75 : 0.38);
    ctx.shadowBlur = focus ? 20 * devicePixelRatio : 8 * devicePixelRatio;
    drawPolygon(left, toneColor(tone, focus ? 0.28 : 0.16), toneColor(tone, 0.48));
    drawPolygon(right, toneColor(tone, focus ? 0.20 : 0.12), toneColor(tone, 0.42));
    drawPolygon(top, toneColor(tone, focus ? 0.46 : 0.26), toneColor(tone, 0.88));
    if (block.label) {
      ctx.shadowBlur = 4 * devicePixelRatio;
      ctx.fillStyle = toneColor(focus ? tone : "white", focus ? 0.95 : 0.58);
      ctx.fillText(String(block.label).slice(0, 14), x + bw * 0.5, y - bh - bd * 0.16);
    }
  }
  ctx.restore();
}

function drawHologram(primitive, w, h, now) {
  const props = primitive.props || {};
  const center = normalizedPoint(props.position || {x: 0.52, y: 0.48}, w, h);
  const scale = Math.max(0.04, Math.min(0.72, finiteNumber(props.scale, 0.18))) * Math.min(w, h);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.82), 0, 1);
  const ringCount = Math.max(1, Math.min(9, Math.floor(finiteNumber(props.rings, 5))));
  const beamCount = Math.max(0, Math.min(16, Math.floor(finiteNumber(props.beams, 5))));
  const panelCount = Math.max(0, Math.min(12, Math.floor(finiteNumber(props.panels, 3))));
  const moteCount = Math.max(0, Math.min(80, Math.floor(finiteNumber(props.motes, 18))));
  const spin = finiteNumber(props.spin, 0.42);
  const seed = finiteNumber(props.seed, 0);
  const phase = now * 0.00028 * spin + seed * 0.017;
  const scanEnabled = props.scan !== false;

  if (typeof window !== "undefined") {
    window.__gibsonHologramState = window.__gibsonHologramState || {};
    window.__gibsonHologramState[primitive.id] = {
      ringCount,
      beamCount,
      panelCount,
      moteCount,
      tone,
      accentTone,
      hasScan: scanEnabled,
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.translate(center.x, center.y);
  ctx.shadowColor = toneColor(tone, 0.74);
  ctx.shadowBlur = 18 * devicePixelRatio;

  const baseRadius = scale * 0.58;
  for (let index = ringCount - 1; index >= 0; index--) {
    const depth = index / Math.max(1, ringCount - 1);
    const radius = baseRadius * (0.34 + depth * 0.86);
    const y = scale * (0.28 - depth * 0.18);
    const alpha = 0.18 + (1 - depth) * 0.34;
    ctx.save();
    ctx.rotate(phase * (index % 2 ? -0.7 : 0.9) + index * 0.18);
    ctx.lineWidth = Math.max(0.8, (1.15 - depth * 0.45) * devicePixelRatio);
    ctx.strokeStyle = toneColor(index % 2 ? accentTone : tone, alpha);
    ctx.beginPath();
    ctx.ellipse(0, y, radius, Math.max(3 * devicePixelRatio, radius * 0.22), 0, 0, Math.PI * 2);
    ctx.stroke();
    if (index % 2 === 0) {
      ctx.setLineDash([8 * devicePixelRatio, 9 * devicePixelRatio]);
      ctx.lineDashOffset = -now * 0.026 * spin;
      ctx.strokeStyle = toneColor("white", alpha * 0.56);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();
  }

  for (let beam = 0; beam < beamCount; beam++) {
    const angle = phase + (beam / Math.max(1, beamCount)) * Math.PI * 2;
    const spread = 0.35 + seededUnit(seed + beam * 3.7) * 0.65;
    const top = {
      x: Math.cos(angle) * baseRadius * spread,
      y: -scale * (0.82 + seededUnit(seed + beam) * 0.18),
    };
    const bottom = {
      x: Math.cos(angle + 0.22) * baseRadius * (0.18 + spread * 0.18),
      y: scale * 0.42,
    };
    ctx.strokeStyle = toneColor(beam % 3 === 0 ? accentTone : tone, 0.12 + seededUnit(seed + beam * 9.1) * 0.16);
    ctx.lineWidth = Math.max(0.4, 0.9 * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(bottom.x, bottom.y);
    ctx.lineTo(top.x, top.y);
    ctx.stroke();
  }

  for (let panel = 0; panel < panelCount; panel++) {
    const panelPhase = phase * 1.4 + panel * 1.17;
    const x = Math.cos(panelPhase) * baseRadius * 0.86;
    const y = -scale * 0.38 + Math.sin(panelPhase * 0.73) * scale * 0.18;
    const width = scale * (0.18 + seededUnit(seed + panel * 5.5) * 0.15);
    const height = scale * (0.11 + seededUnit(seed + panel * 7.1) * 0.09);
    const tilt = Math.sin(panelPhase) * 0.22;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(tilt);
    ctx.fillStyle = toneColor(panel % 2 ? tone : accentTone, 0.055);
    ctx.strokeStyle = toneColor(panel % 2 ? tone : accentTone, 0.46);
    ctx.lineWidth = Math.max(0.5, 0.85 * devicePixelRatio);
    ctx.fillRect(-width * 0.5, -height * 0.5, width, height);
    ctx.strokeRect(-width * 0.5, -height * 0.5, width, height);
    ctx.strokeStyle = toneColor("white", 0.32);
    ctx.beginPath();
    ctx.moveTo(-width * 0.38, 0);
    ctx.lineTo(width * 0.38, 0);
    ctx.moveTo(0, -height * 0.32);
    ctx.lineTo(0, height * 0.32);
    ctx.stroke();
    ctx.restore();
  }

  for (let mote = 0; mote < moteCount; mote++) {
    const orbit = phase * (0.9 + seededUnit(seed + mote) * 1.6) + mote * 0.77;
    const radius = baseRadius * (0.18 + seededUnit(seed + mote * 2.3) * 1.04);
    const x = Math.cos(orbit) * radius;
    const y = Math.sin(orbit * 0.72) * radius * 0.34 - scale * 0.08;
    const alpha = 0.22 + seededUnit(seed + mote * 4.1) * 0.46;
    ctx.fillStyle = toneColor(mote % 5 === 0 ? "white" : tone, alpha);
    ctx.beginPath();
    ctx.arc(x, y, (0.9 + seededUnit(seed + mote * 8.2) * 1.9) * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }

  if (scanEnabled) {
    const scanProgress = (now * 0.00022 * Math.max(0.1, Math.abs(spin)) + seed * 0.031) % 1;
    const y = -scale * 0.72 + scanProgress * scale * 1.34;
    const gradient = ctx.createLinearGradient(-baseRadius, y, baseRadius, y);
    gradient.addColorStop(0, toneColor(accentTone, 0));
    gradient.addColorStop(0.5, toneColor(accentTone, 0.42));
    gradient.addColorStop(1, toneColor(accentTone, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(-baseRadius, y - 2.5 * devicePixelRatio, baseRadius * 2, 5 * devicePixelRatio);
  }

  ctx.strokeStyle = toneColor(tone, 0.62);
  ctx.lineWidth = Math.max(1, 1.1 * devicePixelRatio);
  ctx.beginPath();
  ctx.moveTo(-baseRadius * 0.35, scale * 0.58);
  ctx.lineTo(baseRadius * 0.35, scale * 0.58);
  ctx.moveTo(0, scale * 0.58);
  ctx.lineTo(0, scale * 0.78);
  ctx.stroke();

  if (props.label) {
    ctx.font = `${Math.max(9, scale * 0.085)}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.86);
    ctx.shadowBlur = 8 * devicePixelRatio;
    ctx.fillText(String(props.label).slice(0, 18), 0, -scale * 0.92);
  }

  ctx.restore();
}

function accessMatrixRect(props, w, h) {
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const position = props.position && typeof props.position === "object" ? props.position : {x: 0.5, y: 0.5};
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.34), 0.08, 1.2) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.24), 0.06, 0.9) * h;
  const x = finiteNumber(position.x, 0.5) * w - width * 0.5;
  const y = finiteNumber(position.y, 0.5) * h - height * 0.5;
  return {x, y, width, height};
}

function accessMatrixCells(props, rows, columns) {
  const capacity = rows * columns;
  const rawCells = Array.isArray(props.cells) && props.cells.length
    ? props.cells
    : Array.from({length: capacity}, (_, index) => ({
      id: `cell-${index}`,
      value: ((index % columns) + 1) / Math.max(1, columns),
      active: index % 7 === 0,
      locked: index % 5 === 0,
    }));
  return rawCells
    .filter((cell) => cell && typeof cell === "object")
    .slice(0, capacity)
    .map((cell, index) => {
      const row = Math.floor(clamp(finiteNumber(cell.row ?? cell.r, Math.floor(index / columns)), 0, rows - 1));
      const columnValue = finiteNumber(cell.column ?? cell.col ?? cell.c, index % columns);
      const column = Math.floor(clamp(columnValue, 0, columns - 1));
      const value = clamp(
        finiteNumber(cell.value ?? cell.level ?? cell.intensity, cell.active ? 1 : 0.42),
        0,
        1
      );
      const id = String(cell.id || `cell-${index}`);
      return {
        ...cell,
        id,
        row,
        column,
        value,
        label: String(cell.label || cell.name || id).slice(0, 10),
      };
    });
}

function drawAccessMatrix(primitive, w, h, now) {
  const props = primitive.props || {};
  const rows = Math.max(1, Math.min(10, Math.floor(finiteNumber(props.rows, 4))));
  const columns = Math.max(1, Math.min(14, Math.floor(finiteNumber(props.columns, 6))));
  const cells = accessMatrixCells(props, rows, columns);
  const rect = accessMatrixRect(props, w, h);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.76), 0, 1);
  const speed = Math.max(0, finiteNumber(props.speed, 0.72));
  const seed = finiteNumber(props.seed, 0);
  const hasSweep = props.sweep !== false;
  const hasLabels = props.labels !== false;
  const focusCellId = props.focusCellId || props.focusId || "";
  const activeCount = cells.filter((cell) => cell.active || cell.id === focusCellId).length;
  const lockedCount = cells.filter((cell) => cell.locked).length;
  const breachedCount = cells.filter((cell) => cell.breached || cell.breach).length;
  const gap = Math.max(2, 3.2 * devicePixelRatio);
  const headerHeight = props.label ? 18 * devicePixelRatio : 6 * devicePixelRatio;
  const gridX = rect.x + 7 * devicePixelRatio;
  const gridY = rect.y + headerHeight + 4 * devicePixelRatio;
  const gridWidth = rect.width - 14 * devicePixelRatio;
  const gridHeight = rect.height - headerHeight - 11 * devicePixelRatio;
  const cellWidth = Math.max(4 * devicePixelRatio, (gridWidth - gap * (columns - 1)) / columns);
  const cellHeight = Math.max(4 * devicePixelRatio, (gridHeight - gap * (rows - 1)) / rows);

  if (typeof window !== "undefined") {
    window.__gibsonAccessMatrixState = window.__gibsonAccessMatrixState || {};
    window.__gibsonAccessMatrixState[primitive.id] = {
      rowCount: rows,
      columnCount: columns,
      cellCount: cells.length,
      activeCount,
      lockedCount,
      breachedCount,
      focusCellId: focusCellId || null,
      tone,
      accentTone,
      hasSweep,
      hasLabels,
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, 0.48 * opacity);
  ctx.shadowBlur = 14 * devicePixelRatio;
  ctx.fillStyle = toneColor("white", 0.018 * opacity);
  ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
  ctx.strokeStyle = toneColor(tone, 0.28 * opacity);
  ctx.lineWidth = Math.max(1, 1.1 * devicePixelRatio);
  ctx.strokeRect(rect.x, rect.y, rect.width, rect.height);

  if (props.label) {
    ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.72 * opacity);
    ctx.fillText(String(props.label).slice(0, 24), rect.x + 7 * devicePixelRatio, rect.y + 10 * devicePixelRatio);
  }

  for (const [index, cell] of cells.entries()) {
    const x = gridX + cell.column * (cellWidth + gap);
    const y = gridY + cell.row * (cellHeight + gap);
    const focused = cell.id === focusCellId;
    const active = Boolean(cell.active || focused);
    const breached = Boolean(cell.breached || cell.breach);
    const locked = Boolean(cell.locked);
    const cellTone = breached ? "red" : (cell.tone || (active ? accentTone : tone));
    const pulse = 0.5 + Math.sin(now * 0.004 * speed + seed + index * 0.83) * 0.5;
    const activity = clamp(cell.value + (active ? 0.24 : 0) + (focused ? 0.20 : 0), 0, 1);
    const alpha = (0.08 + activity * 0.30 + pulse * (active ? 0.18 : 0.04)) * opacity;
    ctx.fillStyle = toneColor(cellTone, alpha);
    ctx.fillRect(x, y, cellWidth, cellHeight);
    ctx.strokeStyle = toneColor(cellTone, (0.20 + activity * 0.42) * opacity);
    ctx.lineWidth = Math.max(0.7, (0.8 + (focused ? 0.8 : 0)) * devicePixelRatio);
    ctx.strokeRect(x, y, cellWidth, cellHeight);

    if (locked) {
      const lockX = x + cellWidth * 0.50;
      const lockY = y + cellHeight * 0.42;
      ctx.strokeStyle = toneColor("white", (0.28 + activity * 0.42) * opacity);
      ctx.lineWidth = Math.max(0.7, 0.9 * devicePixelRatio);
      ctx.strokeRect(lockX - cellWidth * 0.16, lockY, cellWidth * 0.32, cellHeight * 0.22);
      ctx.beginPath();
      ctx.arc(lockX, lockY, Math.min(cellWidth, cellHeight) * 0.14, Math.PI, Math.PI * 2);
      ctx.stroke();
    }

    if (breached) {
      ctx.strokeStyle = toneColor("white", (0.42 + pulse * 0.36) * opacity);
      ctx.lineWidth = Math.max(0.8, 1.15 * devicePixelRatio);
      ctx.beginPath();
      ctx.moveTo(x + cellWidth * 0.22, y + cellHeight * 0.22);
      ctx.lineTo(x + cellWidth * 0.78, y + cellHeight * 0.78);
      ctx.moveTo(x + cellWidth * 0.78, y + cellHeight * 0.22);
      ctx.lineTo(x + cellWidth * 0.22, y + cellHeight * 0.78);
      ctx.stroke();
    }

    if (hasLabels && cell.label && cellWidth > 28 * devicePixelRatio && cellHeight > 16 * devicePixelRatio) {
      const labelFontSize = Math.max(
        6 * devicePixelRatio,
        Math.min(8.5 * devicePixelRatio, cellHeight * 0.22)
      );
      ctx.font = `${labelFontSize}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = toneColor("white", (0.34 + activity * 0.34) * opacity);
      ctx.fillText(cell.label, x + cellWidth * 0.5, y + cellHeight - 3 * devicePixelRatio);
    }
  }

  if (hasSweep) {
    const sweep = (now * 0.00020 * speed + seed * 0.011) % 1;
    const sweepX = gridX + sweep * gridWidth;
    const gradient = ctx.createLinearGradient(sweepX - gridWidth * 0.18, 0, sweepX + gridWidth * 0.18, 0);
    gradient.addColorStop(0, toneColor(accentTone, 0));
    gradient.addColorStop(0.5, toneColor(accentTone, 0.34 * opacity));
    gradient.addColorStop(1, toneColor(accentTone, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(sweepX - gridWidth * 0.18, gridY, gridWidth * 0.36, gridHeight);
  }

  ctx.restore();
}

function orbitalMapNodes(props) {
  const seed = finiteNumber(props.seed, 0);
  const rawNodes = Array.isArray(props.nodes) && props.nodes.length
    ? props.nodes
    : [
      {id: "local", label: "LOCAL", lat: 18, lon: -112, tone: "green", active: true},
      {id: "relay", label: "RELAY", lat: 42, lon: -38, tone: props.tone || "cyan"},
      {id: "ice", label: "ICE", lat: 8, lon: 24, tone: "magenta"},
      {id: "gibson", label: "GIBSON", lat: 27, lon: 115, tone: props.accentTone || "amber", active: true},
    ];
  return rawNodes
    .filter((node) => node && typeof node === "object")
    .slice(0, 32)
    .map((node, index) => {
      const id = String(node.id || `node-${index}`);
      return {
        ...node,
        id,
        label: String(node.label || node.name || id).slice(0, 14),
        lat: clamp(finiteNumber(node.lat ?? node.latitude, -38 + seededUnit(seed + index) * 76), -88, 88),
        lon: finiteNumber(node.lon ?? node.lng ?? node.longitude, seededUnit(seed + index * 3.1) * 360 - 180),
        intensity: clamp(finiteNumber(node.intensity ?? node.value, node.active ? 0.9 : 0.55), 0, 1),
      };
    });
}

function orbitalMapNodeLookup(nodes) {
  const lookup = {};
  for (const node of nodes) lookup[node.id] = node;
  return lookup;
}

function orbitalMapArcs(props, nodes) {
  const rawArcs = Array.isArray(props.arcs) ? props.arcs : [];
  if (rawArcs.length) {
    return rawArcs.filter((arc) => arc && typeof arc === "object").slice(0, 48);
  }
  return nodes.slice(1).map((node, index) => ({
    id: `arc-${index}`,
    from: nodes[Math.max(0, index % nodes.length)]?.id,
    to: node.id,
    packets: index + 1,
    active: index % 2 === 0,
  }));
}

function orbitalProject(lat, lon, center, radius, phase, tilt) {
  const latRad = clamp(finiteNumber(lat, 0), -89, 89) * Math.PI / 180;
  const lonRad = finiteNumber(lon, 0) * Math.PI / 180 + phase;
  const cosLat = Math.cos(latRad);
  const x = cosLat * Math.sin(lonRad);
  const y = Math.sin(latRad);
  const z = cosLat * Math.cos(lonRad);
  const tiltedY = y * Math.cos(tilt) - z * Math.sin(tilt);
  const tiltedZ = y * Math.sin(tilt) + z * Math.cos(tilt);
  const perspective = 0.78 + tiltedZ * 0.22;
  return {
    x: center.x + x * radius * perspective,
    y: center.y - tiltedY * radius * 0.78 * perspective,
    z: tiltedZ,
    visible: tiltedZ > -0.42,
    perspective,
  };
}

function orbitalArcEndpoint(value, fallback, nodesById) {
  if (typeof value === "string" && nodesById[value]) return nodesById[value];
  if (value && typeof value === "object") return value;
  return fallback;
}

function orbitalArcPoints(arc, nodes, nodesById, center, radius, phase, tilt) {
  const from = orbitalArcEndpoint(arc.from ?? arc.source, nodes[0], nodesById);
  const to = orbitalArcEndpoint(arc.to ?? arc.target, nodes[1] || nodes[0], nodesById);
  if (!from || !to) return [];
  const fromLat = finiteNumber(from.lat ?? from.latitude, 0);
  const toLat = finiteNumber(to.lat ?? to.latitude, 0);
  const fromLon = finiteNumber(from.lon ?? from.lng ?? from.longitude, 0);
  const toLon = finiteNumber(to.lon ?? to.lng ?? to.longitude, 0);
  let lonDelta = toLon - fromLon;
  if (lonDelta > 180) lonDelta -= 360;
  if (lonDelta < -180) lonDelta += 360;
  const lift = clamp(finiteNumber(arc.lift, 0.10), 0, 0.45);
  const samples = Math.max(8, Math.min(32, Math.floor(finiteNumber(arc.samples, 18))));
  const points = [];
  for (let sample = 0; sample <= samples; sample++) {
    const progress = sample / samples;
    const lat = fromLat + (toLat - fromLat) * progress + Math.sin(progress * Math.PI) * lift * 52;
    const lon = fromLon + lonDelta * progress;
    points.push(orbitalProject(lat, lon, center, radius, phase, tilt));
  }
  return points;
}

function drawOrbitalPath(points, tone, alpha, width) {
  if (points.length < 2) return;
  ctx.strokeStyle = toneColor(tone, alpha);
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  for (const point of points) {
    if (!point.visible && started) {
      started = false;
      continue;
    }
    if (!started) {
      ctx.moveTo(point.x, point.y);
      started = true;
    } else {
      ctx.lineTo(point.x, point.y);
    }
  }
  ctx.stroke();
}

function drawOrbitalGrid(center, radius, phase, tilt, tone, opacity) {
  const width = Math.max(0.55, 0.75 * devicePixelRatio);
  for (let lat = -60; lat <= 60; lat += 30) {
    const points = [];
    for (let lon = -180; lon <= 180; lon += 8) {
      points.push(orbitalProject(lat, lon, center, radius, phase, tilt));
    }
    drawOrbitalPath(points, tone, lat === 0 ? 0.26 * opacity : 0.13 * opacity, width);
  }
  for (let lon = 0; lon < 360; lon += 30) {
    const points = [];
    for (let lat = -82; lat <= 82; lat += 7) {
      points.push(orbitalProject(lat, lon, center, radius, phase, tilt));
    }
    drawOrbitalPath(points, tone, 0.12 * opacity, width);
  }
}

function drawOrbitalMap(primitive, w, h, now) {
  const props = primitive.props || {};
  const center = normalizedPoint(props.position || {x: 0.5, y: 0.5}, w, h);
  const radius = clamp(finiteNumber(props.scale ?? props.radius, 0.16), 0.035, 0.42) * Math.min(w, h);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.72), 0, 1);
  const speed = Math.max(0, finiteNumber(props.speed, 0.62));
  const seed = finiteNumber(props.seed, 0);
  const phase = now * 0.00018 * speed + seed * 0.021;
  const tilt = finiteNumber(props.tilt, -0.42);
  const nodes = orbitalMapNodes(props);
  const nodesById = orbitalMapNodeLookup(nodes);
  const arcs = orbitalMapArcs(props, nodes);
  const rings = Math.max(0, Math.min(8, Math.floor(finiteNumber(props.rings, 3))));
  const packetCount = Math.max(0, Math.min(96, Math.floor(finiteNumber(props.packets, 24))));
  const focusNodeId = props.focusNodeId || props.focusId || "";
  const hasScan = props.scan !== false;
  const hasLabel = Boolean(props.label);

  if (typeof window !== "undefined") {
    window.__gibsonOrbitalMapState = window.__gibsonOrbitalMapState || {};
    window.__gibsonOrbitalMapState[primitive.id] = {
      nodeCount: nodes.length,
      arcCount: arcs.length,
      ringCount: rings,
      packetCount,
      focusNodeId: focusNodeId || null,
      tone,
      accentTone,
      hasScan,
      hasLabel,
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, 0.42 * opacity);
  ctx.shadowBlur = 12 * devicePixelRatio;
  const halo = ctx.createRadialGradient(center.x, center.y, radius * 0.18, center.x, center.y, radius * 1.45);
  halo.addColorStop(0, toneColor(tone, 0.10 * opacity));
  halo.addColorStop(0.62, toneColor(accentTone, 0.045 * opacity));
  halo.addColorStop(1, toneColor(tone, 0));
  ctx.fillStyle = halo;
  ctx.fillRect(center.x - radius * 1.55, center.y - radius * 1.55, radius * 3.1, radius * 3.1);

  for (let ring = 0; ring < rings; ring++) {
    const progress = (ring + 1) / (rings + 1);
    const ringRadius = radius * (1.04 + progress * 0.48);
    const rotation = phase * (ring % 2 ? -0.42 : 0.38) + ring * 0.74;
    ctx.strokeStyle = toneColor(ring % 2 ? accentTone : tone, (0.10 + progress * 0.06) * opacity);
    ctx.lineWidth = Math.max(0.55, (0.7 + progress * 0.55) * devicePixelRatio);
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, ringRadius, ringRadius * (0.27 + progress * 0.11), rotation, 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.strokeStyle = toneColor(tone, 0.32 * opacity);
  ctx.lineWidth = Math.max(0.8, 1.1 * devicePixelRatio);
  ctx.beginPath();
  ctx.ellipse(center.x, center.y, radius, radius * 0.78, 0, 0, Math.PI * 2);
  ctx.stroke();
  drawOrbitalGrid(center, radius, phase, tilt, tone, opacity);

  for (const [index, arc] of arcs.entries()) {
    const arcTone = arc.tone || (arc.active ? accentTone : tone);
    const points = orbitalArcPoints(arc, nodes, nodesById, center, radius, phase, tilt);
    drawOrbitalPath(
      points,
      arcTone,
      (arc.active ? 0.42 : 0.22) * opacity,
      Math.max(0.7, finiteNumber(arc.width, arc.active ? 1.2 : 0.85) * devicePixelRatio)
    );
    const localPackets = Math.max(0, Math.min(8, Math.floor(finiteNumber(arc.packets, 1))));
    for (let packet = 0; packet < localPackets && points.length; packet++) {
      const offset = (now * 0.00020 * speed + packet * 0.31 + index * 0.17 + seed * 0.013) % 1;
      const point = points[Math.min(points.length - 1, Math.floor(offset * (points.length - 1)))];
      if (!point.visible) continue;
      ctx.fillStyle = toneColor(arcTone, (0.48 + point.perspective * 0.22) * opacity);
      ctx.beginPath();
      ctx.arc(point.x, point.y, Math.max(1.1, radius * 0.010 * point.perspective), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  for (let packet = 0; packet < packetCount; packet++) {
    const ring = packet % Math.max(1, rings);
    const angle = phase * (ring % 2 ? -1.35 : 1.55) + packet * 2.399 + seed * 0.19;
    const ringRadius = radius * (1.08 + ((ring + 1) / Math.max(1, rings + 1)) * 0.42);
    const x = center.x + Math.cos(angle) * ringRadius;
    const y = center.y + Math.sin(angle) * ringRadius * (0.25 + ring * 0.035);
    ctx.fillStyle = toneColor(packet % 3 ? tone : accentTone, 0.18 * opacity);
    ctx.fillRect(x - devicePixelRatio, y - devicePixelRatio, 2 * devicePixelRatio, 2 * devicePixelRatio);
  }

  if (hasScan) {
    const scanAngle = phase * 1.8;
    const gradient = ctx.createRadialGradient(center.x, center.y, radius * 0.06, center.x, center.y, radius * 1.08);
    gradient.addColorStop(0, toneColor(accentTone, 0.16 * opacity));
    gradient.addColorStop(1, toneColor(accentTone, 0));
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(center.x, center.y);
    ctx.arc(center.x, center.y, radius * 1.05, scanAngle - 0.10, scanAngle + 0.32);
    ctx.closePath();
    ctx.fill();
  }

  for (const [index, node] of nodes.entries()) {
    const point = orbitalProject(node.lat, node.lon, center, radius, phase, tilt);
    if (!point.visible) continue;
    const focused = node.id === focusNodeId;
    const active = Boolean(node.active || focused);
    const nodeTone = node.tone || (active ? accentTone : tone);
    const pulse = 0.5 + Math.sin(now * 0.004 * speed + seed + index * 0.91) * 0.5;
    const nodeRadius = radius * (0.018 + node.intensity * 0.018 + (focused ? 0.014 : 0)) * point.perspective;
    ctx.fillStyle = toneColor(nodeTone, (0.38 + pulse * (active ? 0.36 : 0.18)) * opacity);
    ctx.strokeStyle = toneColor("white", (0.28 + pulse * 0.24) * opacity);
    ctx.lineWidth = Math.max(0.6, 0.85 * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(point.x, point.y, Math.max(2.2 * devicePixelRatio, nodeRadius), 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (focused || active || node.label) {
      ctx.font = `${Math.max(7, Math.min(10, radius * 0.060)) * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = toneColor("white", (focused ? 0.90 : 0.58) * opacity);
      ctx.fillText(node.label, point.x, point.y - nodeRadius - 4 * devicePixelRatio);
    }
  }

  if (hasLabel) {
    ctx.font = `${11 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = toneColor("white", 0.78 * opacity);
    ctx.fillText(String(props.label).slice(0, 26), center.x, center.y + radius * 0.90);
  }
  ctx.restore();
}

function dataVaultProjectPoint(x, y, z, size, phase) {
  const yaw = phase;
  const pitch = phase * 0.43;
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cx = Math.cos(pitch);
  const sx = Math.sin(pitch);
  const rx = x * cy - z * sy;
  let rz = x * sy + z * cy;
  const ry = y * cx - rz * sx;
  rz = y * sx + rz * cx;
  const perspective = 1 / Math.max(0.32, 1 + rz * 0.34);
  return {
    x: rx * size * perspective,
    y: ry * size * 0.72 * perspective,
    z: rz,
    perspective,
  };
}

function drawDataVaultCube(size, phase, layerScale, tone, accentTone, alpha) {
  const vertices = [
    [-1, -1, -1],
    [1, -1, -1],
    [1, 1, -1],
    [-1, 1, -1],
    [-1, -1, 1],
    [1, -1, 1],
    [1, 1, 1],
    [-1, 1, 1],
  ].map((point) => dataVaultProjectPoint(
    point[0] * layerScale,
    point[1] * layerScale,
    point[2] * layerScale,
    size,
    phase,
  ));
  const edges = [
    [0, 1],
    [1, 2],
    [2, 3],
    [3, 0],
    [4, 5],
    [5, 6],
    [6, 7],
    [7, 4],
    [0, 4],
    [1, 5],
    [2, 6],
    [3, 7],
  ];
  const faces = [
    [0, 1, 2, 3],
    [4, 5, 6, 7],
    [1, 5, 6, 2],
  ];

  for (const [faceIndex, face] of faces.entries()) {
    const averageZ = face.reduce((total, index) => total + vertices[index].z, 0) / face.length;
    const faceTone = faceIndex % 2 ? accentTone : tone;
    ctx.fillStyle = toneColor(faceTone, alpha * (0.035 + Math.max(0, averageZ) * 0.012));
    ctx.beginPath();
    ctx.moveTo(vertices[face[0]].x, vertices[face[0]].y);
    for (const index of face.slice(1)) ctx.lineTo(vertices[index].x, vertices[index].y);
    ctx.closePath();
    ctx.fill();
  }

  for (const [edgeIndex, edge] of edges.entries()) {
    const left = vertices[edge[0]];
    const right = vertices[edge[1]];
    const depth = clamp((left.z + right.z + 2) / 4, 0, 1);
    const edgeTone = edgeIndex % 3 === 0 ? accentTone : tone;
    ctx.strokeStyle = toneColor(edgeTone, alpha * (0.26 + depth * 0.34));
    ctx.lineWidth = Math.max(0.6, (0.8 + depth * 1.3) * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(left.x, left.y);
    ctx.lineTo(right.x, right.y);
    ctx.stroke();
  }

  return vertices;
}

function drawDataVault(primitive, w, h, now) {
  const props = primitive.props || {};
  const center = normalizedPoint(props.position || {x: 0.5, y: 0.5}, w, h);
  const size = clamp(finiteNumber(props.scale, 0.18), 0.04, 0.52) * Math.min(w, h);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.82), 0, 1);
  const layerCount = Math.max(1, Math.min(7, Math.floor(finiteNumber(props.layers, 3))));
  const ringCount = Math.max(0, Math.min(12, Math.floor(finiteNumber(props.rings, 4))));
  const panelCount = Math.max(0, Math.min(16, Math.floor(finiteNumber(props.panels, 4))));
  const lockCount = Math.max(0, Math.min(16, Math.floor(finiteNumber(props.locks, 4))));
  const packetCount = Math.max(0, Math.min(120, Math.floor(finiteNumber(props.packets, 28))));
  const spin = finiteNumber(props.spin, 0.58);
  const seed = finiteNumber(props.seed, 0);
  const phase = now * 0.00032 * spin + seed * 0.041;
  const hasLabel = Boolean(props.label);

  if (typeof window !== "undefined") {
    window.__gibsonDataVaultState = window.__gibsonDataVaultState || {};
    window.__gibsonDataVaultState[primitive.id] = {
      layerCount,
      ringCount,
      panelCount,
      lockCount,
      packetCount,
      tone,
      accentTone,
      hasLabel,
      phase: vectorRounded(((phase / (Math.PI * 2)) % 1 + 1) % 1),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.translate(center.x, center.y);
  ctx.shadowColor = toneColor(tone, 0.72);
  ctx.shadowBlur = 18 * devicePixelRatio;

  for (let ring = 0; ring < ringCount; ring++) {
    const progress = (ring + 1) / Math.max(1, ringCount);
    const radius = size * (0.38 + progress * 0.82);
    const ringTone = ring % 2 ? accentTone : tone;
    ctx.save();
    ctx.rotate(phase * (ring % 2 ? -0.52 : 0.68) + ring * 0.34);
    ctx.scale(1, 0.28 + progress * 0.16);
    ctx.setLineDash([
      (8 + ring * 2) * devicePixelRatio,
      (7 + ring) * devicePixelRatio,
    ]);
    ctx.lineDashOffset = -now * 0.026 * (ring % 2 ? -1 : 1);
    ctx.strokeStyle = toneColor(ringTone, 0.16 + progress * 0.24);
    ctx.lineWidth = Math.max(0.6, (0.7 + progress * 1.2) * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
  ctx.setLineDash([]);

  let coreVertices = [];
  for (let layer = 0; layer < layerCount; layer++) {
    const depth = layerCount <= 1 ? 1 : layer / (layerCount - 1);
    const layerScale = 0.42 + depth * 0.58;
    const layerTone = layer % 2 ? accentTone : tone;
    const vertices = drawDataVaultCube(size, phase + layer * 0.16, layerScale, layerTone, tone, 0.92 - depth * 0.18);
    if (layer === layerCount - 1) coreVertices = vertices;
  }

  for (let panel = 0; panel < panelCount; panel++) {
    const angle = phase * 0.88 + panel * (Math.PI * 2 / Math.max(1, panelCount));
    const x = Math.cos(angle) * size * 1.08;
    const y = Math.sin(angle) * size * 0.48;
    const width = size * (0.18 + seededUnit(seed + panel * 5.7) * 0.08);
    const height = size * (0.10 + seededUnit(seed + panel * 8.1) * 0.06);
    const panelTone = panel % 2 ? accentTone : tone;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(Math.sin(angle) * 0.28);
    ctx.fillStyle = toneColor(panelTone, 0.065);
    ctx.strokeStyle = toneColor(panelTone, 0.42);
    ctx.lineWidth = Math.max(0.5, 0.85 * devicePixelRatio);
    ctx.fillRect(-width * 0.5, -height * 0.5, width, height);
    ctx.strokeRect(-width * 0.5, -height * 0.5, width, height);
    const scan = ((now * 0.00024 + panel * 0.17 + seed * 0.003) % 1 - 0.5) * width;
    ctx.strokeStyle = toneColor("white", 0.38);
    ctx.beginPath();
    ctx.moveTo(scan, -height * 0.42);
    ctx.lineTo(scan, height * 0.42);
    ctx.stroke();
    ctx.restore();
  }

  for (let lock = 0; lock < lockCount; lock++) {
    const angle = phase * (lock % 2 ? -1.18 : 1.34) + lock * (Math.PI * 2 / Math.max(1, lockCount));
    const x = Math.cos(angle) * size * 0.72;
    const y = Math.sin(angle) * size * 0.34;
    const radius = size * (0.045 + seededUnit(seed + lock * 4.4) * 0.025);
    const lockTone = lock % 3 === 0 ? "white" : (lock % 2 ? accentTone : tone);
    ctx.strokeStyle = toneColor(lockTone, 0.58);
    ctx.lineWidth = Math.max(0.7, 1.1 * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(x, y, radius, Math.PI * 0.15, Math.PI * 1.85);
    ctx.stroke();
    ctx.strokeRect(x - radius * 0.58, y - radius * 0.06, radius * 1.16, radius * 0.92);
  }

  for (let packet = 0; packet < packetCount; packet++) {
    const packetSeed = seed + packet * 11.37;
    const angle = phase * (0.7 + seededUnit(packetSeed) * 1.4) + packet * 2.399;
    const orbit = size * (0.48 + seededUnit(packetSeed + 2.1) * 0.55);
    const x = Math.cos(angle) * orbit;
    const y = Math.sin(angle * 0.74 + seededUnit(packetSeed + 3.2)) * orbit * 0.36;
    const alpha = 0.18 + seededUnit(packetSeed + 5.5) * 0.48;
    ctx.fillStyle = toneColor(packet % 5 === 0 ? "white" : (packet % 2 ? accentTone : tone), alpha);
    ctx.beginPath();
    ctx.arc(x, y, (0.85 + seededUnit(packetSeed + 8.8) * 1.8) * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }

  if (coreVertices.length) {
    ctx.fillStyle = toneColor("white", 0.66);
    for (const point of coreVertices.filter((_, index) => index % 2 === 0)) {
      ctx.beginPath();
      ctx.arc(point.x, point.y, Math.max(1.1, 1.5 * point.perspective) * devicePixelRatio, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  if (hasLabel) {
    ctx.font = `${Math.max(9, size * 0.092)}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = toneColor(accentTone, 0.82);
    ctx.shadowBlur = 10 * devicePixelRatio;
    ctx.fillStyle = toneColor("white", 0.84);
    ctx.fillText(String(props.label).slice(0, 20), 0, -size * 1.18);
  }

  ctx.restore();
}

function blackIceBreachPoint(props, left, top, width, height) {
  const raw = props.breachPosition && typeof props.breachPosition === "object" ? props.breachPosition : {};
  return {
    x: left + clamp(finiteNumber(raw.x, 0.52), 0, 1) * width,
    y: top + clamp(finiteNumber(raw.y, 0.50), 0, 1) * height,
  };
}

function drawBlackIce(primitive, w, h, now) {
  const props = primitive.props || {};
  const center = normalizedPoint(props.position || {x: 0.52, y: 0.46}, w, h);
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.46), 0.08, 1.6) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.34), 0.06, 1.2) * h;
  const columns = Math.max(4, Math.min(28, Math.floor(finiteNumber(props.columns, 12))));
  const rows = Math.max(2, Math.min(16, Math.floor(finiteNumber(props.rows, 6))));
  const fractureCount = Math.max(0, Math.min(80, Math.floor(finiteNumber(props.fractures, 14))));
  const sentryCount = Math.max(0, Math.min(18, Math.floor(finiteNumber(props.sentries, 5))));
  const breach = clamp(finiteNumber(props.breach ?? (props.open ? 0.54 : 0.18), 0.18), 0, 1);
  const depth = clamp(finiteNumber(props.depth, 0.32), 0, 1.2);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.84), 0, 1);
  const seed = finiteNumber(props.seed, 0);
  const sweepSpeed = finiteNumber(props.sweepSpeed, 0.82);
  const sweep = props.sweep !== false;
  const phase = ((now * 0.00018 * sweepSpeed + seed * 0.021) % 1 + 1) % 1;
  const left = center.x - width * 0.5;
  const top = center.y - height * 0.5;
  const breachPoint = blackIceBreachPoint(props, left, top, width, height);
  const cellWidth = width / columns;
  const cellHeight = height / rows;

  if (typeof window !== "undefined") {
    window.__gibsonBlackIceState = window.__gibsonBlackIceState || {};
    window.__gibsonBlackIceState[primitive.id] = {
      columnCount: columns,
      rowCount: rows,
      fractureCount,
      sentryCount,
      breach: vectorRounded(breach),
      tone,
      accentTone,
      hasSweep: sweep,
      hasLabel: Boolean(props.label),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, 0.72);
  ctx.shadowBlur = 18 * devicePixelRatio;

  const glow = ctx.createRadialGradient(breachPoint.x, breachPoint.y, 0, breachPoint.x, breachPoint.y, width * 0.52);
  glow.addColorStop(0, toneColor(accentTone, 0.16 + breach * 0.16));
  glow.addColorStop(0.5, toneColor(tone, 0.06));
  glow.addColorStop(1, toneColor(tone, 0));
  ctx.fillStyle = glow;
  ctx.fillRect(left - width * 0.16, top - height * 0.18, width * 1.32, height * 1.36);

  for (let row = 0; row < rows; row++) {
    const rowDepth = rows <= 1 ? 0 : row / (rows - 1);
    const skew = (rowDepth - 0.5) * width * depth * 0.18;
    for (let column = 0; column < columns; column++) {
      const index = row * columns + column;
      const x0 = left + column * cellWidth + skew;
      const x1 = x0 + cellWidth * (0.92 + seededUnit(seed + index * 2.1) * 0.12);
      const y0 = top + row * cellHeight;
      const y1 = y0 + cellHeight * (0.88 + seededUnit(seed + index * 3.7) * 0.15);
      const centerX = (x0 + x1) * 0.5;
      const centerY = (y0 + y1) * 0.5;
      const distanceToBreach = Math.hypot((centerX - breachPoint.x) / width, (centerY - breachPoint.y) / height);
      const breachActivity = clamp(1 - distanceToBreach / Math.max(0.12, 0.18 + breach * 0.32), 0, 1);
      const panelTone = breachActivity > 0.32 ? accentTone : (index % 3 === 0 ? "white" : tone);
      const jitter = Math.sin(now * 0.0011 + seed + index * 0.59) * cellHeight * 0.05;
      const points = [
        {x: x0 + cellWidth * 0.12, y: y0 + jitter},
        {x: x1 - cellWidth * 0.05, y: y0 + cellHeight * 0.10 - jitter * 0.4},
        {x: x1 - cellWidth * 0.14, y: y1 - cellHeight * 0.06},
        {x: x0 + cellWidth * 0.04, y: y1 - cellHeight * 0.12 + jitter * 0.35},
      ];
      const fillAlpha = 0.055 + rowDepth * 0.034 + breachActivity * 0.18;
      const strokeAlpha = 0.28 + breachActivity * 0.44 + (index % 5 === 0 ? 0.12 : 0);
      drawPolygon(points, toneColor(panelTone, fillAlpha), toneColor(panelTone, strokeAlpha));
      if ((index + Math.floor(phase * columns)) % 7 === 0) {
        ctx.strokeStyle = toneColor(accentTone, 0.28 + breachActivity * 0.35);
        ctx.lineWidth = Math.max(0.45, 0.8 * devicePixelRatio);
        ctx.beginPath();
        ctx.moveTo(points[0].x, (points[0].y + points[3].y) * 0.5);
        ctx.lineTo(points[1].x, (points[1].y + points[2].y) * 0.5);
        ctx.stroke();
      }
    }
  }

  ctx.strokeStyle = toneColor("white", 0.28 + breach * 0.18);
  ctx.lineWidth = Math.max(0.7, 1.0 * devicePixelRatio);
  for (let crack = 0; crack < fractureCount; crack++) {
    const crackSeed = seed + crack * 9.17;
    const angle = seededUnit(crackSeed) * Math.PI * 2;
    const startRadius = (0.04 + seededUnit(crackSeed + 2.7) * 0.16) * Math.min(width, height);
    const length = (0.05 + seededUnit(crackSeed + 5.3) * 0.22) * Math.min(width, height);
    const x0 = breachPoint.x + Math.cos(angle) * startRadius;
    const y0 = breachPoint.y + Math.sin(angle) * startRadius * 0.72;
    const midX = x0 + Math.cos(angle + seededUnit(crackSeed + 1.2) * 0.72 - 0.36) * length * 0.55;
    const midY = y0 + Math.sin(angle + seededUnit(crackSeed + 3.4) * 0.72 - 0.36) * length * 0.42;
    const x1 = x0 + Math.cos(angle) * length;
    const y1 = y0 + Math.sin(angle) * length * 0.72;
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(midX, midY);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  if (breach > 0) {
    const radius = Math.min(width, height) * (0.06 + breach * 0.16);
    for (let ring = 0; ring < 4; ring++) {
      const ringProgress = (phase + ring * 0.21) % 1;
      ctx.strokeStyle = toneColor(ring % 2 ? accentTone : "white", (1 - ringProgress) * (0.24 + breach * 0.32));
      ctx.lineWidth = Math.max(0.8, (1 + ring * 0.55) * devicePixelRatio);
      ctx.setLineDash([
        (10 + ring * 4) * devicePixelRatio,
        (7 + ring * 2) * devicePixelRatio,
      ]);
      ctx.lineDashOffset = -now * 0.028 * (ring + 1);
      ctx.beginPath();
      ctx.ellipse(
        breachPoint.x,
        breachPoint.y,
        radius * (1 + ringProgress * 1.4),
        radius * (0.48 + ringProgress * 0.72),
        0,
        0,
        Math.PI * 2,
      );
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  if (sweep) {
    const sweepX = left + width * phase;
    const gradient = ctx.createLinearGradient(sweepX - width * 0.10, top, sweepX + width * 0.10, top + height);
    gradient.addColorStop(0, toneColor(accentTone, 0));
    gradient.addColorStop(0.5, toneColor(accentTone, 0.44));
    gradient.addColorStop(1, toneColor("white", 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(sweepX - width * 0.11, top - height * 0.05, width * 0.22, height * 1.10);
    ctx.strokeStyle = toneColor("white", 0.64);
    ctx.lineWidth = Math.max(0.7, 1.1 * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(sweepX, top - height * 0.05);
    ctx.lineTo(sweepX + depth * width * 0.08, top + height * 1.05);
    ctx.stroke();
  }

  for (let sentry = 0; sentry < sentryCount; sentry++) {
    const progress = sentryCount <= 1 ? 0 : sentry / (sentryCount - 1);
    const side = sentry % 2;
    const x = left + width * progress;
    const y = side ? top + height * 1.06 : top - height * 0.06;
    const lockSize = Math.max(5, Math.min(width, height) * (0.018 + seededUnit(seed + sentry * 4.4) * 0.014));
    const pulse = 0.72 + Math.sin(now * 0.004 + seed + sentry) * 0.20;
    const sentryTone = sentry % 3 === 0 ? accentTone : tone;
    ctx.strokeStyle = toneColor(sentryTone, 0.42 + pulse * 0.28);
    ctx.fillStyle = toneColor(sentryTone, 0.09 + pulse * 0.08);
    ctx.lineWidth = Math.max(0.8, 1.1 * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(x, y - lockSize * 0.42, lockSize * 0.58, Math.PI * 1.05, Math.PI * 1.95);
    ctx.stroke();
    ctx.fillRect(x - lockSize * 0.58, y - lockSize * 0.18, lockSize * 1.16, lockSize * 0.86);
    ctx.strokeRect(x - lockSize * 0.58, y - lockSize * 0.18, lockSize * 1.16, lockSize * 0.86);
  }

  ctx.strokeStyle = toneColor(tone, 0.64);
  ctx.lineWidth = Math.max(1, 1.4 * devicePixelRatio);
  ctx.strokeRect(left, top, width, height);
  if (props.label) {
    ctx.font = `${11.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.shadowColor = toneColor(accentTone, 0.82);
    ctx.shadowBlur = 9 * devicePixelRatio;
    ctx.fillStyle = toneColor("white", 0.84);
    ctx.fillText(String(props.label).slice(0, 24), center.x, top - 8 * devicePixelRatio);
  }
  ctx.restore();
}

function signalScopeBlips(props) {
  if (Array.isArray(props.blips)) {
    return props.blips
      .filter((blip) => blip && typeof blip === "object")
      .slice(0, 48);
  }
  const count = Math.max(0, Math.min(48, Math.floor(finiteNumber(props.blips, 9))));
  return Array.from({length: count}, (_, index) => ({
    id: `auto-${index}`,
    angle: seededUnit(finiteNumber(props.seed, 0) + index * 3.17) * Math.PI * 2,
    radius: 0.12 + seededUnit(finiteNumber(props.seed, 0) + index * 5.73) * 0.78,
    tone: index % 5 === 0 ? props.accentTone || "magenta" : props.tone || "green",
    intensity: 0.42 + seededUnit(finiteNumber(props.seed, 0) + index * 7.91) * 0.52,
  }));
}

function signalScopeBlipPoint(blip, radius, now, seed) {
  const hasPoint = Number.isFinite(Number(blip.x)) || Number.isFinite(Number(blip.y));
  if (hasPoint) {
    return {
      x: clamp(finiteNumber(blip.x, 0), -1, 1) * radius,
      y: clamp(finiteNumber(blip.y, 0), -1, 1) * radius,
    };
  }
  const angle = finiteNumber(blip.angle, seededUnit(seed) * Math.PI * 2)
    + finiteNumber(blip.drift, 0) * now * 0.00008;
  const distance = clamp(finiteNumber(blip.radius, 0.55), 0, 1) * radius;
  return {x: Math.cos(angle) * distance, y: Math.sin(angle) * distance};
}

function signalScopeWaveforms(props) {
  const raw = Array.isArray(props.waveforms) ? props.waveforms : [];
  return raw.filter((waveform) => waveform && typeof waveform === "object").slice(0, 8);
}

function drawSignalScopeWaveforms(waveforms, props, radius, now) {
  if (!waveforms.length) return;
  const tone = props.tone || "green";
  const accentTone = props.accentTone || props.accent || "cyan";
  const top = radius * 0.42;
  const height = radius * 0.42;
  const left = -radius * 0.82;
  const width = radius * 1.64;
  ctx.save();
  ctx.beginPath();
  ctx.arc(0, 0, radius * 0.98, 0, Math.PI * 2);
  ctx.clip();
  ctx.beginPath();
  ctx.rect(left, top, width, height);
  ctx.clip();
  ctx.strokeStyle = toneColor(tone, 0.16);
  ctx.lineWidth = 0.65 * devicePixelRatio;
  for (let row = 0; row <= 3; row++) {
    const y = top + (row / 3) * height;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
  }
  for (const [index, waveform] of waveforms.entries()) {
    const samples = Math.max(12, Math.min(128, Math.floor(finiteNumber(waveform.samples, 64))));
    const amplitude = clamp(finiteNumber(waveform.amplitude, 0.28), 0, 1) * height * 0.44;
    const frequency = Math.max(0.1, finiteNumber(waveform.frequency, 2.6));
    const speed = finiteNumber(waveform.speed, 0.0015);
    const phase = now * speed + finiteNumber(waveform.phase, 0) + index * 0.73;
    const centerY = top + height * (0.22 + ((index + 0.5) / Math.max(1, waveforms.length)) * 0.54);
    const waveTone = waveform.tone || (index % 2 ? accentTone : tone);
    ctx.shadowColor = toneColor(waveTone, 0.72);
    ctx.shadowBlur = 7 * devicePixelRatio;
    ctx.lineWidth = Math.max(0.8, finiteNumber(waveform.width, 1.1) * devicePixelRatio);
    ctx.strokeStyle = toneColor(waveTone, clamp(finiteNumber(waveform.alpha, 0.74), 0, 1));
    ctx.beginPath();
    for (let sample = 0; sample < samples; sample++) {
      const progress = sample / Math.max(1, samples - 1);
      const x = left + progress * width;
      const carrier = Math.sin(progress * Math.PI * 2 * frequency + phase);
      const harmonic = Math.sin(progress * Math.PI * 2 * (frequency * 0.5 + 0.7) - phase * 0.7) * 0.36;
      const y = centerY + (carrier + harmonic) * amplitude;
      if (sample === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    if (waveform.label) {
      ctx.font = `${8.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = toneColor("white", 0.62);
      ctx.fillText(String(waveform.label).slice(0, 10), left + 3 * devicePixelRatio, centerY);
    }
  }
  ctx.restore();
}

function drawSignalScope(primitive, w, h, now) {
  const props = primitive.props || {};
  const center = normalizedPoint(props.position || {x: 0.78, y: 0.33}, w, h);
  const radius = clamp(finiteNumber(props.scale ?? props.radius, 0.16), 0.04, 0.46) * Math.min(w, h);
  if (radius <= 0) return;
  const mode = String(props.mode || "hybrid");
  const tone = props.tone || "green";
  const accentTone = props.accentTone || props.accent || "cyan";
  const opacity = clamp(finiteNumber(props.opacity, 0.82), 0, 1);
  const rings = Math.max(1, Math.min(9, Math.floor(finiteNumber(props.rings, 4))));
  const spokes = Math.max(0, Math.min(24, Math.floor(finiteNumber(props.spokes, 8))));
  const sweepEnabled = props.sweep !== false;
  const sweepSpeed = finiteNumber(props.sweepSpeed, 0.9);
  const seed = finiteNumber(props.seed, 0);
  const sweepAngle = now * 0.00105 * sweepSpeed + seed * 0.071;
  const blips = signalScopeBlips(props);
  const waveforms = signalScopeWaveforms(props);
  const hasLabels = Boolean(props.label)
    || blips.some((blip) => Boolean(blip.label))
    || waveforms.some((waveform) => Boolean(waveform.label));

  if (typeof window !== "undefined") {
    window.__gibsonSignalScopeState = window.__gibsonSignalScopeState || {};
    window.__gibsonSignalScopeState[primitive.id] = {
      mode,
      ringCount: rings,
      spokeCount: spokes,
      blipCount: blips.length,
      waveformCount: waveforms.length,
      hasSweep: sweepEnabled,
      tone,
      accentTone,
      hasLabels,
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.translate(center.x, center.y);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, 0.66);
  ctx.shadowBlur = 12 * devicePixelRatio;
  ctx.strokeStyle = toneColor(tone, 0.34);
  ctx.lineWidth = Math.max(0.55, 0.85 * devicePixelRatio);
  for (let ring = 1; ring <= rings; ring++) {
    const ringRadius = (ring / rings) * radius;
    ctx.beginPath();
    ctx.arc(0, 0, ringRadius, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (spokes > 0) {
    ctx.strokeStyle = toneColor(tone, 0.22);
    for (let spoke = 0; spoke < spokes; spoke++) {
      const angle = (spoke / spokes) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(Math.cos(angle) * radius * 0.12, Math.sin(angle) * radius * 0.12);
      ctx.lineTo(Math.cos(angle) * radius, Math.sin(angle) * radius);
      ctx.stroke();
    }
  }
  ctx.strokeStyle = toneColor(accentTone, 0.54);
  ctx.lineWidth = Math.max(1, 1.25 * devicePixelRatio);
  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.stroke();
  if (sweepEnabled) {
    const gradient = ctx.createRadialGradient(0, 0, radius * 0.08, 0, 0, radius);
    gradient.addColorStop(0, toneColor(accentTone, 0.22));
    gradient.addColorStop(1, toneColor(accentTone, 0));
    ctx.save();
    ctx.rotate(sweepAngle);
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, radius, -0.34, 0.04);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = toneColor(accentTone, 0.74);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(radius, 0);
    ctx.stroke();
    ctx.restore();
  }
  for (const [index, blip] of blips.entries()) {
    const point = signalScopeBlipPoint(blip, radius, now, seed + index * 13.1);
    const distance = Math.hypot(point.x, point.y);
    if (distance > radius) continue;
    const blipTone = blip.tone || (index % 4 === 0 ? accentTone : tone);
    const intensity = clamp(finiteNumber(blip.intensity, 0.72), 0, 1);
    const pulse = 1 + Math.sin(now * 0.0045 + seed + index * 0.9) * 0.18;
    const size = Math.max(1.4, finiteNumber(blip.size, 2.3) * devicePixelRatio * pulse);
    ctx.shadowColor = toneColor(blipTone, 0.82 * intensity);
    ctx.shadowBlur = 11 * devicePixelRatio * intensity;
    ctx.fillStyle = toneColor(blipTone, 0.58 * intensity);
    ctx.beginPath();
    ctx.arc(point.x, point.y, size, 0, Math.PI * 2);
    ctx.fill();
    if (blip.label) {
      ctx.font = `${8.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = toneColor("white", 0.64);
      ctx.fillText(String(blip.label).slice(0, 10), point.x, point.y - size - 3 * devicePixelRatio);
    }
  }
  if (mode !== "radar") drawSignalScopeWaveforms(waveforms, props, radius, now);
  if (props.label) {
    ctx.font = `${11 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.82);
    ctx.shadowBlur = 5 * devicePixelRatio;
    ctx.fillText(String(props.label).slice(0, 22), 0, -radius - 13 * devicePixelRatio);
  }
  ctx.restore();
}

function tunnelPoint(center, width, height, progress, angle, rotation) {
  const radius = Math.pow(clamp(progress, 0, 1), 1.35);
  const rotated = angle + rotation * (1 - radius);
  return {
    x: center.x + Math.cos(rotated) * width * (0.04 + radius * 0.5),
    y: center.y + Math.sin(rotated) * height * (0.035 + radius * 0.48),
    radius,
  };
}

function drawTunnelRing(center, width, height, progress, rotation, tone, alpha) {
  const corners = [
    tunnelPoint(center, width, height, progress, -Math.PI * 0.75, rotation),
    tunnelPoint(center, width, height, progress, -Math.PI * 0.25, rotation),
    tunnelPoint(center, width, height, progress, Math.PI * 0.25, rotation),
    tunnelPoint(center, width, height, progress, Math.PI * 0.75, rotation),
  ];
  ctx.beginPath();
  ctx.moveTo(corners[0].x, corners[0].y);
  for (const corner of corners.slice(1)) ctx.lineTo(corner.x, corner.y);
  ctx.closePath();
  ctx.strokeStyle = toneColor(tone, alpha);
  ctx.stroke();
  if (progress > 0.82) {
    ctx.fillStyle = toneColor(tone, alpha * 0.08);
    ctx.fill();
  }
}

function drawTunnelGrid(primitive, w, h, now) {
  const props = primitive.props || {};
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const center = normalizedPoint(props.position || {x: 0.5, y: 0.5}, w, h);
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.78), 0.08, 1.8) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.70), 0.08, 1.8) * h;
  const ringCount = Math.max(1, Math.min(36, Math.floor(finiteNumber(props.rings, 13))));
  const spokeCount = Math.max(0, Math.min(48, Math.floor(finiteNumber(props.spokes, 16))));
  const laneCount = Math.max(0, Math.min(32, Math.floor(finiteNumber(props.lanes, 8))));
  const packetCount = Math.max(0, Math.min(160, Math.floor(finiteNumber(props.packets, 34))));
  const speed = Math.max(0, finiteNumber(props.speed, 0.72));
  const twist = finiteNumber(props.twist, 0.42);
  const depth = clamp(finiteNumber(props.depth, 1), 0.25, 2.5);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.76), 0, 1);
  const seed = finiteNumber(props.seed, 0);
  const direction = props.direction === "outward" ? "outward" : "inward";
  const directionSign = direction === "outward" ? -1 : 1;
  const phase = ((now * speed * 0.00013 * directionSign + seed * 0.017) % 1 + 1) % 1;

  if (typeof window !== "undefined") {
    window.__gibsonTunnelState = window.__gibsonTunnelState || {};
    window.__gibsonTunnelState[primitive.id] = {
      ringCount,
      spokeCount,
      laneCount,
      packetCount,
      direction,
      tone,
      accentTone,
      hasLabels: Boolean(props.label),
      phase: vectorRounded(phase),
    };
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(center.x - width * 0.55, center.y - height * 0.55, width * 1.1, height * 1.1);
  ctx.clip();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, 0.62);
  ctx.shadowBlur = 15 * devicePixelRatio;

  if (spokeCount > 0) {
    ctx.lineWidth = Math.max(0.5, 0.78 * devicePixelRatio);
    for (let spoke = 0; spoke < spokeCount; spoke++) {
      const angle = (spoke / spokeCount) * Math.PI * 2 + twist * 0.2;
      const inner = tunnelPoint(center, width, height, 0.08, angle, twist);
      const outer = tunnelPoint(center, width, height, 1, angle, twist);
      ctx.strokeStyle = toneColor(spoke % 4 === 0 ? accentTone : tone, spoke % 4 === 0 ? 0.24 : 0.14);
      ctx.beginPath();
      ctx.moveTo(inner.x, inner.y);
      ctx.lineTo(outer.x, outer.y);
      ctx.stroke();
    }
  }

  ctx.lineWidth = Math.max(0.7, finiteNumber(props.lineWidth, 1.05) * devicePixelRatio);
  for (let ring = 0; ring < ringCount; ring++) {
    const rawProgress = (ring / ringCount + phase) % 1;
    const progress = Math.pow(rawProgress, 1 / depth);
    const ringTone = ring % 3 === 0 ? accentTone : tone;
    const alpha = 0.12 + progress * 0.48;
    drawTunnelRing(center, width, height, progress, twist, ringTone, alpha);
  }

  for (let lane = 0; lane < laneCount; lane++) {
    const angle = (lane / Math.max(1, laneCount)) * Math.PI * 2 + phase * twist;
    ctx.setLineDash([11 * devicePixelRatio, 13 * devicePixelRatio]);
    ctx.lineDashOffset = -now * speed * 0.024;
    ctx.strokeStyle = toneColor(lane % 2 ? accentTone : "white", 0.20);
    ctx.lineWidth = Math.max(0.55, 0.7 * devicePixelRatio);
    ctx.beginPath();
    for (let sample = 0; sample <= 20; sample++) {
      const progress = sample / 20;
      const point = tunnelPoint(center, width, height, progress, angle + progress * twist, twist);
      if (sample === 0) ctx.moveTo(point.x, point.y);
      else ctx.lineTo(point.x, point.y);
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for (let packet = 0; packet < packetCount; packet++) {
    const laneIndex = laneCount > 0 ? packet % laneCount : packet;
    const baseAngle = (laneIndex / Math.max(1, laneCount || packetCount)) * Math.PI * 2;
    const progress = (packet / Math.max(1, packetCount) + phase * 1.7 + seededUnit(seed + packet * 3.9) * 0.05) % 1;
    const angle = baseAngle + twist * (1 - progress) + seededUnit(seed + packet * 5.1) * 0.08;
    const point = tunnelPoint(center, width, height, progress, angle, twist);
    const tailProgress = clamp(progress - 0.045, 0, 1);
    const tail = tunnelPoint(center, width, height, tailProgress, angle, twist);
    const packetTone = packet % 5 === 0 ? accentTone : tone;
    const alpha = 0.16 + point.radius * 0.76;
    const radius = (1.2 + point.radius * 4.2 + seededUnit(seed + packet) * 1.5) * devicePixelRatio;
    ctx.shadowColor = toneColor(packetTone, 0.82);
    ctx.shadowBlur = (7 + point.radius * 12) * devicePixelRatio;
    ctx.strokeStyle = toneColor(packetTone, alpha * 0.42);
    ctx.lineWidth = Math.max(0.6, radius * 0.45);
    ctx.beginPath();
    ctx.moveTo(tail.x, tail.y);
    ctx.lineTo(point.x, point.y);
    ctx.stroke();
    ctx.fillStyle = toneColor(packetTone, alpha);
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.shadowBlur = 18 * devicePixelRatio;
  ctx.strokeStyle = toneColor("white", 0.34);
  ctx.lineWidth = Math.max(0.8, 0.9 * devicePixelRatio);
  ctx.beginPath();
  ctx.arc(center.x, center.y, Math.max(2, Math.min(width, height) * 0.025), 0, Math.PI * 2);
  ctx.stroke();

  if (props.label) {
    ctx.font = `${11.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.78);
    ctx.shadowBlur = 6 * devicePixelRatio;
    ctx.fillText(String(props.label).slice(0, 24), center.x, center.y - height * 0.51);
  }

  ctx.restore();
}

function wireLandscapeRect(props, w, h) {
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const position = normalizedPoint(props.position || {x: 0.5, y: 0.62}, w, h);
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.82), 0.08, 1.8) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.48), 0.08, 1.4) * h;
  return {
    x: position.x - width * 0.5,
    y: position.y - height * 0.5,
    width,
    height,
    centerX: position.x,
  };
}

function wireLandscapePeaks(props) {
  const rawPeaks = Array.isArray(props.peaks) ? props.peaks : [];
  return rawPeaks
    .filter((peak) => peak && typeof peak === "object")
    .slice(0, 32)
    .map((peak, index) => ({
      ...peak,
      id: String(peak.id || `peak-${index}`),
      label: peak.label || peak.id || `PEAK ${index + 1}`,
      x: clamp(finiteNumber(peak.x, (index + 1) / Math.max(2, rawPeaks.length + 1)), 0, 1),
      z: clamp(finiteNumber(peak.z ?? peak.y, 0.5), 0, 1),
      height: clamp(finiteNumber(peak.height ?? peak.h, 0.5), 0, 1.8),
      radius: clamp(finiteNumber(peak.radius, 0.22), 0.05, 0.7),
      tone: peak.tone || props.accentTone || props.accent || "magenta",
    }));
}

function wireLandscapeHeightAt(x, z, peaks, seed) {
  let height = 0.035 + seededUnit(seed + x * 19.7 + z * 31.1) * 0.055;
  height += (Math.sin((x * 5.6 + z * 3.1 + seed * 0.03) * Math.PI) + 1) * 0.035;
  for (const peak of peaks) {
    const distance = Math.hypot((x - peak.x) * 1.35, z - peak.z);
    const influence = clamp(1 - distance / peak.radius, 0, 1);
    height += peak.height * influence * influence;
  }
  return clamp(height, 0, 1.8);
}

function wireLandscapePoint(rect, props, x, z, heightValue, now) {
  const depth = clamp(finiteNumber(props.depth, 0.82), 0.2, 2.2);
  const heightScale = clamp(finiteNumber(props.height, 0.30), 0.04, 0.9);
  const speed = Math.max(0, finiteNumber(props.speed, 0.55));
  const perspective = 0.34 + z * (0.58 + depth * 0.18);
  const baseY = rect.y + rect.height * (0.15 + z * 0.76);
  const drift = Math.sin(now * speed * 0.00055 + x * 8.4 + z * 4.8) * rect.height * 0.006;
  return {
    x: rect.centerX + (x - 0.5) * rect.width * perspective,
    y: baseY - heightValue * rect.height * heightScale * (0.55 + z * 0.60) + drift,
    z,
    height: heightValue,
  };
}

function wireLandscapeGrid(props, rect, rows, columns, peaks, seed, now) {
  const grid = [];
  for (let row = 0; row < rows; row++) {
    const z = rows <= 1 ? 0 : row / (rows - 1);
    const points = [];
    for (let column = 0; column < columns; column++) {
      const x = columns <= 1 ? 0.5 : column / (columns - 1);
      const heightValue = wireLandscapeHeightAt(x, z, peaks, seed);
      points.push(wireLandscapePoint(rect, props, x, z, heightValue, now));
    }
    grid.push(points);
  }
  return grid;
}

function drawWireLandscapeLine(points, tone, alpha, width) {
  if (points.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
  ctx.strokeStyle = toneColor(tone, alpha);
  ctx.lineWidth = Math.max(0.45, width * devicePixelRatio);
  ctx.stroke();
}

function drawWireLandscape(primitive, w, h, now) {
  const props = primitive.props || {};
  const rect = wireLandscapeRect(props, w, h);
  const rows = Math.max(2, Math.min(28, Math.floor(finiteNumber(props.rows, 12))));
  const columns = Math.max(2, Math.min(40, Math.floor(finiteNumber(props.columns, 18))));
  const peaks = wireLandscapePeaks(props);
  const packetCount = Math.max(0, Math.min(120, Math.floor(finiteNumber(props.packets, 30))));
  const seed = finiteNumber(props.seed, 0);
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.72), 0, 1);
  const focusPeakId = props.focusPeakId ? String(props.focusPeakId) : (peaks[0]?.id || null);
  const speed = Math.max(0, finiteNumber(props.speed, 0.55));
  const grid = wireLandscapeGrid(props, rect, rows, columns, peaks, seed, now);
  const hasLabels = Boolean(props.label) || peaks.some((peak) => Boolean(peak.label));

  if (typeof window !== "undefined") {
    window.__gibsonWireLandscapeState = window.__gibsonWireLandscapeState || {};
    window.__gibsonWireLandscapeState[primitive.id] = {
      rowCount: rows,
      columnCount: columns,
      peakCount: peaks.length,
      packetCount,
      focusPeakId,
      tone,
      accentTone,
      hasLabels,
    };
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(
    rect.x - 10 * devicePixelRatio,
    rect.y - rect.height * 0.25,
    rect.width + 20 * devicePixelRatio,
    rect.height * 1.35,
  );
  ctx.clip();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, 0.55);
  ctx.shadowBlur = 11 * devicePixelRatio;

  for (let row = 0; row < rows; row++) {
    const z = rows <= 1 ? 0 : row / (rows - 1);
    drawWireLandscapeLine(grid[row], row % 4 === 0 ? accentTone : tone, 0.12 + z * 0.34, 0.65 + z * 0.85);
  }
  for (let column = 0; column < columns; column++) {
    const points = grid.map((row) => row[column]);
    drawWireLandscapeLine(points, column % 5 === 0 ? "white" : tone, column % 5 === 0 ? 0.22 : 0.15, 0.65);
  }

  ctx.setLineDash([10 * devicePixelRatio, 14 * devicePixelRatio]);
  ctx.lineDashOffset = -now * speed * 0.024;
  for (let rail = 0; rail < Math.min(5, columns); rail++) {
    const column = Math.round((rail / Math.max(1, Math.min(5, columns) - 1)) * (columns - 1));
    const points = grid.map((row) => row[column]);
    drawWireLandscapeLine(points, rail % 2 ? accentTone : "white", 0.18, 0.72);
  }
  ctx.setLineDash([]);

  for (let packet = 0; packet < packetCount; packet++) {
    const lane = packet % Math.max(1, columns);
    const rowProgress = (
      now * speed * 0.00016
      + packet / Math.max(1, packetCount)
      + seededUnit(seed + packet * 3.7) * 0.06
    ) % 1;
    const z = clamp(rowProgress, 0, 1);
    const x = columns <= 1 ? 0.5 : lane / (columns - 1);
    const heightValue = wireLandscapeHeightAt(x, z, peaks, seed);
    const point = wireLandscapePoint(rect, props, x, z, heightValue, now);
    const packetTone = packet % 6 === 0 ? accentTone : (packet % 3 === 0 ? "white" : tone);
    const radius = (1.4 + z * 3.4 + seededUnit(seed + packet * 5.3) * 1.6) * devicePixelRatio;
    ctx.shadowColor = toneColor(packetTone, 0.84);
    ctx.shadowBlur = (7 + z * 12) * devicePixelRatio;
    ctx.fillStyle = toneColor(packetTone, 0.30 + z * 0.42);
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  for (const [index, peak] of peaks.entries()) {
    const point = wireLandscapePoint(
      rect,
      props,
      peak.x,
      peak.z,
      wireLandscapeHeightAt(peak.x, peak.z, peaks, seed),
      now,
    );
    const focus = peak.id === focusPeakId;
    const pulse = 1 + Math.sin(now * 0.004 + seed + index) * 0.12;
    const radius = (focus ? 8.5 : 5.2) * pulse * devicePixelRatio;
    ctx.shadowColor = toneColor(peak.tone, focus ? 0.92 : 0.62);
    ctx.shadowBlur = (focus ? 18 : 10) * devicePixelRatio;
    ctx.fillStyle = toneColor(peak.tone, focus ? 0.42 : 0.24);
    ctx.strokeStyle = toneColor("white", focus ? 0.68 : 0.36);
    ctx.lineWidth = Math.max(0.8, (focus ? 1.6 : 0.9) * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (peak.label) {
      ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = toneColor("white", focus ? 0.86 : 0.62);
      ctx.fillText(String(peak.label).slice(0, 14), point.x, point.y - radius - 4 * devicePixelRatio);
    }
  }

  if (props.label) {
    ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.76);
    ctx.shadowColor = toneColor(accentTone, 0.62);
    ctx.shadowBlur = 7 * devicePixelRatio;
    ctx.fillText(String(props.label).slice(0, 28), rect.x + 8 * devicePixelRatio, rect.y + 12 * devicePixelRatio);
  }
  ctx.restore();
}

function meshVertex(value) {
  if (Array.isArray(value)) {
    return {x: Number(value[0] || 0), y: Number(value[1] || 0), z: Number(value[2] || 0)};
  }
  return {
    x: Number(value?.x || 0),
    y: Number(value?.y || 0),
    z: Number(value?.z || 0),
  };
}

function meshPoint(vertex, props, w, h, now) {
  const position = normalizedPoint(props.position || {x: 0.5, y: 0.46}, w, h);
  const scale = Number(props.scale || 0.18) * Math.min(w, h);
  const rotation = props.rotation || {};
  const spin = Number(props.spin || 0);
  const ax = Number(rotation.x || 0.62) + spin * now * 0.00012;
  const ay = Number(rotation.y || 0.72) + spin * now * 0.00022;
  const sinX = Math.sin(ax);
  const cosX = Math.cos(ax);
  const sinY = Math.sin(ay);
  const cosY = Math.cos(ay);
  const vx = vertex.x * cosY - vertex.z * sinY;
  const vz = vertex.x * sinY + vertex.z * cosY;
  const vy = vertex.y * cosX - vz * sinX;
  const rz = vertex.y * sinX + vz * cosX;
  const depth = Math.max(0.8, 2.4 + rz);
  return {
    x: position.x + (vx * scale * 1.7) / depth,
    y: position.y + (vy * scale * 1.7) / depth,
    z: rz,
  };
}

function meshEdgeIndexes(edge) {
  if (Array.isArray(edge)) return [Number(edge[0]), Number(edge[1])];
  return [Number(edge?.source ?? edge?.a), Number(edge?.target ?? edge?.b)];
}

function drawMesh(primitive, w, h, now) {
  const props = primitive.props || {};
  const vertices = Array.isArray(props.vertices) ? props.vertices.map((vertex) => meshVertex(vertex)) : [];
  if (!vertices.length) return;
  const points = vertices.map((vertex) => meshPoint(vertex, props, w, h, now));
  const tone = props.material || props.tone || "cyan";
  const faces = Array.isArray(props.faces) ? props.faces : [];
  const edges = Array.isArray(props.edges) ? props.edges : [];
  ctx.save();
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, 0.68);
  ctx.shadowBlur = 16 * devicePixelRatio;
  ctx.lineWidth = 1.4 * devicePixelRatio;
  for (const face of faces) {
    const indexes = Array.isArray(face) ? face : face?.vertices;
    if (!Array.isArray(indexes) || indexes.length < 3) continue;
    const facePoints = indexes.map((index) => points[Number(index)]).filter(Boolean);
    drawPolygon(facePoints, toneColor(tone, 0.13), toneColor(tone, 0.30));
  }
  for (const edge of edges) {
    const [aIndex, bIndex] = meshEdgeIndexes(edge);
    const a = points[aIndex];
    const b = points[bIndex];
    if (!a || !b) continue;
    const alpha = 0.42 + Math.max(-0.18, Math.min(0.24, (a.z + b.z) * 0.08));
    ctx.strokeStyle = toneColor(tone, alpha);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
  ctx.fillStyle = toneColor("white", 0.76);
  for (const point of points) {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 2.4 * devicePixelRatio, 0, Math.PI * 2);
    ctx.fill();
  }
  if (props.label) {
    const labelPoint = points.reduce((highest, point) => (point.y < highest.y ? point : highest), points[0]);
    ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = toneColor("white", 0.82);
    ctx.fillText(String(props.label).slice(0, 18), labelPoint.x, labelPoint.y - 12 * devicePixelRatio);
  }
  ctx.restore();
}

function vectorViewBox(value) {
  if (!Array.isArray(value) || value.length < 4) {
    return {x: 0, y: 0, width: 100, height: 100};
  }
  return {
    x: Number(value[0] || 0),
    y: Number(value[1] || 0),
    width: Math.max(1, Number(value[2] || 100)),
    height: Math.max(1, Number(value[3] || 100)),
  };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function vectorGradientPaint(value, gradients, fallbackTone, alpha) {
  let gradientId = null;
  if (typeof value === "string" && value.startsWith("gradient:")) {
    gradientId = value.slice("gradient:".length);
  } else if (value && typeof value === "object" && value.gradient) {
    gradientId = String(value.gradient);
  }
  if (!gradientId) return null;
  const spec = gradients.find((gradient) => gradient?.id === gradientId);
  if (!spec) return null;
  const stops = Array.isArray(spec.stops) ? spec.stops : [];
  if (!stops.length) return null;
  let gradient;
  if (spec.type === "radial") {
    const center = spec.center || {x: 50, y: 50};
    const inner = Math.max(0, Number(spec.innerRadius || 0));
    const outer = Math.max(inner + 0.1, Number(spec.outerRadius || 50));
    gradient = ctx.createRadialGradient(
      Number(center.x || 0),
      Number(center.y || 0),
      inner,
      Number(center.x || 0),
      Number(center.y || 0),
      outer,
    );
  } else {
    const from = spec.from || {x: 0, y: 0};
    const to = spec.to || {x: 100, y: 100};
    gradient = ctx.createLinearGradient(
      Number(from.x || 0),
      Number(from.y || 0),
      Number(to.x || 100),
      Number(to.y || 100),
    );
  }
  const denominator = Math.max(1, stops.length - 1);
  for (let index = 0; index < stops.length; index++) {
    const stop = stops[index] || {};
    const offset = clamp(Number(stop.offset ?? index / denominator), 0, 1);
    const stopAlpha = clamp(Number(stop.alpha ?? 1) * alpha, 0, 1);
    gradient.addColorStop(offset, toneColor(stop.tone || fallbackTone, stopAlpha));
  }
  return gradient;
}

function vectorPaint(value, gradients, fallbackTone, alpha) {
  return vectorGradientPaint(value, gradients, fallbackTone, alpha)
    || toneColor(value === true ? fallbackTone : value || fallbackTone, alpha);
}

function vectorPoint(value) {
  if (Array.isArray(value)) return {x: Number(value[0] || 0), y: Number(value[1] || 0)};
  return {x: Number(value?.x || 0), y: Number(value?.y || 0)};
}

function vectorNumber(value, fallback, min = -10000, max = 10000) {
  const numeric = Number(value ?? fallback);
  if (!Number.isFinite(numeric)) return fallback;
  return clamp(numeric, min, max);
}

function vectorRounded(value) {
  return Math.round(Number(value || 0) * 1000) / 1000;
}

function vectorAnimationConfig(source) {
  const animation = source?.animation && typeof source.animation === "object" ? source.animation : {};
  return {
    durationMs: Math.max(1, vectorNumber(source?.durationMs ?? animation.durationMs, 4000, 1, 120000)),
    delayMs: vectorNumber(source?.delayMs ?? animation.delayMs, 0, -120000, 120000),
    loop: source?.loop ?? animation.loop ?? true,
    yoyo: Boolean(source?.yoyo ?? animation.yoyo ?? false),
  };
}

function vectorFilterKind(value) {
  const raw = String(value || "").toLowerCase().replace(/-/g, "_");
  if (raw === "rgb_split") return "chromatic_split";
  if (raw === "chromatic") return "chromatic_split";
  if (raw === "scanlines") return "scanline";
  if (raw === "echo") return "ghost";
  if (raw === "soft_glow") return "glow";
  return raw;
}

function vectorFilterSpecs(source) {
  const raw = [];
  if (source?.filter !== undefined) raw.push(source.filter);
  if (Array.isArray(source?.filters)) raw.push(...source.filters);
  const specs = [];
  for (const item of raw.slice(0, 16)) {
    let kind = "";
    let spec = {};
    if (typeof item === "string") {
      kind = vectorFilterKind(item);
    } else if (item && typeof item === "object") {
      spec = item;
      kind = vectorFilterKind(item.kind || item.type || item.preset);
    }
    if (!["glow", "bloom", "haze", "chromatic_split", "ghost", "scanline"].includes(kind)) continue;
    specs.push({
      ...spec,
      kind,
      intensity: vectorNumber(spec.intensity, 1, 0, 4),
      alpha: vectorNumber(spec.alpha, 1, 0, 1),
      offset: vectorNumber(spec.offset, 1.7, -20, 20),
    });
  }
  return specs;
}

function vectorFilterSpec(specs, kind) {
  return specs.find((spec) => spec.kind === kind) || null;
}

function vectorClipKind(value) {
  const raw = String(value || "").toLowerCase().replace(/-/g, "_");
  if (raw === "scanline") return "scan";
  return raw;
}

function vectorClipState(clip, box, now) {
  if (!clip) return {active: false, kind: null, progress: 1};
  const spec = typeof clip === "string" ? {kind: clip} : clip;
  if (!spec || typeof spec !== "object") return {active: false, kind: null, progress: 1};
  const kind = vectorClipKind(spec.kind || spec.type || spec.shape);
  if (!["rect", "circle", "iris", "wipe", "scan"].includes(kind)) return {active: false, kind: null, progress: 1};
  let progress = Number(spec.progress);
  if (!Number.isFinite(progress)) {
    progress = spec.reveal === false && kind !== "scan" ? 1 : vectorKeyframeProgress(spec, now);
  }
  return {
    active: true,
    kind,
    progress: clamp(progress, 0, 1),
    direction: String(spec.direction || spec.axis || "x").toLowerCase(),
    reverse: Boolean(spec.reverse),
    x: vectorNumber(spec.x, box.x + box.width * 0.5),
    y: vectorNumber(spec.y, box.y + box.height * 0.5),
    width: vectorNumber(spec.w ?? spec.width, box.width, 0, Math.max(1, box.width * 4)),
    height: vectorNumber(spec.h ?? spec.height, box.height, 0, Math.max(1, box.height * 4)),
    radius: vectorNumber(
      spec.r ?? spec.radius,
      Math.max(box.width, box.height) * 0.58,
      0,
      Math.max(box.width, box.height) * 4,
    ),
    size: vectorNumber(spec.size, Math.max(box.width, box.height) * 0.22, 0, Math.max(box.width, box.height) * 4),
  };
}

function applyVectorClip(clip, box, now) {
  const state = vectorClipState(clip, box, now);
  if (!state.active) return state;
  ctx.beginPath();
  if (state.kind === "rect") {
    ctx.rect(state.x - state.width * 0.5, state.y - state.height * 0.5, state.width, state.height);
  } else if (state.kind === "circle" || state.kind === "iris") {
    const radius = state.kind === "iris" ? state.radius * state.progress : state.radius;
    ctx.arc(state.x, state.y, Math.max(0.01, radius), 0, Math.PI * 2);
  } else if (state.kind === "wipe") {
    const horizontal = state.direction !== "y" && state.direction !== "vertical";
    const progress = state.reverse ? 1 - state.progress : state.progress;
    if (horizontal) {
      const width = box.width * progress;
      const x = state.reverse ? box.x + box.width - width : box.x;
      ctx.rect(x, box.y, width, box.height);
    } else {
      const height = box.height * progress;
      const y = state.reverse ? box.y + box.height - height : box.y;
      ctx.rect(box.x, y, box.width, height);
    }
  } else if (state.kind === "scan") {
    const horizontal = state.direction !== "y" && state.direction !== "vertical";
    if (horizontal) {
      const x = box.x + box.width * state.progress;
      ctx.rect(x - state.size * 0.5, box.y, state.size, box.height);
    } else {
      const y = box.y + box.height * state.progress;
      ctx.rect(box.x, y - state.size * 0.5, box.width, state.size);
    }
  }
  ctx.clip();
  return state;
}

function vectorCanvasFilter(specs) {
  const pieces = [];
  const glow = vectorFilterSpec(specs, "glow");
  const bloom = vectorFilterSpec(specs, "bloom");
  const haze = vectorFilterSpec(specs, "haze");
  if (glow) {
    pieces.push(`brightness(${1 + glow.intensity * 0.14})`);
    pieces.push(`contrast(${1 + glow.intensity * 0.08})`);
  }
  if (bloom) {
    pieces.push(`blur(${vectorNumber(bloom.blur, bloom.intensity * 0.42, 0, 6)}px)`);
    pieces.push(`brightness(${1 + bloom.intensity * 0.24})`);
  }
  if (haze) {
    pieces.push(`blur(${vectorNumber(haze.blur, haze.intensity * 0.28, 0, 5)}px)`);
    pieces.push(`opacity(${clamp(0.72 + haze.intensity * 0.12, 0.1, 1)})`);
  }
  return pieces.length ? pieces.join(" ") : "none";
}

function drawVectorScanlines(source, props, box, now, spec) {
  const tone = spec.tone || props.accentTone || props.tone || "cyan";
  const spacing = Math.max(1, vectorNumber(spec.spacing, 5, 1, 50));
  const width = Math.max(0.15, vectorNumber(spec.width, 0.42, 0.1, 8));
  const speed = vectorNumber(spec.speed, 0.018, -1, 1);
  const offset = (now * speed + vectorNumber(spec.offset, 0, -10000, 10000)) % spacing;
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  ctx.globalAlpha *= clamp(0.12 * spec.intensity * spec.alpha, 0, 0.8);
  ctx.lineWidth = width;
  ctx.strokeStyle = toneColor(tone, 1);
  for (let y = box.y - spacing + offset; y <= box.y + box.height + spacing; y += spacing) {
    ctx.beginPath();
    ctx.moveTo(box.x, y);
    ctx.lineTo(box.x + box.width, y);
    ctx.stroke();
  }
  ctx.restore();
}

function drawVectorFilteredContent(source, props, box, now, drawBody) {
  const specs = vectorFilterSpecs(source);
  const chromatic = vectorFilterSpec(specs, "chromatic_split");
  const ghost = vectorFilterSpec(specs, "ghost");
  if (ghost) {
    const offset = ghost.offset * ghost.intensity;
    ctx.save();
    ctx.globalAlpha *= clamp(0.24 * ghost.alpha * ghost.intensity, 0, 0.65);
    ctx.globalCompositeOperation = "screen";
    ctx.translate(offset, -offset * 0.72);
    ctx.filter = `blur(${vectorNumber(ghost.blur, 0.45, 0, 4)}px)`;
    drawBody();
    ctx.restore();
  }
  if (chromatic) {
    const offset = chromatic.offset * chromatic.intensity;
    for (const pass of [
      {x: -offset, y: offset * 0.18, tone: "magenta"},
      {x: offset, y: -offset * 0.18, tone: "cyan"},
    ]) {
      ctx.save();
      ctx.globalAlpha *= clamp(0.34 * chromatic.alpha * chromatic.intensity, 0, 0.7);
      ctx.globalCompositeOperation = "screen";
      ctx.translate(pass.x, pass.y);
      drawBody({...props, tone: pass.tone});
      ctx.restore();
    }
  }
  ctx.save();
  ctx.filter = vectorCanvasFilter(specs);
  drawBody();
  ctx.restore();
  const scanline = vectorFilterSpec(specs, "scanline");
  if (scanline) drawVectorScanlines(source, props, box, now, scanline);
}

function vectorKeyframeProgress(source, now) {
  const config = vectorAnimationConfig(source);
  const elapsed = now - config.delayMs;
  if (elapsed <= 0) return 0;
  if (config.loop === false) return clamp(elapsed / config.durationMs, 0, 1);
  const cycle = Math.floor(elapsed / config.durationMs);
  let progress = (elapsed % config.durationMs) / config.durationMs;
  if (config.yoyo && cycle % 2 === 1) progress = 1 - progress;
  return clamp(progress, 0, 1);
}

function vectorKeyframeOffset(frame, index, count, durationMs) {
  const direct = Number(frame.at ?? frame.offset ?? frame.progress);
  if (Number.isFinite(direct)) return clamp(direct, 0, 1);
  const timeMs = Number(frame.timeMs ?? frame.ms);
  if (Number.isFinite(timeMs)) return clamp(timeMs / durationMs, 0, 1);
  return count <= 1 ? 0 : index / (count - 1);
}

function vectorKeyframes(source) {
  const rawFrames = Array.isArray(source?.keyframes) ? source.keyframes : [];
  if (!rawFrames.length) return [];
  const config = vectorAnimationConfig(source);
  return rawFrames
    .filter((frame) => frame && typeof frame === "object")
    .map((frame, index) => ({
      ...frame,
      at: vectorKeyframeOffset(frame, index, rawFrames.length, config.durationMs),
    }))
    .sort((left, right) => left.at - right.at);
}

function vectorKeyframeNumber(frame, key) {
  const transform = frame?.transform && typeof frame.transform === "object" ? frame.transform : {};
  const numeric = Number(frame?.[key] ?? transform[key]);
  return Number.isFinite(numeric) ? numeric : null;
}

function vectorKeyframeLerp(left, right, key, fallback, progress) {
  const start = vectorKeyframeNumber(left, key);
  const end = vectorKeyframeNumber(right, key);
  if (start === null && end === null) return fallback;
  if (start === null) return end;
  if (end === null) return start;
  return start + (end - start) * progress;
}

function vectorKeyframeTransform(source, now) {
  const frames = vectorKeyframes(source);
  if (!frames.length) {
    return {x: 0, y: 0, scale: 1, rotation: 0, opacity: 1, progress: 0, keyframeCount: 0};
  }
  const progress = vectorKeyframeProgress(source, now);
  let left = frames[0];
  let right = frames[frames.length - 1];
  for (let index = 1; index < frames.length; index++) {
    if (progress <= frames[index].at) {
      left = frames[index - 1];
      right = frames[index];
      break;
    }
  }
  const span = Math.max(0.0001, right.at - left.at);
  const localProgress = clamp((progress - left.at) / span, 0, 1);
  return {
    x: vectorKeyframeLerp(left, right, "x", 0, localProgress),
    y: vectorKeyframeLerp(left, right, "y", 0, localProgress),
    scale: Math.max(0.01, vectorKeyframeLerp(left, right, "scale", 1, localProgress)),
    rotation: vectorKeyframeLerp(left, right, "rotation", 0, localProgress),
    opacity: clamp(vectorKeyframeLerp(left, right, "opacity", 1, localProgress), 0, 1),
    progress,
    keyframeCount: frames.length,
  };
}

function vectorPathNumberTokens(pathData) {
  const text = String(pathData || "");
  const numberPattern = /[+-]?(?:\\d*\\.\\d+|\\d+\\.?)(?:e[+-]?\\d+)?/gi;
  const pieces = [];
  const values = [];
  let offset = 0;
  let match = numberPattern.exec(text);
  while (match) {
    pieces.push(text.slice(offset, match.index));
    values.push(Number(match[0]));
    offset = match.index + match[0].length;
    match = numberPattern.exec(text);
  }
  pieces.push(text.slice(offset));
  return {pieces, values};
}

function vectorFormatPathNumber(value) {
  const rounded = Math.round(Number(value || 0) * 1000) / 1000;
  if (Object.is(rounded, -0)) return "0";
  return String(Number(rounded.toFixed(3)));
}

function vectorInterpolatePathData(leftData, rightData, progress) {
  const left = vectorPathNumberTokens(leftData);
  const right = vectorPathNumberTokens(rightData);
  if (
    left.values.length !== right.values.length
    || left.pieces.length !== right.pieces.length
    || left.pieces.some((piece, index) => piece !== right.pieces[index])
  ) {
    return null;
  }
  let data = "";
  for (let index = 0; index < left.values.length; index++) {
    data += left.pieces[index];
    data += vectorFormatPathNumber(left.values[index] + (right.values[index] - left.values[index]) * progress);
  }
  return data + left.pieces[left.pieces.length - 1];
}

function vectorPathMorph(pathSpec, now) {
  const baseData = String(pathSpec?.d || "");
  const rawMorphs = Array.isArray(pathSpec?.morphs) ? pathSpec.morphs : [];
  const morphSource = {...pathSpec, keyframes: rawMorphs};
  const morphFrames = vectorKeyframes(morphSource)
    .filter((frame) => frame && typeof frame === "object" && typeof frame.d === "string" && frame.d)
    .map((frame) => ({...frame, d: String(frame.d)}));
  if (baseData && !morphFrames.some((frame) => frame.at <= 0.0001)) {
    morphFrames.unshift({at: 0, d: baseData});
  }
  if (!morphFrames.length) {
    return {pathData: baseData, morphCount: 0, mode: "static", progress: 0};
  }
  if (morphFrames.length === 1) {
    return {pathData: morphFrames[0].d, morphCount: morphFrames.length, mode: "single", progress: 0};
  }
  const progress = vectorKeyframeProgress(morphSource, now);
  let left = morphFrames[0];
  let right = morphFrames[morphFrames.length - 1];
  for (let index = 1; index < morphFrames.length; index++) {
    if (progress <= morphFrames[index].at) {
      left = morphFrames[index - 1];
      right = morphFrames[index];
      break;
    }
  }
  const span = Math.max(0.0001, right.at - left.at);
  const localProgress = clamp((progress - left.at) / span, 0, 1);
  const interpolated = vectorInterpolatePathData(left.d, right.d, localProgress);
  return {
    pathData: interpolated || (localProgress < 0.5 ? left.d : right.d),
    morphCount: morphFrames.length,
    mode: interpolated ? "interpolated" : "discrete",
    progress,
  };
}

function polylineSegments(points) {
  const segments = [];
  let total = 0;
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1];
    const b = points[index];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (length <= 0) continue;
    segments.push({a, b, length, start: total});
    total += length;
  }
  return {segments, total};
}

function polylinePoint(path, progress) {
  if (!path.segments.length) return null;
  const distance = clamp(progress, 0, 1) * path.total;
  for (const segment of path.segments) {
    if (distance <= segment.start + segment.length) {
      const local = clamp((distance - segment.start) / segment.length, 0, 1);
      return {
        x: segment.a.x + (segment.b.x - segment.a.x) * local,
        y: segment.a.y + (segment.b.y - segment.a.y) * local,
      };
    }
  }
  const last = path.segments[path.segments.length - 1];
  return {x: last.b.x, y: last.b.y};
}

function drawSvgPath(pathSpec, props, now) {
  const morph = vectorPathMorph(pathSpec, now);
  const pathData = morph.pathData;
  if (!pathData) return;
  let path;
  try {
    path = new Path2D(pathData);
  } catch {
    return;
  }
  const tone = pathSpec.tone || props.tone || "cyan";
  const alpha = Math.max(0, Math.min(1, Number(pathSpec.alpha ?? props.alpha ?? 0.86)));
  const width = Math.max(0.2, Number(pathSpec.width || 1.6));
  const gradients = Array.isArray(props.gradients) ? props.gradients : [];
  ctx.save();
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.lineWidth = width;
  ctx.shadowColor = toneColor(tone, Math.min(0.8, alpha));
  ctx.shadowBlur = Number(pathSpec.glow ?? props.glow ?? 5);
  if (pathSpec.fill && pathSpec.fill !== "none") {
    ctx.fillStyle = vectorPaint(pathSpec.fill, gradients, tone, Number(pathSpec.fillAlpha ?? 0.12));
    ctx.fill(path);
  }
  const dash = Array.isArray(pathSpec.dash) ? pathSpec.dash.map(Number).filter((value) => value > 0) : [];
  if (pathSpec.reveal) {
    const length = Math.max(40, pathData.length * Number(pathSpec.revealScale || 1.8));
    const speed = Number(pathSpec.speed || 0.00024);
    const progress = (now * speed + Number(pathSpec.offset || 0)) % 1;
    ctx.setLineDash([length, length]);
    ctx.lineDashOffset = length * (1 - progress);
  } else if (dash.length) {
    const speed = Number(pathSpec.speed || 0);
    ctx.setLineDash(dash);
    ctx.lineDashOffset = Number(pathSpec.offset || 0) - now * speed;
  }
  if (pathSpec.stroke !== false) {
    ctx.strokeStyle = vectorPaint(pathSpec.stroke || pathSpec.strokeGradient || tone, gradients, tone, alpha);
    ctx.stroke(path);
  }
  ctx.restore();
}

function vectorDash(spec, now) {
  const dash = Array.isArray(spec.dash) ? spec.dash.map(Number).filter((value) => value > 0) : [];
  if (!dash.length) return;
  ctx.setLineDash(dash);
  ctx.lineDashOffset = Number(spec.offset || 0) - now * Number(spec.speed || 0);
}

function vectorStrokeAndFill(spec, props, now, drawShape, defaultFillAlpha = 0.1) {
  const tone = spec.tone || props.tone || "cyan";
  const gradients = Array.isArray(props.gradients) ? props.gradients : [];
  const alpha = clamp(Number(spec.alpha ?? props.alpha ?? 0.84), 0, 1);
  const width = Math.max(0.2, Number(spec.width || spec.strokeWidth || 1.2));
  ctx.save();
  ctx.lineJoin = spec.join || "round";
  ctx.lineCap = spec.cap || "round";
  ctx.lineWidth = width;
  ctx.shadowColor = toneColor(tone, Math.min(0.8, alpha));
  ctx.shadowBlur = Number(spec.glow ?? props.glow ?? 4);
  const pulse = spec.pulse ? 1 + Math.sin(now * Number(spec.pulseSpeed || 0.005)) * 0.06 : 1;
  if (pulse !== 1 && spec.center) {
    const center = vectorPoint(spec.center);
    ctx.translate(center.x, center.y);
    ctx.scale(pulse, pulse);
    ctx.translate(-center.x, -center.y);
  }
  drawShape();
  if (spec.fill && spec.fill !== "none") {
    ctx.fillStyle = vectorPaint(spec.fill, gradients, tone, Number(spec.fillAlpha ?? defaultFillAlpha));
    ctx.fill();
  }
  if (spec.stroke !== false) {
    vectorDash(spec, now);
    ctx.strokeStyle = vectorPaint(spec.stroke || tone, gradients, tone, alpha);
    ctx.stroke();
  }
  ctx.restore();
}

function drawSvgRects(rects, props, now) {
  for (const rect of rects) {
    const x = vectorNumber(rect.x, 0);
    const y = vectorNumber(rect.y, 0);
    const width = Math.max(0, vectorNumber(rect.w ?? rect.width, 0));
    const height = Math.max(0, vectorNumber(rect.h ?? rect.height, 0));
    if (!width || !height) continue;
    const radius = Math.max(0, vectorNumber(rect.rx ?? rect.r ?? 0, 0));
    const spec = {
      ...rect,
      center: rect.center || {x: x + width * 0.5, y: y + height * 0.5},
    };
    vectorStrokeAndFill(spec, props, now, () => {
      ctx.beginPath();
      if (radius > 0 && typeof ctx.roundRect === "function") ctx.roundRect(x, y, width, height, radius);
      else ctx.rect(x, y, width, height);
    }, 0.12);
  }
}

function lineEndpoint(line, key, xKey, yKey, fallbackX = 0, fallbackY = 0) {
  const endpoint = line[key];
  if (endpoint && typeof endpoint === "object") return vectorPoint(endpoint);
  return {
    x: vectorNumber(line[`${key}X`] ?? line[`${key}x`] ?? line[xKey], fallbackX),
    y: vectorNumber(line[`${key}Y`] ?? line[`${key}y`] ?? line[yKey], fallbackY),
  };
}

function drawSvgLines(lines, props, now) {
  for (const line of lines) {
    const from = lineEndpoint(line, "from", "x1", "y1");
    const to = lineEndpoint(line, "to", "x2", "y2");
    vectorStrokeAndFill({...line, fill: "none"}, props, now, () => {
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
    });
  }
}

function drawSvgPolylineLike(shapes, props, now, closed) {
  for (const shape of shapes) {
    const points = Array.isArray(shape.points) ? shape.points.map((point) => vectorPoint(point)) : [];
    if (points.length < (closed ? 3 : 2)) continue;
    vectorStrokeAndFill(shape, props, now, () => {
      ctx.beginPath();
      ctx.moveTo(points[0].x, points[0].y);
      for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
      if (closed) ctx.closePath();
    }, closed ? 0.13 : 0);
  }
}

function drawSvgCircles(circles, props, now) {
  for (const circle of circles) {
    const tone = circle.tone || props.tone || "cyan";
    const pulse = circle.pulse ? 1 + Math.sin(now * Number(circle.speed || 0.005)) * 0.22 : 1;
    const radius = Math.max(0.5, Number(circle.r || 2.5) * pulse);
    const x = Number(circle.x || 0);
    const y = Number(circle.y || 0);
    const alpha = Math.max(0, Math.min(1, Number(circle.alpha ?? 0.84)));
    ctx.save();
    ctx.shadowColor = toneColor(tone, alpha);
    ctx.shadowBlur = Number(circle.glow ?? props.glow ?? 5);
    ctx.fillStyle = toneColor(tone, alpha * 0.82);
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
    if (circle.ring) {
      ctx.lineWidth = Math.max(0.4, Number(circle.width || 0.8));
      ctx.strokeStyle = toneColor("white", alpha * 0.5);
      ctx.stroke();
    }
    ctx.restore();
  }
}

function drawSvgTraces(traces, props, now) {
  const gradients = Array.isArray(props.gradients) ? props.gradients : [];
  for (const trace of traces) {
    const points = Array.isArray(trace.points) ? trace.points.map((point) => vectorPoint(point)) : [];
    if (points.length < 2) continue;
    const path = polylineSegments(points);
    if (!path.total) continue;
    const tone = trace.tone || props.tone || "cyan";
    const alpha = clamp(Number(trace.alpha ?? 0.82), 0, 1);
    const width = Math.max(0.2, Number(trace.width || 0.8));
    const speed = Number(trace.speed || 0.00018);
    const count = Math.max(1, Math.min(48, Number(trace.count || 7)));
    const radius = Math.max(0.3, Number(trace.radius || 1.25));
    const tail = clamp(Number(trace.tail ?? 0.055), 0, 0.4);
    ctx.save();
    ctx.globalCompositeOperation = trace.blend === "source-over" ? "source-over" : "screen";
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.shadowColor = toneColor(tone, alpha);
    ctx.shadowBlur = Number(trace.glow ?? props.glow ?? 7);
    if (trace.drawPath !== false) {
      const dash = Array.isArray(trace.dash) ? trace.dash.map(Number).filter((value) => value > 0) : [6, 7];
      ctx.setLineDash(dash);
      ctx.lineDashOffset = -now * speed * 90 + Number(trace.offset || 0);
      ctx.lineWidth = width;
      ctx.strokeStyle = vectorPaint(trace.stroke || trace.gradient || tone, gradients, tone, alpha * 0.38);
      ctx.beginPath();
      ctx.moveTo(points[0].x, points[0].y);
      for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    for (let index = 0; index < count; index++) {
      const rawProgress = now * speed + Number(trace.offset || 0) + index / count;
      const progress = trace.direction === "reverse" ? 1 - (rawProgress % 1) : rawProgress % 1;
      const head = polylinePoint(path, progress);
      if (!head) continue;
      const tailPoint = tail > 0 ? polylinePoint(path, Math.max(0, progress - tail)) : null;
      const particleAlpha = alpha * (0.58 + ((index % 3) * 0.13));
      if (tailPoint && progress - tail >= 0) {
        ctx.lineWidth = Math.max(0.3, width * 1.15);
        ctx.strokeStyle = toneColor(tone, particleAlpha * 0.6);
        ctx.beginPath();
        ctx.moveTo(tailPoint.x, tailPoint.y);
        ctx.lineTo(head.x, head.y);
        ctx.stroke();
      }
      ctx.fillStyle = toneColor(index % 4 === 0 ? "white" : tone, particleAlpha);
      ctx.beginPath();
      ctx.arc(head.x, head.y, radius * (1 + (index % 3) * 0.18), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }
}

function drawSvgLabels(labels, props) {
  ctx.save();
  ctx.textAlign = props.textAlign || "center";
  ctx.textBaseline = "middle";
  for (const label of labels) {
    const tone = label.tone || props.tone || "white";
    const size = Math.max(3, Number(label.size || 7));
    ctx.font = `${size}px ui-monospace, monospace`;
    ctx.textAlign = label.align || props.textAlign || "center";
    ctx.textBaseline = label.baseline || "middle";
    ctx.fillStyle = toneColor(tone, Number(label.alpha ?? 0.86));
    ctx.shadowColor = toneColor(tone, 0.54);
    ctx.shadowBlur = Number(label.glow ?? props.glow ?? 3);
    ctx.fillText(String(label.text || "").slice(0, 24), Number(label.x || 0), Number(label.y || 0));
  }
  ctx.restore();
}

function vectorSymbolNumber(value, fallback, min, max) {
  const numeric = Number(value ?? fallback);
  if (!Number.isFinite(numeric)) return fallback;
  return clamp(numeric, min, max);
}

function vectorSymbolPoint(symbol, defaultX = 50, defaultY = 50) {
  return {
    x: vectorSymbolNumber(symbol?.x, defaultX, -10000, 10000),
    y: vectorSymbolNumber(symbol?.y, defaultY, -10000, 10000),
  };
}

function rotatedEllipsePoint(cx, cy, rx, ry, rotation, theta) {
  const cosRotation = Math.cos(rotation);
  const sinRotation = Math.sin(rotation);
  const localX = Math.cos(theta) * rx;
  const localY = Math.sin(theta) * ry;
  return {
    x: cx + localX * cosRotation - localY * sinRotation,
    y: cy + localX * sinRotation + localY * cosRotation,
  };
}

function drawSvgSymbolGlobe(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const radius = vectorSymbolNumber(symbol.r ?? symbol.radius, 24, 2, 600);
  const tone = symbol.tone || props.tone || "cyan";
  const accentTone = symbol.accentTone || symbol.accent || "magenta";
  const speed = Number(symbol.speed ?? 0.0012);
  const phase = now * speed + Number(symbol.offset || 0);
  const alpha = clamp(Number(symbol.alpha ?? 0.86), 0, 1);
  const meridians = Math.max(2, Math.min(8, Number(symbol.meridians || 5)));
  const packets = Math.max(0, Math.min(24, Number(symbol.packets || 7)));
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 6);
  ctx.lineWidth = Math.max(0.35, Number(symbol.strokeWidth || 0.85));
  ctx.strokeStyle = toneColor(tone, alpha * 0.82);
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
  ctx.stroke();
  for (const scale of [0.34, 0.58]) {
    ctx.strokeStyle = toneColor(tone, alpha * (0.22 + scale * 0.22));
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, radius, radius * scale, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, radius, radius * scale, Math.PI, 0, Math.PI * 2);
    ctx.stroke();
  }
  for (let index = 0; index < meridians; index++) {
    const orbit = phase + (index / meridians) * Math.PI;
    const width = Math.max(radius * 0.08, Math.abs(Math.cos(orbit)) * radius);
    ctx.strokeStyle = toneColor(index % 2 ? accentTone : tone, alpha * 0.42);
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, width, radius, 0, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (symbol.orbit !== false) {
    const orbitRotation = phase * 0.62;
    const orbitRx = radius * 1.42;
    const orbitRy = radius * 0.38;
    ctx.lineWidth = Math.max(0.25, Number(symbol.orbitWidth || 0.55));
    ctx.strokeStyle = toneColor(accentTone, alpha * 0.52);
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, orbitRx, orbitRy, orbitRotation, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = toneColor("white", alpha * 0.88);
    for (let index = 0; index < packets; index++) {
      const point = rotatedEllipsePoint(
        center.x,
        center.y,
        orbitRx,
        orbitRy,
        orbitRotation,
        phase * 2.3 + (index / Math.max(1, packets)) * Math.PI * 2,
      );
      ctx.beginPath();
      ctx.arc(point.x, point.y, radius * (0.035 + (index % 3) * 0.01), 0, Math.PI * 2);
      ctx.fill();
    }
  }
  if (symbol.label) {
    ctx.font = `${Math.max(3, Number(symbol.labelSize || radius * 0.22))}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", alpha * 0.78);
    ctx.fillText(String(symbol.label).slice(0, 16), center.x, center.y);
  }
  ctx.restore();
}

function drawSvgSymbolFilesystemGate(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const width = vectorSymbolNumber(symbol.w ?? symbol.width, 34, 4, 800);
  const height = vectorSymbolNumber(symbol.h ?? symbol.height, 42, 4, 800);
  const depth = vectorSymbolNumber(symbol.depth, Math.min(width, height) * 0.26, 0, 400);
  const tone = symbol.tone || props.tone || "amber";
  const accentTone = symbol.accentTone || "green";
  const alpha = clamp(Number(symbol.alpha ?? 0.86), 0, 1);
  const left = center.x - width * 0.5;
  const top = center.y - height * 0.5;
  const backLeft = left + depth;
  const backTop = top - depth;
  const scanProgress = (now * Number(symbol.scanSpeed ?? 0.00022) + Number(symbol.offset || 0)) % 1;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 7);
  ctx.lineWidth = Math.max(0.45, Number(symbol.strokeWidth || 0.9));
  ctx.strokeStyle = toneColor(tone, alpha * 0.38);
  ctx.strokeRect(backLeft, backTop, width, height);
  ctx.strokeStyle = toneColor(tone, alpha * 0.62);
  ctx.strokeRect(left, top, width, height);
  const corners = [
    [left, top, backLeft, backTop],
    [left + width, top, backLeft + width, backTop],
    [left, top + height, backLeft, backTop + height],
    [left + width, top + height, backLeft + width, backTop + height],
  ];
  for (const corner of corners) {
    ctx.beginPath();
    ctx.moveTo(corner[0], corner[1]);
    ctx.lineTo(corner[2], corner[3]);
    ctx.stroke();
  }
  const lanes = Math.max(2, Math.min(7, Number(symbol.lanes || 4)));
  ctx.strokeStyle = toneColor(tone, alpha * 0.28);
  for (let lane = 1; lane < lanes; lane++) {
    const x = left + (lane / lanes) * width;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x + depth, backTop);
    ctx.moveTo(x, top + height);
    ctx.lineTo(x + depth, backTop + height);
    ctx.stroke();
  }
  ctx.strokeStyle = toneColor(accentTone, alpha * 0.54);
  for (let row = 1; row < 4; row++) {
    const y = top + (row / 4) * height;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
  }
  if (symbol.scan !== false) {
    const y = top + scanProgress * height;
    ctx.strokeStyle = toneColor("white", alpha * 0.86);
    ctx.fillStyle = toneColor(accentTone, alpha * 0.12);
    ctx.fillRect(left, y - height * 0.045, width, height * 0.09);
    ctx.beginPath();
    ctx.moveTo(left - depth * 0.2, y);
    ctx.lineTo(left + width + depth * 0.2, y);
    ctx.stroke();
  }
  if (symbol.label) {
    ctx.font = `${Math.max(3, Number(symbol.labelSize || height * 0.12))}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = toneColor("white", alpha * 0.72);
    ctx.fillText(String(symbol.label).slice(0, 16), center.x + depth * 0.5, top + height + 2);
  }
  ctx.restore();
}

function drawSvgSymbolReticle(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const radius = vectorSymbolNumber(symbol.r ?? symbol.radius, 18, 2, 500);
  const tone = symbol.tone || props.tone || "green";
  const accentTone = symbol.accentTone || "white";
  const speed = Number(symbol.speed ?? 0.004);
  const pulse = symbol.pulse === false ? 1 : 1 + Math.sin(now * speed + Number(symbol.offset || 0)) * 0.08;
  const alpha = clamp(Number(symbol.alpha ?? 0.82), 0, 1);
  ctx.save();
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 5);
  ctx.lineWidth = Math.max(0.4, Number(symbol.strokeWidth || 0.85));
  for (let index = 0; index < 3; index++) {
    const ringRadius = radius * pulse * (0.52 + index * 0.26);
    ctx.strokeStyle = toneColor(index === 1 ? accentTone : tone, alpha * (0.46 - index * 0.08));
    ctx.beginPath();
    ctx.arc(center.x, center.y, ringRadius, 0.16 * Math.PI, 0.84 * Math.PI);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(center.x, center.y, ringRadius, 1.16 * Math.PI, 1.84 * Math.PI);
    ctx.stroke();
  }
  const spoke = radius * 1.05 * pulse;
  ctx.strokeStyle = toneColor(tone, alpha * 0.62);
  ctx.beginPath();
  ctx.moveTo(center.x - spoke, center.y);
  ctx.lineTo(center.x - radius * 0.24, center.y);
  ctx.moveTo(center.x + radius * 0.24, center.y);
  ctx.lineTo(center.x + spoke, center.y);
  ctx.moveTo(center.x, center.y - spoke);
  ctx.lineTo(center.x, center.y - radius * 0.24);
  ctx.moveTo(center.x, center.y + radius * 0.24);
  ctx.lineTo(center.x, center.y + spoke);
  ctx.stroke();
  ctx.fillStyle = toneColor(accentTone, alpha * 0.86);
  ctx.beginPath();
  ctx.arc(center.x, center.y, Math.max(0.9, radius * 0.06), 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function drawSvgSymbolDataTunnel(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const width = vectorSymbolNumber(symbol.w ?? symbol.width, 44, 6, 900);
  const height = vectorSymbolNumber(symbol.h ?? symbol.height, 30, 6, 900);
  const rings = Math.max(3, Math.min(12, Number(symbol.rings || 6)));
  const packets = Math.max(0, Math.min(28, Number(symbol.packets || 10)));
  const tone = symbol.tone || props.tone || "magenta";
  const accentTone = symbol.accentTone || "cyan";
  const alpha = clamp(Number(symbol.alpha ?? 0.84), 0, 1);
  const phase = (now * Number(symbol.speed ?? 0.00034) + Number(symbol.offset || 0)) % 1;
  const twist = Number(symbol.twist ?? 0.26);
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 7);
  for (let index = 0; index < rings; index++) {
    const depth = (index + phase) / rings;
    const scale = 1 - depth * 0.78;
    const x = center.x + Math.sin(depth * Math.PI * 2 + phase * Math.PI * 2) * width * twist;
    const y = center.y + Math.cos(depth * Math.PI * 2 + phase * Math.PI * 2) * height * twist * 0.34;
    const ringWidth = width * scale;
    const ringHeight = height * scale;
    const ringAlpha = alpha * (0.18 + (1 - depth) * 0.54);
    ctx.lineWidth = Math.max(0.35, Number(symbol.strokeWidth || 0.75) * (1 + (1 - depth) * 0.6));
    ctx.strokeStyle = toneColor(index % 2 ? accentTone : tone, ringAlpha);
    ctx.beginPath();
    ctx.moveTo(x - ringWidth * 0.5, y - ringHeight * 0.36);
    ctx.lineTo(x, y - ringHeight * 0.5);
    ctx.lineTo(x + ringWidth * 0.5, y - ringHeight * 0.36);
    ctx.lineTo(x + ringWidth * 0.5, y + ringHeight * 0.36);
    ctx.lineTo(x, y + ringHeight * 0.5);
    ctx.lineTo(x - ringWidth * 0.5, y + ringHeight * 0.36);
    ctx.closePath();
    ctx.stroke();
  }
  ctx.strokeStyle = toneColor(accentTone, alpha * 0.28);
  ctx.lineWidth = Math.max(0.28, Number(symbol.spokeWidth || 0.45));
  for (const angle of [-0.42, 0, 0.42, Math.PI - 0.42, Math.PI, Math.PI + 0.42]) {
    ctx.beginPath();
    ctx.moveTo(center.x, center.y);
    ctx.lineTo(center.x + Math.cos(angle) * width * 0.72, center.y + Math.sin(angle) * height * 0.62);
    ctx.stroke();
  }
  ctx.fillStyle = toneColor("white", alpha * 0.9);
  for (let index = 0; index < packets; index++) {
    const progress = (phase + index / Math.max(1, packets)) % 1;
    const scale = 1 - progress * 0.72;
    const angle = progress * Math.PI * 2 + index * 0.73;
    const x = center.x + Math.cos(angle) * width * 0.5 * scale;
    const y = center.y + Math.sin(angle) * height * 0.42 * scale;
    ctx.beginPath();
    ctx.arc(x, y, Math.max(0.45, width * 0.018 * (1 + scale)), 0, Math.PI * 2);
    ctx.fill();
  }
  if (symbol.label) {
    ctx.font = `${Math.max(3, Number(symbol.labelSize || height * 0.18))}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", alpha * 0.74);
    ctx.fillText(String(symbol.label).slice(0, 16), center.x, center.y);
  }
  ctx.restore();
}

function drawSvgSymbolIceWall(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const width = vectorSymbolNumber(symbol.w ?? symbol.width, 42, 6, 900);
  const height = vectorSymbolNumber(symbol.h ?? symbol.height, 26, 6, 900);
  const columns = Math.max(3, Math.min(14, Number(symbol.columns || symbol.shards || 7)));
  const tone = symbol.tone || props.tone || "cyan";
  const accentTone = symbol.accentTone || "white";
  const alpha = clamp(Number(symbol.alpha ?? 0.82), 0, 1);
  const left = center.x - width * 0.5;
  const top = center.y - height * 0.5;
  const scan = (now * Number(symbol.scanSpeed ?? 0.00028) + Number(symbol.offset || 0)) % 1;
  ctx.save();
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 8);
  for (let index = 0; index < columns; index++) {
    const x0 = left + (index / columns) * width;
    const x1 = left + ((index + 1) / columns) * width;
    const jitter = Math.sin(now * 0.001 + index * 1.7) * height * 0.08;
    const shardTop = top + Math.abs(Math.sin(index * 1.13)) * height * 0.18 + jitter;
    const shardBottom = top + height - Math.abs(Math.cos(index * 1.31)) * height * 0.16;
    ctx.fillStyle = toneColor(index % 2 ? tone : accentTone, alpha * (0.07 + (index % 3) * 0.035));
    ctx.strokeStyle = toneColor(index % 2 ? accentTone : tone, alpha * 0.48);
    ctx.lineWidth = Math.max(0.35, Number(symbol.strokeWidth || 0.7));
    ctx.beginPath();
    ctx.moveTo(x0, shardBottom);
    ctx.lineTo(x0 + (x1 - x0) * 0.28, shardTop);
    ctx.lineTo(x1, shardTop + height * 0.08);
    ctx.lineTo(x1 - (x1 - x0) * 0.12, shardBottom);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
  ctx.strokeStyle = toneColor(accentTone, alpha * 0.5);
  ctx.lineWidth = Math.max(0.3, Number(symbol.crackWidth || 0.48));
  for (let crack = 0; crack < Math.min(7, columns); crack++) {
    const x = left + ((crack + 0.5) / columns) * width;
    ctx.beginPath();
    ctx.moveTo(x, top + height * 0.16);
    ctx.lineTo(x + Math.sin(crack * 1.9) * width * 0.05, top + height * 0.46);
    ctx.lineTo(x + Math.cos(crack * 2.1) * width * 0.08, top + height * 0.78);
    ctx.stroke();
  }
  if (symbol.scan !== false) {
    const y = top + scan * height;
    ctx.fillStyle = toneColor("white", alpha * 0.16);
    ctx.strokeStyle = toneColor("white", alpha * 0.82);
    ctx.fillRect(left, y - height * 0.045, width, height * 0.09);
    ctx.beginPath();
    ctx.moveTo(left - width * 0.05, y);
    ctx.lineTo(left + width * 1.05, y);
    ctx.stroke();
  }
  if (symbol.label) {
    ctx.font = `${Math.max(3, Number(symbol.labelSize || height * 0.2))}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = toneColor("white", alpha * 0.76);
    ctx.fillText(String(symbol.label).slice(0, 16), center.x, top - 2);
  }
  ctx.restore();
}

function drawSvgSymbolMainframeCore(symbol, props, now) {
  const center = vectorSymbolPoint(symbol);
  const width = vectorSymbolNumber(symbol.w ?? symbol.width, 36, 6, 900);
  const height = vectorSymbolNumber(symbol.h ?? symbol.height, 30, 6, 900);
  const tone = symbol.tone || props.tone || "amber";
  const accentTone = symbol.accentTone || "green";
  const alpha = clamp(Number(symbol.alpha ?? 0.86), 0, 1);
  const phase = now * Number(symbol.speed ?? 0.0018) + Number(symbol.offset || 0);
  const left = center.x - width * 0.5;
  const top = center.y - height * 0.5;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, alpha);
  ctx.shadowBlur = Number(symbol.glow ?? props.glow ?? 7);
  for (let ring = 0; ring < 3; ring++) {
    const inset = ring * Math.min(width, height) * 0.13;
    ctx.lineWidth = Math.max(0.38, Number(symbol.strokeWidth || 0.72) * (1 - ring * 0.12));
    ctx.strokeStyle = toneColor(ring === 1 ? accentTone : tone, alpha * (0.62 - ring * 0.13));
    ctx.strokeRect(left + inset, top + inset, Math.max(1, width - inset * 2), Math.max(1, height - inset * 2));
  }
  const lanes = Math.max(2, Math.min(8, Number(symbol.lanes || 5)));
  ctx.strokeStyle = toneColor(accentTone, alpha * 0.44);
  ctx.lineWidth = Math.max(0.26, Number(symbol.circuitWidth || 0.45));
  for (let lane = 0; lane < lanes; lane++) {
    const y = top + ((lane + 0.5) / lanes) * height;
    const xPulse = ((phase * 18 + lane * 9) % width) + left;
    ctx.beginPath();
    ctx.moveTo(left - width * 0.22, y);
    ctx.lineTo(left + width * 0.18, y);
    ctx.moveTo(left + width * 0.82, y);
    ctx.lineTo(left + width * 1.22, y);
    ctx.stroke();
    ctx.fillStyle = toneColor("white", alpha * 0.78);
    ctx.beginPath();
    ctx.arc(xPulse, y, Math.max(0.45, width * 0.018), 0, Math.PI * 2);
    ctx.fill();
  }
  const corePulse = 1 + Math.sin(phase * 2.4) * 0.08;
  ctx.fillStyle = toneColor(accentTone, alpha * 0.18);
  ctx.strokeStyle = toneColor("white", alpha * 0.72);
  ctx.lineWidth = Math.max(0.4, Number(symbol.coreWidth || 0.75));
  ctx.beginPath();
  ctx.arc(center.x, center.y, Math.min(width, height) * 0.18 * corePulse, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  if (symbol.label) {
    ctx.font = `${Math.max(3, Number(symbol.labelSize || height * 0.17))}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", alpha * 0.78);
    ctx.fillText(String(symbol.label).slice(0, 16), center.x, center.y);
  }
  ctx.restore();
}

function drawSvgSymbols(symbols, props, now) {
  for (const symbol of symbols) {
    const safeSymbol = symbol && typeof symbol === "object" ? symbol : {};
    const kind = String(safeSymbol.kind || safeSymbol.type || "reticle");
    if (kind === "globe" || kind === "spinning_globe") drawSvgSymbolGlobe(safeSymbol, props, now);
    else if (kind === "filesystem_gate" || kind === "gate") drawSvgSymbolFilesystemGate(safeSymbol, props, now);
    else if (kind === "reticle" || kind === "target") drawSvgSymbolReticle(safeSymbol, props, now);
    else if (kind === "data_tunnel" || kind === "tunnel") drawSvgSymbolDataTunnel(safeSymbol, props, now);
    else if (kind === "ice_wall" || kind === "ice") drawSvgSymbolIceWall(safeSymbol, props, now);
    else if (kind === "mainframe_core" || kind === "core") drawSvgSymbolMainframeCore(safeSymbol, props, now);
  }
}

function vectorLayerStats(source, depth = 0) {
  const groups = Array.isArray(source.groups) && depth < 3 ? source.groups : [];
  const paths = Array.isArray(source.paths) ? source.paths : [];
  const morphPaths = paths.filter((path) => path && typeof path === "object" && Array.isArray(path.morphs));
  const base = {
    pathCount: paths.length,
    morphPathCount: morphPaths.length,
    morphFrameCount: morphPaths.reduce(
      (count, path) => count + path.morphs.filter(
        (frame) => frame && typeof frame === "object" && typeof frame.d === "string",
      ).length,
      0,
    ),
    circleCount: Array.isArray(source.circles) ? source.circles.length : 0,
    traceCount: Array.isArray(source.traces) ? source.traces.length : 0,
    symbolCount: Array.isArray(source.symbols) ? source.symbols.length : 0,
    symbolKinds: Array.isArray(source.symbols)
      ? source.symbols.map((symbol) => String(symbol?.kind || symbol?.type || "reticle"))
      : [],
    labelCount: Array.isArray(source.labels) ? source.labels.length : 0,
    rectCount: Array.isArray(source.rects) ? source.rects.length : 0,
    lineCount: Array.isArray(source.lines) ? source.lines.length : 0,
    polylineCount: Array.isArray(source.polylines) ? source.polylines.length : 0,
    polygonCount: Array.isArray(source.polygons) ? source.polygons.length : 0,
    groupCount: groups.length,
    keyframeCount: Array.isArray(source.keyframes)
      ? source.keyframes.filter((frame) => frame && typeof frame === "object").length
      : 0,
    ignoredMarkup: typeof source.rawSvg === "string" || typeof source.markup === "string",
  };
  for (const group of groups) {
    if (!group || typeof group !== "object") continue;
    const child = vectorLayerStats(group, depth + 1);
    base.pathCount += child.pathCount;
    base.morphPathCount += child.morphPathCount;
    base.morphFrameCount += child.morphFrameCount;
    base.circleCount += child.circleCount;
    base.traceCount += child.traceCount;
    base.symbolCount += child.symbolCount;
    base.symbolKinds.push(...child.symbolKinds);
    base.labelCount += child.labelCount;
    base.rectCount += child.rectCount;
    base.lineCount += child.lineCount;
    base.polylineCount += child.polylineCount;
    base.polygonCount += child.polygonCount;
    base.groupCount += child.groupCount;
    base.keyframeCount += child.keyframeCount;
    base.ignoredMarkup = base.ignoredMarkup || child.ignoredMarkup;
  }
  return base;
}

function drawSvgGroups(groups, props, now, depth = 0) {
  if (depth >= 3) return;
  for (const group of groups) {
    if (!group || typeof group !== "object") continue;
    const translate = group.translate || group.position || {};
    const localProps = {
      ...props,
      ...group,
      tone: group.tone || props.tone,
      glow: group.glow ?? props.glow,
      gradients: Array.isArray(group.gradients) ? group.gradients : props.gradients,
    };
    const scale = vectorNumber(group.scale, 1, 0.01, 100);
    const animation = vectorKeyframeTransform(group, now);
    ctx.save();
    ctx.globalAlpha *= clamp(Number(group.opacity ?? 1), 0, 1) * animation.opacity;
    ctx.translate(
      vectorNumber(group.x ?? translate.x, 0) + animation.x,
      vectorNumber(group.y ?? translate.y, 0) + animation.y,
    );
    ctx.rotate(Number(group.rotation || 0) + Number(group.spin || 0) * now * 0.00025 + animation.rotation);
    ctx.scale(scale * animation.scale, scale * animation.scale);
    drawSvgContent(group, localProps, now, depth + 1);
    ctx.restore();
  }
}

function drawSvgContentBody(source, props, now, depth = 0) {
  const paths = Array.isArray(source.paths) ? source.paths : [];
  const rects = Array.isArray(source.rects) ? source.rects : [];
  const lines = Array.isArray(source.lines) ? source.lines : [];
  const polylines = Array.isArray(source.polylines) ? source.polylines : [];
  const polygons = Array.isArray(source.polygons) ? source.polygons : [];
  const circles = Array.isArray(source.circles) ? source.circles : [];
  const traces = Array.isArray(source.traces) ? source.traces : [];
  const symbols = Array.isArray(source.symbols) ? source.symbols : [];
  const labels = Array.isArray(source.labels) ? source.labels : [];
  const groups = Array.isArray(source.groups) ? source.groups : [];
  for (const pathSpec of paths) drawSvgPath(pathSpec, props, now);
  drawSvgRects(rects, props, now);
  drawSvgLines(lines, props, now);
  drawSvgPolylineLike(polylines, props, now, false);
  drawSvgPolylineLike(polygons, props, now, true);
  drawSvgCircles(circles, props, now);
  drawSvgTraces(traces, props, now);
  drawSvgSymbols(symbols, props, now);
  drawSvgLabels(labels, props);
  drawSvgGroups(groups, props, now, depth);
}

function drawSvgContent(source, props, now, depth = 0) {
  const box = vectorViewBox(source.viewBox || props.viewBox);
  ctx.save();
  applyVectorClip(source.clip, box, now);
  drawVectorFilteredContent(source, props, box, now, (overrideProps = props) => {
    drawSvgContentBody(source, overrideProps, now, depth);
  });
  ctx.restore();
}

function drawSvgLayer(primitive, w, h, now) {
  if (typeof Path2D === "undefined") return;
  const props = primitive.props || {};
  const stats = vectorLayerStats(props);
  const hasVectors = stats.pathCount || stats.circleCount || stats.traceCount || stats.symbolCount
    || stats.labelCount || stats.rectCount || stats.lineCount || stats.polylineCount || stats.polygonCount;
  if (!hasVectors) return;
  const animation = vectorKeyframeTransform(props, now);
  const box = vectorViewBox(props.viewBox);
  const filterSpecs = vectorFilterSpecs(props);
  const clipState = vectorClipState(props.clip, box, now);
  if (typeof window !== "undefined") {
    window.__gibsonVectorState = window.__gibsonVectorState || {};
    window.__gibsonVectorState[primitive.id] = stats;
    window.__gibsonVectorAnimationState = window.__gibsonVectorAnimationState || {};
    window.__gibsonVectorAnimationState[primitive.id] = {
      keyframeCount: stats.keyframeCount,
      progress: vectorRounded(animation.progress),
      x: vectorRounded(animation.x),
      y: vectorRounded(animation.y),
      scale: vectorRounded(animation.scale),
      rotation: vectorRounded(animation.rotation),
      opacity: vectorRounded(animation.opacity),
    };
    window.__gibsonVectorEffectState = window.__gibsonVectorEffectState || {};
    window.__gibsonVectorEffectState[primitive.id] = {
      filterCount: filterSpecs.length,
      filterKinds: filterSpecs.map((spec) => spec.kind),
      clipKind: clipState.kind,
      clipProgress: vectorRounded(clipState.progress),
      clipActive: clipState.active,
    };
  }
  const position = normalizedPoint(props.position || {x: 0.5, y: 0.45}, w, h);
  const fit = Math.max(24 * devicePixelRatio, Number(props.scale || 0.22) * Math.min(w, h));
  const unit = fit / Math.max(box.width, box.height);
  const rotation = Number(props.rotation || 0) + Number(props.spin || 0) * now * 0.00025;
  ctx.save();
  ctx.globalAlpha *= animation.opacity;
  ctx.translate(position.x, position.y);
  ctx.rotate(rotation + animation.rotation);
  ctx.scale(unit * animation.scale, unit * animation.scale);
  ctx.translate(animation.x, animation.y);
  ctx.translate(-(box.x + box.width * 0.5), -(box.y + box.height * 0.5));
  ctx.globalCompositeOperation = props.blend === "screen" ? "screen" : "source-over";
  drawSvgContent(props, props, now);
  ctx.restore();
}

function drawNodeGraph(primitive, w, h) {
  const props = primitive.props || {};
  const nodes = Array.isArray(props.nodes) ? props.nodes : [];
  const edges = Array.isArray(props.edges) ? props.edges : [];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  ctx.save();
  ctx.lineWidth = 1.2 * devicePixelRatio;
  ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const edge of edges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) continue;
    const a = normalizedPoint(source, w, h);
    const b = normalizedPoint(target, w, h);
    ctx.strokeStyle = toneColor("cyan", 0.28);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    if (edge.label) {
      ctx.fillStyle = toneColor("white", 0.48);
      ctx.fillText(String(edge.label).slice(0, 12), (a.x + b.x) * 0.5, (a.y + b.y) * 0.5 - 8 * devicePixelRatio);
    }
  }
  for (const node of nodes) {
    const point = normalizedPoint(node, w, h);
    const tone = node.tone || "cyan";
    const focus = node.id === props.focusNodeId;
    const radius = (focus ? 18 : 12) * devicePixelRatio;
    ctx.shadowColor = toneColor(tone, focus ? 0.9 : 0.45);
    ctx.shadowBlur = focus ? 22 * devicePixelRatio : 10 * devicePixelRatio;
    ctx.fillStyle = toneColor(tone, focus ? 0.72 : 0.44);
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = toneColor("white", focus ? 0.78 : 0.45);
    ctx.stroke();
    if (node.label) {
      ctx.shadowBlur = 3 * devicePixelRatio;
      ctx.fillStyle = toneColor("white", 0.82);
      ctx.fillText(String(node.label).slice(0, 16), point.x, point.y + radius + 14 * devicePixelRatio);
    }
  }
  ctx.restore();
}

function spatialMapRect(props, w, h) {
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const position = props.position && typeof props.position === "object" ? props.position : {x: 0.5, y: 0.52};
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.46), 0.10, 1.2) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.32), 0.08, 0.94) * h;
  const x = finiteNumber(position.x, 0.5) * w - width * 0.5;
  const y = finiteNumber(position.y, 0.52) * h - height * 0.5;
  return {x, y, width, height};
}

function spatialMapObjects(props) {
  const rawObjects = Array.isArray(props.objects) ? props.objects : [];
  const capacity = Math.min(rawObjects.length, 160);
  const layout = String(props.layout || "grid");
  const columns = Math.max(1, Math.ceil(Math.sqrt(Math.max(1, capacity))));
  const rows = Math.max(1, Math.ceil(Math.max(1, capacity) / columns));
  const seed = finiteNumber(props.seed, 0);
  return rawObjects
    .filter((object) => object && typeof object === "object")
    .slice(0, capacity)
    .map((object, index) => {
      const entityId = firstString(object.entityId, object.entity_id);
      const entityKind = firstString(object.entityKind, object.entity_kind, object.kind, "object");
      const id = String(object.id || entityId || object.path || object.label || `object-${index}`);
      const column = index % columns;
      const row = Math.floor(index / columns);
      const ringAngle = capacity <= 1 ? 0 : (index / capacity) * Math.PI * 2;
      const ringRadius = 0.18 + 0.30 * seededUnit(seed + index * 2.3);
      const defaultX = layout === "ring"
        ? 0.5 + Math.cos(ringAngle) * ringRadius
        : (column + 0.5) / columns;
      const defaultY = layout === "ring"
        ? 0.5 + Math.sin(ringAngle) * ringRadius * 0.72
        : (row + 0.5) / rows;
      return {
        ...object,
        id,
        entityId,
        entityKind,
        label: String(object.label || object.name || object.path || id).slice(0, 28),
        x: clamp(finiteNumber(object.x, defaultX), 0, 1),
        y: clamp(finiteNumber(object.y, defaultY), 0, 1),
        z: clamp(finiteNumber(object.z ?? object.height, 0), 0, 1),
        mass: clamp(
          finiteNumber(object.mass ?? object.activityCount ?? object.lines, object.active ? 0.75 : 0.42),
          0,
          1,
        ),
        confidence: clamp(finiteNumber(object.confidence, 1), 0, 1),
      };
    });
}

function spatialMapPointInRect(rect, props, object) {
  const paddingX = rect.width * 0.08;
  const paddingY = rect.height * 0.12;
  const projection = String(props.projection || "isometric");
  const z = clamp(finiteNumber(object?.z, 0), 0, 1);
  const x = rect.x + paddingX + finiteNumber(object?.x, 0.5) * Math.max(1, rect.width - paddingX * 2);
  let y = rect.y + paddingY + finiteNumber(object?.y, 0.5) * Math.max(1, rect.height - paddingY * 2);
  if (projection !== "flat") y -= z * rect.height * 0.18;
  return {x, y};
}

function spatialMapObjectPoint(props, object, w, h) {
  return spatialMapPointInRect(spatialMapRect(props, w, h), props, object);
}

function spatialMapObjectTone(object, props) {
  const status = String(object.status || object.health || object.outcome || object.lastOutcome || "").toLowerCase();
  if (status.includes("fail") || status.includes("error") || status.includes("red")) return "red";
  if (status.includes("pass") || status.includes("ok") || status.includes("green")) return "green";
  if (status.includes("stale") || status.includes("warn")) return "amber";
  return object.tone || props.tone || "cyan";
}

function spatialMapObjectByRef(objects, ref) {
  const target = String(ref || "");
  if (!target) return null;
  return objects.find((object) => (
    object.id === target
    || object.entityId === target
    || object.path === target
    || object.label === target
  )) || null;
}

function drawSpatialMap(primitive, w, h, now) {
  const props = primitive.props || {};
  const rect = spatialMapRect(props, w, h);
  const objects = spatialMapObjects(props);
  const edges = Array.isArray(props.edges) ? props.edges.filter((edge) => edge && typeof edge === "object") : [];
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const opacity = clamp(finiteNumber(props.opacity, 0.74), 0, 1);
  const focusObjectId = props.focusObjectId ? String(props.focusObjectId) : (objects[0]?.id || null);
  const objectKinds = Array.from(new Set(objects.map((object) => object.entityKind).filter(Boolean))).slice(0, 8);
  const bindings = Array.isArray(props.worldBindings) ? props.worldBindings : [];
  const objectPoints = new Map(objects.map((object) => [object.id, spatialMapPointInRect(rect, props, object)]));

  if (typeof window !== "undefined") {
    window.__gibsonSpatialMapState = window.__gibsonSpatialMapState || {};
    const focused = objects.find((object) => object.id === focusObjectId || object.entityId === focusObjectId) || null;
    window.__gibsonSpatialMapState[primitive.id] = {
      objectCount: objects.length,
      edgeCount: edges.length,
      focusObjectId,
      focusedEntityId: focused?.entityId || null,
      objectKinds,
      worldBindingCount: bindings.length,
      tone,
      accentTone,
      hasLabels: props.labels !== false,
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.globalAlpha *= opacity;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.rect(rect.x, rect.y, rect.width, rect.height);
  ctx.clip();

  ctx.fillStyle = toneColor("white", 0.025);
  ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
  ctx.strokeStyle = toneColor(tone, 0.18);
  ctx.lineWidth = 1 * devicePixelRatio;
  const gridColumns = 8;
  const gridRows = 5;
  for (let column = 0; column <= gridColumns; column++) {
    const x = rect.x + (column / gridColumns) * rect.width;
    ctx.beginPath();
    ctx.moveTo(x, rect.y);
    ctx.lineTo(x - rect.width * 0.08, rect.y + rect.height);
    ctx.stroke();
  }
  for (let row = 0; row <= gridRows; row++) {
    const y = rect.y + (row / gridRows) * rect.height;
    ctx.beginPath();
    ctx.moveTo(rect.x, y);
    ctx.lineTo(rect.x + rect.width, y - rect.height * 0.08);
    ctx.stroke();
  }

  ctx.setLineDash([9 * devicePixelRatio, 12 * devicePixelRatio]);
  ctx.lineDashOffset = -now * 0.030;
  for (const edge of edges.slice(0, 220)) {
    const source = spatialMapObjectByRef(objects, edge.source ?? edge.from);
    const target = spatialMapObjectByRef(objects, edge.target ?? edge.to);
    if (!source || !target) continue;
    const a = objectPoints.get(source.id);
    const b = objectPoints.get(target.id);
    if (!a || !b) continue;
    const edgeTone = edge.tone || (edge.active ? accentTone : tone);
    const alpha = edge.active || edge.flow ? 0.46 : 0.22;
    ctx.strokeStyle = toneColor(edgeTone, alpha);
    ctx.lineWidth = Math.max(0.65, finiteNumber(edge.width, edge.active ? 1.6 : 1.0)) * devicePixelRatio;
    ctx.beginPath();
    const midX = (a.x + b.x) * 0.5;
    const midY = (a.y + b.y) * 0.5 - rect.height * finiteNumber(edge.curve, 0.05);
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo(midX, midY, b.x, b.y);
    ctx.stroke();
    if (edge.label && props.labels !== false) {
      ctx.setLineDash([]);
      ctx.font = `${9 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.fillStyle = toneColor("white", 0.42);
      ctx.fillText(String(edge.label).slice(0, 14), midX, midY - 5 * devicePixelRatio);
      ctx.setLineDash([9 * devicePixelRatio, 12 * devicePixelRatio]);
    }
  }
  ctx.setLineDash([]);

  for (const [index, object] of objects.entries()) {
    const point = objectPoints.get(object.id);
    if (!point) continue;
    ctx.save();
    ctx.globalAlpha *= clamp(finiteNumber(object.opacity, 1), 0.12, 1);
    const focused = object.id === focusObjectId || object.entityId === focusObjectId;
    const active = Boolean(object.active || focused);
    const objectTone = spatialMapObjectTone(object, props);
    const radius = (5.5 + object.mass * 9 + (focused ? 5 : 0)) * devicePixelRatio;
    const lift = object.z * rect.height * 0.13;
    const pulse = active ? 1 + Math.sin(now * 0.006 + index) * 0.11 : 1;
    const r = radius * pulse;
    ctx.shadowColor = toneColor(objectTone, focused ? 0.92 : 0.54);
    ctx.shadowBlur = (focused ? 22 : 10) * devicePixelRatio;
    if (lift > 1) {
      ctx.strokeStyle = toneColor(objectTone, focused ? 0.45 : 0.24);
      ctx.lineWidth = Math.max(0.8, 1.2 * devicePixelRatio);
      ctx.beginPath();
      ctx.moveTo(point.x, point.y + lift);
      ctx.lineTo(point.x, point.y);
      ctx.stroke();
    }
    ctx.fillStyle = toneColor(objectTone, focused ? 0.58 : 0.34);
    ctx.strokeStyle = toneColor("white", focused ? 0.78 : 0.38 + object.confidence * 0.18);
    ctx.lineWidth = Math.max(0.8, (focused ? 1.8 : 1.0) * devicePixelRatio);
    ctx.beginPath();
    ctx.moveTo(point.x, point.y - r);
    ctx.lineTo(point.x + r * 0.82, point.y);
    ctx.lineTo(point.x, point.y + r * 0.58);
    ctx.lineTo(point.x - r * 0.82, point.y);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    if (object.confidence < 0.99) {
      ctx.strokeStyle = toneColor("amber", (1 - object.confidence) * 0.5);
      ctx.beginPath();
      ctx.arc(point.x, point.y, r * 1.35, 0, Math.PI * 2);
      ctx.stroke();
    }
    if (props.labels !== false && (focused || object.active || objects.length <= 18)) {
      ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = toneColor("white", focused ? 0.88 : 0.58);
      ctx.shadowBlur = 4 * devicePixelRatio;
      ctx.fillText(String(object.label).slice(0, 18), point.x, point.y + r + 4 * devicePixelRatio);
    }
    ctx.restore();
  }

  if (props.label) {
    ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = toneColor("white", 0.76);
    ctx.shadowColor = toneColor(accentTone, 0.58);
    ctx.shadowBlur = 7 * devicePixelRatio;
    ctx.fillText(String(props.label).slice(0, 32), rect.x + 8 * devicePixelRatio, rect.y + 7 * devicePixelRatio);
  }

  ctx.strokeStyle = toneColor(accentTone, 0.34);
  ctx.lineWidth = 1.2 * devicePixelRatio;
  ctx.strokeRect(
    rect.x + 0.5 * devicePixelRatio,
    rect.y + 0.5 * devicePixelRatio,
    rect.width - devicePixelRatio,
    rect.height - devicePixelRatio,
  );
  ctx.restore();
}

// --- projection_scene: themed renderer for resolved perception projections ---
// The engine (Python) decides WHAT is on screen; this code owns HOW it moves:
// per-node tweening (object constancy), camera glide, effect animation, theme.

const projectionTweens = new Map();
const projectionEdgeTweens = new Map();
const projectionEffectClocks = new Map();
let projectionCameraState = {x: 0.5, y: 0.5, zoom: 1.0};
let projectionLastNow = 0;

const PROJECTION_THEMES = {
  gibson: {
    palette: {
      base: "0,229,255", accent: "255,0,170", good: "57,255,20",
      warn: "255,191,0", alarm: "255,45,85", ghost: "150,170,190",
    },
    ink: "235,245,255",
    paper: null,
    grid: true,
    scanlines: true,
    glow: 16,
    nodeShape: "diamond",
  },
  blueprint: {
    palette: {
      base: "52,86,138", accent: "160,40,90", good: "26,122,82",
      warn: "164,116,28", alarm: "182,52,44", ghost: "148,160,176",
    },
    ink: "30,46,72",
    paper: "226,236,246",
    grid: true,
    scanlines: false,
    glow: 0,
    nodeShape: "circle",
  },
};

function projectionTone(theme, tone, alpha) {
  const rgb = theme.palette[tone] || theme.palette.base;
  return `rgba(${rgb},${alpha})`;
}

function projectionStageRect(w, h) {
  return {x: w * 0.03, y: h * 0.07, width: w * 0.94, height: h * 0.60};
}

function projectionNodePoint(rect, tween, camera) {
  const cx = rect.x + rect.width * 0.5;
  const cy = rect.y + rect.height * 0.5;
  const px = rect.x + tween.x * rect.width;
  const py = rect.y + tween.y * rect.height - tween.lift * rect.height * 0.16;
  return {
    x: cx + (px - cx - (camera.x - 0.5) * rect.width * 0.4) * camera.zoom,
    y: cy + (py - cy - (camera.y - 0.5) * rect.height * 0.4) * camera.zoom,
  };
}

function drawProjectionScene(primitive, w, h, now) {
  const props = primitive.props || {};
  // a session reset rebuilds the engine and its revision counter restarts:
  // clear all presentation state so the world re-materializes from scratch
  if (Number(props.revision || 0) < (drawProjectionScene.lastRevision || 0)) {
    projectionTweens.clear();
    projectionEdgeTweens.clear();
    projectionEffectClocks.clear();
    projectionPeekWindows.clear();
    projectionPeekConsumed.clear();
    projectionNarrationStack.length = 0;
    projectionCameraState = {x: 0.5, y: 0.5, zoom: 1.0};
  }
  drawProjectionScene.lastRevision = Number(props.revision || 0);
  const theme = PROJECTION_THEMES[String(props.theme || "gibson")] || PROJECTION_THEMES.gibson;
  const nodes = Array.isArray(props.nodes) ? props.nodes : [];
  const edges = Array.isArray(props.edges) ? props.edges : [];
  const effects = Array.isArray(props.effects) ? props.effects : [];
  const mood = props.mood || {};
  const hud = props.hud || {};
  const rect = projectionStageRect(w, h);
  const dt = projectionLastNow ? Math.min(0.1, (now - projectionLastNow) / 1000) : 0.016;
  projectionLastNow = now;
  const ease = 1 - Math.exp(-dt * 5.0);

  // tween node state toward engine targets. New nodes materialize as a
  // wavefront: a pulse travels down the link from the parent and the node
  // appears when it arrives -- the whole tree on first contact, single buds
  // later. Capture mode snaps (deterministic frames). Nodes in physics layers
  // are integrated by the live simulation instead of eased.
  const physicsLayers = new Set(
    props.physics && Array.isArray(props.physics.layers) ? props.physics.layers : [],
  );
  const simulate = physicsLayers.size > 0 && !captureMode;
  const firstScene = projectionTweens.size === 0;
  const liveIds = new Set();
  for (const node of nodes) {
    liveIds.add(node.id);
    let tween = projectionTweens.get(node.id);
    if (!tween) {
      tween = {x: node.x, y: node.y, size: node.size, opacity: node.opacity ?? 1,
               lift: node.lift || 0, vx: 0, vy: 0};
      if (!captureMode) {
        const depth = Number(node.depth || 0);
        tween.revealAt = firstScene ? now + 350 + depth * 260 : now + 60;
        tween.reveal = 0;
      } else {
        tween.reveal = 1;
      }
      projectionTweens.set(node.id, tween);
    }
    if (tween.revealAt !== undefined) {
      const progress = (now - tween.revealAt) / 520;
      if (progress >= 1) {
        tween.revealAt = undefined;
        tween.reveal = 1;
      } else {
        tween.reveal = Math.max(0, progress);
      }
    } else {
      tween.reveal = 1;
    }
    if (!(simulate && physicsLayers.has(node.layer))) {
      // spring-damper motion: accelerate toward the target, decelerate on
      // approach -- a long hop (the agent cursor changing focus) reads as a
      // swoop instead of a snap; near-stationary nodes are unaffected
      const stiffness = 26;
      const dampingFactor = Math.exp(-9 * dt);
      tween.vx = (tween.vx + (node.x - tween.x) * stiffness * dt) * dampingFactor;
      tween.vy = (tween.vy + (node.y - tween.y) * stiffness * dt) * dampingFactor;
      tween.x += tween.vx * dt;
      tween.y += tween.vy * dt;
    }
    tween.size += (node.size - tween.size) * ease;
    tween.opacity += ((node.opacity ?? 1) - tween.opacity) * ease;
    tween.lift += ((node.lift || 0) - tween.lift) * ease;
    tween.node = node;
    tween.leaving = false;
  }
  if (simulate) stepProjectionPhysics(edges, physicsLayers, dt, now);
  // deselected nodes fade out in place instead of popping
  for (const [id, tween] of Array.from(projectionTweens.entries())) {
    if (liveIds.has(id)) continue;
    tween.leaving = true;
    tween.opacity += (0 - tween.opacity) * ease * 1.6;
    tween.size += (0 - tween.size) * ease * 0.8;
    if (tween.opacity < 0.04) projectionTweens.delete(id);
  }

  // effect clocks run on the browser timebase, keyed by effect id; a newly
  // arrived effect also kicks the physics simulation at its target nodes
  const liveEffects = new Set();
  for (const effect of effects) {
    liveEffects.add(effect.id);
    if (!projectionEffectClocks.has(effect.id)) {
      projectionEffectClocks.set(effect.id, now);
      if (simulate) kickProjectionPhysics(effect);
    }
  }
  for (const id of Array.from(projectionEffectClocks.keys())) {
    if (!liveEffects.has(id)) projectionEffectClocks.delete(id);
  }

  // camera frames the bounding box of the points of interest: center glides
  // to their midpoint and zoom is chosen so every POI stays on screen
  const poiIds = props.camera && Array.isArray(props.camera.targets) && props.camera.targets.length
    ? props.camera.targets
    : (props.camera && props.camera.target ? [props.camera.target] : []);
  const pois = poiIds.map((id) => projectionTweens.get(id)).filter(Boolean);
  let wantX = 0.5;
  let wantY = 0.5;
  let wantZoom = 1.0;
  if (pois.length) {
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const poi of pois) {
      minX = Math.min(minX, poi.x);
      maxX = Math.max(maxX, poi.x);
      minY = Math.min(minY, poi.y);
      maxY = Math.max(maxY, poi.y);
    }
    wantX = (minX + maxX) / 2;
    wantY = (minY + maxY) / 2;
    const fitX = 0.9 / ((maxX - minX) + 0.45);
    const fitY = 0.9 / ((maxY - minY) + 0.45);
    wantZoom = Math.min(1.15, Math.max(0.85, Math.min(fitX, fitY)));
  }
  let shakeX = 0;
  let shakeY = 0;
  for (const effect of effects) {
    if (effect.kind !== "shake") continue;
    const progress = projectionEffectProgress(effect, now);
    if (progress >= 0 && progress < 1) {
      const decay = 1 - progress;
      shakeX += Math.sin(now * 0.09) * 0.012 * decay;
      shakeY += Math.cos(now * 0.117) * 0.010 * decay;
    }
  }
  projectionCameraState.x += (wantX - projectionCameraState.x) * ease * 0.7;
  projectionCameraState.y += (wantY - projectionCameraState.y) * ease * 0.7;
  projectionCameraState.zoom += (wantZoom - projectionCameraState.zoom) * ease * 0.7;
  const camera = {
    x: projectionCameraState.x + shakeX,
    y: projectionCameraState.y + shakeY,
    zoom: projectionCameraState.zoom,
  };

  if (typeof window !== "undefined") {
    window.__gibsonProjectionState = {
      theme: String(props.theme || "gibson"),
      nodeCount: nodes.length,
      edgeCount: edges.length,
      effectCount: effects.length,
      mood: String(mood.name || ""),
      revision: Number(props.revision || 0),
    };
  }
  // the projection owns the assistant narration (hud.narration); suppress the
  // page-chrome stream panel so the voice appears exactly once
  if (streamPanel && !streamPanel.hidden) streamPanel.hidden = true;

  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  if (theme.paper) {
    ctx.fillStyle = `rgba(${theme.paper},0.96)`;
    ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
  }
  if (theme.grid) {
    ctx.strokeStyle = theme.paper ? `rgba(${theme.ink},0.14)` : projectionTone(theme, "base", 0.07);
    ctx.lineWidth = 1;
    const cells = 12;
    for (let i = 0; i <= cells; i++) {
      const gx = rect.x + (i / cells) * rect.width;
      const gy = rect.y + (i / cells) * rect.height;
      ctx.beginPath();
      ctx.moveTo(gx, rect.y);
      ctx.lineTo(gx - rect.width * 0.05, rect.y + rect.height);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(rect.x, gy);
      ctx.lineTo(rect.x + rect.width, gy - rect.height * 0.05);
      ctx.stroke();
    }
  }

  const pointFor = (id) => {
    const tween = projectionTweens.get(id);
    return tween ? projectionNodePoint(rect, tween, camera) : null;
  };

  // edge object constancy: edges fade in when relations appear and DECAY when
  // they vanish -- attention beams and causality flows ghost out over a few
  // seconds (the previous focus lingers, tenuous), structure fades quickly
  const liveEdgeKeys = new Set();
  for (const edge of edges) {
    const edgeKey = `${edge.from}|${edge.to}|${edge.style || "skeleton"}`;
    liveEdgeKeys.add(edgeKey);
    let edgeTween = projectionEdgeTweens.get(edgeKey);
    if (!edgeTween) {
      edgeTween = {edge, alpha: 0};
      projectionEdgeTweens.set(edgeKey, edgeTween);
    }
    edgeTween.edge = edge;
  }
  for (const [edgeKey, edgeTween] of Array.from(projectionEdgeTweens.entries())) {
    const live = liveEdgeKeys.has(edgeKey);
    const style = edgeTween.edge.style || "skeleton";
    const rate = live ? 6 : (style === "skeleton" ? 4 : 0.55);
    edgeTween.alpha += ((live ? 1 : 0) - edgeTween.alpha) * Math.min(1, dt * rate);
    if (!live && edgeTween.alpha < 0.04) {
      projectionEdgeTweens.delete(edgeKey);
      continue;
    }
    const edge = edgeTween.edge;
    const a = pointFor(edge.from);
    const b = pointFor(edge.to);
    if (!a || !b) continue;
    const tone = edge.tone || "base";
    const sourceReveal = projectionTweens.get(edge.from)?.reveal ?? 1;
    const targetReveal = projectionTweens.get(edge.to)?.reveal ?? 1;
    if (sourceReveal <= 0 || targetReveal <= 0) continue;
    ctx.save();
    ctx.globalAlpha *= edgeTween.alpha;
    if (targetReveal < 1) {
      // the materialize pulse: the link draws itself toward the unborn node,
      // a bright head riding the wavefront
      const t = targetReveal * targetReveal * (3 - 2 * targetReveal);
      const tipX = a.x + (b.x - a.x) * t;
      const tipY = a.y + (b.y - a.y) * t;
      ctx.setLineDash([]);
      ctx.strokeStyle = projectionTone(theme, tone, 0.55);
      ctx.lineWidth = 1.3 * devicePixelRatio;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(tipX, tipY);
      ctx.stroke();
      ctx.save();
      ctx.shadowColor = projectionTone(theme, "accent", 0.9);
      ctx.shadowBlur = 10 * devicePixelRatio;
      ctx.fillStyle = projectionTone(theme, "accent", 0.95);
      ctx.beginPath();
      ctx.arc(tipX, tipY, 2.4 * devicePixelRatio, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
      ctx.restore();
      continue;
    }
    if (edge.style === "flow") {
      ctx.setLineDash([7, 9]);
      ctx.lineDashOffset = -now * 0.04;
      ctx.strokeStyle = projectionTone(theme, tone, 0.7);
      ctx.lineWidth = 1.7 * devicePixelRatio;
    } else if (edge.style === "beam") {
      ctx.setLineDash([]);
      ctx.strokeStyle = projectionTone(theme, tone, 0.35 + 0.2 * Math.sin(now * 0.005));
      ctx.lineWidth = 2.2 * devicePixelRatio;
    } else {
      ctx.setLineDash([]);
      ctx.strokeStyle = theme.paper
        ? `rgba(${theme.ink},0.40)`
        : projectionTone(theme, tone, 0.34);
      ctx.lineWidth = 1.1 * devicePixelRatio;
    }
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    const midX = (a.x + b.x) * 0.5;
    const midY = (a.y + b.y) * 0.5 - rect.height * 0.03;
    ctx.quadraticCurveTo(midX, midY, b.x, b.y);
    ctx.stroke();
    ctx.restore();
  }
  ctx.setLineDash([]);

  for (const effect of effects) {
    drawProjectionEffect(effect, theme, rect, pointFor, now, w, h);
  }
  flushProjectionPeekWindows(theme, now);

  const showAllLabels = nodes.length <= 40;
  for (const tween of projectionTweens.values()) {
    const node = tween.node;
    if (!node) continue;
    const reveal = tween.reveal ?? 1;
    if (reveal <= 0) continue;
    const point = projectionNodePoint(rect, tween, camera);
    // a materializing node blooms in over the tail of its reveal
    const bloom = reveal >= 1 ? 1 : Math.max(0, (reveal - 0.72) / 0.28);
    if (bloom <= 0) continue;
    const radius = (3.5 + tween.size * 11) * devicePixelRatio * (0.4 + 0.6 * bloom);
    const tone = node.tone || "base";
    const isFocus = Boolean(node.focus) && !tween.leaving;
    // labels degrade by importance on crowded scenes instead of vanishing:
    // directories, the focus, alarms, and busy (large) nodes stay named
    const showLabel = !tween.leaving && bloom >= 1 && node.label
      && (showAllLabels || isFocus || tone === "alarm" || node.kind === "dir" || tween.size > 0.55);
    ctx.save();
    ctx.globalAlpha *= Math.max(0.05, tween.opacity) * bloom;
    if (tween.lift > 0.02) {
      ctx.strokeStyle = projectionTone(theme, tone, 0.3);
      ctx.lineWidth = devicePixelRatio;
      ctx.beginPath();
      ctx.moveTo(point.x, point.y + tween.lift * rect.height * 0.16);
      ctx.lineTo(point.x, point.y);
      ctx.stroke();
    }
    ctx.shadowColor = projectionTone(theme, tone, isFocus ? 0.95 : 0.6);
    ctx.shadowBlur = theme.glow * (isFocus ? 1.6 : 1) * devicePixelRatio * 0.12 * 8;
    ctx.fillStyle = projectionTone(theme, tone, isFocus ? 0.7 : 0.42);
    ctx.strokeStyle = theme.paper
      ? `rgba(${theme.ink},0.85)`
      : projectionTone(theme, tone, isFocus ? 1.0 : 0.8);
    ctx.lineWidth = (isFocus ? 1.9 : 1.1) * devicePixelRatio;
    ctx.beginPath();
    if (node.kind === "agent") {
      const wob = 1 + Math.sin(now * 0.008) * 0.12;
      ctx.moveTo(point.x, point.y - radius * 1.25 * wob);
      ctx.lineTo(point.x + radius * 0.9 * wob, point.y + radius * 0.7 * wob);
      ctx.lineTo(point.x - radius * 0.9 * wob, point.y + radius * 0.7 * wob);
      ctx.closePath();
    } else if (node.kind === "dir" || theme.nodeShape === "circle") {
      ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    } else {
      ctx.moveTo(point.x, point.y - radius);
      ctx.lineTo(point.x + radius * 0.85, point.y);
      ctx.lineTo(point.x, point.y + radius * 0.65);
      ctx.lineTo(point.x - radius * 0.85, point.y);
      ctx.closePath();
    }
    ctx.fill();
    ctx.stroke();
    if (isFocus) {
      ctx.setLineDash([4, 5]);
      ctx.beginPath();
      ctx.arc(point.x, point.y, radius * 1.7, now * 0.002, now * 0.002 + Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (showLabel) {
      ctx.shadowBlur = 3;
      ctx.font = `${9.5 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = theme.paper
        ? `rgba(${theme.ink},${isFocus ? 0.95 : 0.7})`
        : `rgba(${theme.ink},${isFocus ? 0.95 : 0.62})`;
      ctx.fillText(String(node.label), point.x, point.y + radius + 4 * devicePixelRatio);
    }
    ctx.restore();
  }

  drawProjectionHud(props, theme, mood, hud, rect, w, h, now);

  if (mood.alert) {
    const throb = 0.10 + 0.06 * Math.sin(now * 0.004);
    const vignette = ctx.createRadialGradient(w / 2, h / 2, h * 0.3, w / 2, h / 2, h * 0.85);
    vignette.addColorStop(0, "rgba(0,0,0,0)");
    vignette.addColorStop(1, projectionTone(theme, "alarm", throb));
    ctx.fillStyle = vignette;
    ctx.fillRect(0, 0, w, h);
  }
  if (theme.scanlines) {
    ctx.fillStyle = "rgba(0,0,0,0.10)";
    for (let y = rect.y; y < rect.y + rect.height; y += 4 * devicePixelRatio) {
      ctx.fillRect(rect.x, y, rect.width, devicePixelRatio);
    }
  }
  ctx.restore();
}

const PROJECTION_KICKS = {breach: 0.34, shake: 0.0, alarm: 0.0, banner: 0.0, ring: 0.16, pulse: 0.1, beam: 0.08};

function kickProjectionPhysics(effect) {
  // events are physical: a breach detonates at its nodes and ripples the web
  const strength = PROJECTION_KICKS[effect.kind] ?? 0.08;
  if (strength <= 0) return;
  const magnitude = Math.max(0.2, Number(effect.magnitude || 1));
  const targets = Array.isArray(effect.targets) ? effect.targets : [];
  for (const targetId of targets) {
    const epicenter = projectionTweens.get(targetId);
    if (!epicenter || !epicenter.node) continue;
    const phase = (epicenter.phase ?? 0) + targets.length;
    epicenter.vx = (epicenter.vx || 0) + Math.cos(phase) * strength * magnitude;
    epicenter.vy = (epicenter.vy || 0) + Math.sin(phase) * strength * magnitude;
    for (const other of projectionTweens.values()) {
      if (other === epicenter || !other.node || other.leaving) continue;
      const dx = other.x - epicenter.x;
      const dy = other.y - epicenter.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 0.0001 || dist > 0.22) continue;
      const shove = strength * magnitude * 0.6 * (1 - dist / 0.22);
      other.vx = (other.vx || 0) + (dx / dist) * shove;
      other.vy = (other.vy || 0) + (dy / dist) * shove;
    }
  }
}

function stepProjectionPhysics(edges, physicsLayers, dt, now) {
  // Live spring-mass integration between engine updates. The engine's
  // resolved positions stay canonical: every node is softly attracted to its
  // target, so the simulation adds continuous motion without drift.
  const bodies = [];
  for (const tween of projectionTweens.values()) {
    if (tween.node && physicsLayers.has(tween.node.layer) && !tween.leaving) bodies.push(tween);
  }
  if (!bodies.length) return;
  const byId = new Map(bodies.map((tween) => [tween.node.id, tween]));
  const rest = Math.max(0.10, Math.min(0.20, 0.55 / Math.sqrt(bodies.length)));
  const clampedDt = Math.min(0.05, dt);
  for (const tween of bodies) {
    if (tween.phase === undefined) {
      let hash = 5381;
      for (const ch of tween.node.id) hash = (hash * 33 + ch.charCodeAt(0)) % 6283;
      tween.phase = hash / 1000;
    }
    tween.fx = (tween.node.x - tween.x) * 6.0;       // anchor to engine layout
    tween.fy = (tween.node.y - tween.y) * 6.0;
    tween.fx += (0.5 - tween.x) * 0.4;               // gentle center gravity
    tween.fy += (0.5 - tween.y) * 0.4;
    // ambient excitation so the web never fully freezes between events
    tween.fx += Math.sin(now * 0.0009 + tween.phase) * 0.07;
    tween.fy += Math.cos(now * 0.0007 + tween.phase * 1.7) * 0.07;
  }
  for (let i = 0; i < bodies.length; i++) {
    for (let j = i + 1; j < bodies.length; j++) {
      const a = bodies[i];
      const b = bodies[j];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const distSq = Math.max(0.0004, dx * dx + dy * dy);
      const push = 0.0011 / distSq;
      a.fx += dx * push;
      a.fy += dy * push;
      b.fx -= dx * push;
      b.fy -= dy * push;
    }
  }
  for (const edge of edges) {
    if (edge.style !== "skeleton") continue;
    const a = byId.get(edge.from);
    const b = byId.get(edge.to);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;
    const pull = (dist - rest) * 3.2;
    a.fx += (dx / dist) * pull;
    a.fy += (dy / dist) * pull;
    b.fx -= (dx / dist) * pull;
    b.fy -= (dy / dist) * pull;
  }
  const damping = Math.pow(0.0035, clampedDt); // strong exponential damping
  for (const tween of bodies) {
    tween.vx = (tween.vx + tween.fx * clampedDt) * damping;
    tween.vy = (tween.vy + tween.fy * clampedDt) * damping;
    const speed = Math.sqrt(tween.vx * tween.vx + tween.vy * tween.vy);
    if (speed > 0.5) {
      tween.vx *= 0.5 / speed;
      tween.vy *= 0.5 / speed;
    }
    tween.x = Math.min(0.98, Math.max(0.02, tween.x + tween.vx * clampedDt));
    tween.y = Math.min(0.98, Math.max(0.02, tween.y + tween.vy * clampedDt));
  }
}

function projectionEffectProgress(effect, now) {
  const started = (projectionEffectClocks.get(effect.id) ?? now) + Number(effect.delayMs || 0);
  const duration = Math.max(1, Number(effect.durationMs || effect.ttlMs || 2000));
  return Math.min(1, (now - started) / duration); // negative while delayed
}

function drawProjectionEffect(effect, theme, rect, pointFor, now, w, h) {
  const progress = projectionEffectProgress(effect, now);
  if (progress >= 1) return;
  const tone = effect.tone || "accent";
  const fade = 1 - progress;
  const targets = Array.isArray(effect.targets) ? effect.targets : [];
  if (effect.kind === "peek" && targets.length) {
    drawProjectionPeek(effect, theme, pointFor(targets[0]), now);
    return;
  }
  if (progress < 0) return; // delayMs: holding for its cue
  // heavy beats ramp in instead of cutting hard over whatever came before
  const attack = effect.kind === "breach" || effect.kind === "alarm"
    ? Math.min(1, progress / 0.15)
    : 1;
  if (effect.kind === "alarm") {
    ctx.fillStyle = projectionTone(theme, tone, 0.16 * fade * attack * (0.6 + 0.4 * Math.sin(now * 0.02)));
    ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
    return;
  }
  if (effect.kind === "banner") {
    ctx.font = `${15 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = projectionTone(theme, tone, 0.9 * fade);
    ctx.fillText(String(effect.label || ""), rect.x + rect.width / 2, rect.y + rect.height * 0.16);
    return;
  }
  if (effect.kind === "beam" && targets.length >= 2) {
    const a = pointFor(targets[0]);
    const b = pointFor(targets[1]);
    if (a && b) {
      ctx.strokeStyle = projectionTone(theme, tone, 0.85 * fade);
      ctx.lineWidth = 2.6 * devicePixelRatio;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(a.x + (b.x - a.x) * Math.min(1, progress * 2), a.y + (b.y - a.y) * Math.min(1, progress * 2));
      ctx.stroke();
    }
    return;
  }
  for (const targetId of targets) {
    const point = pointFor(targetId);
    if (!point) continue;
    const magnitude = Math.max(0.15, Number(effect.magnitude || 1));
    if (effect.kind === "breach") {
      for (let ring = 0; ring < 3; ring++) {
        const ringProgress = Math.min(1, progress * 1.4 + ring * 0.12);
        ctx.strokeStyle = projectionTone(theme, tone, (0.8 - ring * 0.22) * fade * attack);
        ctx.lineWidth = (2.4 - ring * 0.6) * devicePixelRatio;
        ctx.beginPath();
        const jag = 10;
        const baseRadius = (8 + ringProgress * 46 + ring * 7) * devicePixelRatio;
        for (let k = 0; k <= jag; k++) {
          const theta = (k / jag) * Math.PI * 2;
          const wobble = 1 + 0.12 * Math.sin(theta * 5 + now * 0.01 + ring);
          const px = point.x + Math.cos(theta) * baseRadius * wobble;
          const py = point.y + Math.sin(theta) * baseRadius * 0.8 * wobble;
          if (k === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.stroke();
      }
    } else if (effect.kind === "ring") {
      ctx.strokeStyle = projectionTone(theme, tone, 0.85 * fade);
      ctx.lineWidth = 2 * devicePixelRatio;
      ctx.beginPath();
      ctx.arc(point.x, point.y, (10 + progress * 70) * devicePixelRatio, 0, Math.PI * 2);
      ctx.stroke();
      if (effect.label) {
        ctx.font = `${11 * devicePixelRatio}px ui-monospace, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillStyle = projectionTone(theme, tone, 0.95 * fade);
        ctx.fillText(String(effect.label), point.x, point.y - (14 + progress * 30) * devicePixelRatio);
      }
    } else {
      ctx.strokeStyle = projectionTone(theme, tone, 0.7 * fade);
      ctx.lineWidth = 1.6 * devicePixelRatio;
      ctx.beginPath();
      ctx.arc(point.x, point.y, (4 + progress * 26 * magnitude) * devicePixelRatio, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
}

const projectionPeekWindows = new Map();
const projectionPeekConsumed = new Set(); // effect ids already shown, OUTLIVES windows
const PEEK_HOLD_MS = 2600;   // window stays open this long after the last new diff
const PEEK_OPEN_MS = 220;
const PEEK_WINK_MS = 260;
const PEEK_VISIBLE_LINES = 10;
const PEEK_BACKLOG_LINES = 4000; // sanity only: the full diff is the show

function drawProjectionPeek(effect, theme, point, now) {
  // an accruing terminal window per node: diffs append as the agent edits,
  // the box grows up to PEEK_VISIBLE_LINES then bottom-follows like a tail,
  // and it winks closed after a quiet period. One edit = one short visit;
  // a burst of edits keeps the window open and scrolling. The WINDOW outlives
  // the effect (flushProjectionPeekWindows): a long diff finishes its scroll
  // even after the scene drops the effect that delivered it.
  if (!point) return;
  const newLines = Array.isArray(effect.lines) ? effect.lines : [];
  const key = `peek:${effect.targets[0]}`;
  let win = projectionPeekWindows.get(key);
  const unseen = !projectionPeekConsumed.has(effect.id);
  // a closed window stays closed for content it already showed, even though
  // the effect outlives the window in scene state
  if (!win && !unseen) return;
  if (!win) {
    win = {lines: [], lastChunk: "", openedAt: now,
           lastAppendAt: now, offset: 0, lastNow: now, drawnAt: 0};
    projectionPeekWindows.set(key, win);
  }
  win.point = point;
  win.tone = effect.tone || "accent";
  if (unseen) {
    projectionPeekConsumed.add(effect.id);
    if (projectionPeekConsumed.size > 600) projectionPeekConsumed.clear();
    const chunk = newLines.join("\\n");
    if (chunk && chunk !== win.lastChunk) {
      win.lastChunk = chunk;
      win.lines.push(...newLines);
      win.lastAppendAt = now;
      if (win.lines.length > PEEK_BACKLOG_LINES) {
        const dropped = win.lines.length - PEEK_BACKLOG_LINES;
        win.lines.splice(0, dropped);
        win.offset = Math.max(0, win.offset - dropped);
      }
    }
  }
  renderProjectionPeekWindow(key, win, theme, now);
}

function flushProjectionPeekWindows(theme, now) {
  // windows whose delivering effects have expired keep drawing until their
  // scroll completes and the quiet hold runs out
  for (const [key, win] of projectionPeekWindows) {
    if (win.drawnAt !== now) renderProjectionPeekWindow(key, win, theme, now);
  }
}

function renderProjectionPeekWindow(key, win, theme, now) {
  if (win.drawnAt === now) return; // several live effects, one window
  win.drawnAt = now;
  if (!win.lines.length || !win.point) return;
  const point = win.point;

  const dt = Math.min(0.1, (now - win.lastNow) / 1000);
  win.lastNow = now;
  const lineHeight = 11 * devicePixelRatio;
  const visibleLines = Math.min(PEEK_VISIBLE_LINES, win.lines.length);
  const innerHeight = visibleLines * lineHeight;
  const boxHeight = innerHeight + 10 * devicePixelRatio;
  const boxWidth = 260 * devicePixelRatio;
  // stream toward the newest lines at a readable pace, but let a huge
  // backlog FLY past (rate scales with distance) and decelerate into a
  // readable tail as it catches up
  const targetOffset = Math.max(0, win.lines.length - visibleLines);
  const rate = Math.max(14, (targetOffset - win.offset) * 0.6);
  win.offset = Math.min(targetOffset, win.offset + dt * rate);
  if (targetOffset - win.offset > 0.5) win.lastAppendAt = now; // scrolling counts as activity

  const quiet = now - win.lastAppendAt;
  if (quiet > PEEK_HOLD_MS + PEEK_WINK_MS) {
    projectionPeekWindows.delete(key);
    return;
  }
  const openRamp = Math.min(1, (now - win.openedAt) / PEEK_OPEN_MS);
  const wink = quiet > PEEK_HOLD_MS ? 1 - (quiet - PEEK_HOLD_MS) / PEEK_WINK_MS : 1;
  const openness = Math.min(openRamp, Math.max(0, wink));

  // keep the box on screen: prefer above-right of the node, flip when clipped
  let x = point.x + 14 * devicePixelRatio;
  if (x + boxWidth > canvas.width - 4) x = point.x - boxWidth - 14 * devicePixelRatio;
  let y = point.y - boxHeight - 10 * devicePixelRatio;
  if (y < 4) y = point.y + 14 * devicePixelRatio;
  // the narration column owns the top-left: diff peeks dodge it (they share
  // its styling, so a collision reads as narration corruption)
  const narration = projectionNarrationBounds;
  if (narration && x < narration.right && y < narration.bottom && y + boxHeight > narration.top) {
    x = Math.min(narration.right + 8 * devicePixelRatio, canvas.width - boxWidth - 4);
  }

  ctx.save();
  // vertical openness gives the pop/wink; width stays (CRT collapse)
  const visibleHeight = Math.max(1.5 * devicePixelRatio, boxHeight * openness);
  const boxY = y + (boxHeight - visibleHeight) / 2;
  ctx.fillStyle = "rgba(2,6,10,0.9)";
  ctx.strokeStyle = projectionTone(theme, win.tone || "accent", 0.85);
  ctx.lineWidth = devicePixelRatio;
  ctx.beginPath();
  ctx.rect(x, boxY, boxWidth, visibleHeight);
  ctx.fill();
  ctx.stroke();
  if (openness > 0.55) {
    ctx.beginPath();
    // clip to the content area (not the border) so tail-offset lines never
    // bleed half-glyphs into the top padding
    ctx.rect(x, boxY + 4 * devicePixelRatio, boxWidth,
             Math.max(1, visibleHeight - 7 * devicePixelRatio));
    ctx.clip();
    ctx.globalAlpha *= Math.min(1, (openness - 0.55) / 0.35);
    ctx.font = `${8.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const baseY = boxY + 5 * devicePixelRatio - win.offset * lineHeight;
    let drew = 0;
    for (let i = 0; i < win.lines.length; i++) {
      const lineY = baseY + i * lineHeight;
      if (lineY < boxY - lineHeight || lineY > boxY + visibleHeight) continue;
      const line = win.lines[i];
      const tone = line.startsWith("+") ? "good" : line.startsWith("-") ? "alarm" : "ghost";
      ctx.fillStyle = projectionTone(theme, tone, 0.95);
      ctx.fillText(line.slice(0, 46), x + 5 * devicePixelRatio, lineY);
      drew += 1;
    }
    if (drew === 0) win.lastAppendAt = Math.min(win.lastAppendAt, now - PEEK_HOLD_MS);
  }
  ctx.restore();
}

const projectionNarrationStack = [];
let projectionNarrationBounds = null; // exclusion zone other widgets respect
const NARRATION_RETIRED_HOLD_MS = 2200; // a superseded message lingers briefly, then fades
const NARRATION_SCROLL_DELAY_MS = 2600; // read the head before the scroll begins
const NARRATION_WINK_MS = 280;
const NARRATION_VISIBLE_LINES = 12;
const NARRATION_RETIRED_LINES = 3; // retired messages collapse to their tail
const NARRATION_STACK = 3;
const NARRATION_WRAP_CHARS = 56;

function wrapNarration(text) {
  const lines = [];
  const cleaned = text.replace(/[*_`#]+/g, "");
  for (const paragraph of cleaned.split(/\\n+/)) {
    let current = "";
    for (const word of paragraph.split(/\\s+/)) {
      if (!word) continue;
      if (current && current.length + word.length + 1 > NARRATION_WRAP_CHARS) {
        lines.push(current);
        current = word;
      } else {
        current = current ? `${current} ${word}` : word;
      }
    }
    if (current) lines.push(current);
    lines.push(""); // paragraph break
  }
  while (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function drawProjectionNarration(hud, theme, rect, now) {
  // the agent's voice: a stack of terminal windows under the mast. The live
  // message streams on top (head-first, wrapped paragraphs, tail-follow,
  // blinking cursor); a new message pushes the previous one DOWN, where it
  // collapses to its tail and fades out on its own time.
  const text = String(hud.narration || "");
  const messageIndex = Number(hud.narrationMessageIndex || 0);
  let current = projectionNarrationStack[0];
  if (text && (!current || current.retiredAt)) {
    current = {text, messageIndex, lines: wrapNarration(text), offset: 0,
               openedAt: now, lastChangeAt: now, retiredAt: null};
    projectionNarrationStack.unshift(current);
  } else if (text && text !== current.text) {
    if (messageIndex === current.messageIndex) {
      // same message: it grew, or its spoken text superseded the monologue --
      // replace in place rather than churning the stack
      if (!text.startsWith(current.text)) current.offset = 0;
      current.text = text;
      current.lines = wrapNarration(text);
      current.lastChangeAt = now;
    } else {
      current.retiredAt = now;                   // a NEW message supersedes it
      projectionNarrationStack.unshift({
        text, messageIndex, lines: wrapNarration(text), offset: 0,
        openedAt: now, lastChangeAt: now, retiredAt: null,
      });
    }
  }
  while (projectionNarrationStack.length > NARRATION_STACK) projectionNarrationStack.pop();
  if (!projectionNarrationStack.length) return;

  const dt = Math.min(0.1, (now - (projectionNarrationStack.lastNow || now)) / 1000);
  projectionNarrationStack.lastNow = now;
  const lineHeight = 13 * devicePixelRatio;
  const boxWidth = 330 * devicePixelRatio;
  const x = rect.x + 6 * devicePixelRatio;
  let y = rect.y + 38 * devicePixelRatio;

  for (let index = projectionNarrationStack.length - 1; index >= 0; index--) {
    const entry = projectionNarrationStack[index];
    const retired = Boolean(entry.retiredAt);
    if (retired) {
      const age = now - entry.retiredAt;
      if (age > NARRATION_RETIRED_HOLD_MS + NARRATION_WINK_MS) {
        projectionNarrationStack.splice(index, 1);
        continue;
      }
    }
  }
  // draw newest at the top, older entries stacked beneath it
  for (const entry of projectionNarrationStack) {
    const retired = Boolean(entry.retiredAt);
    const visibleLines = retired
      ? Math.min(NARRATION_RETIRED_LINES, entry.lines.length)
      : Math.min(NARRATION_VISIBLE_LINES, entry.lines.length);
    const boxHeight = visibleLines * lineHeight + 14 * devicePixelRatio;
    const targetOffset = Math.max(0, entry.lines.length - visibleLines);
    if (retired) {
      entry.offset = targetOffset;               // retired entries show their tail
    } else {
      // an initial pause lets the head be read before the scroll begins;
      // then reading pace
      if (now - entry.openedAt > NARRATION_SCROLL_DELAY_MS) {
        entry.offset = Math.min(targetOffset, entry.offset + dt * 3.2);
      }
    }
    // the narrative is the storyline: a live message is PERMANENT until the
    // next one replaces it; only retired entries fade (briefly), so nothing
    // can wink out early and be "resurrected" by its own retirement
    const quiet = retired ? now - entry.retiredAt : 0;
    const openRamp = Math.min(1, (now - entry.openedAt) / 240);
    const wink = retired && quiet > NARRATION_RETIRED_HOLD_MS
      ? Math.max(0, 1 - (quiet - NARRATION_RETIRED_HOLD_MS) / NARRATION_WINK_MS)
      : 1;
    const openness = retired && quiet > NARRATION_RETIRED_HOLD_MS + NARRATION_WINK_MS
      ? 0
      : Math.min(openRamp, wink);
    const dim = retired ? 0.55 : 1;

    // every slot height is EASED, never snapped: whatever state transition
    // happens (retire, collapse, re-open after a quiet gap), stacked boxes
    // move continuously and can never paint over each other
    const targetHeight = boxHeight * openness;
    entry.slotH = entry.slotH === undefined
      ? targetHeight
      : entry.slotH + (targetHeight - entry.slotH) * Math.min(1, dt * 9);
    const visibleHeight = entry.slotH;
    if (visibleHeight < 1.5 * devicePixelRatio) {
      continue; // fully collapsed: takes no space, draws nothing
    }
    // top-anchored: the box opens/collapses at its bottom edge, so stacked
    // neighbors below never get overlapped mid-animation
    const boxY = y;
    ctx.save();
    ctx.globalAlpha *= dim;
    ctx.fillStyle = "rgba(2,6,10,0.82)";
    ctx.strokeStyle = projectionTone(theme, "good", retired ? 0.3 : 0.55);
    ctx.lineWidth = devicePixelRatio;
    ctx.beginPath();
    ctx.rect(x, boxY, boxWidth, visibleHeight);
    ctx.fill();
    ctx.stroke();
    if (openness > 0.55) {
      ctx.beginPath();
      // clip to the CONTENT area, not the border: a tail-offset line above
      // the window must not bleed its bottom half into the top padding
      // (which reads as the box above overlapping this one)
      ctx.rect(x, boxY + 5 * devicePixelRatio, boxWidth,
               Math.max(1, visibleHeight - 9 * devicePixelRatio));
      ctx.clip();
      ctx.globalAlpha *= Math.min(1, (openness - 0.55) / 0.35);
      ctx.font = `${10 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      const baseY = boxY + 7 * devicePixelRatio - entry.offset * lineHeight;
      for (let i = 0; i < entry.lines.length; i++) {
        const lineY = baseY + i * lineHeight;
        if (lineY < boxY - lineHeight || lineY > boxY + visibleHeight) continue;
        ctx.fillStyle = projectionTone(theme, "good", 0.85);
        ctx.fillText(entry.lines[i], x + 6 * devicePixelRatio, lineY);
      }
      // streaming cursor on the live message only
      if (!retired && hud.narrationComplete === false) {
        const cursorOn = Math.floor(now / 420) % 2 === 0;
        if (cursorOn) {
          ctx.fillStyle = projectionTone(theme, "good", 0.9);
          const lastIndex = entry.lines.length - 1;
          const lastY = baseY + lastIndex * lineHeight;
          const width = ctx.measureText(entry.lines[lastIndex] || "").width;
          ctx.fillRect(x + 8 * devicePixelRatio + width, lastY, 5 * devicePixelRatio, lineHeight * 0.8);
        }
      }
    }
    ctx.restore();
    y += visibleHeight + 6 * devicePixelRatio;
  }
  projectionNarrationBounds = y > rect.y + 40 * devicePixelRatio
    ? {x: x, right: x + boxWidth, top: rect.y + 38 * devicePixelRatio, bottom: y}
    : null;
}

const PROJECTION_TICKER_GLYPHS = {
  file_changed: "\\u0394",
  command_completed: "$",
  check_completed: "\\u2713",
  commit_created: "\\u25C6",
};

function drawProjectionHud(props, theme, mood, hud, rect, w, h, now) {
  // top of the stage belongs to the page mast and status chip (the chip
  // already shows the mood text); the canvas HUD draws only the bottom strip
  ctx.textBaseline = "top";
  const hudTop = h * 0.685;
  ctx.font = `${10.5 * devicePixelRatio}px ui-monospace, monospace`;
  ctx.textAlign = "left";
  ctx.fillStyle = `rgba(${theme.ink},0.72)`;
  const focusLine = hud.focus ? `FOCUS ${hud.focus}` : "FOCUS \\u2014";
  const commandLine = hud.command ? `$ ${hud.command} [${hud.commandStatus || "?"}]` : "";
  const workspaceLine = [String(props.title || ""), String(hud.workspace || "")]
    .filter(Boolean)
    .join(" // ");
  ctx.fillText(focusLine, rect.x, hudTop);
  if (commandLine) ctx.fillText(commandLine, rect.x, hudTop + 14 * devicePixelRatio);
  ctx.fillText(workspaceLine, rect.x, hudTop + 28 * devicePixelRatio);
  drawProjectionNarration(hud, theme, rect, now);
  ctx.textAlign = "right";
  ctx.fillStyle = projectionTone(theme, mood.alert ? "alarm" : "good", 0.85);
  ctx.fillText(String(hud.checks || ""), rect.x + rect.width, hudTop);
  const ticker = Array.isArray(hud.ticker) ? hud.ticker : [];
  if (ticker.length) {
    ctx.fillStyle = `rgba(${theme.ink},0.6)`;
    const glyphs = ticker.map((item) => PROJECTION_TICKER_GLYPHS[item.kind] || "\\u00B7").join(" ");
    ctx.fillText(glyphs, rect.x + rect.width, hudTop + 28 * devicePixelRatio);
  }
}

function traceRouteHops(props, w, h) {
  const rawHops = Array.isArray(props.hops) ? props.hops : [];
  return rawHops.map((hop, index) => {
    const safeHop = hop && typeof hop === "object" ? hop : {};
    const point = normalizedPoint({
      x: safeHop.x ?? (0.14 + index * 0.14),
      y: safeHop.y ?? (0.35 + Math.sin(index * 1.3) * 0.18),
    }, w, h);
    return {
      ...safeHop,
      id: String(safeHop.id || `hop-${index}`),
      label: safeHop.label || safeHop.id || `hop-${index}`,
      tone: safeHop.tone || props.tone || "cyan",
      point,
    };
  });
}

function traceRouteLinks(props, hops) {
  const hopById = new Map(hops.map((hop) => [hop.id, hop]));
  const rawLinks = Array.isArray(props.links) && props.links.length
    ? props.links
    : hops.slice(1).map((hop, index) => ({source: hops[index].id, target: hop.id}));
  return rawLinks
    .map((link, index) => {
      const safeLink = link && typeof link === "object" ? link : {};
      const source = hopById.get(String(safeLink.source ?? safeLink.from ?? ""));
      const target = hopById.get(String(safeLink.target ?? safeLink.to ?? ""));
      if (!source || !target) return null;
      return {
        ...safeLink,
        index,
        source,
        target,
        tone: safeLink.tone || target.tone || props.tone || "cyan",
        curve: finiteNumber(safeLink.curve, index % 2 === 0 ? 0.12 : -0.10),
      };
    })
    .filter(Boolean);
}

function traceRoutePoint(link, progress) {
  const a = link.source.point;
  const b = link.target.point;
  const mid = {x: (a.x + b.x) * 0.5, y: (a.y + b.y) * 0.5};
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.max(1, Math.hypot(dx, dy));
  const control = {
    x: mid.x - (dy / length) * length * link.curve,
    y: mid.y + (dx / length) * length * link.curve,
  };
  const t = clamp(progress, 0, 1);
  const oneMinus = 1 - t;
  return {
    x: oneMinus * oneMinus * a.x + 2 * oneMinus * t * control.x + t * t * b.x,
    y: oneMinus * oneMinus * a.y + 2 * oneMinus * t * control.y + t * t * b.y,
    control,
  };
}

function drawTraceRouteLink(link, props, now) {
  const a = link.source.point;
  const b = link.target.point;
  const mid = traceRoutePoint(link, 0.5);
  const tone = link.tone || props.tone || "cyan";
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = toneColor(tone, 0.64);
  ctx.shadowBlur = 12 * devicePixelRatio;
  ctx.strokeStyle = toneColor(tone, 0.18);
  ctx.lineWidth = Math.max(5 * devicePixelRatio, finiteNumber(props.width, 4) * devicePixelRatio);
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.quadraticCurveTo(mid.control.x, mid.control.y, b.x, b.y);
  ctx.stroke();
  ctx.setLineDash([14 * devicePixelRatio, 13 * devicePixelRatio]);
  ctx.lineDashOffset = -now * 0.028 * Math.max(0.1, finiteNumber(props.speed, 0.65));
  ctx.strokeStyle = toneColor("white", 0.58);
  ctx.lineWidth = Math.max(1.2 * devicePixelRatio, finiteNumber(props.width, 2) * devicePixelRatio);
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.quadraticCurveTo(mid.control.x, mid.control.y, b.x, b.y);
  ctx.stroke();
  ctx.restore();
  if (link.label) {
    ctx.save();
    ctx.font = `${10 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", 0.62);
    ctx.fillText(String(link.label).slice(0, 14), mid.x, mid.y - 10 * devicePixelRatio);
    ctx.restore();
  }
}

function drawTraceRoute(primitive, w, h, now) {
  const props = primitive.props || {};
  const hops = traceRouteHops(props, w, h);
  const links = traceRouteLinks(props, hops);
  if (!hops.length) return;
  const tone = props.tone || "cyan";
  const accentTone = props.accentTone || props.accent || "magenta";
  const focusHopId = String(props.focusHopId || props.focus || hops[hops.length - 1].id);
  const packetCount = Math.max(0, Math.min(96, Math.floor(finiteNumber(props.packets, links.length * 4 || 4))));
  const speed = Math.max(0, finiteNumber(props.speed, 0.65));
  const seed = finiteNumber(props.seed, 0);
  if (typeof window !== "undefined") {
    window.__gibsonTraceRouteState = window.__gibsonTraceRouteState || {};
    window.__gibsonTraceRouteState[primitive.id] = {
      hopCount: hops.length,
      linkCount: links.length,
      packetCount,
      focusHopId,
      tone,
      accentTone,
      hasLabels: hops.some((hop) => Boolean(hop.label)) || links.some((link) => Boolean(link.label)),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  for (const link of links) drawTraceRouteLink(link, props, now);
  for (let index = 0; index < packetCount && links.length; index++) {
    const link = links[index % links.length];
    const progress = (now * speed * 0.00018 + index / Math.max(1, packetCount) + seed * 0.013) % 1;
    const point = traceRoutePoint(link, progress);
    const packetTone = index % 5 === 0 ? accentTone : link.tone || tone;
    const radius = (2.2 + seededUnit(seed + index * 4.7) * 2.4) * devicePixelRatio;
    ctx.shadowColor = toneColor(packetTone, 0.82);
    ctx.shadowBlur = 12 * devicePixelRatio;
    ctx.fillStyle = toneColor(packetTone, 0.72);
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = toneColor("white", 0.58);
    ctx.lineWidth = 0.7 * devicePixelRatio;
    ctx.stroke();
  }
  for (const hop of hops) {
    const focus = hop.id === focusHopId;
    const radius = (focus ? 18 : 12) * devicePixelRatio;
    const hopTone = focus ? accentTone : hop.tone || tone;
    const pulse = 1 + Math.sin(now * 0.004 + seed + hops.indexOf(hop)) * 0.12;
    ctx.shadowColor = toneColor(hopTone, focus ? 0.9 : 0.54);
    ctx.shadowBlur = (focus ? 23 : 13) * devicePixelRatio;
    ctx.fillStyle = toneColor(hopTone, focus ? 0.46 : 0.28);
    ctx.strokeStyle = toneColor("white", focus ? 0.78 : 0.48);
    ctx.lineWidth = Math.max(1, 1.2 * devicePixelRatio);
    ctx.beginPath();
    ctx.arc(hop.point.x, hop.point.y, radius * pulse, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(hop.point.x, hop.point.y, radius * (0.46 + (focus ? 0.08 : 0)), 0, Math.PI * 2);
    ctx.fillStyle = toneColor("white", focus ? 0.58 : 0.34);
    ctx.fill();
    if (hop.label) {
      ctx.shadowBlur = 4 * devicePixelRatio;
      ctx.font = `${11 * devicePixelRatio}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = toneColor("white", focus ? 0.88 : 0.68);
      ctx.fillText(String(hop.label).slice(0, 14), hop.point.x, hop.point.y + radius + 14 * devicePixelRatio);
    }
  }
  if (props.label) {
    const first = hops[0].point;
    ctx.shadowBlur = 7 * devicePixelRatio;
    ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor(accentTone, 0.86);
    ctx.fillText(String(props.label).slice(0, 24), first.x, first.y - 28 * devicePixelRatio);
  }
  ctx.restore();
}

function drawRibbon(primitive, w, h, now) {
  const props = primitive.props || {};
  const points = Array.isArray(props.points) ? props.points.map((point) => normalizedPoint(point, w, h)) : [];
  if (points.length < 2) return;
  const tone = props.material || "cyan";
  const dashOffset = -(now / 28) % (34 * devicePixelRatio);
  ctx.save();
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, 0.8);
  ctx.shadowBlur = 18 * devicePixelRatio;
  ctx.strokeStyle = toneColor(tone, 0.32);
  ctx.lineWidth = Math.max(8 * devicePixelRatio, Number(props.width || 3) * 4 * devicePixelRatio);
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) ctx.lineTo(point.x, point.y);
  ctx.stroke();
  ctx.setLineDash([18 * devicePixelRatio, 16 * devicePixelRatio]);
  ctx.lineDashOffset = dashOffset;
  ctx.strokeStyle = toneColor("white", 0.76);
  ctx.lineWidth = Math.max(2 * devicePixelRatio, Number(props.width || 3) * devicePixelRatio);
  ctx.stroke();
  ctx.restore();
}

function drawGlyphLayer(primitive, w, h, now) {
  const props = primitive.props || {};
  const text = String(props.text || "GIBSON");
  if (!text) return;
  const density = Math.max(0.1, Math.min(1, Number(props.density || 0.5)));
  const tone = props.palette || "green";
  const columns = Math.max(8, Math.floor(18 * density));
  const rows = Math.max(5, Math.floor(10 * density));
  const drift = Math.floor(now / 120 + Number(props.seed || 0));
  ctx.save();
  ctx.font = `${12 * devicePixelRatio}px ui-monospace, monospace`;
  ctx.textBaseline = "top";
  ctx.shadowColor = toneColor(tone, 0.58);
  ctx.shadowBlur = 8 * devicePixelRatio;
  for (let col = 0; col < columns; col++) {
    for (let row = 0; row < rows; row++) {
      const index = (col * 7 + row * 11 + drift) % text.length;
      const x = (0.06 + (col / columns) * 0.88) * w;
      const y = (0.08 + (row / rows) * 0.78 + ((col + drift) % 5) * 0.008) * h;
      const alpha = 0.18 + (((col + row + drift) % 7) / 7) * 0.42;
      ctx.fillStyle = toneColor(tone, alpha);
      ctx.fillText(text[index], x, y);
    }
  }
  ctx.restore();
}

function terminalWallRect(props, w, h) {
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const position = props.position && typeof props.position === "object" ? props.position : {x: 0.5, y: 0.5};
  const width = clamp(finiteNumber(size.w ?? size.width ?? props.width, 0.72), 0.08, 1.4) * w;
  const height = clamp(finiteNumber(size.h ?? size.height ?? props.height, 0.32), 0.06, 1.2) * h;
  const x = finiteNumber(position.x, 0.5) * w - width * 0.5;
  const y = finiteNumber(position.y, 0.5) * h - height * 0.5;
  return {x, y, width, height};
}

function terminalWallPanelLines(panel) {
  const rawLines = Array.isArray(panel.lines)
    ? panel.lines
    : String(panel.text || panel.content || "").split(/\\r?\\n/);
  return rawLines
    .filter((line) => line !== null && line !== undefined)
    .slice(0, 48)
    .map((line) => String(line).replace(/\\s+/g, " ").trim())
    .filter((line) => line.length > 0);
}

function terminalWallPanels(props) {
  const rawPanels = Array.isArray(props.panels) && props.panels.length ? props.panels : [
    {
      id: "terminal-0",
      title: props.title || "TERMINAL",
      lines: Array.isArray(props.lines) ? props.lines : [props.text || "AWAITING HARN STREAM"],
      active: true,
    },
  ];
  return rawPanels
    .filter((panel) => panel && typeof panel === "object")
    .slice(0, 12)
    .map((panel, index) => {
      const id = String(panel.id || `panel-${index}`);
      const lines = terminalWallPanelLines(panel);
      return {
        ...panel,
        id,
        title: String(panel.title || panel.label || id).slice(0, 28),
        lines: lines.length ? lines : ["NO SIGNAL"],
      };
    });
}

function drawTerminalWall(primitive, w, h, now) {
  const props = primitive.props || {};
  const panels = terminalWallPanels(props);
  if (!panels.length) return;
  const rect = terminalWallRect(props, w, h);
  const defaultColumns = Math.ceil(Math.sqrt(panels.length));
  const columns = Math.max(1, Math.min(4, Math.floor(finiteNumber(props.columns, defaultColumns))));
  const defaultRows = Math.ceil(panels.length / columns);
  const rows = Math.max(1, Math.min(4, Math.floor(finiteNumber(props.rows, defaultRows))));
  const tone = props.tone || "green";
  const accentTone = props.accentTone || props.accent || "cyan";
  const opacity = clamp(finiteNumber(props.opacity, 0.74), 0, 1);
  const hasScan = props.scan !== false;
  const hasCursor = props.cursor !== false;
  const speed = Math.max(0, finiteNumber(props.speed, 0.72));
  const seed = finiteNumber(props.seed, 0);
  const gap = 8 * devicePixelRatio;
  const panelWidth = Math.max(18 * devicePixelRatio, (rect.width - gap * (columns - 1)) / columns);
  const panelHeight = Math.max(20 * devicePixelRatio, (rect.height - gap * (rows - 1)) / rows);
  const fontSize = Math.max(7 * devicePixelRatio, finiteNumber(props.fontSize, 10) * devicePixelRatio);
  const lineHeight = fontSize * 1.25;
  const headerHeight = Math.max(16 * devicePixelRatio, lineHeight * 1.7);
  const panelMetrics = panels.map((panel, index) => {
    const row = Math.floor(index / columns);
    if (row >= rows) {
      return {visibleLines: 0, renderedLines: 0, scroll: 0};
    }
    const visibleLines = Math.max(
      1,
      Math.floor((panelHeight - headerHeight - 7 * devicePixelRatio) / lineHeight)
    );
    const scroll = panel.lines.length > visibleLines
      ? Math.floor(now * speed * 0.0022 + seed + index * 3.1) % panel.lines.length
      : 0;
    return {
      visibleLines,
      renderedLines: Math.min(visibleLines, panel.lines.length),
      scroll,
    };
  });
  const streamingCount = panels.filter((panel) => panel.streaming || panel.active).length;
  const activePanel = panels.find((panel) => panel.active || panel.streaming) || panels[0];
  const lineCount = panels.reduce((total, panel) => total + panel.lines.length, 0);
  const renderedLineCount = panelMetrics.reduce((total, metrics) => total + metrics.renderedLines, 0);

  if (typeof window !== "undefined") {
    window.__gibsonTerminalWallState = window.__gibsonTerminalWallState || {};
    window.__gibsonTerminalWallState[primitive.id] = {
      panelCount: panels.length,
      lineCount,
      renderedLineCount,
      columnCount: columns,
      rowCount: rows,
      activePanelId: activePanel.id,
      streamingCount,
      tone,
      accentTone,
      hasScan,
      hasCursor,
      panelLineCounts: panels.map((panel) => panel.lines.length),
      panelRenderedLineCounts: panelMetrics.map((metrics) => metrics.renderedLines),
    };
  }

  ctx.save();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = toneColor(tone, 0.58 * opacity);
  ctx.shadowBlur = 16 * devicePixelRatio;
  ctx.fillStyle = toneColor("white", 0.025 * opacity);
  ctx.strokeStyle = toneColor(tone, 0.28 * opacity);
  ctx.lineWidth = Math.max(1, 1.1 * devicePixelRatio);
  ctx.strokeRect(rect.x, rect.y, rect.width, rect.height);
  if (props.title) {
    ctx.font = `${10.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = toneColor("white", 0.68 * opacity);
    ctx.fillText(String(props.title).slice(0, 42), rect.x + 7 * devicePixelRatio, rect.y - 4 * devicePixelRatio);
  }

  for (const [index, panel] of panels.entries()) {
    const col = index % columns;
    const row = Math.floor(index / columns);
    if (row >= rows) continue;
    const x = rect.x + col * (panelWidth + gap);
    const y = rect.y + row * (panelHeight + gap);
    const panelTone = panel.tone || (panel.active || panel.streaming ? accentTone : tone);
    const panelAccent = panel.accentTone || accentTone;
    const activity = panel.active || panel.streaming ? 1 : 0.36 + seededUnit(seed + index * 13.7) * 0.26;
    const panelOpacity = opacity * clamp(finiteNumber(panel.opacity, 0.82), 0, 1);
    const bodyTop = y + headerHeight;
    const {visibleLines, renderedLines, scroll} = panelMetrics[index];

    ctx.fillStyle = toneColor(panelTone, 0.035 * panelOpacity + activity * 0.018);
    ctx.fillRect(x, y, panelWidth, panelHeight);
    ctx.strokeStyle = toneColor(panelTone, (0.25 + activity * 0.28) * panelOpacity);
    ctx.lineWidth = Math.max(1, (0.8 + activity * 0.6) * devicePixelRatio);
    ctx.strokeRect(x, y, panelWidth, panelHeight);

    const headerGradient = ctx.createLinearGradient(x, y, x + panelWidth, y);
    headerGradient.addColorStop(0, toneColor(panelTone, 0.14 * panelOpacity));
    headerGradient.addColorStop(0.55, toneColor(panelAccent, 0.08 * panelOpacity));
    headerGradient.addColorStop(1, toneColor(panelTone, 0.02));
    ctx.fillStyle = headerGradient;
    ctx.fillRect(x, y, panelWidth, headerHeight);

    ctx.font = `${8.5 * devicePixelRatio}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = toneColor("white", (0.54 + activity * 0.24) * panelOpacity);
    ctx.fillText(panel.title, x + 7 * devicePixelRatio, y + headerHeight * 0.52);

    const meterWidth = panelWidth * (0.18 + 0.72 * seededUnit(seed + index * 5.2 + Math.floor(now / 260)));
    ctx.fillStyle = toneColor(panelAccent, (0.20 + activity * 0.22) * panelOpacity);
    ctx.fillRect(
      x + panelWidth - meterWidth - 6 * devicePixelRatio,
      y + headerHeight - 4 * devicePixelRatio,
      meterWidth,
      1.6 * devicePixelRatio
    );

    ctx.save();
    ctx.beginPath();
    ctx.rect(x + 5 * devicePixelRatio, bodyTop, panelWidth - 10 * devicePixelRatio, panelHeight - headerHeight);
    ctx.clip();
    ctx.font = `${fontSize}px ui-monospace, monospace`;
    ctx.textBaseline = "top";
    for (let lineIndex = 0; lineIndex < renderedLines; lineIndex++) {
      const sourceIndex = panel.lines.length > visibleLines
        ? (scroll + lineIndex) % panel.lines.length
        : lineIndex;
      const text = panel.lines[sourceIndex];
      const yLine = bodyTop + 4 * devicePixelRatio + lineIndex * lineHeight;
      const isHot = sourceIndex === panel.lines.length - 1 && (panel.streaming || panel.active);
      const prefix = isHot ? ">" : (lineIndex % 3 === 0 ? "$" : " ");
      const maxChars = Math.max(8, Math.floor((panelWidth - 16 * devicePixelRatio) / (fontSize * 0.62)));
      const displayText = `${prefix} ${text}`.slice(0, maxChars);
      ctx.fillStyle = toneColor(isHot ? panelAccent : panelTone, (isHot ? 0.84 : 0.42) * panelOpacity);
      ctx.fillText(displayText, x + 7 * devicePixelRatio, yLine);
    }
    ctx.restore();

    if (hasScan) {
      const scanY = bodyTop + ((now * speed * 0.026 + index * 19 + seed) % Math.max(1, panelHeight - headerHeight));
      const scanGradient = ctx.createLinearGradient(x, scanY, x + panelWidth, scanY);
      scanGradient.addColorStop(0, toneColor(panelAccent, 0));
      scanGradient.addColorStop(0.5, toneColor(panelAccent, 0.20 * panelOpacity));
      scanGradient.addColorStop(1, toneColor(panelAccent, 0));
      ctx.fillStyle = scanGradient;
      ctx.fillRect(x + 4 * devicePixelRatio, scanY, panelWidth - 8 * devicePixelRatio, 1.4 * devicePixelRatio);
    }

    if (hasCursor && (panel.active || panel.streaming) && Math.floor(now / 320 + index) % 2 === 0) {
      ctx.fillStyle = toneColor("white", 0.76 * panelOpacity);
      ctx.fillRect(
        x + panelWidth - 12 * devicePixelRatio,
        y + headerHeight * 0.36,
        6 * devicePixelRatio,
        8 * devicePixelRatio,
      );
    }
  }
  ctx.restore();
}

function seededUnit(seed) {
  const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function finiteNumber(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function dataRainRect(props, w, h) {
  const size = props.size && typeof props.size === "object" ? props.size : {};
  const hasPosition = props.position && typeof props.position === "object";
  const rawPosition = hasPosition ? props.position : {x: 0.5, y: 0.5};
  const position = {
    x: finiteNumber(rawPosition.x, 0.5) * w,
    y: finiteNumber(rawPosition.y, 0.5) * h,
  };
  const widthRatio = clamp(finiteNumber(size.w ?? size.width ?? props.width, 1), 0.05, 1.8);
  const heightRatio = clamp(finiteNumber(size.h ?? size.height ?? props.height, 1), 0.05, 1.8);
  const width = widthRatio * w;
  const height = heightRatio * h;
  return {
    x: position.x - width * 0.5,
    y: position.y - height * 0.5,
    width,
    height,
  };
}

function drawDataRain(primitive, w, h, now) {
  const props = primitive.props || {};
  const glyphs = String(props.glyphs || "01ABCDEF#$%&*+-/<>[]{}");
  if (!glyphs) return;
  const columns = Math.max(4, Math.min(96, Math.floor(finiteNumber(props.columns, 34))));
  const density = clamp(finiteNumber(props.density, 0.68), 0.05, 1);
  const speed = Math.max(0, finiteNumber(props.speed, 0.55));
  const tone = props.tone || props.palette || "green";
  const accentTone = props.accentTone || props.accent || "white";
  const direction = props.direction === "up" ? "up" : "down";
  const opacity = clamp(finiteNumber(props.opacity, 0.74), 0, 1);
  const trail = Math.max(3, Math.min(32, Math.floor(finiteNumber(props.trail, 9 + density * 13))));
  const bands = Math.max(0, Math.min(8, Math.floor(finiteNumber(props.bands, 0))));
  const glitchAmount = props.glitch === true ? 0.35 : clamp(finiteNumber(props.glitch, 0), 0, 1);
  const seed = finiteNumber(props.seed, 0);
  const rect = dataRainRect(props, w, h);
  const fontSize = Math.max(8 * devicePixelRatio, finiteNumber(props.fontSize, 13) * devicePixelRatio);
  const rowHeight = fontSize * 1.16;
  const rows = Math.max(6, Math.ceil(rect.height / rowHeight) + trail);
  const columnWidth = rect.width / columns;
  let visibleColumns = 0;

  if (typeof window !== "undefined") {
    window.__gibsonDataRainState = window.__gibsonDataRainState || {};
    window.__gibsonDataRainState[primitive.id] = {
      columns,
      direction,
      density: Number(density.toFixed(3)),
      glyphCount: glyphs.length,
      bandCount: bands,
      hasGlitch: glitchAmount > 0,
      tone,
      accentTone,
    };
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(rect.x, rect.y, rect.width, rect.height);
  ctx.clip();
  ctx.globalCompositeOperation = props.blend === "source-over" ? "source-over" : "screen";
  ctx.font = `${fontSize}px ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.shadowBlur = 9 * devicePixelRatio;
  ctx.shadowColor = toneColor(tone, 0.72);

  for (let col = 0; col < columns; col++) {
    const columnSeed = seed + col * 17.31;
    if (seededUnit(columnSeed) > density) continue;
    visibleColumns += 1;
    const phase = (now * speed * 0.00018 + seededUnit(columnSeed + 4.2) + col * 0.013) % 1;
    const head = phase * rows;
    const x = rect.x + (col + 0.5 + (seededUnit(columnSeed + 2.6) - 0.5) * 0.32) * columnWidth;
    const columnAlpha = 0.62 + seededUnit(columnSeed + 9.7) * 0.38;
    for (let drop = 0; drop < trail; drop++) {
      const row = Math.floor(head - drop);
      const wrappedRow = ((row % rows) + rows) % rows;
      const y = direction === "up"
        ? rect.y + rect.height - wrappedRow * rowHeight
        : rect.y + wrappedRow * rowHeight - trail * rowHeight;
      if (y < rect.y - rowHeight || y > rect.y + rect.height + rowHeight) continue;
      const fade = 1 - drop / trail;
      const alpha = opacity * columnAlpha * Math.pow(fade, 1.45);
      const tick = Math.floor(now / Math.max(1, finiteNumber(props.shuffleMs, 95)));
      const glyphIndex = Math.abs(Math.floor(columnSeed * 13 + wrappedRow * 7 + drop * 19 + tick)) % glyphs.length;
      const glyph = glyphs[glyphIndex];
      const headGlyph = drop === 0;
      ctx.shadowColor = toneColor(headGlyph ? accentTone : tone, headGlyph ? 0.88 : 0.54);
      ctx.fillStyle = toneColor(headGlyph ? accentTone : tone, headGlyph ? Math.min(1, alpha + 0.18) : alpha);
      ctx.fillText(glyph, x, y);
    }
  }

  if (bands > 0) {
    const bandHeight = Math.max(2 * devicePixelRatio, rowHeight * 0.45);
    for (let band = 0; band < bands; band++) {
      const progress = (now * speed * 0.00009 + band / bands + seed * 0.0017) % 1;
      const y = rect.y + progress * rect.height;
      const gradient = ctx.createLinearGradient(rect.x, y, rect.x + rect.width, y);
      gradient.addColorStop(0, toneColor(accentTone, 0));
      gradient.addColorStop(0.5, toneColor(accentTone, opacity * 0.26));
      gradient.addColorStop(1, toneColor(accentTone, 0));
      ctx.fillStyle = gradient;
      ctx.fillRect(rect.x, y, rect.width, bandHeight);
    }
  }

  if (glitchAmount > 0) {
    ctx.lineWidth = Math.max(1, 1.1 * devicePixelRatio);
    for (let index = 0; index < Math.ceil(10 * glitchAmount); index++) {
      const jitter = seededUnit(seed + index * 11 + Math.floor(now / 120));
      const y = rect.y + jitter * rect.height;
      const length = rect.width * (0.12 + seededUnit(seed + index * 7.3) * 0.28);
      const x = rect.x + seededUnit(seed + index * 19.9) * Math.max(1, rect.width - length);
      ctx.strokeStyle = toneColor(index % 2 ? "magenta" : accentTone, opacity * 0.36);
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + length, y + (seededUnit(seed + index) - 0.5) * 6 * devicePixelRatio);
      ctx.stroke();
    }
  }

  if (typeof window !== "undefined" && window.__gibsonDataRainState?.[primitive.id]) {
    window.__gibsonDataRainState[primitive.id].visibleColumns = visibleColumns;
  }
  ctx.restore();
}

function drawParticleField(primitive, w, h, now) {
  const props = primitive.props || {};
  const emitters = particleFieldEmitters(props, w, h);
  if (!emitters.length) return;
  const count = Math.max(0, Math.min(160, Number(props.count || emitters.length * 24)));
  const velocity = Number(props.velocity || 0.25);
  ctx.save();
  ctx.globalCompositeOperation = props.blend === "screen" ? "screen" : "source-over";
  ctx.lineCap = "round";
  const perEmitter = Math.max(1, Math.ceil(count / emitters.length));
  for (const emitter of emitters) {
    const emitterCount = Math.max(0, Math.min(perEmitter, Number(emitter.config.count || perEmitter)));
    const tone = emitter.config.color || emitter.config.tone || props.color || "cyan";
    const seed = Number(emitter.config.seed ?? props.seed ?? 0);
    const spread = Number(emitter.config.spread || 0.37);
    const angleBase = Number(emitter.config.angle || -0.82);
    for (let index = 0; index < emitterCount; index++) {
      const phase = ((now * velocity * 0.00025) + index * 0.071 + seed * 0.013) % 1;
      const angle = angleBase + index * spread + emitter.index * 0.19;
      const distance = phase * Math.max(w, h) * 0.62;
      const x = emitter.point.x + Math.cos(angle) * distance;
      const y = emitter.point.y + Math.sin(angle) * distance * 0.62;
      const alpha = Math.max(0, 1 - phase);
      ctx.strokeStyle = toneColor(tone, alpha * 0.34);
      ctx.lineWidth = 1.4 * devicePixelRatio;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - Math.cos(angle) * 22 * devicePixelRatio, y - Math.sin(angle) * 14 * devicePixelRatio);
      ctx.stroke();
      ctx.fillStyle = toneColor("white", alpha * 0.74);
      ctx.beginPath();
      ctx.arc(x, y, 2.2 * devicePixelRatio, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.restore();
}

function particleFieldEmitters(props, w, h) {
  const raw = Array.isArray(props.emitters) && props.emitters.length
    ? props.emitters
    : [props.emitter || {x: 0.5, y: 0.5}];
  return raw.slice(0, 12).map((value, index) => {
    const config = value && typeof value === "object" ? value : {};
    const point = normalizedPoint(config.position || config, w, h);
    return {config, index, point};
  });
}

function pushEvent(event) {
  const update = event.event ? event : {event, scene: null, mutations: []};
  const current = update.event;
  const scene = update.scene;
  statusEl.textContent = "linked";
  phaseEl.textContent = current.phase || "unknown";
  eventEl.textContent = current.eventType || "unknown";
  sequenceEl.textContent = String(current.sequence || 0);
  signalTitle.textContent = current.title || current.eventType || "SIGNAL";
  signalSummary.textContent = current.summary || `${current.phase || "event"}:${current.eventType || "unknown"}`;
  if (scene) {
    renderScene(scene);
  } else {
    if (update.renderIntent) {
      intentLog.textContent = JSON.stringify([update.renderIntent], null, 2);
    }
    appendFeedItem(current);
  }
  // legacy ambient pulses: decorative circles at hash-derived positions for
  // each animation mutation. A projection scene owns the whole stage and
  // emits animations every step, so the legacy layer stands down for it.
  const projectionOwnsStage = Boolean(update.scene?.primitives?.["projection-scene"]);
  if (!projectionOwnsStage) {
    for (const mutation of update.mutations || []) {
      if (mutation.op === "start_animation" && mutation.animation) {
        const seed = Number(mutation.animation.props?.sequence || current.sequence || Date.now());
        pulses.push({
          x: ((seed * 37) % 100) / 100,
          y: ((seed * 71) % 100) / 100,
          age: 0,
          color: colorFor(mutation.animation.props?.phase || current.phase),
        });
      }
    }
  }
}

function appendFeedItem(event) {
  const item = document.createElement("li");
  item.className = event.phase || "lifecycle";
  item.innerHTML = `<b>${event.title || event.eventType}</b><span>${event.summary || ""}</span>`;
  feed.appendChild(item);
  while (feed.children.length > 80) feed.removeChild(feed.firstChild);
}

function renderScene(scene) {
  currentScene = scene;
  window.__gibsonScene = scene;
  applyStylePack(stylePackFromScene(scene));
  syncAnimationClocks(scene, performance.now());
  const status = scene.primitives?.status?.props || {};
  const stream = scene.primitives?.["assistant-stream"]?.props || {};
  if (status.text) statusEl.textContent = status.text;
  renderStream(stream);
  intentLog.textContent = JSON.stringify(scene.metadata?.renderIntents || [], null, 2);
  traceLog.textContent = JSON.stringify(scene.primitives?.["trace-log"]?.props?.text || [], null, 2);
  feed.replaceChildren();
  for (const entry of scene.log || []) {
    appendFeedItem({
      phase: entry.phase,
      eventType: entry.eventType,
      title: entry.title,
      summary: entry.summary,
    });
  }
  const latest = scene.log?.[scene.log.length - 1];
  if (latest) {
    signalTitle.textContent = latest.title || latest.eventType || "SIGNAL";
    signalSummary.textContent = latest.summary || `${latest.phase || "event"}:${latest.eventType || "unknown"}`;
  }
  markSceneDirty();
}

function stylePackFromScene(scene) {
  const stylePack = scene?.metadata?.stylePack || scene?.primitives?.stage?.props?.stylePack;
  if (!stylePack || typeof stylePack !== "object") return DEFAULT_STYLE_PACK;
  return {
    ...DEFAULT_STYLE_PACK,
    ...stylePack,
    tones: {...DEFAULT_STYLE_PACK.tones, ...(stylePack.tones || {})},
    canvas: {...DEFAULT_STYLE_PACK.canvas, ...(stylePack.canvas || {})},
    cssVars: stylePack.cssVars || {},
    motifs: Array.isArray(stylePack.motifs) ? stylePack.motifs : DEFAULT_STYLE_PACK.motifs,
  };
}

function applyStylePack(stylePack) {
  currentStylePack = stylePack;
  window.__gibsonStylePack = stylePack;
  document.body.dataset.style = stylePack.id || "gibson";
  const cssVars = stylePack.cssVars || {};
  for (const [name, value] of Object.entries(cssVars)) {
    if (name.startsWith("--")) document.documentElement.style.setProperty(name, String(value));
  }
}

function renderStream(stream) {
  if (!stream || !stream.text) {
    streamPanel.hidden = true;
    return;
  }
  streamPanel.hidden = false;
  streamTitle.textContent = stream.title || "STREAM";
  streamText.textContent = stream.text;
}

function updateBridgeStatus(bridge) {
  if (!bridge) return;
  bridgeStatus.classList.toggle("waiting", bridge.pendingInputs > 0 && !bridge.inputPollerConnected);
  if (bridge.inputPollerConnected) {
    bridgeStatus.textContent = "harn bridge linked";
  } else if (bridge.pendingInputs > 0) {
    bridgeStatus.textContent = "harn bridge waiting";
  } else {
    bridgeStatus.textContent = "harn bridge idle";
  }

  if (bridge.pendingInputs > 0 && !bridge.inputPollerConnected) {
    inputStatus.textContent = `${bridge.pendingInputs} input waiting for harn`;
  } else if (bridge.pendingInputs > 0) {
    inputStatus.textContent = `${bridge.pendingInputs} input queued`;
  } else if (lastQueuedInputId && bridge.deliveredInputs > 0) {
    inputStatus.textContent = `${lastQueuedInputId} delivered to harn`;
  }
}

async function refreshHealth() {
  try {
    const response = await fetch("/health", {cache: "no-store"});
    const payload = await response.json();
    updateBridgeStatus(payload.inputBridge);
    // the kicker is baked into the page at load; keep it current so a tab
    // that outlives its original server shows the right project
    const kickerEl = document.getElementById("kicker");
    if (kickerEl && payload.projectName && kickerEl.textContent !== payload.projectName.toUpperCase()) {
      kickerEl.textContent = payload.projectName.toUpperCase();
    }
  } catch {
    bridgeStatus.textContent = "harn bridge unknown";
  }
}

const source = new EventSource("/events/stream");
let streamWasLost = false;
source.onopen = () => {
  statusEl.textContent = "listening";
  if (streamWasLost) {
    // a stale tab just reconnected (server restarted): resync the scene and
    // the replay control instead of showing the previous session's frame
    streamWasLost = false;
    fetch("/scene").then((r) => r.json()).then(renderScene).catch(() => {});
    initReplayControl();
  }
};
source.onerror = () => { streamWasLost = true; statusEl.textContent = "reconnecting"; };
source.onmessage = (message) => {
  try {
    pushEvent(JSON.parse(message.data));
  } catch {
    statusEl.textContent = "decode fault";
  }
};

fetch("/scene")
  .then((response) => response.json())
  .then((scene) => renderScene(scene))
  .catch(() => { statusEl.textContent = "scene fetch failed"; });

refreshHealth();
setInterval(refreshHealth, 1000);
"""

__all__ = [
    "BrowserInput",
    "BrowserInputQueue",
    "CORE_PRIMITIVE_KINDS",
    "GibsonServerState",
    "HarnBridgeState",
    "INPUT_DELIVERY_KINDS",
    "SUPPORTED_RENDER_MODES",
    "SUPPORTED_RENDER_TIMING_MODES",
    "apply_event_to_scene",
    "backend_contract_payload",
    "build_state_from_env",
    "browser_input_event_payload",
    "create_server",
    "diagnostic_event_payload",
    "enqueue_browser_input",
    "event_from_payload",
    "format_sse",
    "health_payload",
    "make_handler",
    "publish_diagnostic_event",
    "renderer_interest_from_env",
    "renderer_context_config_from_env",
    "route_rules_from_env",
    "run_server",
    "submit_event_to_renderer",
]
