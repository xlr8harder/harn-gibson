# Building on the world model: result and what's still needed

*Follow-up to `world-model-vision.md`, after Codex shipped `world_model.py`.*

Codex landed the first three things the vision asked for, and I rebuilt the
renderer on top of them to test the central claim — *does meaningful data lift
the same primitives out of decoration?* It does. This note records what got
unblocked, the new renderer, and the gaps that remain.

## What landed (and maps to which requirement)

- **R1 — framework-owned world model.** `context.project.worldModel`, schema
  `harn-gibson.world-model.v1`, event-sourced, bounded by the framework. This is
  the pivot the whole vision depended on.
- **R3 — structured change/command/outcome facts.** Durable **file entities**
  (cumulative `activityCount`, `lastOutcome`, recency), **command entities**
  (`status`, `touchedPaths`, `durationMs`), **change entities** (`addedLines`/
  `removedLines`/`magnitudeLines`, `planned`/`observed`/`ok`/`error`), and
  `recentOutcomes`.
- **R5 — provenance.** Every fact carries `provenance` (observed vs inferred +
  confidence). Health is correctly marked `inferred` at 0.85.

R2 (agent attention/intent), R4 (semantic graph), R8 (spatial/binding
primitives), and R9 (presentation cleanups) are not in yet — as expected; the doc
flagged those as the deeper items.

## The renderer: `examples/renderers/claude_gibson_world_renderer.py`

Same primitive vocabulary as before, but **every visual property is now bound to
a world-model fact** instead of to event phase:

| visual | bound to | so it means |
|---|---|---|
| building exists | a `file` entity | the agent has touched this file (world accretes) |
| building position | deterministic FNV hash of path | a given file is always in the same spot (stable map) |
| building height | `activityCount` + change `magnitudeLines` | how much work happened there |
| building color | `lastOutcome` / failing-health membership | red=erroring, green=confirmed-ok, cyan, white=stale |
| building focus | recency (`lastSequence`) | "you are here" |
| route packets | latest command's `touchedPaths` | real causality — flow runs to the files it touched |
| global stakes | `test`/`build` health status | ICE on TESTS RED; one-shot UNLOCK on recovery |
| HUD "INFERRED" tag | `provenance.source` + `confidence` | honest about what's observed vs inferred |

### What this fixed

- **"Colors shift randomly each step" is gone.** Color encoded `phase` before, which
  oscillates `before→after` every tool call. It now encodes *health*, which changes
  rarely and meaningfully — the city goes red→green when tests actually recover,
  and holds otherwise.
- **The world is no longer amnesiac.** The skyline accretes across the session;
  `cli.py`/`test_cli.py` grow to `activityCount=4` and persist in fixed positions
  instead of being redrawn from the latest batch.
- **Stakes are earned.** The ICE/breach fires because tests are *actually* failing
  and centers on the implicated files; the green UNLOCK fires because they
  recovered. (Verified on the bootstrap → fail → runtime-error → recovery arc:
  the same buildings go red at the failing step and green at recovery.)

This is the concrete evidence for the vision doc's claim: the set-pieces were
never the bottleneck. The *same* primitives that produced decoration in the
phase-driven renderer produce a display that's about the work once they're bound
to a world model.

Coherence is preserved: 6 fixed primitives + 3 fixed animations, alert overlay
torn down on recovery, no per-event id minting. All plans validated clean (no
`renderPlanDiagnostics`).

Run it:

```bash
uv run harn-gibson replay examples/claude-gibson-replays/long-session-arc.json \
  --renderer-command 'uv run python examples/renderers/claude_gibson_world_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root examples/dogfood-workspaces/repo-map --project-name repo-map \
  --review-dir test-artifacts/iter/cgw/review
```

## What I still want (and why each is now the bottleneck)

The remaining items are exactly the ones that the world-model rebuild made me feel
the *absence* of:

1. **R4 — a semantic graph (imports/calls, test↔code).** Two concrete needs:
   - **Causality should run along real edges.** Today the route connects HARN → the
     files a command touched. With a call/import graph it could connect *the edited
     function → its callers → the failing test that exercises it* — the actual
     blast radius, which is the real drama.
   - **City layout should cluster by module, not scatter by hash.** I hash paths to
     grid cells for spatial stability; a real directory/dependency structure would
     give a *meaningful* skyline (districts = packages) instead of a stable-but-
     arbitrary one. This is the single biggest visual upgrade available.

2. **R2 — agent attention/intent.** The display can show *what happened* but not
   *what the agent is trying to do*. With intent/plan/attention I could: point the
   camera at the file the agent is about to edit (not just the last one it
   touched), show the current subgoal as a HUD objective, and render
   confidence/uncertainty. This is what turns "a readout of state" into "a journey."

3. **R8 — spatial + binding primitives.** I'm hand-rolling: an FNV hash with
   linear-probe collision handling for layout, and per-property tone logic in
   Python. A primitive that took *typed world objects with stable ids and declared
   bindings* (height←field, color←field, position←layout) would delete most of this
   renderer and make the mapping legible and reusable. This is the cheapest win and
   the one inside the existing "primitive" integration point — worth doing once R4
   gives it real structure to lay out.

4. **R9 — presentation cleanups still apply.** Most-wanted here: an
   object-addressable camera (so I can frame the failing district rather than shake
   the whole scene) and a TTL/auto-remove lifecycle (so the UNLOCK one-shot tears
   itself down instead of needing a `stop_animation` on the next plan).

## One perception-layer nit for the world model

On a `sed -i 's/return 2/return 0/' src/repo_map/cli.py` step, the touched-file
extractor parsed fragments of the sed expression as file paths (`s/return`,
`2/return`), and no `change` entity was produced for the actual edit (the sed
wasn't recognized as a write, so `changes` stayed empty for that step). The
renderer filters the junk paths defensively, but it's worth tightening upstream:
path extraction from shell commands is over-eager, and `sed`/`perl -i` style
in-place edits are a real edit signal the change-delta layer currently misses.
Lower priority than R2/R4, but it's the kind of thing that erodes trust in the
"observed" provenance label.

## Net

With R1+R3+R5 in, the perception core is real and sufficient to make a display
that's *about the work* — demonstrated. R4 (semantic graph) is now the highest-
leverage next step for the visualization specifically, R2 (agent intent) is the
thing that would make it a *journey*, and R8 would make renderers that consume all
of it small and declarative instead of hand-rolled.

---

## Addendum — `work/1.1-spatial-bindings` @ c843761

Codex then landed almost the entire remaining list on the 1.1 branch. Verified
populating on the arc fixture and exercised by a third renderer,
`examples/renderers/claude_gibson_semantic_renderer.py`:

- **R4 — `context.project.semanticGraph`** (`harn-gibson.semantic-repo-graph.v1`):
  repoRoot→package→file→symbol nodes with `contains`/`defines`/`imports`/`tests`
  edges, each carrying provenance. The renderer now clusters the city into **package
  districts** and draws **import + test→code edges between the towers** (the
  green `tests` edge from `test_cli.py` to `cli.py` shows the real link). Files
  known from the graph but untouched render as ghosted "dormant" buildings.
- **R2 — `context.project.agentAttention`** (`harn-gibson.agent-attention.v1`):
  inferred `action` (e.g. `verify`/`edit`/`respond`), `focus.primaryPath`, focus
  entities, inferred provenance. The renderer aims the camera at `focus.primaryPath`
  and shows the action/objective as a HUD objective line.
- **R8 (metadata) — `props.worldBindings`** (`harn-gibson.world-binding.v1`):
  declared on the city (`activityCount`→height, `lastOutcome.status`→tone). Bounded
  and surfaced in `visualContinuity`. (The executable `spatial_map` primitive is
  still in flight and intentionally untouched here.)
- **R9 — animation `ttlMs`/`expiresAtMs` + object-addressable camera**: transient
  breach/jolt/interference beats now carry a TTL and the framework prunes them — no
  more manual `stop_animation` bookkeeping (confirmed: only the persistent camera +
  the recovery beat survived in the final scene). `camera_jolt`/`camera_path`
  `props.targetRef` resolves `{"path": ...}` against `city_block` blocks (so blocks
  need a `path` prop for object framing — worth noting in the contract).
- **Perception nit fixed**: `sed`/`perl -i` programs are no longer mis-parsed as
  file paths, and in-place edits now emit conservative `change` facts.

All plans validate clean (no `renderPlanDiagnostics`), coherence holds (6 fixed
primitives, TTL-pruned animations). This checkpoint makes the city finally reflect
*real repo structure and dependencies* rather than a stable-but-arbitrary hash
layout — the single biggest visual upgrade called out in the original list. The
remaining open item is the in-progress `spatial_map` primitive (declarative R8
layout), which I'll test against once it's pushed.
