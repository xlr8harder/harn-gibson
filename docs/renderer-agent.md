# Renderer Agent

The renderer agent is the model-driven version of the current deterministic renderer. It receives harn events, current scene state, and recent visualization context, then returns a `RenderPlan`.

There is no model-backed renderer agent in the current implementation. The server uses the deterministic renderer by default while we harden the event, scene, and browser fixtures. Dogfood runs can also use an external renderer command as a process-backed adapter for future model calls.

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

## External Renderer Command

Set `HARN_GIBSON_RENDERER_COMMAND` to run a renderer as a subprocess. The command is parsed as a shell-style command string, or as a JSON array of argv strings when the value starts with `[`. `HARN_GIBSON_RENDERER_TIMEOUT_MS` controls the per-render timeout.

```bash
HARN_GIBSON_RENDERER_COMMAND='uv run python examples/renderers/gibson_echo_renderer.py' \
HARN_GIBSON_RENDERER_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood --style neon-noir
```

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

The same list is accepted as JSON in `HARN_GIBSON_ROUTE_RULES`.

## Context Strategy

The renderer agent should not receive a full new transcript on every event. Use a rolling context:

- Stable project metadata: repo name, current renderer schema, primitive catalog, active display style, and full style-pack palette/backdrop metadata.
- Current renderer state: compact `SceneState` summary, not screenshots.
- Bounded repo topology: project root name, top-level directories/files, and an optional clipped file-tree sample.
- Touched files: recent file paths from harn/tool events or coalesced batches, with operation hints when available.
- Recent harn events: the newest event batch plus short summaries of recent prior events.
- Recent visualization context: recent render intents, render plans, and active animations/effects.

The executable fixture for this is `RendererContext`. A renderer that only implements `render(requests, scene)` receives the existing deterministic-compatible call shape. A renderer that implements `render_with_context(requests, scene, context)` receives a `harn-gibson.renderer-context.v1` object with project metadata, bounded repo topology, touched-file summaries, catalog data, scene context, render input, recent agent context, visualization history, and compaction metadata. `context.project.displayStyle` is the selected style id and `context.project.stylePack` is a `harn-gibson.style-pack.v1` payload with tones, canvas backdrop settings, CSS variables, and motifs.

After enough events or token growth, do a renderer compaction:

1. Send the full current `SceneState`.
2. Send stable project metadata.
3. Send a compact summary of prior render intent and visual motifs.
4. Reset the short rolling context and continue with new event batches.

The first renderer context is a compaction context. Later contexts are rolling summaries until the configured event interval is reached, at which point the next context includes the full scene again. This gives a future model renderer a predictable place to refresh state without forcing every event batch to resend the whole display state.

This mirrors harn session compaction, but it is separate from the primary agent conversation. The renderer agent owns visual continuity; harn owns task state.

Use `harn-gibson replay --output-render-contexts path.json ...` to inspect the exact renderer contexts produced by a fixture or converted event log. The artifact is `harn-gibson.replay-renderer-contexts.v1` and contains only contexts for steps that actually reached the renderer boundary. It is the quickest way to review model prompt inputs, compaction mode, catalog summaries, repo topology, touched-file batches, and render-input timing without starting a live model-backed renderer.

Streaming deltas need special handling before a remote renderer agent is added. `message_update` and similar stream events should update local stream buffers or named text primitives with throttled display refreshes. The renderer agent should receive coarse stream milestones or compact summaries, not every streaming delta as a separate model turn.

Repo topology follows the same rule. The current context includes a bounded top-level directory/file sample and a coalesced `touchedFiles` list extracted from path-like event payload fields and command strings. Runtime/auth-looking paths such as `.harn`, `.venv`, `.env`, `auth.json`, caches, and test artifacts are omitted. The deterministic renderer already turns this context into a `repo-map` `node_graph`, a `repo-city` `city_block` mapped from the visible depth-2 repo sample, and, when files are touched, `repo-touch-field` particles plus repo-city extrusion. City district height is based on visible file/directory counts, while touched paths select and recolor the matching district or child block. A future renderer can use the same context to create richer directory graphs, edited-file pulses, or flythrough paths without receiving file contents or a full repository listing every turn.

## Visual Catalog

The renderer interface is generic, but prompts can include a visual catalog. The current default catalog exposes low-level primitives such as `mesh`, `svg_layer`, `glyph_layer`, `particle_field`, `city_block`, `ribbon`, and `text_stream`, plus effects such as `glitch`, `flythrough`, `extrude`, `packet_burst`, `vector_trace`, `typewriter`, and `hold`. The browser currently renders `mesh`, `svg_layer`, `city_block`, `node_graph`, `ribbon`, `glyph_layer`, `particle_field`, and `text_stream` scene state. It also renders persistent `SceneAnimation` effects for phase pulses, packet bursts, scans, glitches, flythrough rays, extrusion frames, and hold brackets. The deterministic fallback emits several of those directly, while checked-in replay fixtures exercise the broader set. This keeps useful renderers possible while still giving the Gibson prompt enough raw material for 3D filesystem cities, vector sigils, data corridors, curated SVG-style symbols, and gratuitous animation.

`svg_layer` is intentionally structured vector data, not raw SVG markup. It accepts a `viewBox`, path `d` strings, `rects`, `lines`, `polylines`, `polygons`, circles, labels, small transformed `groups`, named gradients, vector-space trace routes, curated `symbols`, placement, scale, tone, and simple animation hints such as stroke reveal, moving dashes, pulse, spin, group transforms, gradient paint, path-trace particles, symbol orbits, and symbol scans. Current symbols are `globe`, `filesystem_gate`, and `reticle`; they render through Canvas as SVG-style vector assets with animated meridians, packets, scan beams, and target pulses. The browser never inserts model-authored markup into the DOM: no scripts, event handlers, `foreignObject`, or external references. Fuller SVG-style features such as masks, filters, and timed morphs remain future catalog work if they prove useful without making renderer prompts brittle.

## Hook Reuse

The renderer agent should observe the same normalized harn events and hook decisions as the deterministic renderer. Hook decisions are display inputs, not authoritative policy, by the time they reach the renderer. Blocking/interdiction still happens in the harn extension hook dispatcher.
