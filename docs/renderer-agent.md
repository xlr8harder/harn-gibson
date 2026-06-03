# Renderer Agent

The renderer agent is the model-driven version of the current deterministic renderer. It receives harn events, current scene state, and recent visualization context, then returns a `RenderPlan`.

There is no model-backed renderer agent in the current implementation. The server uses the deterministic renderer while we harden the event, scene, and browser fixtures.

## Render Plan Contract

```json
{
  "steps": [
    {
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

## Blocking Vs Async

`HARN_GIBSON_RENDER_MODE=blocking` means the display server applies the render plan before responding to harn. This is easier to reason about and guarantees the scene has acknowledged the event.

`HARN_GIBSON_RENDER_MODE=async` means the display server accepts events immediately and queues render jobs. The async worker batches events for `HARN_GIBSON_RENDER_BATCH_MS` milliseconds before asking the renderer for a plan. This avoids slowing the agent, but the renderer must tolerate receiving several events at once.

## Context Strategy

The renderer agent should not receive a full new transcript on every event. Use a rolling context:

- Stable project metadata: repo name, current renderer schema, primitive catalog, active display style.
- Current renderer state: compact `SceneState` summary, not screenshots.
- Recent harn events: the newest event batch plus short summaries of recent prior events.
- Recent visualization context: recent render plans and active animations/effects.

After enough events or token growth, do a renderer compaction:

1. Send the full current `SceneState`.
2. Send stable project metadata.
3. Send a compact summary of prior render intent and visual motifs.
4. Reset the short rolling context and continue with new event batches.

This mirrors harn session compaction, but it is separate from the primary agent conversation. The renderer agent owns visual continuity; harn owns task state.

Streaming deltas need special handling before a remote renderer agent is added. `message_update` and similar stream events should update local stream buffers or named text primitives with throttled display refreshes. The renderer agent should receive coarse stream milestones or compact summaries, not every streaming delta as a separate model turn.

## Hook Reuse

The renderer agent should observe the same normalized harn events and hook decisions as the deterministic renderer. Hook decisions are display inputs, not authoritative policy, by the time they reach the renderer. Blocking/interdiction still happens in the harn extension hook dispatcher.
