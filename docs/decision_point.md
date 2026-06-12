# Decision Point: Perception Model Direction

This is a snapshot of the current design discussion. It is not a final contract.
The purpose is to record the tradeoffs before changing the implementation.

## Background

The current implementation has several overlapping inputs to renderers:

- normalized harn events;
- `worldModel`;
- `repoTopology`;
- `semanticGraph`;
- touched-file batches;
- visual continuity and scene state.

That was useful for exploration, but it is beginning to create multiple partial
truths. Claude's proposed repositioning is to replace the "world model plus
semantic graph" direction with one perception model: a temporal entity-relation
graph that displays project into different visual forms.

The core rule is:

> The perception layer observes agent activity and workspace structure. It does
> not interpret code.

## Current Lean

The display should remain primarily about what the agent is doing live.
Visualizing git history alone is not enough; if the goal were only repository
history, this project would not need harn integration.

The best current direction is therefore:

1. Harn events are the primary clock and causality source.
2. Git state is the first workspace reconciliation source.
3. Commits are important milestones, not the main timeline.
4. Filesystem watching is a plausible later enhancement, not required for the
   first perception refactor.

## Source Options

### Option A: Git-Centric

Use git state as the dominant source of truth: tracked files, dirty paths,
untracked paths, diffs, and commits.

Benefits:

- Clear, robust, and easy to test.
- Gives measured change data through `git diff --numstat`.
- Avoids building a file-indexing subsystem too early.
- Aligns with the proposed "no code interpretation" rule.

Costs:

- Too coarse if treated as the main timeline.
- Misses transient edits that are reverted before a git snapshot.
- Temporary/generated files may be ignored.
- Does not explain intent or command causality by itself.

Conclusion: good as a reconciliation layer, too weak as the center of the
experience.

### Option B: Harn-Event-Centric

Use harn events as the primary source: tool calls, tool results, command
lifecycle, stream events, runtime errors, user input, and session lifecycle.

Benefits:

- Captures what the agent is doing.
- Preserves timing, intent, command boundaries, and failures.
- Lets the display dramatize work while it happens, before commits exist.
- Already available through the extension.

Costs:

- Tool payloads may not fully describe actual file changes.
- Shell commands can modify files indirectly.
- Some path/change extraction becomes inference.
- Does not independently verify what changed on disk.

Conclusion: this should be the primary live layer, but it needs workspace
reconciliation.

### Option C: Filesystem Watcher

Subscribe to filesystem change events, likely through Linux inotify via a Python
library such as `watchfiles` or `watchdog`.

Benefits:

- Sees actual file create/modify/delete activity, including shell/script changes.
- Can associate disk changes with currently running commands.
- More live than polling git after events.

Costs:

- Potentially noisy.
- Needs careful ignore rules for `.git`, virtualenvs, caches, test artifacts,
  secrets, and generated output.
- Raw watcher events are too low-level to send directly to a renderer model.
- Adds another moving part before the core perception contract is settled.

Conclusion: useful later if git snapshots are too delayed or blind, but not the
first implementation target.

## Recommended Near-Term Model

Use an event-first perception model:

- Harn events create command/check/agent activity and tentative relations.
- Git snapshots reconcile workspace state around those events.
- Git diffs produce measured change data where available.
- Commit changes become high-signal milestone events.
- Renderers receive compact perception facts and recent perception events, not
  raw watcher/git/harn internals.

This keeps the product centered on agent activity while using git as an
unambiguous workspace backbone.

## Perception Shape

The target remains a temporal entity-relation graph.

Entities, v1:

- `file`
- `dir`
- `command`
- `check`
- `commit`
- `agent`

Relations, v1:

- `contains`: directory tree structure;
- `touched`: command or event touched a file/dir;
- `produced`: command produced a check/commit/outcome;
- `focused_on`: inferred current agent attention.

Events are the timeline. They should carry transition facts such as file changes,
command completion, check status, and commit creation.

## File Facts

Keep durable file attributes small and literal:

```json
{
  "id": "file:src/app.py",
  "type": "file",
  "attrs": {
    "sizeBytes": 9024,
    "exists": true,
    "touchCount": 4,
    "lastTouchedSeq": 12
  }
}
```

Avoid view-normalized fields such as `sizeMagnitude`. Displays can track the
largest file they have seen and rescale their own layout. That scaling is
renderer state, not perception state.

## File Change Events

Change is event-local, not a durable file attribute.

```json
{
  "kind": "file_changed",
  "entity": "file:src/app.py",
  "sizeBefore": 8310,
  "sizeAfter": 9024,
  "churnFraction": 0.18
}
```

Rules:

- `sizeBefore` and `sizeAfter` are enough to infer growth or shrinkage.
- Do not add a redundant `deltaBytes` field unless a concrete consumer needs it.
- `churnFraction` is a display-safe intensity from `0.0` to `1.0`.
- If exact churn is unknown but a change is observed, use `churnFraction: 1.0`.
- Provenance can record whether the basis was git, harn payload, or another
  observer, but simple renderers should not need to branch on provider identity.

## Initial State For Renderer Models

Do not send a flat whole-repo file list by default.

Initial context should be a bounded perception snapshot:

- project summary;
- root and top-level directories/files;
- aggregate directory facts such as `sizeBytes` and file counts;
- expanded detail around touched, dirty, focused, or recently active areas;
- `contains` relations for the visible slice;
- truncation metadata.

Rolling contexts should send recent perception events plus changed/new entities
and relations. Periodic compaction can send a fresh bounded snapshot.

## What To Stop Doing By Default

- Do not make `semanticGraph` load-bearing.
- Do not parse code in the perception/display path.
- Do not let renderer-specific city data become perception data.
- Do not route every raw stream or filesystem event to the renderer model.
- Do not make commits the primary session clock.

Optional future enrichment can still exist behind explicit provenance, but it
should not be required by default renderers.

## Open Questions

- Should the perception model replace `worldModel` immediately, or should it be
  introduced beside it for one migration slice?
- How often should git reconciliation run: tool end only, every renderer batch,
  or on a short active-session interval?
- Should untracked files be included in the initial visible tree, and under what
  bounds?
- What is the minimal commit event shape for v1?
- When is a filesystem watcher worth adding?

## Proposed Next Implementation Slice

1. Add `harn-gibson.perception-model.v1` as a new renderer context field.
2. Populate it from current harn events and git-backed workspace facts.
3. Include first-class `entities`, `relations`, and recent perception `events`.
4. Keep the schema small: file `sizeBytes`, `exists`, `touchCount`,
   `lastTouchedSeq`; file-change events with `sizeBefore`, `sizeAfter`, and
   `churnFraction`.
5. Stop presenting `semanticGraph` as default renderer context.
6. Update `gibson1` to project from the perception model.

