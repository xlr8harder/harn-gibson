# Architecture

This project treats harn as the source of truth and adds a parallel display layer.

## Boundary

The harn extension subscribes to harn events and normalizes each one into a `GibsonEvent`.
Events are published to one or more sinks:

- HTTP POST to a local display server (`HARN_GIBSON_ENDPOINT`);
- JSONL append-only log (`HARN_GIBSON_EVENT_LOG`);
- in-memory server buffer for browser SSE clients.

The graphical server does not own the harn session. It displays normalized events and can later send user input through harn RPC, but this first fixture keeps the terminal harn interface usable.

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
