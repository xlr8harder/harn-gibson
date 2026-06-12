# Project state — 2026-06-12

*Snapshot of where the claude-gibson rework stands, what shipped, what's next.
Branch: `work/perception-model` (forked from upstream `work/1.1-spatial-bindings`
@ 209c94c).*

## The architecture that emerged

Two layers, replacing the renderer-in-the-hot-path design:

1. **Perception model** (`src/harn_gibson/perception.py`,
   `harn-gibson.perception-model.v1`) — a temporal entity-relation graph fed by
   harn events (primary clock/causality) and git snapshots (workspace
   reconciliation). Entities: file/dir/command/check/commit/agent. Relations:
   contains/touched/produced/focused_on. Events: file_changed (sizes, line
   deltas, bounded diffPreview), command_completed, check_completed,
   commit_created. Agent narration (thinking/text deltas only — tool-call
   argument streams filtered out) is an agent-entity attribute. The layer never
   parses code; `semanticGraph` is no longer default context.

2. **Projection engine** (`src/harn_gibson/projection.py`,
   `harn-gibson.projection.v1`) — scene = f(projection, perception). A
   projection is a standing declarative spec: select / layout (radial-tree,
   force, grid, ring) / encode / edges / event→effect rules / camera / theme.
   The engine re-resolves per step into one `projection_scene` primitive;
   the browser tweens (object constancy), simulates live physics for force
   layers, materializes new nodes as a wavefront from the root, frames the
   camera on points of interest, and themes everything (gibson, blueprint).
   Effects are entity-anchored: pulse, ring, breach, beam, shake, alarm,
   banner, peek (diff scroll box). A renderer is a JSON file
   (`examples/projections/`); no spec at all is a complete display
   (`--projection 1`). `POST /projection` redirects a live session (the
   director hook); `GET /projection` introspects.

Supporting: browser replay button (`POST /replay/restart` + session reset),
project-name page kicker, literal labels, legacy display channels
(ambient pulses, animation overlays, stream panel, status-chip stamping)
stand down structurally when a projection owns the stage.

## Proven against a real session

`harn-gibson dogfood-capture` ran a real harn agent (gpt-5.5) on a seeded task
workspace (`~/git/gibson-demo-task`, the "linkjar" project): it fixed a bug,
implemented search, added a CLI command, tests, docs — five staged commits, all
green — while the projection rendered live. Captured 2,380 events
(`captures/linkjar-session.jsonl`, local) → converted to
`examples/claude-gibson-replays/linkjar-live-session.json`, the standing demo
and regression fixture (replays headless in ~2.6s).

Demo command:

```bash
uv run harn-gibson watch-replay examples/claude-gibson-replays/linkjar-live-session.json \
  --port 8765 --browser --hold --playback-timing real-time --speed 4 --max-step-delay-ms 4000 \
  --projection examples/projections/gibson-organic.json \
  --project-root /home/user/git/gibson-demo-task --project-name linkjar
```

## Bugs found by the real session (all fixed, regression-tested)

- harn's extension loader execs entry files without registering them in
  `sys.modules`; module-level `@dataclass` crashes loading → harn silently ran
  extensionless. **Report upstream to harn.**
- harn blocks reading a never-closing non-TTY stdin under headless dogfood
  (launch with stdin closed).
- The touched-path extractor surfaces tool output text as paths ("Successfully
  replaced 1 block(s) in src/x.py.") → phantom nodes. Perception now filters;
  **the extractor itself is upstream-fixable.**
- Effect storms (same beat many times/sec) → same-shape effects refresh.
- `$root` targets resolved empty under force layouts.
- Re-run of a settled command must mint a new command entity (retry loop).
- Subdirectory project roots: git path-base mismatch (`--show-prefix` strip).

## Suite

345 tests, 100% line+branch coverage (repo gate). Engine resolution, layouts,
effects, perception, replay control, projection endpoints all covered;
browser JS verified via replay frame review.

## Next (in rough priority)

1. Upstream/Codex hand-off writeup (perception+projection migration story,
   harn bug reports, legacy-primitive disposition: freeze catalog, absorb
   city-as-theme / black-ice-as-sustained-effect / terminal-wall-as-HUD,
   delete after gibson1 migrates).
2. A second *designed* view (session timeline/board) + a `timeline` layout.
3. Director-agent experiment: a model emitting projection specs at dramatic
   moments via POST /projection.
4. Stream route cleanup: projection subsumes direct routes entirely.
5. Theme depth: gibson nodes as towers (city look) on the projection skeleton.
