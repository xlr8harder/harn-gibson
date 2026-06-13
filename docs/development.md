# Development Workflows

This document holds the detailed commands that are useful while building or testing `harn-gibson`. The README stays focused on the normal interactive viewer path.

## Local Launcher

For launcher-based runs, run one command from the repo root:

```bash
uv run harn-gibson run
```

This starts the graphical display server with the `default` visualization, opens the browser, and launches `harn` with the display endpoint wired into the extension environment.

`run` chooses a free local port by default, so it can run even if a manual display server is already using `8765`. Pass `--port 8765` if you want a fixed port.

Forward arguments to harn after `--`:

```bash
uv run harn-gibson run -- -p "summarize this repo"
```

Run harn in a separate project directory while keeping this repo's Gibson extension wired in:

```bash
mkdir -p test-artifacts/dogfood-workspaces/tiny-project
uv run harn-gibson run --cwd test-artifacts/dogfood-workspaces/tiny-project -- -p "bootstrap a tiny project here"
```

With `--cwd`, renderer context, repo topology, touched-file summaries, and visuals use the target project directory instead of the `harn-gibson` checkout.

Use a specific harn executable with `--harn-bin`:

```bash
uv run harn-gibson run --harn-bin /path/to/harn
```

## Manual Server

Lower-level manual mode is still available:

```bash
uv run harn-gibson serve --host 127.0.0.1 --port 8765
```

Then run `harn` from the repo root in another terminal. Because the project shim lives in `.harn/extensions/`, `/reload` can reload it during development.

For offline inspection without the launcher, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn --no-extensions -e .harn/extensions/gibson.py
```

## Capture Fixtures

For longer capture sessions, use the capture wrapper. It writes normalized JSONL to an ignored `test-artifacts/captures/` path by default and prints the replay-review command to run afterward:

```bash
uv run harn-gibson capture -- -p "bootstrap a tiny project here"
```

For 15-20 minute captures, use built-in trajectories:

```bash
uv run harn-gibson capture --list-trajectories
uv run harn-gibson capture --trajectory tiny-project
uv run harn-gibson capture --trajectory repo-map
```

Built-in presets create ignored bare workspaces under `test-artifacts/dogfood-workspaces/`, inject prompt templates from `examples/prompts/`, capture to `test-artifacts/captures/`, default the follow-up review to split fixtures, and leave raw JSONL out of git.

To customize the workspace or prompt while keeping the same capture path:

```bash
mkdir -p test-artifacts/dogfood-workspaces/custom-tiny-project
uv run harn-gibson capture \
  --cwd test-artifacts/dogfood-workspaces/custom-tiny-project \
  --split-every 200 \
  -- -p "$(cat examples/prompts/dogfood-tiny-project.md)"
```

Pass `--event-log path/to/session.jsonl` if you want a stable capture path. With `--cwd`, relative event-log paths are resolved before launching harn so the log still lands under the launcher directory, not the target project.

## Redaction

`event-log-to-replay` redacts by default. It recursively walks event dictionaries, lists, and strings.

Whole values are replaced with `[redacted]` when the key matches one of:

```text
access_token api_key apikey auth_token authorization client_secret cookie
credential credentials id_token password private_key refresh_token secret
set_cookie token tokens
```

String values are also scanned for common token shapes, including API-key environment assignments, `Bearer ...`, `sk-...`, `sk-proj-...`, `github_pat_...`, and `ghp_...` / `gho_...` / `ghs_...` / `ghu_...`.

This is a heuristic safety net, not a privacy boundary. It does not generally redact arbitrary prompts, file paths, usernames, tracebacks, proprietary output, local project names, or uncommon secret formats. Raw captures should stay under ignored paths, and promoted fixtures should be reviewed manually.

Use `--no-redact-sensitive` only for private local debugging.

## Replay Review

Convert a captured event log into a replay fixture:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json \
  --output-result test-artifacts/replays/captured-session-result.json \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-review
```

`--visual-fixture` adds capture-summary metadata plus conservative screenshot expectations, so the converted trajectory can be run through `replay-dir --screenshot-dir` as a visual regression input. Conversion records redaction metadata with an enabled flag and replacement count.

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

Split conversion writes one fixture per chunk plus `manifest.json`. With `--review-dir`, conversion immediately replays the generated directory and writes one complete per-chunk review bundle under `files/` plus a top-level suite overview.

Replay fixtures can drive the same scene pipeline without a live harn process:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --output-scene test-artifacts/replays/scene.json \
  --output-result test-artifacts/replays/result.json \
  --review-dir test-artifacts/replays/review \
  --screenshot test-artifacts/replays/scene.png
```

The fixture format is documented in [replay.md](replay.md).

## Renderer Adapter Development

To exercise the external renderer process boundary, pass a renderer command. The command receives `harn-gibson.external-renderer-request.v1` JSON on stdin and returns a render plan on stdout.

```bash
uv run harn-gibson replay-dir examples/replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000
```

Use `examples/renderers/gibson_echo_renderer.py` when you want the smallest possible external-renderer contract example.

To exercise the model-prompt boundary without binding to a provider SDK, use a prompt-command renderer:

```bash
HARN_GIBSON_RENDERER_MODEL_COMMAND='uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS=10000 \
uv run harn-gibson run
```

Command failures are fail-open: the deterministic fallback still updates the scene, and the failure is added to the debug trace surface. Returned plans are validated against the current scene and catalog; unsafe plans are rejected and traced in the browser debug drawer.

## Timing And Routing

Render mode is configurable:

```bash
HARN_GIBSON_RENDER_MODE=blocking  # default
HARN_GIBSON_RENDER_MODE=async HARN_GIBSON_RENDER_BATCH_MS=40
HARN_GIBSON_RENDER_TIMING=immediate  # default
HARN_GIBSON_RENDER_TIMING=scheduled  # honor render-plan startOffsetMs during playback
HARN_GIBSON_PROJECT_ROOT=/path/to/project
HARN_GIBSON_PROJECT_NAME=my-project
```

Renderer event interest can be narrowed with JSON. Events outside the interest fall back locally instead of going to the renderer:

```bash
HARN_GIBSON_RENDERER_INTEREST='{"eventTypes":["tool_call","tool_result"],"fallbackRoute":"direct_scene"}'
```

Specific event types can be forced to renderer, direct scene, debug-only, or drop routes:

```bash
HARN_GIBSON_ROUTE_RULES='[{"eventType":"runtime_error","route":"debug_only"},{"eventType":"model_select","route":"drop"}]'
```

Noisy event types can also be sampled before routing:

```bash
HARN_GIBSON_ROUTE_RULES='[{"eventType":"session_tree","route":"renderer_agent","sampleEvery":4,"fallbackRoute":"debug_only"}]'
```

## Hook Modules

Hook modules are an optional harn-gibson extension point for local policy, diagnostics, and interdict experiments. They are not the event source for the viewer; harn events come from the Gibson harn extension itself.

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

## Acceptance

Run the full local acceptance gate before a release checkpoint:

```bash
bash scripts/acceptance.sh
```

Use `bash scripts/acceptance.sh --dry-run` to inspect the exact commands without running the heavyweight browser/replay gates. The script runs lint, the full covered test suite, a dynamic-port launcher smoke, generic replay screenshots, classic renderer screenshots, stress-renderer screenshots, whitespace checks, and runtime/secret hygiene scans.

Run the checked-in replay fixture suite:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

Run the classic renderer fixture:

```bash
uv run harn-gibson replay-dir examples/gibson1-replays \
  --renderer-command 'uv run python examples/renderers/gibson1_renderer.py' \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/gibson1-replays \
  --screenshot-dir test-artifacts/replays/gibson1-screenshots
```

Run the stress renderer fixture:

```bash
uv run harn-gibson replay-dir examples/dogfood-replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000 \
  --baseline-dir examples/baselines/dogfood-replays \
  --screenshot-dir test-artifacts/replays/dogfood-screenshots
```
