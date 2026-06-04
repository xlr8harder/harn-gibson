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

## Integration Layers

There are three useful extension levels.

The renderer layer decides what should happen. A renderer consumes normalized harn events, route/coalescing metadata, current scene state, recent visual continuity, project metadata, repo topology, touched files, style information, and the visual catalog, then returns a `RenderPlan`. Renderers can be in-process deterministic Python, an external JSON command, a prompt-command model wrapper, or a future provider-backed model adapter. This is the right level for product-specific behavior, different visual personalities, event-routing experiments, and AI-driven scene direction.

The primitive layer decides what a scene can show. Browser primitives and animations are reusable scene vocabulary: `city_block`, `terminal_wall`, `signal_scope`, `svg_layer`, `data_rain`, `timeline_cue`, `route_trace`, and the rest of the catalog. A primitive should be browser-local, bounded, deterministic, and parameterized enough that many renderers can use it without authoring per-frame drawing instructions. This is the right level for adding a new visual toy, richer animation behavior, or a generic display building block.

The display-backend layer decides where pixels or terminal cells are drawn. The browser/canvas backend is the only production backend today, but the scene protocol is not inherently web-only. A native app, terminal renderer, game engine, OpenGL scene, or remote wall display could consume the same scene snapshots and scene-update payloads, then implement the catalog it supports with its own drawing and input loop. `GET /backend-contract` exposes this as `harn-gibson.display-backend-contract.v1`: endpoint paths, scene/update schema names, core primitive kinds, catalog primitive kinds, effect kinds, and the current browser backend declaration.

The boundary between these layers is `SceneMutation`: renderers produce mutations against primitive props and animation records; display backends own drawing, timing loops, and viewport-specific behavior. That lets a user build a new renderer from existing Gibson primitives, add a primitive/effect that deterministic, hard-coded, and AI renderers can all target, or build a non-web display backend that implements the agreed primitive catalog or a declared subset.

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

The browser treats `SceneAnimation` records as persistent renderable state, not only one-shot SSE effects. Current browser-rendered animation kinds include phase pulses, packet bursts, timeline cues, scans, glitches, breach waves, camera jolts, scene camera paths, flythrough rays, extrusion frames, and hold brackets. Structured vector primitives also include numeric transform keyframes, path morph frames, bounded filter/clip presets, plus curated SVG-style symbols such as animated globes, filesystem gates, reticles, data tunnels, ICE walls, and mainframe cores while still avoiding raw SVG markup. `hologram` adds a structured projection primitive for spinning rings, scan planes, projector beams, floating panels, and orbiting motes without requiring the renderer to draw each part by hand. `signal_scope` adds a structured radar/oscilloscope instrument with browser-local sweeps, blip pulses, spokes, rings, and waveforms for telemetry or intrusion scenes. `tunnel_grid` adds a structured perspective corridor with animated rings, lanes, and packet motes for flythroughs or mainframe traversal. `data_vault` adds a rotating wireframe vault/core with nested layers, panels, orbiting locks, and packet motes for access targets. `black_ice` adds a faceted security barrier with scanner shutters, breach glow, fracture rays, and sentry locks for access gates or failure beats. `trace_route` adds structured hop/link navigation with animated packet pulses for network traversal, command routing, or filesystem flythrough scenes. `data_rain` adds a structured animated glyph curtain for code rain, telemetry waterfalls, and packet noise without requiring the renderer to place every glyph itself. Replay screenshots load final scene state and render those animations from the scene record, which gives renderer-side fixtures a way to review effects without a live harn stream.

The raw event details, event feed, render intents, tracebacks, and hook decisions are treated as debug surfaces. They remain in scene state for inspection, but the default browser layout hides them behind a debug drawer.

Display style is scene metadata, not a separate browser-only setting. `HARN_GIBSON_STYLE` or `--style` selects a style pack such as `gibson`, `neon-noir`, `mainframe`, or `satellite-uplink`. Non-default style packs are stored in `scene.metadata.stylePack`, applied to the browser palette and canvas backdrop, and included in renderer context so a future renderer agent can choose effects that match the active visual language. Style motifs also drive browser-local backdrop overlays, including packet routes, neon slashes, phosphor/audit frames, orbital grids, radar sweeps, and warning chevrons.

## Replay Testing

The scene layer includes a replay harness that accepts recorded harn events, browser input events, renderer decisions, saved render plans, and explicit scene mutations. A replay run can produce a final scene JSON snapshot, a full replay result JSON file, expectation checks against the final scene, a canonical scene baseline comparison, and a browser screenshot of the final scene. That gives us a deterministic way to compare display effects against baselines and a manual way to inspect whether staged effects leave the scene in the intended state.

Replay works on both sides of the renderer boundary. Agent-side replay feeds historical harn events through routing, coalescing, and a renderer to generate a visualization. Renderer-side replay applies saved render plans or raw scene mutations against scene state. Those modes also support a later "full session visualization" workflow where a historical session is rendered all at once or in timed chunks.

When replay is asked for a timeline, it captures a full scene keyframe after each replay step. These keyframes are not used for canonical baselines by default; they are a review and future-renderer input format for chunking historical sessions, comparing visual continuity across steps, or generating later screenshot/video tooling. The same keyframes can be rendered back through the browser display as a deterministic screenshot sequence with per-frame canvas metrics. `watch-replay` is the live counterpart: it starts the display server and publishes replay steps through the same scene pipeline over time, so renderer choices can be watched in the browser without starting harn. It can use fixed per-step delays or real-time timestamp deltas from captured event metadata, including the request events embedded in saved renderer-plan steps. Long captures can also be watched as 1-based step ranges; full playback checks final expectations by default, while partial playback is treated as visual inspection unless expectation checks are explicitly requested.

## Render Pipeline

The display server accepts routed events into a render pipeline.

Before events reach a renderer, an `EventRouter` can choose whether they should go to a renderer agent, patch scene state directly, update a stream buffer, remain debug-only, or be dropped/sampled. Explicit `EventRouteRule` entries provide direct-scene, debug-only, renderer, drop, and every-N-event sampled routing for specific event types, and dogfood runs can provide them with `HARN_GIBSON_ROUTE_RULES`. Local stream bindings handle noisy stream deltas. Then a renderer-advertised `RendererEventInterest` can decide which remaining events should actually be sent to the renderer. Streaming assistant deltas currently update a local `text_stream` primitive so a future remote renderer agent does not need to receive every token-sized update.

In blocking mode, the server builds and applies a render plan before responding to harn. This guarantees the scene saw the event before harn proceeds.

In async mode, the server accepts the event immediately and a background worker batches queued events before rendering. This avoids slowing harn, but updates may arrive later and the renderer agent must handle multiple input events per plan.

Immediately before renderer execution, the pipeline normalizes the queued requests into a render-input batch. That gives each request a timeline offset, coalesced count, and batch metadata, and the same render-input envelope is included on published scene updates for replay/debug inspection.

Render-plan playback has two timing modes. Immediate timing is the default and applies steps as soon as the renderer plan is processed, preserving only explicit per-step delays. Scheduled timing treats `RenderStep.startOffsetMs` as an absolute offset within the coalesced batch timeline and publishes step-schedule metadata with each scene update. `timeline_cue` and `route_trace` animations can then represent labeled beats and packet motion inside persistent animation records, which gives future renderer agents a way to plan effects across a 5-10 second context window without blocking harn in async mode.

Each applied render plan also records a bounded render-intent history in scene metadata. A render intent summarizes the renderer, requested intent, event types, routes, timeline, effects, targets, and original plan metadata. The browser debug drawer, replay final-scene snapshots, `--output-render-intents`, and `--render-intent-review` expose this history so a future model-backed renderer can preserve visual continuity across turns and reviewers can inspect renderer decisions without digging through a full scene JSON file. Replay can also turn captured renderer contexts into provider-neutral prompt/message artifacts with `--output-render-prompts` and `--render-prompt-review`, keeping "what the renderer saw", "what a model would be asked", and "what the renderer decided" inspectable as separate surfaces.

The pipeline also builds a `RendererContext` for renderers that opt into `render_with_context`. The context alternates between full compaction payloads and rolling summaries, combining project metadata, bounded repo topology, touched-file summaries, catalog entries, current scene state, recent agent context, render intents, recent visualization history, and compact visual-continuity anchors without requiring a full transcript on each renderer turn.

The deterministic renderer returns one render step per event today. An external renderer command can also receive the same context as JSON on stdin and return render-plan JSON on stdout. The prompt-command model adapter sits one layer closer to a real model provider: it receives provider-neutral system/user messages built from the same renderer context and returns model-style JSON text. Command failures are converted into visible trace/debug scene state while the deterministic renderer keeps harn progress fail-open. External and model plans are validated before scene application, with warning-only unsupported choices recorded as `renderPlanDiagnostics` metadata and unsafe plans rejected into deterministic fallback plus trace/debug state. A provider-backed renderer should return the same `RenderPlan` shape and may include multiple delayed steps for sequential effects.

Repo topology is already visual input, not only prompt metadata. `HARN_GIBSON_PROJECT_ROOT` selects the repository sampled for renderer context; `dogfood --cwd PATH` sets it to the target harn workspace automatically, and `HARN_GIBSON_PROJECT_NAME` controls the display name. The deterministic renderer maps the bounded depth-2 repo sample into both a `node_graph` and a Gibson-style `city_block`, while the hard-coded dogfood renderer also maps it into a `wire_landscape`, `terminal_wall`, `access_matrix`, and `orbital_map`: top-level entries become districts, terrain peaks, terminal panels, grid cells, or uplink nodes, sampled children become smaller nearby blocks, bounded line-count metadata plus visible file/directory counts drive height, and touched files recolor/focus the matching district, peak, panel, access cell, or uplink node with particle, extrusion, terrain, route, and slow `cameraPath` drift effects.

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
