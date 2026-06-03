# Architecture

This project treats harn as the source of truth and adds a parallel display layer.

## Boundary

The harn extension subscribes to harn events and normalizes each one into a `GibsonEvent`.
Events are published to one or more sinks:

- HTTP POST to a local display server (`HARN_GIBSON_ENDPOINT`);
- JSONL append-only log (`HARN_GIBSON_EVENT_LOG`);
- in-memory server buffer for browser SSE clients.

The graphical server does not own the harn session. It displays normalized events and keeps a browser input queue. The harn extension polls that queue and forwards messages into harn with `harn.sendUserMessage`, so the browser can send lightweight follow-up or steering input while harn remains the primary session owner.

The repository includes harn as a development dependency for one-command dogfooding, but the display server and extension modules are intentionally independent of harn's TUI implementation. A later package split can expose a web-only relay package and keep the harn CLI launcher as an optional integration layer.

## Browser Input

The display server exposes:

- `POST /input`: enqueue browser input, with `message` and optional `deliverAs`;
- `GET /input/next`: harn extension poll endpoint, returning one queued input or `204`.

`deliverAs="followUp"` is the default. In harn this runs immediately when idle and becomes a follow-up queue item while streaming. `deliverAs="steer"` sends steering input for the active run.

The display applies a synthetic `browser_input` scene event as soon as input is accepted, so the visual layer reacts before harn consumes the queued message.

## Scene Engine

The display is a persistent scene, not a sequence of independent event renderings.
`SceneState` contains primitives, animations, and a bounded event log. Events flow through a display agent that emits `SceneMutation` objects:

- `upsert`: create or replace a primitive;
- `patch`: update primitive props;
- `remove`: remove a primitive and its animations;
- `append_log`: add a log/readout entry;
- `start_animation` / `stop_animation`: control transient visual activity;
- `reset_scene`: return to the boot scene.

The current display agent is deterministic and maps each harn event to status, log, decision, and pulse mutations. Later, the LLM display agent should receive recent harn context plus recent scene context and return the same mutation format.

The raw event details, event feed, and hook decisions are treated as debug surfaces. They remain in scene state for inspection, but the default browser layout hides them behind a debug drawer.

## Replay Testing

The scene layer includes a replay harness that accepts recorded harn events, browser input events, renderer decisions, saved render plans, and explicit scene mutations. A replay run can produce a final scene JSON snapshot, a full replay result JSON file, and a browser screenshot of the final scene. That gives us a deterministic way to compare display effects against baselines and a manual way to inspect whether staged effects leave the scene in the intended state.

Replay works on both sides of the renderer boundary. Agent-side replay feeds historical harn events through routing, coalescing, and a renderer to generate a visualization. Renderer-side replay applies saved render plans or raw scene mutations against scene state. Those modes also support a later "full session visualization" workflow where a historical session is rendered all at once or in timed chunks.

## Render Pipeline

The display server accepts routed events into a render pipeline.

Before events reach a renderer, an `EventRouter` can choose whether they should go to a renderer agent, patch scene state directly, update a stream buffer, remain debug-only, or be dropped/sampled. Explicit `EventRouteRule` entries provide direct-scene, debug-only, renderer, and drop routing for specific event types, and dogfood runs can provide them with `HARN_GIBSON_ROUTE_RULES`. Local stream bindings handle noisy stream deltas. Then a renderer-advertised `RendererEventInterest` can decide which remaining events should actually be sent to the renderer. Streaming assistant deltas currently update a local `text_stream` primitive so a future remote renderer agent does not need to receive every token-sized update.

In blocking mode, the server builds and applies a render plan before responding to harn. This guarantees the scene saw the event before harn proceeds.

In async mode, the server accepts the event immediately and a background worker batches queued events before rendering. This avoids slowing harn, but updates may arrive later and the renderer agent must handle multiple input events per plan.

The deterministic renderer returns one render step per event today. A model-backed renderer should return the same `RenderPlan` shape and may include multiple delayed steps for sequential effects.

## Hook Phases

Each harn event is assigned a phase:

- `before`: input, provider request, agent start, tool calls, and session preflight events;
- `during`: streaming message/tool updates;
- `after`: completed messages, tools, turns, provider responses, and session changes;
- `lifecycle`: session/model/resource events that do not naturally fit a mutation point.

Before hooks can interdict where harn allows it. After hooks can inspect output and request supported mutations, such as replacing tool result content.

## Current Mutation Support

The dispatcher maps hook decisions back to harn result shapes:

- `input`: `handled` or `transform`;
- `tool_call`: `block`;
- `tool_result`: `content`, `details`, `isError`;
- `message_end`: `message`;
- `before_agent_start`: `message`, `systemPrompt`;
- `session_before_*`: `cancel`.

Other events are display-only until harn exposes a mutation result for them.
