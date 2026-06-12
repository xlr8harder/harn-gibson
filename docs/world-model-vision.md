# A world-model orientation for harn-gibson

*Notes from building a renderer, for the agent working on the framework.*

This is written after building a cinematic renderer (`claude_gibson_renderer.py`)
against the current interface, getting it coherent over long sessions, and then
hitting the ceiling of what the interface can express. It is deliberately about
**requirements and contracts**, not implementation. The aim is to describe what
a genuinely *meaningful* agent-workflow display needs from the framework, where
today's system can't supply it, and a rough sense of sequencing and tradeoffs.

The short version: the three documented integration points (renderer, primitive,
display backend) are all **presentation**. The thing that's actually missing is
**perception** — a model of what the agent is doing and what the code is — and it
doesn't enter through any of those three points. The visual primitives were never
the bottleneck; the sensorium is.

---

## 1. What I'm trying to achieve

A display that is *about* the agent's work — legible as a journey with stakes —
rather than a dashboard of instruments. The reference is the Gibson sequence in
*Hackers*, but the operative quality isn't "neon city," it's that it made an
abstract process **legible as spatial navigation with narrative tension**: you
flew *to* a place, the system had *structure*, data had *mass and location*, and
there were stakes (approach → barrier → breach → grab the file → escape). Every
glowing thing *meant* something.

Translated to agent workflows, the design principle is:

> The display is a **persistent, navigable model of the codebase, inhabited by
> the agent as a located presence**, where data has mass/type/health, causality
> flows between real objects, and a director-camera narrates the work with stakes
> that track real progress.

The architecture I've concluded toward (and which I think is correct independent
of the final aesthetic):

- **An event-sourced world model that is a fold over the agent event stream.**
  The world model is the reducer's accumulated state; events are the inputs.
- **Filesystem as scaffold and oracle, not as the world.** The tree provides a
  stable coordinate system (so the user can form a durable mental map) and a
  periodic ground-truth to reconcile against — but the agent stream is the spine,
  because the subject is the *work*, not the repo, and the stream is where
  attention, causality, and stakes live.
- **Provenance/confidence as a first-class property** of every fact in the model
  (observed / inferred / assumed / stale, plus last-confirmed time). This both
  keeps the model honest and *is* the aesthetic: staleness and uncertainty render
  as fog-of-war and signal degradation for free.
- **Lazy, progressive detail.** The world materializes around where the agent
  actually goes; you never pay to model the parts of the repo it never touches.

The key consequence for the framework: in this orientation the **visual styling
is downstream and nearly interchangeable.** The same substrate could render as a
Gibson city, a subway map, or a dungeon. The asset worth building is the
substrate, not any one look — which is also the strongest argument for building
it, since it isn't a bet on one piece of eye-candy.

---

## 2. Where the current system can't get me there

### 2.1 The integration points are all downstream of two contracts they can't touch

The pipeline is roughly:

```
harn agent ─▶ extension(normalize) ─▶ EVENT STREAM ─▶ context builder ─▶ CONTEXT ─▶ [renderer] ─▶ mutations ─▶ scene ─▶ [backend] draws [primitives]
                                      └────────── perception (ingest) ──────────┘   └──────────────── presentation ───────────────┘
```

"Renderer," "primitive," and "display backend" all live on the **presentation**
half. None of them can change **the event stream** or **the renderer context** —
the perception half, produced by the extension and the context builder, which are
framework-internal and not plug-in surfaces. So a renderer can only ever encode
what those two contracts already carry, which today is essentially **event phase
and touched paths**. That is why any competent renderer on this toolkit converges
on the same maximalist neon dashboard (the renderer I built ≈ the one Codex
produced): we are both decorating an identical stage with an identical, shallow
input signal.

### 2.2 The primitives are pre-styled set-pieces, so the aesthetic is overdetermined

`signal_scope`, `tunnel_grid`, `black_ice`, `data_vault`, `orbital_map`,
`data_rain` are fully-styled nouns from the movie, each with baked-in animation. A
renderer's real job collapses to "choose set-pieces, arrange them, pick tones."
That's set-dressing, not visualization. The vocabulary is pitched at the altitude
of *widgets/scenes* rather than *a spatial language plus a way to bind meaning to
it*, so renderers can't invent metaphors — only assemble the supplied ones.

### 2.3 The concrete deficits, grouped by layer

**Perception gaps (the ones that actually gate the vision):**

- No semantic model of the codebase: no symbol graph, no imports/calls, no
  test↔code mapping. You can't draw "edited `foo`, which `bar` calls" because that
  structure is never provided.
- No change magnitude: touched files arrive as paths with optional op hints — no
  edit deltas (file + line range + added/removed), so "buildings grow as they're
  edited" is impossible; height can only reflect static line-count.
- No outcome/health state: no test results, build status, or pass/fail signal in
  the stream, even though the agent's own commands produce all of it.
- No agent state: no attention ("now looking at X"), no intent/plan ("subgoal Y,
  step 2 of 5"), no confidence/uncertainty, no disposition (exploring vs editing
  vs debugging vs verifying). This is the single biggest gap — without it the
  display can only ever be a log, never a journey.
- No cumulative memory: `touchedFiles` is a recent window. There's no decaying
  per-path activity, so the world can't "remember" where work concentrated
  earlier in a long session. (`visualContinuity` helps for *visual* objects but
  not for *semantic* activity.)

**Persistence / identity gaps:**

- The renderer is effectively stateless across events: external-command and
  prompt-command renderers get a fresh process per call and must rebuild from
  bounded context every time; the context is explicitly designed to be
  re-sendable/compaction-based *because* it assumes a possibly-stateless model.
  So there is no first-class place to accumulate a world model.
- No durable entity identity: nothing says a given visual object *is*
  `src/cli.py` with a stable identity that survives the whole session and binds
  its properties to that entity's state. Today that mapping lives in the
  renderer's head and is recomputed per event. (Storing world state in the scene
  via stable primitive ids — the trick I used to stay garbage-free — is a
  workaround, not a contract.)

**Presentation/expressiveness gaps (smaller, but real; found while building):**

- No ephemeral/TTL lifecycle: finished animations and one-shot primitives persist
  until explicitly removed, so a renderer must emit teardown mutations every calm
  frame. The bundled deterministic renderer demonstrates the failure mode — it
  mints `pulse-{seq}`/`repo-touch-{seq}` per event and its animation table grows
  unboundedly (22 animations after 10 events). A `ttlMs`/auto-remove would prevent
  this by construction.
- `position`/`size` are honored inconsistently: `city_block` ignores them
  (viewport-absolute coords) while `hologram`/`signal_scope`/`terminal_wall`/
  `tunnel_grid` respect them, so composing a non-overlapping layout is trial and
  error.
- No canvas layout/region system and no z-order control: everything in
  `region:"stage"` is painted in a fixed kind-priority stack over the full
  viewport; non-`stage` regions don't draw on canvas at all.
- Camera is global only: `camera_jolt`/`camera_path` compose into one scene-wide
  transform, so `targetId` is effectively decorative — you can't move/frame a
  specific object.
- Command/subprocess renderers can't advertise `event_interest` (only in-process
  Python renderers can), and there's no stream-milestone event — so a renderer
  can't react to streaming progress without out-of-band env config, and the
  harness-owned stream primitive appears and collides with renderer-placed objects
  with no way to reposition or restyle it.

### 2.4 A cadence mismatch

Per-event, coalesced, fail-open, stateless rendering is excellent for robustness
but fights a *continuous living world*. A flythrough wants a continuous simulation
that events **perturb**, not a sequence of discrete scene replacements. (The
browser backend already runs a continuous animation loop — so this gap is about
the *update contract*, not the renderer's drawing ability.)

---

## 3. What I'd need — requirements, not implementations

Ordered roughly by leverage. Each is a capability/contract, with rationale and the
tradeoff I see.

### R1 — A perception/world-model contract, distinct from presentation
The single most important ask. There should be an explicit place where **derived
world-state** lives — fed primarily by the agent stream, owned by the framework
(not by each renderer), and consumed by renderers as structured input. This is
what lets the visual layer be *about* the work instead of decorating it, and it's
what keeps statelessness and fail-open intact (the framework owns the model;
renderers stay thin and replaceable).
*Tradeoff:* it's new surface area and the framework takes on responsibility for a
model that can drift. Mitigated by R5 (provenance) and by making it optional/
degradable so a cheap mode still produces a usable display.

### R2 — Agent-state signals in the event stream
Attention (current file/symbol), intent/plan (goal → subgoal → step), progress,
confidence/uncertainty, and disposition (exploring/editing/debugging/verifying).
Highest-leverage *and* hardest: some of this may not be exposed by harn today and
may require upstream changes, and some may not be cleanly available at all. The
ask is: surface whatever subset is exposable now, with room to add more over time.
*Tradeoff:* this is the deep, possibly-upstream item; treat the rest of the list
as not blocked on it.

### R3 — Structured change + outcome signals (cheap; mostly already in the exhaust)
Edit deltas as structured data (file + line range + added/removed counts) from the
edit tool's own arguments; reliable tool success/exit-code booleans even without
parsing; a normalized command↔result pairing. Strongly prefer **structured signals
over scraping stdout** — exit codes are cheap and reliable; free-text parsing is
brittle across tools/versions and should be best-effort enrichment, corrected by
true-up.
*Tradeoff:* near-zero cost, high immediate value; this is low-hanging fruit.

### R4 — Semantic codebase context, built incrementally and lazily
A symbol/dependency graph (imports/calls, test↔code), delivered as an
**incrementally-updated, enrichable** structure rather than a per-event recompute:
seed it cheaply, deepen it where the agent is active, reconcile it on idle and on
the agent's own checkpoint commands. It should be **optional and degradable** — if
the scan budget isn't there, the display still works on cheaper signals.
*Tradeoff:* full graphs are expensive on large repos; the incremental+lazy+true-up
approach is what makes it affordable (see §4). Don't make the display *depend* on
the full graph existing.

### R5 — Provenance and confidence as first-class fields
Every derived fact carries its source (observed/inferred/assumed/stale) and a
last-confirmed timestamp. Rationale: enables honest rendering, enables true-up
(you know what to re-check), and turns uncertainty into an aesthetic asset rather
than a hidden liability.
*Tradeoff:* a little extra bookkeeping on every fact; pays for itself immediately.

### R6 — Renderer-owned persistent state / durable entity identity
A way to maintain a world model across events without re-deriving from bounded
context each call, and to refer to domain entities by stable identity (this file,
this symbol) that survives the session and binds visual properties to entity
state. Note: if R1 is met (framework owns the world model), this is largely
subsumed — renderers reference entity ids from the shared model. If R1 is *not*
met, this can only really be served by in-process renderers holding their own
state, which couples the vision to one renderer mode.
*Tradeoff:* argues for R1 over per-renderer state, to preserve fail-open and
replaceability.

### R7 — A continuous-world update contract (events as impulses)
Let the update contract treat events as perturbations of a persistent world rather
than discrete scene replacements. The backend already animates continuously; this
is about not forcing renderers to express continuity as a stream of full redraws.
*Tradeoff:* lower priority; it mostly shapes ergonomics, and the stable-id
re-upsert pattern approximates it today.

### R8 — A lower-level spatial + binding vocabulary
So the aesthetic stops being overdetermined: positioned typed objects, edges, an
object-addressable camera, and **bindings** (an object property ← a world-model
field), instead of pre-styled set-pieces. This is the one requirement that sits
*squarely inside the existing "primitive" integration point* — it's the cheapest
win and the part I can prototype myself once there's a world model (R1) to bind to.
*Tradeoff:* without R1–R5 it's still just prettier decoration; with them it's the
whole game. Sequence it *after* there's something to bind to.

### R9 — Presentation-contract cleanups
Independently useful regardless of the larger vision: TTL/auto-remove lifecycle; a
consistent "lay out inside position+size box" contract for every stage primitive;
region/layout + z-order control; object-addressable camera; `event_interest` for
command renderers plus a coarse stream-milestone event; and either renderer access
to the shell/HTML regions or, at minimum, control over their placement so
renderer-drawn objects and shell-owned panels stop colliding.
*Tradeoff:* small, well-scoped, shippable in isolation; good momentum work.

---

## 4. Practical limitations, tradeoffs, and sequencing

**Cost.** Test/health/graph scans are expensive and can't run per-event. The
answer (which makes R3/R4 affordable) is an **event-sourced model**: a cheap
baseline, deltas derived from the agent's *own* command output (it already paid
for those scans), and reconciliation that happens **opportunistically** — harvest
the agent's own `git status`/full-test runs as free true-ups, and do expensive
work on **idle** (agents pause constantly; that's free compute). Keep perception
optional and degradable so a low-budget mode still yields a coherent display.

**Reliability.** The agent stream is an *unreliable narrator* — it shows what the
agent did and *believes*, not necessarily what's true. For pure observability that's
a liability; for a POV display it's arguably an asset (the belief-vs-truth gap, made
visible when a true-up *snaps* the world back, is a strong narrative beat). R5
(provenance) + R4 reconciliation are what keep it honest without flattening the
drama. Worth a deliberate decision about which framing the framework is optimizing
for.

**Don't sacrifice the good properties.** Fail-open, coalescing, and statelessness
are genuine strengths — the renderer survived every malformed/degenerate input I
threw at it, and that matters. A world model must *preserve* these, which is the
main reason R1 puts the model in the framework rather than in every renderer.

**Don't over-fit to the aesthetic.** R1–R5 are style-agnostic; they'd serve a
subway map or an honest debugger as well as a Gibson city. That's the point: build
the substrate as infrastructure, not as a feature of one visualization.

**Suggested sequencing** (value early, deep items not blocking):

1. **R3 + R9** — cheap, already-in-the-exhaust signals and contract cleanups.
   Immediate improvement, no architectural commitment.
2. **R1 + R5** — stand up a framework-owned world model with provenance, even if
   it starts thin (touched files + edit deltas + exit-code health). This is the
   pivot; everything else compounds on it.
3. **R4** — incremental semantic graph feeding the model, lazy and degradable.
4. **R8** — spatial/binding primitives, now that there's a model to bind to. (I
   can take this on at the primitive integration point.)
5. **R2** — agent-state signals, in whatever subset harn can expose; the deepest
   and possibly upstream item, deliberately last so nothing else waits on it.
6. **R7** — continuous-world update ergonomics, as polish.

The throughline: the set-pieces were never the bottleneck. Give the renderer a
**world model fed by the agent stream, with provenance and reconciliation**, plus
a **spatial/binding vocabulary** to express it, and the specific look — Gibson or
otherwise — almost falls out, and is finally *about the work* instead of about the
widgets.
