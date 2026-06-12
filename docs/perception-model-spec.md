# Perception model: a temporal entity–relation graph

*Design spec for harn-gibson's perception layer. For the framework agent.*

This supersedes the "world model + semantic graph" direction. It generalizes the
existing `worldModel` into a single model that **all displays are projections
of** — a situation board, a directory treemap, and a cinematic city are then just
different *queries + layouts* over the same source of truth, never separate
implementations that can disagree.

## Governing principle

> **The perception layer observes *activity* and *structure*. It never interprets
> *code*.**

Two consequences:

1. **Structure comes from the tree, not from parsing.** Directory/file hierarchy
   (`contains`) is the filesystem/git tree — cheap, language-agnostic, and never
   "broken." This is fine and stays.
2. **Meaning is never derived by parsing file contents.** No AST, no import/symbol
   extraction, no test-to-code inference. That work is expensive, language-specific,
   and — critically — it parses *possibly-broken code* during exactly the moments
   we visualize (the agent constantly leaves files in half-written, non-parsing
   states). It is most likely to be wrong precisely when we're watching most
   closely. **The `semanticGraph` source is therefore deprecated** (see below).

Everything the model knows is either *observed activity* (what commands ran, what
they touched, what outcomes occurred, where attention is) or *observed structure*
(the directory tree, commit history) — from two sources only: **git** and the
**event stream**.

## The model

Three first-class collections, each fact carrying `provenance`
(`observed`/`inferred`/`stale`, confidence, last-confirmed seq/ts — the existing
`worldModel` provenance shape, reused).

### Entities

Typed nodes with a stable id and an attribute bag.

```json
{
  "id": "file:src/repo_map/cli.py",
  "type": "file",
  "attrs": { "sizeLines": 120, "activity": 4, "lastSeenSeq": 9, "health": "error", "changeMagnitude": 7 },
  "provenance": { "source": "observed", "confidence": 1.0, "lastConfirmedSeq": 9 }
}
```

Types (v1): `file`, `dir`, `command`, `check` (a test/build run), `commit`,
`agent` (the singleton "cursor"). *Dropped from the old plan: `symbol`* (parsing).

### Relations

Typed, directed edges with provenance. This is the piece the current `worldModel`
lacks as a first-class, queryable thing — today containment is implicit in path
strings, causality is an ad-hoc `touchedPaths` list, and time is implicit in
`sequence`.

```json
{ "type": "contains",    "from": "dir:src/repo_map", "to": "file:src/repo_map/cli.py", "provenance": {"source":"observed"} }
{ "type": "touched",     "from": "command:9",        "to": "file:src/repo_map/cli.py", "provenance": {"source":"observed"} }
{ "type": "produced",    "from": "command:9",        "to": "check:test:9" }
{ "type": "focused_on",  "from": "agent",            "to": "file:src/repo_map/cli.py", "provenance": {"source":"inferred","confidence":0.7} }
```

Relation types (v1): `contains` (tree), `touched` (command→file), `produced`
(command→check), `focused_on` (agent→entity). *Dropped: `depends_on` / `tested_by`*
(those required parsing).

### Event log

Time-ordered transitions. Each event names the entities/relations it touched and
the attribute changes it caused. This *is* the timeline and the canonical "what
changed when."

```json
{ "seq": 9, "ts": 24500, "kind": "tool_result", "status": "ok",
  "entities": ["command:9", "file:src/repo_map/cli.py", "check:test:9"],
  "changes": [{ "entity": "file:src/repo_map/cli.py", "attr": "health", "to": "ok" }],
  "provenance": { "source": "observed", "confidence": 1.0 } }
```

## Sources → facts (two only)

Neither source interprets code; both report facts.

| source | entities | relations | attrs / events |
|---|---|---|---|
| **git** | `dir`, `file` (from tree), `commit` | `contains` (tree) | `sizeLines` (wc), `touched` + `changeMagnitude` (`diff --numstat`), commit history → `precedes`, working-tree dirty/clean |
| **event stream** | `command`, `check`, `agent` | `touched` (cmd→file), `produced` (cmd→check), `focused_on` (attention) | command status/duration, `check` red/green (from exit code, not parsing), outcomes → events |

Concretely on the git side: `git ls-files` / tree walk → `contains`; `git diff --numstat`
→ `touched` + line deltas (authoritative — kills the `sed`/`perl` mis-parsing
class of bug entirely); `git log` → `commit` entities + temporal order; exit codes
of test/build commands → `check` status (no output parsing required).

The renderer never knows which source a fact came from. git is the cheap,
language-agnostic backbone; the event stream is the live driver; there is no third
source.

## Displays are projections (renderers project, never place)

**The point of the model is that radically different displays can be driven from
the exact same facts.** This isn't just a feature — it's the *test* of whether the
abstraction is right. Keep a deliberately divergent set of reference displays in
mind as a design instrument: if a status board, a treemap, a 3D city, and an
organic physics graph can *all* be driven from the model with no view-specific
data, then perception is cleanly separated from presentation. The moment one
display needs a fact the others can't get, that fact belongs in the *model*, not
in that renderer. Use the spread of reference displays to find those gaps.

Four reference displays, chosen to be as unlike each other as possible:

| view | projects | layout | needs |
|---|---|---|---|
| **Situation board** | the event log + entity status + `focused_on` | none (table/timeline) | events, `health`, attention |
| **Directory treemap** | the `contains` relation | treemap (deterministic from the tree) | tree + size/activity/health attrs |
| **Cinematic city** | `contains` (districts) + `touched` (causality edges) | force-directed / nested | entities + those two relations + attrs |
| **Physics history graph** (Gource / code_swarm lineage) | entities + `contains`/`touched` + the event log over time | live force simulation: springs pull related nodes, charge repels, activity blooms and decays | entities, `contains` + `touched`, events |

The fourth is worth calling out because it's the one that *looks* least like a
"dashboard" yet needs nothing extra: the classic animated git-history visualizers
(Gource's branching file-tree with committers darting around; code_swarm's organic
particle bursts) are exactly this model played as a force-directed graph over time
— directories branch via `contains`, files bloom when `touched`, the event log is
the clock. If our model can drive that *and* a NORAD board from one fact-set, the
abstraction has earned its keep. (It's also the natural showcase for the in-flight
`spatial_map` primitive — entities + a relation + a physics layout it owns.)

Same model; different query + layout. Two rules fall out, and they fix the
failures that motivated this rewrite:

1. **Position always comes from a real relation** (the tree) or a real layout over
   a real relation (`touched`/co-touch), or it isn't needed (board). There is no
   such thing as an arbitrary coordinate. "Towers in a line with no relationship"
   was "projected no relation"; it becomes impossible.
2. **Effects target entity ids, not coordinates.** A breach erupts from
   `file:…/cli.py` wherever the active layout placed it, in any view. "Free-floating
   expanding circles" becomes impossible.

Layout, stability, and animated reflow (force-directed settle, treemap squarify)
live in the **renderer/primitive**, not the model — which is also the right home
for the in-flight `spatial_map` primitive: it should take *entities + a relation*
and own layout + stable identity + animated transitions, so renderers supply
semantics and never coordinates.

## Deprecate: `semanticGraph`

Remove `context.project.semanticGraph`, the `depends_on`/`tested_by` relations, and
`symbol` entities. Rationale:

- **Unscalable / arbitrary at scale.** The AST scan bounds output to ~96 files but
  picks a BFS-arbitrary slice that isn't the agent's working set on a large repo,
  and re-parses every render with no caching.
- **Language-specific.** Python-only; every other ecosystem gets nothing.
- **Interprets possibly-broken code.** It parses source mid-edit, when files are
  most likely non-parsing — wrong exactly when it matters, and a sharp-edge magnet.
- **Low payoff.** Its only unique contribution was import arrows; districts (tree)
  and causality edges (`touched`) are both observed and robust, and carry the city.

If symbol-level structure is ever wanted, it belongs behind this same contract as
an explicitly-optional, separately-sourced enrichment with `inferred` provenance —
never as a load-bearing input, and never parsed in the display path.

## Migration from today's `worldModel`

The current model is close and mostly survives:

- **Keep:** entity lists (files/commands/changes/health), attributes, provenance,
  recent outcomes.
- **Promote to first-class:** `contains`, `touched`, `produced`, `focused_on` as a
  typed `relations` collection (today: implicit paths + ad-hoc `touchedPaths`).
- **Add:** a unified `events` log (today: implicit in `sequence`).
- **Add git as a source** for `contains`, `touched`/deltas, `commit`, outcomes —
  replacing the command-string scraping that produces inferred, sometimes-wrong
  facts with observed ones.
- **Remove:** `semanticGraph` and its relations/entities.

Net: one provenanced temporal entity–relation graph, fed by git + the event
stream, that a board, a treemap, and a city all project — no code interpretation
anywhere in the perception or display path.
