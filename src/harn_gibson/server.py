"""Local browser display server for harn-gibson."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from typing import Any

from harn_gibson.catalog import VisualCatalog, default_visual_catalog
from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.external_renderer import external_renderer_from_env
from harn_gibson.model_renderer import model_renderer_from_env
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
    coerce_render_mode,
    coerce_render_timing_mode,
    decisions_from_payload,
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
from harn_gibson.scene import SceneEngine, apply_style_to_scene, initial_scene
from harn_gibson.sinks import EventBuffer
from harn_gibson.styles import DEFAULT_STYLE_ID, StylePack, default_style_pack, style_pack_from_name


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
    pipeline: RenderPipeline = field(init=False)

    def __post_init__(self) -> None:
        style_payload = self.style_pack.to_dict()
        if self.style_pack.id != DEFAULT_STYLE_ID:
            self.scene.configure_initial_scene(lambda: initial_scene(style_payload))
            apply_style_to_scene(self.scene.state, style_payload)
        renderer_interest = self.renderer_interest or renderer_event_interest_from_renderer(self.renderer)
        if renderer_interest is not None and self.router.renderer_interest is None:
            self.router.renderer_interest = renderer_interest
        self.pipeline = RenderPipeline(
            scene=self.scene,
            buffer=self.buffer,
            renderer=self.renderer,
            catalog=self.catalog,
            context_builder=RendererContextBuilder(
                RendererContextConfig(
                    display_style=self.style_pack.id,
                    style_pack=style_payload,
                )
            ),
            mode=self.render_mode,
            batch_window_ms=self.render_batch_window_ms,
            timing_mode=self.render_timing_mode,
        )


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
        if deliver_as not in {"followUp", "steer"}:
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
    return ThreadingHTTPServer((host, port), make_handler(state or build_state_from_env()))


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
    renderer_interest = renderer_interest_from_env(source.get("HARN_GIBSON_RENDERER_INTEREST"))
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
        renderer=model_renderer or renderer or DeterministicSceneRenderer(),
        style_pack=style_pack_from_name(source.get("HARN_GIBSON_STYLE")),
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
            if self.path in {"/", "/index.html"}:
                self._write(HTTPStatus.OK, HTML, "text/html; charset=utf-8")
                return
            if self.path == "/assets/app.css":
                self._write(HTTPStatus.OK, CSS, "text/css; charset=utf-8")
                return
            if self.path == "/assets/app.js":
                self._write(HTTPStatus.OK, JS, "application/javascript; charset=utf-8")
                return
            if self.path == "/healthz":
                self._json(HTTPStatus.OK, health_payload(state))
                return
            if self.path == "/scene":
                self._json(HTTPStatus.OK, state.scene.state.to_dict())
                return
            if self.path == "/catalog":
                self._json(HTTPStatus.OK, state.catalog.to_dict())
                return
            if self.path == "/input/next":
                item = state.inputs.pop()
                state.input_bridge.record_input_poll(delivered=item is not None)
                if item is None:
                    self._empty(HTTPStatus.NO_CONTENT)
                    return
                self._json(HTTPStatus.OK, item.to_dict())
                return
            if self.path == "/events":  # pragma: no cover
                self._stream_events()  # pragma: no cover
                return  # pragma: no cover
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/events":
                self._handle_event_post()
                return
            if self.path == "/input":
                self._handle_input_post()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

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
    route = state.router.route(event, decisions_from_payload(payload))
    if route.dropped:
        return RenderSubmitResult(mode=state.pipeline.mode, queued=state.pipeline.pending_count())
    if not route.uses_renderer:
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
            <p class="kicker">HARN DISPLAY RELAY</p>
            <h1>GIBSON LINK</h1>
          </div>
          <div class="topbar">
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
        <div class="panel">
          <h2>Hook Decisions</h2>
          <pre id="decisionLog">[]</pre>
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
#decisionLog {
  height: calc(100% - 32px);
  margin: 0;
  overflow: auto;
  color: var(--amber);
  white-space: pre-wrap;
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
const decisionLog = document.getElementById("decisionLog");
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

debugToggle.addEventListener("click", () => {
  const expanded = document.body.classList.toggle("debug-open");
  debugToggle.setAttribute("aria-expanded", String(expanded));
});

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

function draw() {
  const w = canvas.width;
  const h = canvas.height;
  const now = performance.now();
  drawBackdrop(w, h, now);
  drawScenePrimitives(currentScene, w, h, now);
  drawSceneAnimations(currentScene, w, h, now);
  drawPulses(w, h);
  requestAnimationFrame(draw);
}
draw();

function drawBackdrop(w, h, now) {
  const canvasStyle = currentStylePack.canvas || DEFAULT_STYLE_PACK.canvas;
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
  if ((currentStylePack.motifs || []).includes("phosphor-grid")) {
    ctx.fillStyle = toneColor("green", 0.035 + Math.sin(now * 0.004) * 0.01);
    for (let y = 0; y < h; y += 6 * devicePixelRatio) ctx.fillRect(0, y, w, devicePixelRatio);
  }
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
    "particle_field",
    "mesh",
    "city_block",
    "hologram",
    "svg_layer",
    "ribbon",
    "trace_route",
    "node_graph",
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
  if (primitive.kind === "city_block") drawCityBlock(primitive, w, h);
  if (primitive.kind === "hologram") drawHologram(primitive, w, h, now);
  if (primitive.kind === "svg_layer") drawSvgLayer(primitive, w, h, now);
  if (primitive.kind === "node_graph") drawNodeGraph(primitive, w, h);
  if (primitive.kind === "trace_route") drawTraceRoute(primitive, w, h, now);
  if (primitive.kind === "ribbon") drawRibbon(primitive, w, h, now);
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

function colorPhaseTone(phase) {
  if (phase === "after") return "magenta";
  if (phase === "during") return "cyan";
  if (phase === "lifecycle") return "amber";
  if (phase === "before") return "green";
  return null;
}

function primitiveAnchor(primitive, w, h) {
  const props = primitive?.props || {};
  if (props.position) return normalizedPoint(props.position, w, h);
  if (primitive?.kind === "node_graph") {
    const nodes = Array.isArray(props.nodes) ? props.nodes : [];
    const focus = nodes.find((node) => node.id === props.focusNodeId) || nodes[0];
    if (focus) return normalizedPoint(focus, w, h);
  }
  if (primitive?.kind === "city_block") {
    const blocks = Array.isArray(props.blocks) ? props.blocks : [];
    const focus = blocks.find((block) => block.id === props.focusBlockId) || blocks[0];
    if (focus) return {x: Number(focus.x || 0.5) * w, y: Number(focus.y || 0.5) * h};
  }
  if (primitive?.kind === "ribbon") {
    const points = Array.isArray(props.points) ? props.points : [];
    if (points.length) return normalizedPoint(points[Math.floor(points.length / 2)], w, h);
  }
  if (primitive?.kind === "particle_field" && props.emitter) return normalizedPoint(props.emitter, w, h);
  return {x: w * 0.5, y: h * 0.48};
}

function animationAnchor(animation, scene, w, h) {
  if (animation.targetId === "scan-grid") return {x: w * 0.5, y: h * 0.52};
  return primitiveAnchor(scene?.primitives?.[animation.targetId], w, h);
}

function drawSceneAnimations(scene, w, h, now) {
  if (!scene?.animations) return;
  syncAnimationClocks(scene, now);
  for (const animation of Object.values(scene.animations)) {
    const progress = animationProgress(animation, now);
    if (!animation.loop && progress >= 1) continue;
    drawSceneAnimation(animation, scene, w, h, now, progress);
  }
}

function drawSceneAnimation(animation, scene, w, h, now, progress) {
  if (animation.kind === "packet_burst") drawPacketBurstAnimation(animation, scene, w, h, now, progress);
  else if (animation.kind === "scan") drawScanAnimation(animation, w, h, progress);
  else if (animation.kind === "glitch") drawGlitchAnimation(animation, scene, w, h, now, progress);
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

function drawCityBlock(primitive, w, h) {
  const props = primitive.props || {};
  const blocks = Array.isArray(props.blocks) ? props.blocks : [];
  ctx.save();
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
  const pathData = String(pathSpec?.d || "");
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
  const base = {
    pathCount: Array.isArray(source.paths) ? source.paths.length : 0,
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
  const count = Math.max(0, Math.min(120, Number(props.count || 0)));
  const velocity = Number(props.velocity || 0.25);
  const emitter = normalizedPoint(props.emitter || {x: 0.5, y: 0.5}, w, h);
  const tone = props.color || "cyan";
  ctx.save();
  ctx.globalCompositeOperation = props.blend === "screen" ? "screen" : "source-over";
  ctx.lineCap = "round";
  for (let index = 0; index < count; index++) {
    const phase = ((now * velocity * 0.00025) + index * 0.071 + Number(props.seed || 0) * 0.013) % 1;
    const angle = -0.82 + index * 0.37;
    const distance = phase * Math.max(w, h) * 0.62;
    const x = emitter.x + Math.cos(angle) * distance;
    const y = emitter.y + Math.sin(angle) * distance * 0.62;
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
  ctx.restore();
}

function pushEvent(event) {
  const update = event.event ? event : {event, scene: null, mutations: []};
  const current = update.event;
  const scene = update.scene;
  const decisions = update.decisions || [];
  statusEl.textContent = "linked";
  phaseEl.textContent = current.phase || "unknown";
  eventEl.textContent = current.eventType || "unknown";
  sequenceEl.textContent = String(current.sequence || 0);
  signalTitle.textContent = current.title || current.eventType || "SIGNAL";
  signalSummary.textContent = current.summary || `${current.phase || "event"}:${current.eventType || "unknown"}`;
  if (scene) {
    renderScene(scene);
  } else {
    decisionLog.textContent = JSON.stringify(decisions, null, 2);
    if (update.renderIntent) {
      intentLog.textContent = JSON.stringify([update.renderIntent], null, 2);
    }
    appendFeedItem(current);
  }
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
  decisionLog.textContent = JSON.stringify(scene.primitives?.["decision-log"]?.props?.text || [], null, 2);
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
    const response = await fetch("/healthz", {cache: "no-store"});
    const payload = await response.json();
    updateBridgeStatus(payload.inputBridge);
  } catch {
    bridgeStatus.textContent = "harn bridge unknown";
  }
}

const source = new EventSource("/events");
source.onopen = () => { statusEl.textContent = "listening"; };
source.onerror = () => { statusEl.textContent = "reconnecting"; };
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
    "GibsonServerState",
    "HarnBridgeState",
    "apply_event_to_scene",
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
    "route_rules_from_env",
    "run_server",
    "submit_event_to_renderer",
]
