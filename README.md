# harn-gibson

`harn-gibson` is a first-pass display fixture for [secemp9/harn](https://github.com/secemp9/harn). It provides:

- a harn extension that hooks before, during, and after core agent events;
- a normalized JSON event stream for graphical displays;
- a persistent scene engine with primitives, animations, and mutations;
- a browser-based local display server with SSE updates and an input composer;
- a hook dispatcher so future policies can block, transform, or post-process events.

The current display agent is deterministic. The later LLM-driven visualization layer can consume the same event stream and emit scene mutations without changing the harn integration.

`harn` is included as a development dependency so `uv run harn-gibson dogfood` works from this checkout. The display server and extension modules do not import `harn` or `harn-tui`; the dogfood launcher is the only place that shells out to the harn CLI. A future packaging split should keep the web relay installable without harn's terminal UI stack.

The deterministic renderer remains the default. Events pass through a routing layer before rendering: normal events become renderer requests, streaming assistant deltas update a local `text_stream` primitive, and debug-only events can bypass renderer execution. The deterministic fallback emits browser-rendered `city_block`, `node_graph`, `ribbon`, `glyph_layer`, and `particle_field` primitives, including a bounded repo map when renderer context includes topology or touched-file data. The browser renderer also supports low-level `mesh`, cinematic `hologram` projections, animated `trace_route` paths, constrained `svg_layer` vector primitives with transform keyframes, `data_rain` glyph curtains, and persistent scene animations for pulses, packet bursts, scans, glitches, flythrough rays, extrusion frames, and hold brackets. Dogfood runs can opt into either an external render-plan command or a prompt-command model adapter; unsafe external/model plans are rejected before scene application and recorded as diagnostics. The display server exposes `/catalog`, a generic primitive/effect catalog for future renderer prompts.

Renderer implementations can stay simple with `render(requests, scene)`, or opt into `render_with_context(requests, scene, context)` to receive compact project metadata, scene state, catalog entries, recent agent context, and recent visualization history. See [docs/renderer-agent.md](docs/renderer-agent.md) for the context and compaction contract.

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
```

Immediate timing keeps dogfood and replay runs responsive while still honoring explicit `delayMs`. Scheduled timing treats `startOffsetMs` as an absolute offset inside the coalesced render batch, which is useful for async renderer-agent plans that want a 5-10 second visual playback window after harn has already continued.

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

To dogfood the renderer-agent process boundary without a live model call, point the server at an external renderer command. The command receives `harn-gibson.external-renderer-request.v1` JSON on stdin and returns a render plan with `steps` on stdout:

```bash
HARN_GIBSON_RENDERER_COMMAND='uv run python examples/renderers/gibson_echo_renderer.py' \
HARN_GIBSON_RENDERER_TIMEOUT_MS=10000 \
uv run harn-gibson dogfood
```

Renderer command failures are fail-open: the deterministic renderer still updates the scene, and the failure is added to the debug trace surface. Returned plans are also validated against the current scene and catalog. Unsupported but safe primitives/effects are kept with `renderPlanDiagnostics` warnings in render intent metadata; unsafe plans such as missing patch targets, raw `svg_layer` markup, or unbounded vector keyframes are rejected, replaced with deterministic fallback output, and traced in the browser debug drawer.

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
  --renderer-command 'uv run python examples/renderers/gibson_echo_renderer.py' \
  --renderer-timeout-ms 10000
```

For offline inspection, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn --no-extensions -e .harn/extensions/gibson.py
```

Convert a captured event log into a replay fixture:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json
```

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

Replay files can include final-scene expectations so fixtures act as verifiers as well as demos. The fixture format is documented in [docs/replay.md](docs/replay.md).

The checked-in replay set includes agent-side routing, renderer-side plan, primitive-gallery, and animation-gallery fixtures so browser screenshots can review both harness behavior and generic visual primitives/effects.

Run the checked-in replay fixture suite:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

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
