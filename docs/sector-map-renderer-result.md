# Sector map: the perception-spec rules on today's tools

*Result note for `examples/renderers/claude_gibson_map_renderer.py` (v4), built on
`work/1.1-spatial-bindings` @ 209c94c — the checkpoint where `spatial_map` and
world-model `lifecycle` landed. Companion to `docs/perception-model-spec.md`.*

The question this renderer answers: **how much of the perception-model spec's
"displays are projections" discipline can be enforced with the current toolkit,
before any rework?** Answer: almost all of it — and the exercise sharpens exactly
what the rework should change.

## What it does

One `spatial_map` is the whole stage. Layout is a **radial tree of the `contains`
relation**: repo root at center, directory hubs on an inner ring in angular wedges
sized by file count, files on an outer arc inside their directory's wedge. The
position of every node is a pure function of the sorted path set — no hashes, no
activity ranks, no grid cells. Proximity means siblinghood; the "towers in a line
with no relationship to one another" failure mode is structurally impossible.

Everything dynamic binds to an observed fact:

| visual | bound to |
|---|---|
| node size | `file.activityCount` |
| node lift (z) | change `magnitudeLines` |
| node tone | `lastOutcome` / failing-health status (via spatial_map's own status→tone) |
| node opacity | `lifecycle.recency` (new) — stale facts literally fade |
| confidence ring | provenance (tree-known-but-untouched files render ghosted at 0.7) |
| AGENT node position | `agentAttention.focus.primaryPath` (Gource-style darting cursor) |
| flow edges | latest command's real `touchedPaths` |
| camera | object-addressable `targetRef {path}` against map objects |
| breach / unlock / black-ice | positioned **on the implicated node** by replicating the map's coordinate math |

Coherence: 4 fixed primitives (rain, map, HUD, conditional ice), TTL-pruned
animations, no per-event id minting. Node count only changes when a new file is
genuinely discovered (accretion, not reflow). Verified on both fixtures, all plans
clean.

## What the exercise proved

- **The spec's two rules are implementable today.** "Position from a real
  relation" and "effects target entities" needed no framework change — only the
  discipline. The semantic graph was not needed for any of it: structure came from
  the path tree, activity from the world model. This is the working demonstration
  that deprecating `semanticGraph` loses nothing the display path actually used.
- **`spatial_map` is the right shape.** Typed objects with `entityId`/`path`,
  status→tone, confidence rings, camera anchors by path — the renderer got smaller
  and more declarative than any previous version.
- **`lifecycle` earns its place immediately** — recency→opacity is one line and
  makes staleness visible for free.

## What still required workarounds (rework targets)

1. **Effect anchoring is duplicated math.** To put a breach *on* a node, the
   renderer re-implements `spatialMapPointInRect` (rect + padding + z-lift) in
   Python. Any future drift in that arithmetic silently mislocates every effect.
   Wanted: `position: {"ref": {"path": ...}}` accepted by overlay primitives and
   positioned animations, resolved framework-side — the same resolution the camera
   already does.
2. **Edge styling has one usable alpha.** Inactive edges (0.22) vanish against the
   map's own backdrop grid, so the structural skeleton had to claim `active: true`,
   spending the channel that should distinguish *causal* activity. Wanted: an edge
   `opacity`/`emphasis` prop, or a quieter default grid.
3. **`spatial_map` still doesn't own layout.** `layout` accepts only `grid`/`ring`
   defaults; the renderer supplies every coordinate. That keeps the spec's promise
   ("renderers supply semantics, never coordinates") unmet — and means no animated
   reflow when the tree grows: positions jump instead of settling. The perception
   model's `contains` relation + a framework-owned tree/force layout with object
   constancy is the missing piece, and it's the same gap the physics-history
   reference display needs.
4. **The renderer still assembles the graph itself** — unioning graph file lists
   with world-model paths, deriving directories by splitting strings. Under the
   perception model this is exactly the `entities` + `contains` query the
   framework would serve directly.

## Run it

```bash
uv run harn-gibson replay examples/claude-gibson-replays/long-session-arc.json \
  --renderer-command 'uv run python examples/renderers/claude_gibson_map_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root examples/dogfood-workspaces/repo-map --project-name repo-map \
  --review-dir test-artifacts/iter/cgm/review
```
