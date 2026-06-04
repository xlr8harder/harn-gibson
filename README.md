# harn-gibson

`harn-gibson` is a first-pass display fixture for [secemp9/harn](https://github.com/secemp9/harn). It provides:

- a harn extension that hooks before, during, and after core agent events;
- a normalized JSON event stream for graphical displays;
- a persistent scene engine with primitives, animations, and mutations;
- a browser-based local display server with SSE updates and an input composer;
- a hook dispatcher so future policies can block, transform, or post-process events.

The current display agent is deterministic. The later LLM-driven visualization layer can consume the same event stream and emit scene mutations without changing the harn integration.

`harn` is included as a development dependency so `uv run harn-gibson dogfood` works from this checkout. The display server and extension modules do not import `harn` or `harn-tui`; the dogfood launcher is the only place that shells out to the harn CLI. A future packaging split should keep the web relay installable without harn's terminal UI stack.

The deterministic renderer remains the default. Events pass through a routing layer before rendering: normal events become renderer requests, streaming assistant deltas update a local `text_stream` primitive, and debug-only events can bypass renderer execution. The deterministic fallback emits browser-rendered `city_block`, `node_graph`, `ribbon`, `glyph_layer`, and `particle_field` primitives, including a bounded repo map when renderer context includes topology or touched-file data. Repo city building height uses numeric line-count metadata plus visible file/directory counts, without sending file contents to the renderer. The browser renderer also supports low-level `mesh`, cinematic `hologram` projections, animated `signal_scope` radar/oscilloscope instruments, animated `tunnel_grid` data corridors, animated `wire_landscape` terrain/filesystem planes, `terminal_wall` banks for command/output/file panels, `access_matrix` lock/security grids, rotating `data_vault` cores, faceted `black_ice` barriers, animated `trace_route` paths, camera-drifting `city_block` filesystem districts, constrained `svg_layer` vector primitives with transform keyframes, path morph frames, and safe filter/clip presets, `data_rain` glyph curtains, and persistent scene animations for pulses, packet bursts, timeline cues, route traces, scans, glitches, signal interference overlays, breach waves, camera jolts, scene camera paths, flythrough rays, extrusion frames, and hold brackets. Dogfood runs can opt into either an external render-plan command or a prompt-command model adapter; unsafe external/model plans are rejected before scene application and recorded as diagnostics. The display server exposes `/catalog`, a generic primitive/effect catalog for future renderer prompts.

Renderer implementations can stay simple with `render(requests, scene)`, or opt into `render_with_context(requests, scene, context)` to receive compact project metadata, scene state, catalog entries, recent agent context, visual-continuity anchors, and recent visualization history. See [docs/renderer-agent.md](docs/renderer-agent.md) for the context and compaction contract.

## Development

```bash
uv sync
uv run pytest
```

Coverage is enforced at 100% for the Python package.

The 1.0 release boundary is defined in [docs/1.0-feature-set.md](docs/1.0-feature-set.md).

Install `harn` separately when you want to run against a live agent.

## Run The Display

For normal dogfooding, run one command from the repo root:

```bash
uv run harn-gibson dogfood
```

This starts the graphical display server, opens the browser, imports existing Codex CLI OAuth credentials into harn's user auth store, and launches `harn` with the display endpoint wired into the extension environment. Project-local `.harn/settings.json` selects the Codex provider/model and points harn at `.harn/extensions/gibson.py`; that shim adds `src/` to `sys.path` and loads the real `harn_gibson.extension` module.

`dogfood` chooses a free local port by default, so it can run even if a manual display server is already using `8765`. Pass `--port 8765` if you want a fixed port.

If you want to import Codex auth without launching harn:

```bash
uv run harn-gibson import-codex-auth
```

This copies the OAuth token shape from `~/.codex/auth.json` to `~/.harn/agent/auth.json` under the `openai-codex` provider key. The target file is outside the repo and is written with user-only permissions. Pass `--no-codex-auth-import` to `dogfood` if you want to manage harn auth yourself.

Forward arguments to harn after `--`:

```bash
uv run harn-gibson dogfood -- -p "summarize this repo"
```

Run harn in a separate project directory while keeping this repo's Gibson extension and Codex model defaults:

```bash
mkdir -p test-artifacts/dogfood-workspaces/tiny-project
uv run harn-gibson dogfood --cwd test-artifacts/dogfood-workspaces/tiny-project -- -p "bootstrap a tiny project here"
```

With `--cwd`, renderer context, repo topology, touched-file summaries, and repo-city visuals use the target project directory instead of the `harn-gibson` checkout.

Use a specific harn executable with `--harn-bin`:

```bash
uv run harn-gibson dogfood --harn-bin /path/to/harn
```

Lower-level manual mode is still available. Start the graphical display server:

```bash
uv run harn-gibson serve --host 127.0.0.1 --port 8765
```

Then run `harn` from the repo root in another terminal. Because the project shim lives in `.harn/extensions/`, `/reload` can reload it during development.

The browser page has a lightweight input composer for small follow-up or steering messages. Submitted text is queued on the display server at `/input`; the harn extension polls `/input/next` and forwards messages via `harn.sendUserMessage`.

Delivery modes:

- `queue`: default. Runs immediately if harn is idle, or queues as a follow-up if harn is streaming.
- `steer`: queues steering input for the active agent run.

The raw event details, event feed, render intents, tracebacks, and hook decisions are in the debug drawer. Use `DEBUG` to open it and `CLOSE` inside the drawer to collapse it. Dogfood launcher failures and extension delivery exceptions are published into the same feed. If harn exits with an error while the browser is open, the display stays up until Ctrl-C so the failure remains visible.

Project-local harn settings in `.harn/settings.json` select the `openai-codex` provider, `gpt-5.5`, and this extension. The Codex auth import is a temporary workaround for harn's current Codex `/login` callback issue.

Render mode is configurable:

```bash
HARN_GIBSON_RENDER_MODE=blocking  # default
HARN_GIBSON_RENDER_MODE=async HARN_GIBSON_RENDER_BATCH_MS=40
HARN_GIBSON_RENDER_TIMING=immediate  # default
HARN_GIBSON_RENDER_TIMING=scheduled  # honor render-plan startOffsetMs during playback
HARN_GIBSON_PROJECT_ROOT=/path/to/project  # repo topology/touched-file context root
HARN_GIBSON_PROJECT_NAME=my-project        # display name in renderer context
```

Immediate timing keeps dogfood and replay runs responsive while still honoring explicit `delayMs`. Scheduled timing treats `startOffsetMs` as an absolute offset inside the coalesced render batch, which is useful for async renderer-agent plans that want a 5-10 second visual playback window after harn has already continued.

Renderer context budgets can be tuned for long sessions:

```bash
HARN_GIBSON_RENDERER_COMPACTION_EVENTS=40      # full context cadence
HARN_GIBSON_RENDERER_MAX_RECENT_PLANS=6        # visual history retained between turns
HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES=64       # top-level repo entries sampled
HARN_GIBSON_RENDERER_MAX_REPO_CHILDREN=8       # visible children per directory
HARN_GIBSON_RENDERER_MAX_TOUCHED_FILES=24      # touched-file batch size
HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS=240
```

Lower limits keep renderer prompts small for fast model turnaround; higher limits give the renderer more continuity, topology, and touched-file evidence.

Renderer event interest can also be narrowed with JSON. Events outside the interest fall back locally instead of going to the renderer:

```bash
HARN_GIBSON_RENDERER_INTEREST='{"eventTypes":["tool_call","tool_result"],"fallbackRoute":"direct_scene"}'
```

Specific event types can be forced to renderer, direct scene, debug-only, or drop routes:

```bash
HARN_GIBSON_ROUTE_RULES='[{"eventType":"runtime_error","route":"debug_only"},{"eventType":"model_select","route":"drop"}]'
```

Noisy event types can also be sampled before routing. This keeps one matching event per four-event window on the renderer path and sends skipped events to `debug_only`:

```bash
HARN_GIBSON_ROUTE_RULES='[{"eventType":"session_tree","route":"renderer_agent","sampleEvery":4,"fallbackRoute":"debug_only"}]'
```

To dogfood the renderer-agent process boundary without a live model call, point the server at an external renderer command. The command receives `harn-gibson.external-renderer-request.v1` JSON on stdin and returns a render plan with `steps` on stdout. `examples/renderers/gibson_dogfood_renderer.py` is the hard-coded showcase renderer for live harn sessions; it reacts to event phase, event type, coalesced timing, touched files, repo topology, and the active style pack with a staged scene using the current cinematic primitive/effect set, including a project hologram, data vault, black-ICE barrier, control graph, opcode glyph layer, Hollywood terminal wall, access matrix, ICE mesh, command ribbon, touched-file spark field, data tunnel, wire terrain, signal scope, route trace, signal interference overlay, repo city, vector sigil, data rain, and persistent effects. Non-default styles alter the emitted renderer tones and intent metadata, so `--style mainframe`, `--style neon-noir`, or `--style satellite-uplink` changes the showcase plan as well as the browser shell:

```bash
HARN_GIBSON_RENDERER_COMMAND='uv run python examples/renderers/gibson_dogfood_renderer.py' \
HARN_GIBSON_RENDERER_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood
```

For longer capture sessions, use the capture wrapper. It launches dogfood with the showcase renderer, writes normalized JSONL to an ignored `test-artifacts/captures/` path by default, and prints the exact replay-review command to run afterward:

```bash
uv run harn-gibson dogfood-capture -- -p "bootstrap a tiny project here"
```

For 15-20 minute captures, ask the wrapper to print the split-review follow-up directly:

```bash
uv run harn-gibson dogfood-capture --list-trajectories
uv run harn-gibson dogfood-capture --trajectory tiny-project
uv run harn-gibson dogfood-capture --trajectory repo-map
```

Built-in presets create ignored bare workspaces under `test-artifacts/dogfood-workspaces/`, inject prompt templates from `examples/prompts/`, capture to `test-artifacts/captures/`, default the follow-up review to split fixtures, and leave raw JSONL out of git. `tiny-project` is the general bootstrap trajectory; `repo-map` is aimed at depth-2 repo topology, varied line counts, and touched-file animation coverage. Treat these live harn runs as the source material for hard-coded renderer tests: capture every normalized event, review the split browser frames, then promote only redacted replay fixtures and baselines. To customize the workspace or prompt while keeping the same capture path:

```bash
mkdir -p test-artifacts/dogfood-workspaces/custom-tiny-project
uv run harn-gibson dogfood-capture \
  --cwd test-artifacts/dogfood-workspaces/custom-tiny-project \
  --split-every 200 \
  -- -p "$(cat examples/prompts/dogfood-tiny-project.md)"
```

Pass `--event-log path/to/session.jsonl` if you want a stable capture path. With `--cwd`, relative event-log paths are resolved before launching harn so the log still lands under the launcher directory, not the target project; the printed follow-up conversion command also includes the matching `--project-root` and `--project-name` so replayed repo-city visuals use the captured workspace. JSONL captures can contain prompts, tool output, file paths, and diagnostics, so keep the raw logs under ignored artifact paths. The follow-up `event-log-to-replay` conversion redacts common token, key, password, and credential values by default before writing replay fixtures; use `--no-redact-sensitive` only for private local debugging.

Use `examples/renderers/gibson_echo_renderer.py` when you want the smallest possible external-renderer contract example.

Renderer command failures are fail-open: the deterministic renderer still updates the scene, and the failure is added to the debug trace surface. Returned plans are also validated against the current scene and catalog. Unsupported but safe primitives/effects are kept with `renderPlanDiagnostics` warnings in render intent metadata; unsafe plans such as missing patch targets, raw `svg_layer` markup, or unbounded vector keyframes are rejected, replaced with deterministic fallback output, and traced in the browser debug drawer. Unsupported `svg_layer` filter or clip presets are warning-only and simply fall back to the bounded browser set.

To dogfood the model-prompt boundary without binding to a provider SDK, use a prompt-command renderer. The command receives `harn-gibson.model-renderer-request.v1` JSON with the exact provider-neutral messages that a model would receive and returns model-style JSON text containing a render plan:

```bash
HARN_GIBSON_RENDERER_MODEL_COMMAND='uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood
```

`HARN_GIBSON_RENDERER_MODEL_COMMAND` takes precedence over `HARN_GIBSON_RENDERER_COMMAND`. Model-command failures and unsafe model plans use the same fail-open deterministic fallback and trace/debug reporting as external render-plan commands.

Replay is deterministic by default and ignores ambient renderer command environment. To intentionally dogfood renderer adapters against captured sessions or fixture suites, pass explicit replay flags:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --renderer-model-command 'uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
  --renderer-model-timeout-ms 10000 \
  --output-scene test-artifacts/replays/model-rendered-scene.json

uv run harn-gibson replay-dir examples/replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000
```

To watch a replay animate through the live browser display instead of only producing a final scene or review bundle, use `watch-replay`. It starts a local display server, opens the browser, applies one replay step at a time, and accepts the same explicit renderer flags:

```bash
uv run harn-gibson watch-replay examples/dogfood-replays/repo-map-trajectory.json \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000 \
  --step-delay-ms 900
```

Use `--no-hold` for automated smoke checks, `--no-browser` when you only want the server URL, and omit `--renderer-command` to compare the deterministic renderer against the same fixture.

For captured sessions from a separate workspace, add `--project-root PATH` and optionally `--project-name NAME` so renderer context and repo-city visuals sample the preserved target project instead of this checkout.

For offline inspection without the dogfood launcher, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn --no-extensions -e .harn/extensions/gibson.py
```

A useful capture workflow is to run `uv run harn-gibson dogfood-capture --trajectory tiny-project` or `--trajectory repo-map`. The presets ask harn to initialize git, create project files, run tests, make commits, introduce and fix a failure, and summarize status. `repo-map` adds a deliberate depth-2 directory spread so renderer-regression fixtures can exercise repo-city height, area, and touched-file effects. Those 15-20 minute captured trajectories should become the basis for future renderer-regression fixtures and screenshot reviews; raw JSONL remains ignored, and committed fixtures should be redacted replay JSON plus baselines.

Convert a captured event log into a replay fixture:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json \
  --output-result test-artifacts/replays/captured-session-result.json \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-review \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py'
```

`--visual-fixture` adds capture-summary metadata plus conservative screenshot expectations, so the converted trajectory can be run through `replay-dir --screenshot-dir` as a visual regression input. Conversion records redaction metadata with an enabled flag and replacement count. `--output-result` writes the replay result JSON produced from the converted fixture. `--review-dir` replays the converted log immediately, captures per-step browser frames, renderer contexts, prompts, chunks, render intents, and writes an HTML review bundle.

For long captures, split the event log into a replay fixture directory:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output-dir test-artifacts/replays/captured-session-split \
  --split-every 200 \
  --output-result test-artifacts/replays/captured-session-split-result.json \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-split-review \
  --project-root test-artifacts/dogfood-workspaces/tiny-project
```

Split conversion writes one fixture per chunk plus `manifest.json`. With `--review-dir`, conversion immediately replays the generated directory and writes one complete per-chunk review bundle under `files/` plus a top-level suite overview that links the chunk frame players, renderer contexts, prompts, chunks, and render-intent reviews. The suite overview also lists reviewed event types, tools, touched-file areas, renderer routes, and renderer names so long captures can be checked before opening every frame player. You can still run `replay-dir` on the generated directory later; it skips the split manifest and replays the chunk fixtures directly. `replay-dir --output-result` also writes an aggregate `summary` with event type/source/phase/timing coverage, tool counts, command-field counts, failed tool counts, touched files, touched top-level areas, route counts, renderer counts, screenshot counts, baseline counts, visible anchors, active animations, effects, targets, renderer continuity, and a `trajectoryCoverage` block with compact signals/gaps for deciding whether a long capture is ready to promote into renderer-regression material.

Replay fixtures can drive the same scene pipeline without a live harn process:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --output-scene test-artifacts/replays/scene.json \
  --output-result test-artifacts/replays/result.json \
  --output-render-contexts test-artifacts/replays/renderer-contexts.json \
  --output-render-prompts test-artifacts/replays/renderer-prompts.json \
  --output-render-chunks test-artifacts/replays/renderer-chunks.json \
  --render-chunk-review test-artifacts/replays/renderer-chunks.html \
  --render-prompt-review test-artifacts/replays/renderer-prompts.html \
  --output-render-intents test-artifacts/replays/render-intents.json \
  --render-intent-review test-artifacts/replays/render-intents.html \
  --review-dir test-artifacts/replays/review \
  --screenshot test-artifacts/replays/scene.png

uv run harn-gibson replay examples/replays/renderer-plan.json \
  --output-scene test-artifacts/replays/renderer-scene.json \
  --output-result test-artifacts/replays/renderer-result.json \
  --screenshot test-artifacts/replays/renderer-scene.png
```

Replay files can include final-scene expectations and screenshot metric expectations, so fixtures act as verifiers as well as demos. The fixture format is documented in [docs/replay.md](docs/replay.md).

The checked-in replay set includes agent-side routing, renderer-side plan, primitive-gallery, animation-gallery, and style-showcase fixtures so browser screenshots can review both harness behavior and generic visual primitives/effects. Screenshot captures record canvas metrics and checked-in fixtures assert broad nonblank/visibility thresholds.

Run the full local acceptance gate before a release checkpoint:

```bash
bash scripts/acceptance.sh
```

Use `bash scripts/acceptance.sh --dry-run` to inspect the exact commands without running the heavyweight browser/replay gates. The script runs lint, the full covered test suite, a dynamic-port dogfood smoke, both replay baseline/screenshot suites, whitespace checks, and runtime/secret hygiene scans.

Run the checked-in replay fixture suite:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

Run the hard-coded dogfood renderer against the checked-in dogfood trajectory fixtures:

```bash
uv run harn-gibson replay-dir examples/dogfood-replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/dogfood-replays \
  --screenshot-dir test-artifacts/replays/dogfood-screenshots
```

Checked-in dogfood replay fixtures carry `metadata.projectRoot` and `metadata.projectName`, so mixed suites can review multiple fixture workspaces in one command. Use `--project-root` and `--project-name` when intentionally overriding that metadata for a copied or freshly captured workspace.

## Browser Tests

```bash
uv run playwright install chromium
uv run pytest
```

Browser screenshots are written to `test-artifacts/screenshots/`.

## Hook Modules

Hook modules are Python files listed in `HARN_GIBSON_HOOKS`, separated by `:`. Each module exports `register_gibson_hooks(dispatcher)`.

```python
from harn_gibson import HookDecision


def register_gibson_hooks(dispatcher):
    @dispatcher.on("tool_call", "before")
    def block_rm(event):
        command = event.payload.get("input", {}).get("command", "")
        if "rm -rf" in command:
            return HookDecision(block=True, reason="Blocked by harn-gibson hook")
```

Supported interdict points include `input`, `tool_call`, `tool_result`, `message_end`, `before_agent_start`, and session-before events. All harn lifecycle/display events are still emitted even when they do not support mutation.
