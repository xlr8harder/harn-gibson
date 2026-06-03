"""Local browser display server for harn-gibson."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from harn_gibson.events import GibsonEvent
from harn_gibson.scene import SceneEngine, default_mutations_for_event, scene_update_payload
from harn_gibson.sinks import EventBuffer


@dataclass(slots=True)
class GibsonServerState:
    buffer: EventBuffer = field(default_factory=EventBuffer)
    scene: SceneEngine = field(default_factory=SceneEngine)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    state: GibsonServerState | None = None,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state or GibsonServerState()))


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:  # pragma: no cover
    server = create_server(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.server_close()


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
                    },
                )
                return
            if self.path == "/scene":
                self._json(HTTPStatus.OK, state.scene.state.to_dict())
                return
            if self.path == "/events":  # pragma: no cover
                self._stream_events()  # pragma: no cover
                return  # pragma: no cover
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/events":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "event payload must be an object"})
                return
            try:
                update = apply_event_to_scene(payload, state)
            except (KeyError, TypeError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            state.buffer.publish(update)
            self._json(HTTPStatus.ACCEPTED, {"ok": True, "sceneRevision": state.scene.state.revision})

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

        def _write(self, status: HTTPStatus, body: str, content_type: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return GibsonRequestHandler


def format_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def apply_event_to_scene(payload: dict[str, Any], state: GibsonServerState) -> dict[str, Any]:
    event = event_from_payload(payload)
    decisions = [decision for decision in payload.get("decisions", []) if isinstance(decision, dict)]
    mutations = default_mutations_for_event(event, decisions)
    scene = state.scene.apply(mutations)
    update = scene_update_payload(event, mutations, scene)
    if decisions:
        update["decisions"] = decisions
    return update


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
          <div id="status" class="status">awaiting signal</div>
        </div>
        <div class="readout">
          <div>
            <span>phase</span>
            <strong id="phase">idle</strong>
          </div>
          <div>
            <span>event</span>
            <strong id="eventType">none</strong>
          </div>
          <div>
            <span>sequence</span>
            <strong id="sequence">0</strong>
          </div>
        </div>
      </section>
      <aside class="side" aria-label="Event stream">
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
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 18px;
  min-height: 100vh;
  padding: 18px;
}
.stage {
  position: relative;
  overflow: hidden;
  min-height: calc(100vh - 36px);
  border: 1px solid var(--line);
  background: #070b0f;
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
.status {
  max-width: 220px;
  padding: 8px 10px;
  border: 1px solid rgba(255, 204, 102, 0.35);
  color: var(--amber);
  text-align: right;
}
.readout {
  position: absolute;
  left: 24px;
  right: 24px;
  bottom: 24px;
  z-index: 1;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.readout div,
.panel {
  border: 1px solid var(--line);
  background: var(--panel);
}
.readout div {
  min-height: 78px;
  padding: 12px;
}
.readout span,
.panel h2 {
  display: block;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  text-transform: uppercase;
}
.readout strong {
  display: block;
  margin-top: 10px;
  color: var(--cyan);
  font-size: 17px;
  overflow-wrap: anywhere;
}
.side {
  display: grid;
  grid-template-rows: 1fr 260px;
  gap: 18px;
  min-height: calc(100vh - 36px);
}
.panel {
  min-width: 0;
  overflow: hidden;
  padding: 14px;
}
.panel h2 {
  margin: 0 0 12px;
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
  .shell { grid-template-columns: 1fr; }
  .stage { min-height: 58vh; }
  .side { min-height: 42vh; grid-template-rows: 1fr 220px; }
  .readout { grid-template-columns: 1fr; }
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
const decisionLog = document.getElementById("decisionLog");
const pulses = [];

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
"""

__all__ = [
    "GibsonServerState",
    "apply_event_to_scene",
    "create_server",
    "event_from_payload",
    "format_sse",
    "make_handler",
    "run_server",
]
