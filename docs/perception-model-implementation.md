# Perception model v1: implementation notes

*Companion to `docs/decision_point.md` (the design record) and
`docs/perception-model-spec.md` (the original proposal). This documents what
landed on `work/perception-model` and the decisions taken on the open questions.*

## What landed

- **`src/harn_gibson/perception.py`** — `PerceptionModel`, an event-first
  temporal entity-relation graph with git reconciliation, plus
  `capture_git_snapshot`. Schema: `harn-gibson.perception-model.v1`.
- **Context wiring** — `context.project.perceptionModel` is built by
  `RendererContextBuilder` beside `worldModel` (one migration slice, per the
  decision doc's first open question). New `RendererContextConfig` fields:
  `max_perception_entities` (96), `max_perception_relations` (144),
  `max_perception_events` (24), `include_semantic_graph` (False).
- **`semanticGraph` is no longer default context.** It renders as
  `{available: false, enabled: false}` unless `include_semantic_graph=True`
  (env: `HARN_GIBSON_RENDERER_SEMANTIC_GRAPH=1`). The gibson1 replay baselines
  opt in until gibson1 migrates.
- **`examples/renderers/claude_gibson_perception_renderer.py`** — a renderer
  that projects *only* from the perception model (stands in for the "update
  gibson1" slice item; gibson1 itself is left to its owner to migrate).
- **`tests/test_perception.py`** — 24 tests; the suite holds the repo's 100%
  coverage bar.

## The shape

```json
{
  "schema": "harn-gibson.perception-model.v1",
  "revision": 9,
  "workspace": {"rootName": "repo-map", "git": {"available": true, "branch": "main",
                 "headSha": "abc123def456", "dirtyPathCount": 1, "untrackedPathCount": 0},
                "fileCount": 8, "basis": "git"},
  "entities": [
    {"id": "agent", "type": "agent", "attrs": {"eventType": "tool_result", "sequence": 9}},
    {"id": "dir:.", "type": "dir", "attrs": {"fileCount": 8, "root": true}},
    {"id": "dir:src/repo_map", "type": "dir", "attrs": {"fileCount": 2, "sizeBytes": 1184}},
    {"id": "file:src/repo_map/cli.py", "type": "file",
     "attrs": {"exists": true, "touchCount": 4, "lastTouchedSeq": 9, "sizeBytes": 612,
               "tracked": true, "dirty": true},
     "provenance": {"source": "observed", "confidence": 1.0, "basis": "git"}},
    {"id": "command:5", "type": "command",
     "attrs": {"preview": "python -m pytest tests/test_cli.py", "toolName": "bash",
               "status": "error", "startSeq": 5, "endSeq": 6}},
    {"id": "check:test:6", "type": "check", "attrs": {"category": "test", "status": "error", "seq": 6}},
    {"id": "commit:abc123def456", "type": "commit",
     "attrs": {"sha": "abc123def456", "subject": "fix exit code", "filesChanged": 1}}
  ],
  "relations": [
    {"type": "contains", "from": "dir:src/repo_map", "to": "file:src/repo_map/cli.py"},
    {"type": "touched", "from": "command:5", "to": "file:tests/test_cli.py", "lastSeq": 5},
    {"type": "produced", "from": "command:5", "to": "check:test:6"},
    {"type": "focused_on", "from": "agent", "to": "file:src/repo_map/cli.py",
     "provenance": {"source": "inferred", "confidence": 0.7}}
  ],
  "events": [
    {"seq": 9, "ts": 24500, "kind": "file_changed", "entity": "file:src/repo_map/cli.py",
     "sizeBefore": 598, "sizeAfter": 612, "churnFraction": 0.0229, "basis": "git",
     "addedLines": 1, "removedLines": 1},
    {"seq": 9, "kind": "command_completed", "entity": "command:9", "status": "ok"},
    {"seq": 9, "kind": "check_completed", "entity": "check:test:9", "category": "test", "status": "ok"}
  ],
  "counts": {"entitiesByType": {"...": 0}, "relationsByType": {"...": 0}, "events": 12},
  "truncation": {"files": false, "events": false, "workspaceFileCount": 8, "renderedFileCount": 8}
}
```

Per the decision doc: attributes are small and literal (`sizeBytes`, `exists`,
`touchCount`, `lastTouchedSeq` — no view-normalized fields); change is
event-local (`sizeBefore`/`sizeAfter`/`churnFraction`, with `churnFraction: 1.0`
when a change is observed but unmeasured); no flat whole-repo dump (active
slice + top-level always, directories carry aggregate facts, truncation is
explicit metadata).

## Decisions taken on the open questions

1. **Replace `worldModel` immediately?** No — introduced beside it. Nothing
   that consumes `worldModel` changed behavior; `perceptionModel` is additive.
   Removal is a later slice once gibson1 and the prompts migrate.
2. **Git reconciliation cadence:** on any batch containing an `after`-phase
   event (command/tool results), plus once on first observation. The git calls
   are 3s-capped subprocesses; on the dogfood repos a full snapshot is a few
   milliseconds. An interval throttle can be added if large repos need it.
3. **Untracked files:** included (bounded to 200, sensitive/excluded names
   filtered) and marked `dirty`, with `tracked: true/false` on every file when
   git is available. Untracked files are where agents do half their work;
   leaving them out would blind the display to scratch files.
4. **Minimal commit event shape:** `commit_created` with `entity`
   (`commit:<sha12>`), `subject` (clipped), `filesChanged` (from `diff-tree`,
   omitted if unavailable). A `produced` relation links the commit to the most
   recent `git commit` command when one exists (inferred, 0.8).
5. **Filesystem watcher:** not added. The fallback for non-git roots is a
   bounded breadth-first walk with the standard exclusion/sensitive-path rules.

## Things learned building it

- **Subdirectory roots need path normalization.** `git ls-files` is cwd-relative
  but `status --porcelain` and `diff --numstat` are repo-root-relative; the
  snapshot strips `rev-parse --show-prefix` so all facts are project-root
  relative. (The dogfood workspaces are subdirectories of this repo — the first
  replay run hit this immediately.)
- **Re-runs must mint new command entities.** A `tool_result` whose command text
  matches an already-settled command is a *re-run* (the retry-after-failure
  loop); attributing its outcome to the old entity swallowed the green
  `check_completed` that ends an alert arc. This was caught because the
  long-session fixture's recovery beat went missing.
- **The blast radius works along real edges.** The consumer renderer derives
  "what's implicated by the failing check" as
  `check ← produced ← command → touched → files` with zero path heuristics —
  the thing the old `failing_paths` guesswork approximated.

## Try it

```bash
uv run harn-gibson replay examples/claude-gibson-replays/long-session-arc.json \
  --renderer-command 'uv run python examples/renderers/claude_gibson_perception_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root examples/dogfood-workspaces/repo-map --project-name repo-map \
  --review-dir test-artifacts/iter/cgp/review
```

The arc renders idle → verify → alert (black ice + breach anchored on the blast
files) → recovery, with the whole workspace tree present from step 0 (git serves
structure immediately; activity lights it up).

## Suggested next slices

1. Migrate gibson1 to project from `perceptionModel`; drop its semantic-graph
   branches and the baseline opt-in env.
2. Migrate `agentAttention` to consume perception entities (or fold it in as
   richer `focused_on`/`agent` attrs) so there is one focus story.
3. Retire `worldModel` + `touchedFiles` from default context once nothing
   default consumes them; keep `repoTopology` or fold it into dir entities.
4. A `spatial_map`-owned tree/force layout that takes entities + a relation, so
   renderers stop assigning coordinates entirely (the remaining gap from
   `docs/sector-map-renderer-result.md`).
