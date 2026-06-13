# harn-gibson

`harn-gibson` adds a cinematic browser viewer to a live [secemp9/harn](https://github.com/secemp9/harn) agent session. The normal setup has three steps:

1. install harn;
2. install the Gibson harn package into your configured harn;
3. start the browser viewer from harn with `/gibson-view`.

The viewer is a display layer. Harn remains the primary agent interface, and the browser can also queue small follow-up or steering messages back into harn.

## Quick Start: Interactive Viewer

Install harn, then add Gibson as a harn package:

```bash
harn install git:github.com/xlr8harder/harn-gibson
```

For a team/project-local install, write the package to `.harn/settings.json` instead:

```bash
harn install git:github.com/xlr8harder/harn-gibson -l
```

Then launch harn normally from your project:

```bash
harn
```

Inside harn, open the viewer:

```text
/gibson-view
```

That command starts a local Gibson display server, opens the browser, flushes recent harn events into the scene, and streams future events as the agent works.

Gibson does not require separate model credentials. Use your existing harn provider/auth configuration.

To confirm harn has the package configured:

```bash
harn list
```

Useful variants:

```text
/gibson-renderers
/gibson-view --renderer default
/gibson-view --renderer classic
/gibson-view --renderer stress
/gibson-view --port 8765
/gibson-view --no-browser
```

`/gibson-view` uses the `default` visualization when no renderer is specified. `/gibson-renderers` lists the built-in visualization names. `default` is the built-in organic force-layout visualization driven by the perception model, `classic` is the older hard-coded coherent visualizer, and `stress` is the busy showcase/stress visualizer. A custom command can be supplied with `/gibson-view --renderer-command 'python my_renderer.py'`; a custom perception visualization spec can be supplied with `/gibson-view --renderer examples/projections/gibson-organic.json`.

`--port 8765` uses a predictable browser URL, `http://127.0.0.1:8765`. `--no-browser` starts the server without opening a browser, which is useful over SSH or when another tool will open the page.

## Development Checkout

When working from a local checkout, install dependencies and run harn with the package loaded transiently:

```bash
uv sync
uv run harn -e .
```

Or install the checkout into your harn settings:

```bash
harn install /path/to/harn-gibson
```

The package entry point is `extensions/gibson.py`, and both `pyproject.toml` and `package.json` declare it for harn discovery.

## Browser Input

The browser page includes a small composer. Submitted text is queued on the Gibson server and delivered into harn by the extension.

Delivery modes:

- `queue`: default. Runs immediately if harn is idle, or queues as a follow-up if harn is streaming.
- `steer`: queues steering input for the active agent run.

Runtime diagnostics, tracebacks, raw events, render intents, and hook decisions are available in the browser debug drawer.

## One-Command Run

If you want Gibson to own the whole local process tree for a demo or test run, use the launcher:

```bash
uv run harn-gibson run -- -p "summarize this repo"
```

This starts the viewer, opens the browser, imports existing Codex CLI OAuth credentials into harn's user auth store, and launches harn with the extension wired in.

`run` uses the same `default` visualization as `/gibson-view`.

## Capture And Replay

Capture a live harn/Gibson session:

```bash
uv run harn-gibson capture --event-log test-artifacts/captures/manual.jsonl -- -p "summarize this repo"
```

Convert the captured JSONL into a redacted replay fixture:

```bash
uv run harn-gibson event-log-to-replay test-artifacts/captures/manual.jsonl \
  --output test-artifacts/replays/manual.json \
  --visual-fixture
```

Watch the replay with the default fixed pacing:

```bash
uv run harn-gibson watch-replay test-artifacts/replays/manual.json
```

Use source timestamps when you want the captured event timing:

```bash
uv run harn-gibson watch-replay test-artifacts/replays/manual.json --playback-timing real-time
```

Replay can also opt into a hard-coded renderer:

```bash
uv run harn-gibson watch-replay test-artifacts/replays/manual.json --renderer classic
```

## Watch A Recorded Session

Replay does not need harn. It feeds captured events through the same scene pipeline and browser backend:

```bash
uv run harn-gibson watch-replay examples/dogfood-replays/repo-map-trajectory.json
```

Then open the printed URL if the browser does not open automatically. The browser replay button re-runs the same file. Add `--playback-timing real-time` to use captured source timestamps, or `--renderer classic` / `--renderer stress` to call a hard-coded visualization live while replaying event steps.

## How It Works

The harn extension subscribes to harn events and normalizes them into sequenced `GibsonEvent`s. The display server receives those events, routes them through the renderer/projection pipeline, and streams scene updates to the browser over SSE. The browser owns smooth presentation details such as animation clocks, scrolling panels, and canvas rendering.

Sessions can also be captured to JSONL with `HARN_GIBSON_EVENT_LOG`, converted into replay fixtures, and re-rendered later with different projections or hard-coded renderers. See [docs/launch-modes.md](docs/launch-modes.md), [docs/architecture.md](docs/architecture.md), and [docs/renderer-agent.md](docs/renderer-agent.md) for the deeper architecture and renderer contracts.

## Development

```bash
uv sync
uv run pytest
```

Coverage is enforced at 100% for the Python package.

The 1.0 release boundary is defined in [docs/1.0-feature-set.md](docs/1.0-feature-set.md).

The dev environment includes harn for local dogfooding. Install harn separately when using Gibson outside this checkout.

## Advanced Launching

For launcher-based runs, run one command from the repo root:

```bash
uv run harn-gibson run
```

This starts the graphical display server with the `default` visualization, opens the browser, imports existing Codex CLI OAuth credentials into harn's user auth store, and launches `harn` with the display endpoint wired into the extension environment. Project-local `.harn/settings.json` selects the Codex provider/model and points harn at `.harn/extensions/gibson.py`; that shim adds `src/` to `sys.path` and loads the real `harn_gibson.extension` module.

`run` chooses a free local port by default, so it can run even if a manual display server is already using `8765`. Pass `--port 8765` if you want a fixed port.

If you want to import Codex auth without launching harn:

```bash
uv run harn-gibson import-codex-auth
```

This copies the OAuth token shape from `~/.codex/auth.json` to `~/.harn/agent/auth.json` under the `openai-codex` provider key. The target file is outside the repo and is written with user-only permissions. Pass `--no-codex-auth-import` to `run` if you want to manage harn auth yourself.

Forward arguments to harn after `--`:

```bash
uv run harn-gibson run -- -p "summarize this repo"
```

Run harn in a separate project directory while keeping this repo's Gibson extension and Codex model defaults:

```bash
mkdir -p test-artifacts/dogfood-workspaces/tiny-project
uv run harn-gibson run --cwd test-artifacts/dogfood-workspaces/tiny-project -- -p "bootstrap a tiny project here"
```

With `--cwd`, renderer context, repo topology, touched-file summaries, and repo-city visuals use the target project directory instead of the `harn-gibson` checkout.

Use a specific harn executable with `--harn-bin`:

```bash
uv run harn-gibson run --harn-bin /path/to/harn
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

Project-local harn settings in `.harn/settings.json` select the `openai-codex` provider, `gpt-5.5`, and this extension. The package-level harn extension entry point is `extensions/gibson.py`; the `.harn/extensions/gibson.py` file is only the project-local dogfood shim. The Codex auth import is a temporary workaround for harn's current Codex `/login` callback issue.

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
HARN_GIBSON_RENDERER_MAX_WORLD_ENTITIES=24     # durable world-model entities
HARN_GIBSON_RENDERER_MAX_SEMANTIC_FILES=96     # Python files scanned for semantic graph
HARN_GIBSON_RENDERER_MAX_SEMANTIC_EDGES=192    # import/test/definition graph edges
HARN_GIBSON_RENDERER_MAX_SEMANTIC_SYMBOLS=160  # top-level symbols exposed
HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS=240
```

Lower limits keep renderer prompts small for fast model turnaround; higher limits give the renderer more continuity, topology, semantic graph evidence, touched-file evidence, and accumulated world-model facts.

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

To dogfood the renderer-agent process boundary without a live model call, select a built-in visualization or point the server at an external renderer command. The command receives `harn-gibson.external-renderer-request.v1` JSON on stdin and returns a render plan with `steps` on stdout. The default visualization is the built-in organic force-layout display:

```bash
uv run harn-gibson run
```

There are three integration levels. A renderer decides how events become scene mutations. A primitive/effect expands the visual vocabulary that renderers can target; `spatial_map` is the first lower-level world-binding primitive for typed objects, edges, stable entity ids, and object-addressable camera targets. A display backend consumes scene state and implements that vocabulary in a runtime; the current backend is browser/canvas, but a terminal, native, game-engine, or OpenGL backend can work if it implements the catalog or an advertised subset. `GET /backend-contract` and `uv run harn-gibson backend-contract` expose the endpoint paths, scene/update schema names, core primitive kinds, catalog primitive kinds, effect kinds, mutation ops, input delivery modes, render timing modes, supported style packs, active style pack, and current backend capability profile for that use case. `GET /catalog` and `uv run harn-gibson catalog --tag gibson --compact` expose the full or filtered primitive/effect vocabulary. A custom primitive layer can either translate the advertised Gibson catalog to backend-native drawing calls or pair a custom catalog with a renderer that targets it.

`examples/renderers/gibson_dogfood_renderer.py` remains the stress showcase for live harn sessions; it reacts to event phase, event type, coalesced timing, touched files, repo topology, and the active style pack with a staged scene using the current cinematic primitive/effect set, including a project hologram, data vault, black-ICE barrier, control graph, opcode glyph layer, Hollywood terminal wall, access matrix, orbital uplink map, ICE mesh, command ribbon, touched-file spark field, data tunnel, wire terrain, signal scope, route trace, signal interference overlay, repo city, vector sigil, data rain, and persistent effects. Non-default styles alter the emitted renderer tones and intent metadata, so `--style mainframe`, `--style neon-noir`, or `--style satellite-uplink` changes the showcase plan as well as the browser shell:

```bash
uv run harn-gibson run --renderer stress
```

For longer capture sessions, use the capture wrapper. It launches the stress visualization, writes normalized JSONL to an ignored `test-artifacts/captures/` path by default, and prints the exact replay-review command to run afterward:

```bash
uv run harn-gibson capture -- -p "bootstrap a tiny project here"
```

For 15-20 minute captures, ask the wrapper to print the split-review follow-up directly:

```bash
uv run harn-gibson capture --list-trajectories
uv run harn-gibson capture --trajectory tiny-project
uv run harn-gibson capture --trajectory repo-map
```

Built-in presets create ignored bare workspaces under `test-artifacts/dogfood-workspaces/`, inject prompt templates from `examples/prompts/`, capture to `test-artifacts/captures/`, default the follow-up review to split fixtures, and leave raw JSONL out of git. `tiny-project` is the general bootstrap trajectory; `repo-map` is aimed at depth-2 repo topology, varied line counts, and touched-file animation coverage. Treat these live harn runs as the source material for hard-coded renderer tests: capture every normalized event, review the split browser frames, then promote only redacted replay fixtures and baselines. To customize the workspace or prompt while keeping the same capture path:

```bash
mkdir -p test-artifacts/dogfood-workspaces/custom-tiny-project
uv run harn-gibson capture \
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
uv run harn-gibson run
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
  --playback-timing real-time \
  --speed 1
```

Captured `event` and `raw_event` replay steps call the configured renderer live, so the command above dogfoods the hard-coded renderer against the historical agent events. Saved `render_plan` steps are renderer-side fixtures: replay applies the recorded renderer output directly so browser/display changes can be compared against an exact plan. Use `--step-delay-ms` for fixed-delay playback, `--max-step-delay-ms` to cap long real-time gaps, `--start-step N --end-step M` to inspect a 1-based inclusive range from a long capture, `--no-hold` for automated smoke checks, `--no-browser` when you only want the server URL, and omit `--renderer-command` to compare the deterministic renderer against the same fixture. Full playback checks fixture expectations by default; partial playback skips them unless `--check-expectations` is supplied.

For captured sessions from a separate workspace, add `--project-root PATH` and optionally `--project-name NAME` so renderer context and repo-city visuals sample the preserved target project instead of this checkout.

For offline inspection without the launcher, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn --no-extensions -e .harn/extensions/gibson.py
```

A useful capture workflow is to run `uv run harn-gibson capture --trajectory tiny-project` or `--trajectory repo-map`. The presets ask harn to initialize git, create project files, run tests, make commits, introduce and fix a failure, and summarize status. `repo-map` adds a deliberate depth-2 directory spread so renderer-regression fixtures can exercise repo-city height, area, and touched-file effects. Those 15-20 minute captured trajectories should become the basis for future renderer-regression fixtures and screenshot reviews; raw JSONL remains ignored, and committed fixtures should be redacted replay JSON plus baselines.

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

Use `bash scripts/acceptance.sh --dry-run` to inspect the exact commands without running the heavyweight browser/replay gates. The script runs lint, the full covered test suite, a dynamic-port launcher smoke, generic replay screenshots, the classic replay screenshots, the mainframe-styled classic replay screenshots, the stress-renderer screenshots, whitespace checks, and runtime/secret hygiene scans.

Run the checked-in replay fixture suite:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

Run the `classic` renderer against its checked-in visual fixture:

```bash
uv run harn-gibson replay-dir examples/gibson1-replays \
  --renderer-command 'uv run python examples/renderers/gibson1_renderer.py' \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/gibson1-replays \
  --screenshot-dir test-artifacts/replays/gibson1-screenshots
```

Run the same classic fixture under a non-default style pack:

```bash
uv run harn-gibson replay-dir examples/gibson1-replays \
  --style mainframe \
  --renderer-command 'uv run python examples/renderers/gibson1_renderer.py' \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/gibson1-mainframe-replays \
  --screenshot-dir test-artifacts/replays/gibson1-mainframe-screenshots
```

Run the hard-coded stress renderer against the checked-in dogfood trajectory fixtures:

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
