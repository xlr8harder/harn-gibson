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

from harn_gibson.events import GibsonEvent
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RenderMode,
    RenderPipeline,
    RenderRequest,
    RenderSubmitResult,
    SceneRenderer,
    coerce_batch_window_ms,
    coerce_render_mode,
    decisions_from_payload,
    render_accept_payload,
)
from harn_gibson.scene import SceneEngine
from harn_gibson.sinks import EventBuffer


@dataclass(slots=True)
class GibsonServerState:
    buffer: EventBuffer = field(default_factory=EventBuffer)
    scene: SceneEngine = field(default_factory=SceneEngine)
    inputs: BrowserInputQueue = field(default_factory=lambda: BrowserInputQueue())
    render_mode: RenderMode = "blocking"
    render_batch_window_ms: int = 40
    renderer: SceneRenderer = field(default_factory=DeterministicSceneRenderer)
    pipeline: RenderPipeline = field(init=False)

    def __post_init__(self) -> None:
        self.pipeline = RenderPipeline(
            scene=self.scene,
            buffer=self.buffer,
            renderer=self.renderer,
            mode=self.render_mode,
            batch_window_ms=self.render_batch_window_ms,
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


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    state: GibsonServerState | None = None,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state or build_state_from_env()))


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:  # pragma: no cover
    state = build_state_from_env()
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
    return GibsonServerState(
        render_mode=coerce_render_mode(source.get("HARN_GIBSON_RENDER_MODE")),
        render_batch_window_ms=coerce_batch_window_ms(source.get("HARN_GIBSON_RENDER_BATCH_MS")),
    )


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
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "events": len(state.buffer.snapshot()),
                        "sceneRevision": state.scene.state.revision,
                        "renderMode": state.pipeline.mode,
                        "pendingRenderJobs": state.pipeline.pending_count(),
                    },
                )
                return
            if self.path == "/scene":
                self._json(HTTPStatus.OK, state.scene.state.to_dict())
                return
            if self.path == "/input/next":
                item = state.inputs.pop()
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


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def format_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def submit_event_to_renderer(payload: dict[str, Any], state: GibsonServerState) -> RenderSubmitResult:
    event = event_from_payload(payload)
    return state.pipeline.submit(RenderRequest(event=event, decisions=decisions_from_payload(payload)))


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
            <div id="status" class="status">awaiting signal</div>
          </div>
        </div>
        <div class="signal-copy">
          <span id="signalTitle">CHANNEL IDLE</span>
          <strong id="signalSummary">awaiting harn stream</strong>
        </div>
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
  background: #070b0f;
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
  max-width: 220px;
  padding: 8px 10px;
  border: 1px solid rgba(255, 204, 102, 0.35);
  color: var(--amber);
  text-align: right;
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
  grid-template-rows: auto auto minmax(0, 1fr) 240px;
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
@media (max-width: 900px) {
  .shell { padding: 10px; }
  .stage { min-height: calc(100vh - 20px); }
  .mast { flex-direction: column; padding: 16px; }
  .topbar { width: 100%; justify-content: space-between; }
  .signal-copy {
    left: 16px;
    right: 16px;
    bottom: 214px;
    max-height: 84px;
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
const phaseEl = document.getElementById("phase");
const eventEl = document.getElementById("eventType");
const sequenceEl = document.getElementById("sequence");
const signalTitle = document.getElementById("signalTitle");
const signalSummary = document.getElementById("signalSummary");
const decisionLog = document.getElementById("decisionLog");
const debugToggle = document.getElementById("debugToggle");
const debugClose = document.getElementById("debugClose");
const inputForm = document.getElementById("inputForm");
const promptInput = document.getElementById("promptInput");
const deliverAs = document.getElementById("deliverAs");
const inputStatus = document.getElementById("inputStatus");
const pulses = [];

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
    inputStatus.textContent = `queued ${payload.input.id}`;
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
  ctx.fillStyle = "#05070b";
  ctx.fillRect(0, 0, w, h);
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(88, 215, 255, 0.12)";
  const step = 42 * devicePixelRatio;
  for (let x = -step; x < w + step; x += step) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x + w * 0.32, h);
    ctx.stroke();
  }
  for (let y = 0; y < h; y += step) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y + h * 0.08);
    ctx.stroke();
  }
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
  requestAnimationFrame(draw);
}
draw();

function colorFor(phase) {
  if (phase === "after") return "rgba(255, 91, 200, 1)";
  if (phase === "during") return "rgba(88, 215, 255, 1)";
  if (phase === "lifecycle") return "rgba(255, 204, 102, 1)";
  return "rgba(105, 255, 184, 1)";
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
  const status = scene.primitives?.status?.props || {};
  if (status.text) statusEl.textContent = status.text;
  decisionLog.textContent = JSON.stringify(scene.primitives?.["decision-log"]?.props?.text || [], null, 2);
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
"""

__all__ = [
    "BrowserInput",
    "BrowserInputQueue",
    "GibsonServerState",
    "apply_event_to_scene",
    "build_state_from_env",
    "browser_input_event_payload",
    "create_server",
    "enqueue_browser_input",
    "event_from_payload",
    "format_sse",
    "make_handler",
    "run_server",
    "submit_event_to_renderer",
]
