# Renderer Agent

The renderer agent is the model-driven version of the current deterministic renderer. It receives harn events, current scene state, and recent visualization context, then returns a `RenderPlan`.

There is no model-backed renderer agent in the current implementation. The server uses the deterministic renderer while we harden the event, scene, and browser fixtures.

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

## Blocking Vs Async

`HARN_GIBSON_RENDER_MODE=blocking` means the display server applies the render plan before responding to harn. This is easier to reason about and guarantees the scene has acknowledged the event.

`HARN_GIBSON_RENDER_MODE=async` means the display server accepts events immediately and queues render jobs. The async worker batches events for `HARN_GIBSON_RENDER_BATCH_MS` milliseconds before asking the renderer for a plan. This avoids slowing the agent, but the renderer must tolerate receiving several events at once.

Before the renderer is called, every batch is normalized into the render-input shape. Each `RenderRequest` receives a `timelineOffsetMs`, a `coalescedCount`, and `metadata.renderBatch` with the batch index, batch size, route, and full timeline. Published scene updates also include the same render-input envelope so replay/debug tools can inspect exactly what the renderer saw.

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

- Stable project metadata: repo name, current renderer schema, primitive catalog, active display style.
- Current renderer state: compact `SceneState` summary, not screenshots.
- Bounded repo topology: project root name, top-level directories/files, and an optional clipped file-tree sample.
- Touched files: recent file paths from harn/tool events or coalesced batches, with operation hints when available.
- Recent harn events: the newest event batch plus short summaries of recent prior events.
- Recent visualization context: recent render intents, render plans, and active animations/effects.

The executable fixture for this is `RendererContext`. A renderer that only implements `render(requests, scene)` receives the existing deterministic-compatible call shape. A renderer that implements `render_with_context(requests, scene, context)` receives a `harn-gibson.renderer-context.v1` object with project metadata, bounded repo topology, touched-file summaries, catalog data, scene context, render input, recent agent context, visualization history, and compaction metadata.

After enough events or token growth, do a renderer compaction:

1. Send the full current `SceneState`.
2. Send stable project metadata.
3. Send a compact summary of prior render intent and visual motifs.
4. Reset the short rolling context and continue with new event batches.

The first renderer context is a compaction context. Later contexts are rolling summaries until the configured event interval is reached, at which point the next context includes the full scene again. This gives a future model renderer a predictable place to refresh state without forcing every event batch to resend the whole display state.

This mirrors harn session compaction, but it is separate from the primary agent conversation. The renderer agent owns visual continuity; harn owns task state.

Streaming deltas need special handling before a remote renderer agent is added. `message_update` and similar stream events should update local stream buffers or named text primitives with throttled display refreshes. The renderer agent should receive coarse stream milestones or compact summaries, not every streaming delta as a separate model turn.

Repo topology follows the same rule. The current context includes a bounded top-level directory/file sample and a coalesced `touchedFiles` list extracted from path-like event payload fields and command strings. Runtime/auth-looking paths such as `.harn`, `.venv`, `.env`, `auth.json`, caches, and test artifacts are omitted. The deterministic renderer already turns this context into a `repo-map` `node_graph` and, when files are touched, a `repo-touch-field` particle burst. A future renderer can use the same context to create richer directory graphs, edited-file pulses, or flythrough paths without receiving file contents or a full repository listing every turn.

## Visual Catalog

The renderer interface is generic, but prompts can include a visual catalog. The current default catalog exposes low-level primitives such as `mesh`, `glyph_layer`, `particle_field`, `city_block`, `ribbon`, and `text_stream`, plus effects such as `glitch`, `flythrough`, `extrude`, `packet_burst`, `typewriter`, and `hold`. The browser currently renders `mesh`, `city_block`, `node_graph`, `ribbon`, `glyph_layer`, `particle_field`, and `text_stream` scene state. The deterministic fallback emits several of those directly, while checked-in replay fixtures exercise the broader set. This keeps useful renderers possible while still giving the Gibson prompt enough raw material for 3D filesystem cities, data corridors, and gratuitous animation.

An explicit SVG/vector primitive probably belongs in this catalog later, but it should be a constrained `svg_layer` or structured vector primitive rather than arbitrary model-authored browser markup. The safe version would allow declarative paths, groups, labels, gradients, and selected filters while rejecting scripts, event handlers, `foreignObject`, and external references. Useful SVG-style effects include stroke-dash path reveal, scan masks, transform loops on groups, glitch/filter pulses, path-following particles, and timed morphs between compatible paths. That should wait until we know where the balance belongs between prefilled renderer affordances and low-level primitives the model can compose itself.

## Hook Reuse

The renderer agent should observe the same normalized harn events and hook decisions as the deterministic renderer. Hook decisions are display inputs, not authoritative policy, by the time they reach the renderer. Blocking/interdiction still happens in the harn extension hook dispatcher.
