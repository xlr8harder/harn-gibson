# Renderer Agent

The renderer agent is the model-driven version of the current deterministic renderer. It receives harn events, current scene state, and recent visualization context, then returns a `RenderPlan`.

The server still uses the deterministic renderer by default. For dogfood and adapter work, it now has two optional process boundaries: an external render-plan command that receives raw renderer context, and a prompt-command model adapter that receives the exact provider-neutral messages a model would receive and returns model-style JSON text. Provider SDK wiring remains deferred, but the prompt, parse, validation, diagnostics, and fail-open path are now executable.

## Integration Levels

Most downstream work falls into one of three levels.

Build a renderer when you want to decide how harn activity becomes a scene. The renderer gets events, coalesced timing, current scene/context, repo and touched-file summaries, style metadata, and the visual catalog, then returns `SceneMutation` steps in a `RenderPlan`. This can be deterministic Python, a hard-coded cinematic renderer, an external command, a prompt-command model wrapper, or a future provider-backed model renderer.

Build a primitive or animation when you want to expand what every renderer can draw. Primitives are browser-rendered vocabulary with bounded props and local animation loops. Renderers should be able to say "show this repo district", "stream these terminal lines", or "run this route trace" without emitting per-frame pixels or raw SVG/HTML. New primitives belong in the catalog, browser renderer, validation rules, replay fixtures, screenshot checks, and renderer docs.

Build a display backend when you want the scene somewhere other than the web canvas. The current server/browser pair is the first backend, but a non-web display can consume `SceneState` snapshots, published scene updates, replay timelines, or JSONL-derived replay output and implement the primitive catalog in another runtime. `GET /backend-contract` returns the machine-readable display-backend contract, including endpoint paths, scene/update schemas, core primitive kinds, catalog primitive kinds, effect kinds, mutation ops, input delivery modes, render timing modes, supported style packs, the active style pack, and a capability profile. A backend may support the full catalog or clearly advertise a subset, but it should preserve the scene mutation contract so existing deterministic, hard-coded, and AI renderers can target it. A custom primitive layer can work by translating the Gibson catalog or by using a renderer and catalog that agree on the custom vocabulary.

The renderer layer composes and mutates; the primitive/backend layer renders and animates. These are public integration points, and they intentionally meet at the scene mutation contract.

## Render Plan Contract

Renderer inputs use a generic render-input envelope. It preserves event timing so a 5-10 second renderer-agent turnaround can still produce a plan whose effects cover the same approximate interval:

```json
{
  "schema": "harn-gibson.render-input.v1",
  "route": "renderer_agent",
  "timeline": {"startMs": 1000, "endMs": 11000, "durationMs": 10000},
  "requests": [
    {
      "timelineOffsetMs": 1200,
      "event": {"eventType": "tool_call", "summary": "bash starting with {command}"}
    }
  ],
  "metadata": {}
}
```

The renderer can answer with steps that use delays and start offsets to make the visualization feel like it is replaying the coalesced time window rather than reacting to one isolated event.

```json
{
  "steps": [
    {
      "startOffsetMs": 1200,
      "delayMs": 0,
      "mutations": [
        {"op": "patch", "targetId": "status", "props": {"text": "before:tool_call"}}
      ]
    }
  ],
  "metadata": {
    "intent": "show tool preflight"
  }
}
```

Each step can include multiple scene mutations. Multiple steps let the renderer queue effects in sequence, for example: flash a node, add a trace line, then settle the status panel.

Every applied plan is summarized as `harn-gibson.render-intent.v1` and stored in `scene.metadata.renderIntents`. The intent summary includes renderer name, human intent, event types, routes, timeline, effects, targets, and original plan metadata. Published scene updates also include the current `renderIntent`, which makes replay/debug inspection independent of renderer implementation details.

For long dogfood sessions, tune renderer context budgets instead of changing prompt code:

```bash
HARN_GIBSON_RENDERER_COMPACTION_EVENTS=40
HARN_GIBSON_RENDERER_MAX_RECENT_PLANS=6
HARN_GIBSON_RENDERER_MAX_RECENT_LOG_ENTRIES=12
HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS=240
HARN_GIBSON_RENDERER_MAX_VISUAL_ANCHORS=16
HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES=64
HARN_GIBSON_RENDERER_MAX_REPO_CHILDREN=8
HARN_GIBSON_RENDERER_MAX_TOUCHED_FILES=24
HARN_GIBSON_RENDERER_MAX_TOUCHED_PATH_CHARS=160
HARN_GIBSON_RENDERER_MAX_WORLD_ENTITIES=24
```

Lower values reduce prompt size and remote renderer latency; higher values give a model more scene continuity, repo topology, touched-file evidence, and accumulated world-model facts.

## External Renderer Command

Set `HARN_GIBSON_RENDERER_COMMAND` to run a renderer as a subprocess. The command is parsed as a shell-style command string, or as a JSON array of argv strings when the value starts with `[`. `HARN_GIBSON_RENDERER_TIMEOUT_MS` controls the per-render timeout.

```bash
HARN_GIBSON_RENDERER_COMMAND='uv run python examples/renderers/gibson_dogfood_renderer.py' \
HARN_GIBSON_RENDERER_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood --style satellite-uplink
```

For longer live sessions that should become replay fixtures, prefer the capture wrapper:

```bash
uv run harn-gibson dogfood-capture --list-trajectories
uv run harn-gibson dogfood-capture --trajectory tiny-project --style satellite-uplink
uv run harn-gibson dogfood-capture --trajectory repo-map --style satellite-uplink
```

Built-in trajectories create ignored bare workspaces, inject long prompts, set the showcase renderer, record normalized JSONL under ignored artifacts by default, and print the matching split `event-log-to-replay --review-dir ... --output-result ...` command when harn exits, including the captured workspace's `--project-root` and `--project-name`. `tiny-project` is the general bootstrap path; `repo-map` is aimed at depth-2 repo topology, line-count variation, and touched-file batches for repo-city renderer work. For hard-coded renderer regression work, prefer recording real harn trajectories first, then promote only redacted replay chunks and browser baselines after reviewing the generated frames. For custom prompts or workspace reuse, pass `--cwd PATH --split-every N -- -p "$(cat your-prompt.md)"` instead.

For 15-20 minute captures, use `event-log-to-replay --split-every N --output-dir DIR --review-dir REVIEW` instead of one large fixture. It writes the split fixture directory and immediately builds a suite overview plus per-chunk frame players, renderer contexts, prompts, renderer chunks, render intents, final scenes, and result JSON. `replay-dir DIR --review-dir ...` can rerun that same review later. Suite result and review manifests include `trajectoryCoverage`, a compact signal/gap summary for commands, failed tool results, runtime errors, browser input, touched files, top-level repo spread, renderer routes/plans, visual anchors, active animations, effects, and screenshots. Use it as the quick first pass for deciding whether a capture is rich enough to promote into renderer-regression fixtures.

`examples/renderers/gibson1_renderer.py` is the default calmer hard-coded renderer for normal dogfood watching. It is deterministic, uses real renderer context, and deliberately emits a smaller scene: status, terminal wall, depth-2 repo city with compact child blocks, repo wire terrain, signal scope, trace route, low-opacity data rain, style-aware tones, and timeline/route animations.

`examples/renderers/gibson_dogfood_renderer.py` remains the showcase and stress renderer for live sessions. It is deterministic, but it uses real renderer context: event phase/type, coalesced timing, touched files, repo topology, and current style. Non-default styles alter its emitted tones and plan metadata before the browser renders the scene. It emits a staged scene with data rain, tunnel grids, wire landscapes, terminal walls, access matrices, orbital uplink maps, signal scopes, black-ICE barriers, trace routes, repo city blocks, structured SVG sigils, timeline cues, route-trace animations, camera paths, camera jolts, packet bursts, breach waves, scans, and extrusion. `examples/renderers/gibson_echo_renderer.py` remains the smallest external-renderer contract example.

The command receives one JSON object on stdin:

```json
{
  "schema": "harn-gibson.external-renderer-request.v1",
  "requests": [{"event": {"eventType": "tool_call"}}],
  "scene": {"schema": "harn-gibson.scene.v1"},
  "context": {"schema": "harn-gibson.renderer-context.v1"}
}
```

The command returns a render plan on stdout. Live requests from harn remain authoritative; the adapter binds returned steps to the current request batch and ignores model-supplied request objects.

```json
{
  "schema": "harn-gibson.render-plan.v1",
  "metadata": {"renderer": "example", "intent": "pulse current tool"},
  "steps": [
    {
      "eventIndex": 0,
      "mutations": [
        {"op": "patch", "targetId": "status", "props": {"text": "external:tool_call"}}
      ]
    }
  ]
}
```

If the command exits nonzero, times out, or writes invalid JSON, harn-gibson applies the deterministic fallback and patches the renderer failure into the trace/debug scene state. That keeps harn progress fail-open while making renderer-agent problems visible in the browser.

External renderer plans are validated before they are applied to scene state. Warning-only plans still run, but the applied render intent metadata includes `renderPlanDiagnostics` with `harn-gibson.render-plan-diagnostics.v1` issues such as unsupported primitive kinds, unknown SVG symbols, malformed vector keyframes, unsupported vector filter/clip presets, unknown regions, or animation targets that will fall back to generic pulse placement. Unsafe plans are rejected before scene application. Current hard failures include patching a missing target, missing mutation payloads required by the scene engine, exceeding plan size limits, exceeding the bounded `svg_layer` keyframe count, and trying to render raw SVG/HTML/external references through `svg_layer` upserts or patches. Rejected plans use the deterministic fallback and add the diagnostics payload to both render metadata and the trace/debug scene state.

## Prompt-Command Model Adapter

Set `HARN_GIBSON_RENDERER_MODEL_COMMAND` to run a local command at the provider-neutral prompt boundary. This is useful for testing model prompts, mock model processes, or thin provider wrappers without giving the command direct access to the full in-process renderer objects. The command receives one JSON object on stdin:

```json
{
  "schema": "harn-gibson.model-renderer-request.v1",
  "messageCount": 2,
  "messages": [
    {"role": "system", "content": "You are the harn-gibson cinematic renderer..."},
    {"role": "user", "content": "Render the current harn-gibson batch..."}
  ],
  "metadata": {
    "renderer": "model-command",
    "prompt": {"schema": "harn-gibson.renderer-prompt.v1", "mode": "compaction"}
  }
}
```

The command writes the model response text to stdout. The adapter accepts a raw render-plan JSON object, a JSON object with `content`, fenced JSON, or text with one embedded JSON object. It binds the returned plan to the current live request batch, validates it against scene/catalog safety rules, and records compact `rendererPrompt` metadata in the render intent. Use `HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS` to control the prompt-command timeout; if omitted, it falls back to `HARN_GIBSON_RENDERER_TIMEOUT_MS`.

```bash
HARN_GIBSON_RENDERER_MODEL_COMMAND='uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood
```

If both `HARN_GIBSON_RENDERER_MODEL_COMMAND` and `HARN_GIBSON_RENDERER_COMMAND` are set, the model command wins. Command failures, invalid model output, and unsafe model plans are fail-open: harn-gibson applies deterministic fallback mutations and patches the failure into trace/debug scene state.

## Blocking Vs Async

`HARN_GIBSON_RENDER_MODE=blocking` means the display server applies the render plan before responding to harn. This is easier to reason about and guarantees the scene has acknowledged the event.

`HARN_GIBSON_RENDER_MODE=async` means the display server accepts events immediately and queues render jobs. The async worker batches events for `HARN_GIBSON_RENDER_BATCH_MS` milliseconds before asking the renderer for a plan. This avoids slowing the agent, but the renderer must tolerate receiving several events at once.

Before the renderer is called, every batch is normalized into the render-input shape. Each `RenderRequest` receives a `timelineOffsetMs`, a `coalescedCount`, and `metadata.renderBatch` with the batch index, batch size, route, and full timeline. Published scene updates also include the same render-input envelope so replay/debug tools can inspect exactly what the renderer saw.

`HARN_GIBSON_RENDER_TIMING=immediate` is the default playback mode. It applies steps as soon as the plan is processed while still honoring explicit per-step `delayMs`. `HARN_GIBSON_RENDER_TIMING=scheduled` treats `startOffsetMs` as an absolute offset within the render-input timeline, then adds `delayMs`. Scheduled mode is most useful with async rendering: harn can continue immediately while the display plays back a coalesced 5-10 second renderer-agent plan over a matching visual interval.

## Event Interest

A renderer can advertise which normalized events it wants to receive by exposing an `event_interest` attribute. The value may be a `RendererEventInterest`, a mapping with the same fields, or a callable returning either form.

```python
from harn_gibson import RendererEventInterest


class GibsonRenderer:
    event_interest = RendererEventInterest(
        event_types=("tool_call", "tool_result", "runtime_error", "browser_input"),
        fallback_route="direct_scene",
    )
```

Routing precedence is explicit route rules, local stream buffers, then renderer interest. If an event does not match the advertised interest, the router uses the configured fallback route:

- `direct_scene`: apply deterministic local scene mutations without sending the event to the renderer;
- `debug_only`: keep the event out of the renderer and avoid scene mutation;
- `drop`: accept the event without renderer or scene work.

The same shape is accepted as JSON in `HARN_GIBSON_RENDERER_INTEREST` for dogfood runs:

```json
{
  "eventTypes": ["tool_call", "tool_result"],
  "phases": ["before", "after"],
  "excludeEventTypes": ["message_update"],
  "fallbackRoute": "direct_scene",
  "reason": "renderer only wants tool boundaries"
}
```

Explicit route rules have higher precedence than renderer interest and are useful for keeping diagnostics or low-value lifecycle events local during dogfood runs:

```json
[
  {"eventType": "runtime_error", "route": "debug_only", "reason": "keep diagnostics local"},
  {"eventType": "model_select", "route": "drop", "reason": "sample model chatter"}
]
```

Route rules can also sample noisy events before routing them. `sampleEvery` keeps one matching event per N-event window, `sampleOffset` chooses which zero-based position in the sample window is kept, and `fallbackRoute` handles skipped events with `direct_scene`, `debug_only`, or `drop`:

```json
[
  {
    "eventType": "session_tree",
    "route": "renderer_agent",
    "reason": "sample repo snapshots",
    "sampleEvery": 4,
    "sampleOffset": 0,
    "fallbackRoute": "debug_only"
  }
]
```

The same list is accepted as JSON in `HARN_GIBSON_ROUTE_RULES`.

## Context Strategy

The renderer agent should not receive a full new transcript on every event. Use a rolling context:

- Stable project metadata: repo name, current renderer schema, primitive catalog, active display style, and full style-pack palette/backdrop metadata.
- Current renderer state: compact `SceneState` summary, not screenshots.
- Bounded repo topology: project root name, top-level directories/files, and an optional clipped file-tree sample.
- Touched files: recent file paths from harn/tool events or coalesced batches, with operation hints when available.
- World model: durable per-file activity plus structured change, command, health, and outcome facts with provenance, schema `harn-gibson.world-model.v1`.
- Agent attention: inferred current action, objective hint, focus paths/entities, and health focus, schema `harn-gibson.agent-attention.v1`.
- World bindings: optional `props.worldBindings` entries on scene primitives, schema `harn-gibson.world-binding.v1`, declaring which project/world fact a visual property follows.
- Recent harn events: the newest event batch plus short summaries of recent prior events.
- Recent visualization context: recent render intents, render plans, and active animations/effects.
- Visual continuity: compact anchors for currently visible stage objects, active animations, world-binding counts/summaries, recent targets/effects, and style motifs.

The executable fixture for this is `RendererContext`. A renderer that only implements `render(requests, scene)` receives the existing deterministic-compatible call shape. A renderer that implements `render_with_context(requests, scene, context)` receives a `harn-gibson.renderer-context.v1` object with project metadata, bounded repo topology, touched-file summaries, world-model facts, catalog data, scene context, render input, recent agent context, visualization history, visual-continuity anchors, and compaction metadata. `context.project.displayStyle` is the selected style id and `context.project.stylePack` is a `harn-gibson.style-pack.v1` payload with tones, canvas backdrop settings, CSS variables, and motifs.

## World Model

The world model is the first framework-owned perception layer. `RendererContextBuilder` folds normalized harn events plus the current touched-file batch into an event-sourced `harn-gibson.world-model.v1` payload at `context.project.worldModel`. The first version tracks file entities, command entities, change entities, health entities, activity counts, phases, operation hints, source fields, command previews, command start/result pairing when possible, command status/duration, touched command paths, bounded added/removed line counts from structured edit/write/diff fields, test/build health checkpoints derived from command text, last observed outcome, recent tool/runtime outcomes, revision, truncation, and provenance. File, command, change, and outcome facts are observed. Health category is inferred from observed command text, while health status is copied from the observed command state or command outcome.

This is intentionally narrower than the long-term Gibson-world vision. It does not yet model symbols, imports, test-to-code relationships, full semantic agent plans, stale/reconciled facts, or full semantic diffs beyond observed structured tool fields. Those should be added as enrichable perception facts behind the same contract, not as one-off renderer prompt decoration.

`context.project.agentAttention` is the first narrow attention layer. It is inferred from the current render batch, touched files, and world-model health, and it is deliberately labeled as inferred provenance rather than observed task state. The payload includes an `action` such as `verify`, `build`, `edit`, `inspect`, `checkpoint`, `diagnose`, `follow_user`, `respond`, or `command`; an optional objective string; bounded focus paths/entities; optional health focus; and the signals used to infer it. This gives renderers enough context to aim cameras, route packets, or label HUD objectives around what the agent appears to be doing without asking them to re-derive intent from raw event payloads.

After enough events or token growth, do a renderer compaction:

1. Send the full current `SceneState`.
2. Send stable project metadata.
3. Send a compact summary of prior render intent and visual motifs.
4. Reset the short rolling context and continue with new event batches.

The first renderer context is a compaction context. Later contexts are rolling summaries until the configured event interval is reached, at which point the next context includes the full scene again. This gives a future model renderer a predictable place to refresh state without forcing every event batch to resend the whole display state.

`context.visualContinuity` is always compact, even during compaction turns. It includes `anchors` for visible stage primitives, whether each anchor is currently animated, active animation summaries including `timeline_cue` cue labels and `route_trace` waypoint ids/labels, compact `worldBindings` for anchored primitives, a total `worldBindingCount`, recent effects/targets from render-intent history, and style motifs. Use it to preserve visual motifs and avoid recreating the same objects under new ids just because a rolling context omitted the full scene.

This mirrors harn session compaction, but it is separate from the primary agent conversation. The renderer agent owns visual continuity; harn owns task state.

Use `harn-gibson replay --output-render-contexts path.json ...` to inspect the exact renderer contexts produced by a fixture or converted event log. The artifact is `harn-gibson.replay-renderer-contexts.v1` and contains only contexts for steps that actually reached the renderer boundary. It is the quickest way to review model prompt inputs, compaction mode, catalog summaries, repo topology, touched-file batches, accumulated world-model facts, and render-input timing without starting a live model-backed renderer.

Use `harn-gibson replay --output-render-prompts prompts.json --render-prompt-review prompts.html ...` to inspect the provider-neutral system/user messages that a prompt-command or future provider-backed renderer would receive for each captured renderer context. The artifact is `harn-gibson.replay-renderer-prompts.v1`; each prompt is `harn-gibson.renderer-prompt.v1` with message content, context index, mode, event types, routes, timeline metadata, and prompt size. This remains offline and model-free so the prompt contract, context growth, and safety instructions can be reviewed before wiring a live provider adapter.

Use `harn-gibson replay --output-render-chunks chunks.json --render-chunk-size 4 --render-chunk-review chunks.html ...` when a full historical session should be fed to a renderer in pieces. The artifact is `harn-gibson.replay-renderer-chunks.v1`; each chunk includes the original renderer contexts, the exact prompt artifacts for those contexts, context indexes, modes, display styles, event types, routes, request counts, covered timeline, estimated prompt/context characters, and bounded visual-continuity summaries for anchors, active animations, recent effects, recent targets, recent renderers, and style motifs. The HTML review page summarizes those batches and previews the first prompt in each chunk. Single-replay and suite review manifests roll those continuity summaries up so long captures can be scanned from the top-level overview before opening per-chunk JSON.

When the adapter itself needs to be exercised offline, `replay` and `replay-dir` accept explicit `--renderer-model-command` and `--renderer-command` flags. These do not inherit ambient renderer command environment, so ordinary baseline runs stay deterministic while targeted dogfood runs can replay a captured session through the same prompt-command or external render-plan subprocess used by `dogfood`.

Use `harn-gibson replay --output-render-intents intents.json --render-intent-review intents.html ...` to review the renderer decisions that were actually recorded in scene metadata. The JSON artifact is `harn-gibson.replay-render-intents.v1`; the HTML page summarizes renderer, intent text, event types, routes, timeline, effects, targets, mutation counts, and plan metadata. This is intentionally separate from renderer contexts: contexts answer "what did the renderer see?", while render intents answer "what did the renderer decide to do?".

Streaming deltas need special handling before a remote renderer agent is added. `message_update` and similar stream events should update local stream buffers or named text primitives with throttled display refreshes. The renderer agent should receive coarse stream milestones or compact summaries, not every streaming delta as a separate model turn.

Repo topology follows the same rule. The current context includes a bounded top-level directory/file sample from `HARN_GIBSON_PROJECT_ROOT` and a coalesced `touchedFiles` list extracted from path-like event payload fields and command strings. `context.project.worldModel` accumulates touched-file activity, command/change facts, health checkpoints, and outcomes across renderer batches, while `touchedFiles` remains the bounded current batch. `dogfood --cwd PATH` sets that project root to the harn target workspace automatically, and `HARN_GIBSON_PROJECT_NAME` can override the display name. Runtime/auth-looking paths, virtualenvs, env files, caches, and test artifacts are omitted. The deterministic renderer already turns this context into a `repo-map` `node_graph`, a `repo-city` `city_block` mapped from the visible depth-2 repo sample, and, when files are touched, `repo-touch-field` particles plus repo-city extrusion; the hard-coded dogfood renderer also maps the same sample into a `wire_landscape` terrain plane with touched peaks, a `terminal_wall` file panel, an `access_matrix`, and an `orbital_map` uplink. City district and terrain peak height are based on bounded line-count metadata plus visible file/directory counts, while touched paths select and recolor the matching district, child block, peak, panel, access cell, or uplink node. The line counts are numeric metadata only; file contents are not included in renderer context. `city_block.cameraPath` accepts bounded transform keyframes so the browser can add slow camera drift over filesystem districts without changing the underlying scene. A future renderer can use the same context to create richer directory graphs, edited-file pulses, health beacons, terrain flyovers, terminal panel banks, uplink maps, or flythrough paths without receiving file contents or a full repository listing every turn.

Scene primitives can declare how visuals are bound to durable facts without making the browser interpret those facts directly:

```json
{
  "props": {
    "worldBindings": [
      {
        "schema": "harn-gibson.world-binding.v1",
        "entityId": "file:src/repo_map/cli.py",
        "entityKind": "file",
        "fieldPath": "entities.files[].activityCount",
        "targetProp": "blocks[3].h",
        "source": "worldModel",
        "relationship": "scales",
        "intent": "building height follows accumulated file activity"
      }
    ]
  }
}
```

`entityId`, `fieldPath`, and `targetProp` are the required core. `entityKind`, `source`, `relationship`, `intent`, `targetId`, and a bounded `transform` preview are optional. The context builder normalizes and bounds these entries before including them in rolling scene summaries and visual-continuity anchors. Malformed bindings are ignored rather than rejecting a plan; this metadata is for continuity and renderer/back-end interpretation, not for direct code execution.

Shell-command path extraction uses shell tokenization before repo-path normalization so quoted `sed`/`perl` edit programs such as `s/return 2/return 0/` are not mistaken for files. `sed -i` and `perl -i` style commands are also treated as conservative edit signals by the world model, producing bounded `change` facts for the touched files even when exact line deltas are not available.

## Visual Catalog

The renderer interface is generic, but prompts can include a visual catalog. The current default catalog exposes low-level primitives such as `mesh`, `hologram`, `signal_scope`, `tunnel_grid`, `wire_landscape`, `terminal_wall`, `access_matrix`, `orbital_map`, `data_vault`, `black_ice`, `trace_route`, `svg_layer`, `glyph_layer`, `data_rain`, `particle_field`, `city_block`, `ribbon`, and `text_stream`, plus effects such as `glitch`, `signal_interference`, `breach_wave`, `camera_jolt`, `camera_path`, `flythrough`, `extrude`, `packet_burst`, `timeline_cue`, `route_trace`, `vector_trace`, `vector_keyframes`, `typewriter`, and `hold`. The browser currently renders `mesh`, `hologram`, `signal_scope`, `tunnel_grid`, `wire_landscape`, `terminal_wall`, `access_matrix`, `orbital_map`, `data_vault`, `black_ice`, `trace_route`, `svg_layer`, `city_block`, `node_graph`, `ribbon`, `glyph_layer`, `data_rain`, `particle_field`, and `text_stream` scene state. It also renders persistent `SceneAnimation` effects for phase pulses, packet bursts, timeline cues, route traces, scans, glitches, signal interference overlays, breach waves, camera jolts, scene camera paths, flythrough rays, extrusion frames, and hold brackets. The deterministic fallback emits several of those directly, while checked-in replay fixtures exercise the broader set. This keeps useful renderers possible while still giving the Gibson prompt enough raw material for 3D filesystem cities, holographic projections, radar sweeps, vector tunnel flythroughs, wireframe terrain flyovers, terminal walls, access grids, spinning uplink globes, trace-route intrusions, CRT interference, rotating data vaults, black-ICE barriers, route-trace packet windows, vector sigils, data corridors, curated SVG-style symbols, code-rain curtains, and gratuitous animation.

Use `uv run harn-gibson catalog` to inspect that vocabulary without starting the display server. `--kind`, `--tag`, `--id`, and `--compact` can produce smaller slices for prompt fixtures or backend experiments, for example `uv run harn-gibson catalog --kind primitive --tag gibson --compact`.

`city_block` is the structured shortcut for Gibson filesystem districts. Renderer plans provide `blocks`, `heightScale`, optional labels, a `focusBlockId`, and optional `cameraPath` keyframes. The browser samples `cameraPath` with the same numeric transform fields used by vector keyframes: `at` or `timeMs`, `x`, `y`, `scale`, `rotation`, plus `durationMs`, `loop`, and `yoyo`. Fractional `x`/`y` values are viewport-relative drift, which keeps small camera moves stable across desktop and mobile captures.

`hologram` is a structured shortcut for Hollywood projection effects. Renderer plans choose placement, scale, tone, accent tone, opacity, ring count, beam count, floating panels, motes, scan-plane behavior, spin, label, and seed. The browser animates the rings, beams, scan line, panels, and motes locally, which gives a renderer agent a flashy object for 5-10 second plan windows without emitting dozens of low-level shapes.

`signal_scope` is a structured shortcut for radar, oscilloscope, and telemetry instruments. Renderer plans choose `mode`, placement, scale, tone, accent tone, opacity, ring count, spoke count, sweep behavior, sweep speed, blips, waveform traces, label, and seed. Blips can be explicit polar points with `angle` and `radius`, explicit normalized `x`/`y` points, or generated from a count and seed. The browser animates sweep wedges, blip pulses, and waveform motion locally, so a renderer can summarize a coalesced event batch as pings and traces without sending every stream delta to the model.

`tunnel_grid` is a structured shortcut for perspective data corridors and mainframe flythroughs. Renderer plans choose placement, size, ring count, spoke count, lane count, packet count, speed, twist, depth, direction, tone, accent tone, opacity, label, and seed. The browser animates rings, dashed lanes, and packet motes locally, which gives a renderer a single primitive for a 5-10 second traversal effect without emitting per-frame geometry.

`wire_landscape` is a structured shortcut for 90s wireframe terrain, filesystem planes, and repo-topology flyovers. Renderer plans choose placement, size, bounded row/column counts, perspective depth, height scale, peaks with normalized `x`/`z` coordinates, packet count, speed, focus peak, tone, accent tone, opacity, label, and seed. The browser generates the grid, peak influence, rails, packet motion, and labels locally, so a renderer can turn repo topology or touched-file batches into a navigable terrain without sending raw vertex buffers.

`terminal_wall` is a structured shortcut for banks of Hollywood terminal panes. Renderer plans choose placement, size, row/column bounds, panels with titles, bounded line arrays or text content, active/streaming flags, tone, accent tone, opacity, scanlines, cursor flashes, speed, and seed. The browser animates activity meters, scan passes, cursor ticks, and bounded line scrolling locally, so a renderer can summarize 5-10 seconds of command, output, traceback, stream, and touched-file evidence without sending token-sized stream deltas or per-frame text effects.

`access_matrix` is a structured shortcut for lock boards, access grids, module heatmaps, and security status panels. Renderer plans choose placement, row/column bounds, cells with labels, values, active/locked/breached flags, focus cell, tones, opacity, sweep behavior, speed, and seed. The browser animates cell pulses, breach marks, lock glyphs, labels, and scan sweeps locally, so a renderer can turn command phases, touched files, or access decisions into a flashy status board without emitting per-cell animation frames.

`orbital_map` is a structured shortcut for spinning globe uplinks, satellite relays, worldwide scans, and route maps. Renderer plans choose placement, scale, bounded nodes with lat/lon coordinates, bounded arcs, orbit rings, packet count, focus node, tones, opacity, scan beam behavior, speed, label, and seed. The browser animates meridians, parallels, orbit rings, route arcs, packet motes, scan wedges, node pulses, and labels locally, so a renderer can ask for a global uplink beat without managing 3D math or per-frame particles.

`data_vault` is a structured shortcut for rotating mainframe cores, security vaults, and ICE lockboxes. Renderer plans choose placement, scale, tone, accent tone, opacity, nested layer count, ring count, panel count, lock count, packet count, spin, label, and seed. The browser animates the wireframe cube layers, orbiting lock rings, scanner panels, and packet motes locally, giving a renderer one compact primitive for a dramatic access target or breached repository core.

`black_ice` is a structured shortcut for hostile access barriers. Renderer plans choose placement, size, column/row counts, perspective depth, breach amount, breach position, fracture count, sentry-lock count, sweep behavior, tones, opacity, label, and seed. The browser animates scanner shutters, panel jitter, fracture rays, breach rings, and sentry pulses locally, which gives renderer plans one compact object for failed commands, runtime errors, or dramatic access gates.

`trace_route` is a structured shortcut for intrusion paths, command routing, host traversal, or repo flythroughs. Renderer plans choose `hops`, optional curved `links`, `focusHopId`, packet count, speed, tone, accent tone, label, and seed. The browser animates link dashes and packet pulses locally, so the renderer can ask for a visible "route to the Gibson" without managing per-frame particles.

`svg_layer` is intentionally structured vector data, not raw SVG markup. It accepts a `viewBox`, path `d` strings, `rects`, `lines`, `polylines`, `polygons`, circles, labels, small transformed `groups`, named gradients, vector-space trace routes, curated `symbols`, placement, scale, tone, and simple animation hints such as stroke reveal, moving dashes, pulse, spin, group transforms, gradient paint, path morphs, path-trace particles, symbol orbits, and symbol scans. It also accepts bounded numeric transform `keyframes` on the layer or a nested group; each frame can set `at` or `timeMs` plus `x`, `y`, `scale`, `rotation`, and `opacity`, with `durationMs`, `delayMs`, `loop`, and `yoyo` controlling playback. Individual path objects can set bounded `morphs` frames, each with timing plus a `d` string; compatible path strings with the same command/separator structure interpolate numerically, while incompatible strings fall back to discrete frame switching. Root layers and groups can use bounded `filter`/`filters` presets (`glow`, `bloom`, `haze`, `chromatic_split`, `ghost`, `scanline`) and `clip` presets (`rect`, `circle`, `iris`, `wipe`, `scan`) for local Canvas animation without raw CSS filters or SVG masks. Current symbols are `globe`, `filesystem_gate`, `reticle`, `data_tunnel`, `ice_wall`, and `mainframe_core`; they render through Canvas as SVG-style vector assets with animated meridians, packets, scan beams, target pulses, perspective tunnels, cracking ICE panels, and circuit cores. The browser never inserts model-authored markup into the DOM: no scripts, event handlers, `foreignObject`, or external references. The external-renderer validator enforces this boundary for subprocess/model output by rejecting `svg_layer` props that try to carry raw SVG, HTML, external references, unbounded keyframe arrays, or unbounded path morph arrays while warning on unsupported filter/clip presets.

`data_rain` is the equivalent structured shortcut for high-volume glyph motion. Renderer plans choose glyph text, columns, density, speed, direction, tone, accent tone, opacity, optional position/size bounds, trail length, scan bands, glitch amount, and seed. The browser animates the individual glyph columns locally, so a renderer agent can ask for a 10-second telemetry curtain or foreground packet storm without sending thousands of tiny text mutations.

`signal_interference` is a persistent `SceneAnimation` kind for full-scene CRT noise and signal breakup. Renderer plans choose a target for continuity, duration, tone/accent tone, intensity, bounded jitter band count, dropout block count, noise streak count, speed, label, and seed. The browser draws scanlines, horizontal jitter bands, dropout rectangles, burst streaks, and optional warning labels locally, so failed commands or coalesced high-activity windows can look unstable without a renderer model emitting per-line glitch geometry.

`timeline_cue` is a persistent `SceneAnimation` kind for coalesced render windows. Renderer plans choose a target, duration, tone/accent tone, optional placement offsets, and up to 32 cue objects with `at`, `timeMs`, `label`, and optional tone. The browser draws one animated timeline with local cue pulses and active-label state, so a renderer can represent several 5-10 second beats without returning one animation per beat.

`route_trace` is a persistent `SceneAnimation` kind for coalesced render windows that should feel like packets moving through a path. Renderer plans choose a target, duration, tone/accent tone, packet count, tail length, optional label, and up to 24 normalized waypoint objects with `id`, `label`, `x`, `y`, and optional tone. The browser draws dashed route segments, packet heads and tails, pulsing waypoint nodes, and active-label state locally, so a renderer can turn several seconds of agent activity into one continuous traversal effect.

`breach_wave` is a persistent `SceneAnimation` kind for full-scene access, intrusion, or ICE-crack moments. Renderer plans choose a target or normalized `position`, duration, tone/accent tone, intensity, ring count, shard count, optional scan slices, label, and seed. The browser draws expanding rings, radial flash, shards, and scan slices locally, so a renderer can mark one dramatic beat in a scheduled timeline without creating many separate primitives.

`camera_jolt` is a persistent `SceneAnimation` kind for scene-level impact motion. Renderer plans choose a target or normalized `position`, duration, intensity, zoom, roll, and seed. The browser applies the resulting shake/zoom/roll transform while drawing stage primitives, so a renderer can make breach, command, or traversal beats feel physical without modifying each primitive.

`camera_path` is a persistent `SceneAnimation` kind for scene-level pan/zoom/roll keyframes. Renderer plans choose a target or normalized `position`, duration, `loop`, optional `props.yoyo`, and bounded `props.keyframes` with `at`/`timeMs`, `x`, `y`, `scale`, and `rotation`. Fractional `x`/`y` values are viewport-relative, while larger values are treated as device-scaled pixels. The browser composes camera paths with camera jolts, letting a coalesced 5-10 second window keep drifting while impact beats shake the same scene.

## Hook Reuse

The renderer agent should observe the same normalized harn events and hook decisions as the deterministic renderer. Hook decisions are display inputs, not authoritative policy, by the time they reach the renderer. Blocking/interdiction still happens in the harn extension hook dispatcher.
