# The projection engine: scene = f(projection, perception)

*Design for the presentation layer rebuild. Companion to
`docs/perception-model-implementation.md` (the data layer this projects from).*

## The inversion

Today's information flow puts the renderer in the per-event hot path:

```
events ──> renderer (assigns coordinates, accumulates mutations) ──> scene
```

Everything that has gone wrong across four hand-built renderers — jumping
layouts, free-floating effects, orphaned primitives, duplicated anchoring math —
is a consequence of that flow: the scene is *mutable accumulated state* and
every renderer must re-implement layout, stability, and coherence discipline.

The rebuild inverts it:

```
events ──> perception model (entities / relations / events)
                     │
        projection (a standing, declarative spec)
                     │
              scene = f(projection, perception)      ← engine-owned, re-derived every step
                     │
              theme renderer (browser: tween + draw)
```

The scene is a **derived value**. A *projection* declares which entities to
show, which relation drives layout, how attributes map to visual channels,
which perception events fire which effects, and what theme to draw in. The
engine re-resolves it whenever the perception model changes.

Consequences, each fixing a chronic failure:

- **Renderers never place anything.** Position comes from a layout over a real
  relation. "Towers in a line" is unexpressible.
- **Effects target entity ids.** The engine anchors them wherever the active
  layout put the entity. "Free-floating circles" is unexpressible.
- **Coherence is structural.** Nothing accumulates; the scene is rebuilt from
  facts each step. A 9-hour session cannot leak primitives.
- **Object constancy is engine-owned.** Layouts warm-start from previous
  positions and the browser tweens between resolved scenes, so reflow is an
  animated settle — never a jump.
- **A renderer can be a JSON file.** Different visualizations from the same
  facts = different projection files. The art direction *is* the artifact.
- **Renderer-optional.** With no projection supplied, a default projection
  gives a complete, good display out of the box. Smart defaults all the way
  down; every field is an override.

A live "renderer agent" still has a role — as a *director*, not a puppeteer:
it changes the projection at dramatic moments (swap layout, retarget camera,
retheme) instead of relaying every event. That is a later, thin addition; the
projection itself carries a session unattended.

## The projection spec (`harn-gibson.projection.v1`)

```json
{
  "schema": "harn-gibson.projection.v1",
  "theme": "gibson",
  "layers": [
    {
      "id": "world",
      "select": {"types": ["dir", "file"]},
      "layout": {"kind": "force", "relations": ["contains"]},
      "encode": {
        "size":    {"attr": "touchCount", "range": [0.25, 1.0]},
        "tone":    {"attr": "status", "map": {"error": "alarm", "ok": "good"}},
        "opacity": {"attr": "touchCount", "zero": 0.4},
        "label":   {"attr": "name"}
      },
      "edges": [
        {"relation": "contains", "style": "skeleton"},
        {"relation": "touched", "recent": true, "style": "flow"}
      ]
    },
    {"id": "cursor", "select": {"ids": ["agent"]}, "place": {"near": "$focus"}}
  ],
  "camera": {"follow": "focused_on"},
  "on": [
    {"event": "file_changed",  "effects": [{"kind": "pulse", "target": "$entity", "magnitude": "$churnFraction"}]},
    {"event": "check_completed", "when": {"status": "error"},
     "effects": [{"kind": "alarm"}, {"kind": "breach", "target": "$blast"}, {"kind": "shake"}]},
    {"event": "check_completed", "when": {"status": "ok", "recovers": true},
     "effects": [{"kind": "ring", "target": "$blast", "label": "LOCK RELEASED", "tone": "good"}]},
    {"event": "commit_created", "effects": [{"kind": "ring", "target": "$root", "label": "$subject"}]}
  ]
}
```

Every field is optional; omitted fields take the organic force-layout defaults
above. The pieces:

- **`select`** — which entities a layer shows (`types`, `ids`).
- **`layout`** — engine-owned placement: `radial-tree` (over a relation),
  `force` (springs along relations, seeded + warm-started, deterministic),
  `grid` (sorted by an attr), `ring`. Layouts return normalized positions and
  are pure given (entities, relations, previous positions).
- **`encode`** — attr → channel bindings with normalization *here*, not in the
  perception layer (display scaling is renderer state; the decision doc was
  right about that). Channels: `size`, `tone`, `lift`, `opacity`, `label`,
  `glow`. Tones are **semantic** (`base`, `accent`, `good`, `alarm`, `warn`,
  `ghost`) and resolve to colors per theme.
- **`edges`** — relations drawn as `skeleton` (structure), `flow` (animated
  causality), `beam` (emphasis).
- **`on`** — perception events → effects. Effects are few and entity-targeted:
  `pulse`, `ring`, `beam`, `shake`, `alarm`, `banner`. Target selectors:
  `$entity` (the event's entity), `$blast` (check ← produced ← command →
  touched → files, traversed along real edges), `$focus`, `$root`. All effects
  carry TTLs and prune themselves.
- **`camera`** — follow a relation (`focused_on`) or pin an entity.
- **`theme`** — the entire look: palette, node/edge drawing, backdrop,
  typography, effect styling. `gibson` is neon-noir 90s-hackers; structure is
  identical under any theme.

The engine also derives **mood** (idle / work / verify / alert / recovery from
check entities — the stakes logic every renderer rebuilt by hand) and a default
**HUD** (objective, workspace facts, checks, event ticker), both overridable.

## Where things run

**Python resolves; the browser performs.** `ProjectionEngine.resolve(spec,
perceptionModel, now)` produces a concrete scene — nodes with positions and
channels, edges, active effects, camera target, mood, HUD — shipped as one
`projection_scene` primitive through the existing scene transport. That keeps
resolution deterministic and 100%-coverage testable, and replay review frames
faithful. The browser keeps per-node tween state (exponential approach toward
targets — the "force graph settling" feel), animates effects by elapsed time,
and draws in the active theme. The browser never decides *what* is on screen,
only *how beautifully* it gets there.

`ProjectionSceneRenderer` implements the existing `SceneRenderer` protocol, so
the pipeline, replay, review tooling, and server need no structural change:
enable with `HARN_GIBSON_RENDERER=<path.json|default>` (`default` = built-in projection).

## What this should feel like (UX commitments)

1. **Glanceable.** One dominant spatial view; mood as a global tint; alarms
   unmistakable; HUD thin and quiet.
2. **Continuous.** Nothing teleports. Entities are born small and grow; reflow
   settles; the camera glides.
3. **Story-shaped.** Quiet during routine work; check failures are *events*
   (alarm + breach on the implicated files + shake); recovery is release;
   commits are milestones. The drama budget is spent where the stakes are.
4. **Truthful.** Every visual property is a projection of an observed fact;
   ghosted/dim = stale or untouched; nothing decorative pretends to be data.

## Build order

1. `src/harn_gibson/projection.py` + packaged default JSON —
   spec defaults + `ProjectionEngine`
   (layouts, encodings, effect rules, mood, HUD) + `ProjectionSceneRenderer`.
2. Catalog entry + browser `projection_scene` theme renderer (tween cache,
   gibson theme).
3. Example projections in `examples/projections/`, such as `blueprint-web.json`,
   to prove divergent displays from the same spec language.
4. Replay/watch-replay demo on the long-session arc.

Later: a second polished theme, treemap/timeline layouts, the director op
(`set_projection` mid-session), and offering the engine upstream.
