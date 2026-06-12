# claude-gibson renderer

A custom external renderer for harn-gibson that aims for a **coherent, cinematic
"fly through the Gibson"** display in the spirit of the 1995 film *Hackers* —
and, crucially, one that **stays coherent over long coding sessions** instead of
slowly burying the screen in orphaned primitives and animations.

- Renderer: [`examples/renderers/claude_gibson_renderer.py`](../examples/renderers/claude_gibson_renderer.py)
- Test fixtures: [`examples/claude-gibson-replays/`](../examples/claude-gibson-replays/)

## Run it

Live, against a real harn session:

```bash
HARN_GIBSON_RENDERER_COMMAND='uv run python examples/renderers/claude_gibson_renderer.py' \
  uv run harn-gibson dogfood
```

Against the bundled long-session arc fixture, with per-step review frames:

```bash
uv run harn-gibson replay examples/claude-gibson-replays/long-session-arc.json \
  --renderer-command 'uv run python examples/renderers/claude_gibson_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root examples/dogfood-workspaces/repo-map --project-name repo-map \
  --review-dir test-artifacts/iter/cg/review
# open test-artifacts/iter/cg/review/index.html
```

## The composition

Six primitives, always present, always in the same place (the "control room"):

| id            | primitive       | role                                                        |
|---------------|-----------------|-------------------------------------------------------------|
| `cg-rain`     | `data_rain`     | faint full-screen code curtain; thickens while streaming    |
| `cg-tunnel`   | `tunnel_grid`   | perspective data corridor behind everything (the flythrough)|
| `cg-city`     | `city_block`    | **hero** — the repo as a neon skyline; touched files glow   |
| `cg-scope`    | `signal_scope`  | radar instrument; touched files become blips                |
| `cg-route`    | `trace_route`   | INPUT → HARN → TOOL → FS → GIBSON intrusion path            |
| `cg-terminal` | `terminal_wall` | live EVENT / COMMAND / FILES / OUTPUT panes                 |

Three persistent looping animations keep the scene breathing between events:
`cg-cam` (camera drift), `cg-route-trace` (packets along the route), `cg-cues`
(timeline markers for the coalesced render window).

A **removable alert overlay** fires only on failures (`*error*`, `isError`,
`severity: error`): `cg-ice` (a black-ICE barrier), plus `cg-breach`,
`cg-jolt`, and `cg-noise` effects, all in red/amber. On the next calm event the
overlay is explicitly torn down, returning to the clean baseline.

Phase drives a consistent palette: `before` → green "ARMING", `during` → cyan
"UPLINK", `after` → magenta "TRACE LOCK", failures → red "ICE BREACH".

## Why it stays coherent over long sessions

The renderer owns a **fixed set of ids** and re-`upsert`s that same set on every
plan (`upsert` replaces by id). The scene's primitive and animation dictionaries
therefore have a hard ceiling regardless of session length. Verified on a
10-event arc (bootstrap → stream → failure → runtime error → recovery): the
final scene contained exactly the 6 fixed primitives + 3 fixed animations, with
the alert overlay fully removed — no per-sequence leakage.

This is deliberately *not* how the bundled deterministic renderer behaves: it
mints `pulse-{sequence}`, `repo-touch-{sequence}`, … per event, so its animation
table grows without bound (measured: 22 animations after 10 events). Avoiding
that is the core design constraint here.

---

## Interface shortcomings & missing features

Findings from building this renderer against the current interface. Ordered
roughly by how much they constrained a *coherent, long-session, cinematic*
display specifically.

### 1. No ephemeral / TTL lifecycle for primitives or animations
Finished non-loop animations stay in `scene.animations` forever; one-shot
primitives stay until explicitly removed. To keep the scene clean I emit four
teardown mutations (`remove`/`stop_animation`) on *every* calm plan. A
`ttlMs` field or an "auto-remove on completion" flag on `SceneAnimation` /
`ScenePrimitive` would let a renderer fire a transient dramatic beat without
manual bookkeeping — and would have prevented the deterministic renderer's
unbounded-animation behavior by construction.

### 2. `position` / `size` are honored inconsistently across primitives
`city_block` ignores `props.position`/`size` entirely — blocks use
viewport-absolute normalized `x`/`y` around a hardcoded center — while
`hologram`, `signal_scope`, `terminal_wall`, and `tunnel_grid` *do* respect
`position`/`size`. Composing a non-overlapping layout is therefore trial and
error. A uniform contract ("every stage primitive lays out inside its
`position`+`size` box; internal coordinates are normalized to that box") would
make layout composable.

### 3. No layout/region system for the canvas
Everything in `region: "stage"` is painted in a fixed kind-priority z-stack over
the full viewport. There is no dock/grid affordance (top strip, right rail,
hero) and non-`stage` regions don't render on the canvas at all. I placed every
primitive with magic numbers and still get intentional-but-busy overlap between
the route and the city. A minimal region/dock layout for stage primitives would
directly serve the "coherent display" goal.

### 4. Command/subprocess renderers can't advertise `event_interest`
Only in-process Python renderers can set the `event_interest` attribute. The
documented dogfood path is a subprocess renderer, which has no way to declare
interest, so the only lever is the `HARN_GIBSON_ROUTE_RULES` env var. As a
result `message_update` is routed `direct` to a local stream buffer and never
reaches the renderer (confirmed: 1 of 10 steps rendered `direct`). A cinematic
renderer can't react to streaming progress without out-of-band env config.

### 5. No stream-milestone event, and the stream primitive is un-styleable
The docs say the renderer should receive "coarse stream milestones," but no such
event is defined or emitted. Meanwhile the harness-owned `assistant-stream`
text primitive appears during streaming and visually collides with
renderer-placed primitives (it overlapped `cg-scope`), with no way for the
renderer to reposition, restyle, or suppress it.

### 6. Touched-file context has no edit magnitude and no cumulative memory
`touchedFiles` is a recent window of paths with optional op hints — there's no
added/removed line count, so the city's building heights can only reflect total
line-count metadata, not the size of a change. And there's no decaying
per-path activity score, so the city can't "remember" where work concentrated
earlier in a long session without the renderer maintaining its own state
(which a stateless subprocess renderer can't). An edit event carrying `+/-`
line deltas, plus a cumulative activity heatmap in context, would unlock
"buildings grow as they're edited" and a persistent heat map.

### 7. `camera_jolt` / `camera_path` `targetId` is effectively decorative
Both compose into a single global scene transform (`sceneCameraState`), so
targeting `cg-city` is accepted but shakes/pans the entire scene. There's no
per-primitive camera, so I can't drift only the hero city while keeping
instruments locked. The contract's `targetId` implies more than it delivers.

### 8. Draw order isn't controllable
Stage z-order is a fixed kind-priority list in the browser; a renderer can't say
"draw the scope above the city." For deliberately layered cinematic
compositions this forces working around the priority list rather than declaring
intent (e.g. a `z` / `layer` prop).

### 9. The "reuse stable ids" pattern is essential but undocumented
Reusing a fixed id set is what makes a long-session renderer safe, but nothing
in the renderer docs states this, and the reference deterministic renderer
models the opposite. A short authoring note ("the scene is persistent; reuse
stable ids; transient effects must be torn down") plus an optional scene-size
budget warning in `renderPlanDiagnostics` would steer authors away from the
accumulation trap.

### What worked well
The primitive/effect vocabulary is genuinely rich and expressive — `city_block`,
`tunnel_grid`, `black_ice`, `breach_wave`, `signal_interference`, and
`camera_jolt` together are more than enough to build the *Hackers* look. The
fail-open validation contract is excellent: every malformed/degenerate input
(empty stdin, junk JSON, missing context) still produced a valid scene, and the
plan validator never silently dropped a well-formed plan. `timeline`/
`timelineOffsetMs` made it straightforward to spread cues across a coalesced
window. The replay + `--review-dir` workflow is a first-class iteration loop.
