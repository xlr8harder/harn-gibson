# Architecture

This project treats harn as the source of truth and adds a parallel display layer.

## Boundary

The harn extension subscribes to harn events and normalizes each one into a `GibsonEvent`.
Events are published to one or more sinks:

- HTTP POST to a local display server (`HARN_GIBSON_ENDPOINT`);
- JSONL append-only log (`HARN_GIBSON_EVENT_LOG`);
- in-memory server buffer for browser SSE clients.

The graphical server does not own the harn session. It displays normalized events and keeps a browser input queue. The harn extension polls that queue and forwards messages into harn with `harn.sendUserMessage`, so the browser can be used as the primary interface while harn remains the session owner.

## Browser Input

The display server exposes:

- `POST /input`: enqueue browser input, with `message` and optional `deliverAs`;
- `GET /input/next`: harn extension poll endpoint, returning one queued input or `204`.

`deliverAs="followUp"` is the default. In harn this runs immediately when idle and becomes a follow-up queue item while streaming. `deliverAs="steer"` sends steering input for the active run.

The display applies a synthetic `browser_input` scene event as soon as input is accepted, so the primary display reacts before harn consumes the queued message.

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

The event feed and hook decisions are treated as debug surfaces. They remain in scene state for inspection, but the default browser layout hides them behind a debug drawer.

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
