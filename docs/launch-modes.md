# Launch Modes

Gibson has one display protocol and several launch modes. The right owner depends on who controls the harn process.

## Recommended Shape

### Interactive harn, Second Visualizer

This is the default user story: harn is already running in a terminal and Gibson attaches as a parallel display.

Target UX:

```text
/gibson-view
```

The harn extension owns this mode. The command starts or reuses a local Gibson display server, opens the browser, publishes a bootstrap batch from the extension's recent-event buffer, and then streams future events to the display endpoint. Harn remains the primary input surface, while the browser composer can still enqueue small follow-up or steering messages.

Current support:

```text
/gibson-renderers
/gibson-view
/gibson-view --renderer default
/gibson-view --renderer classic
/gibson-view --renderer stress
/gibson-view --port 8765
/gibson-view --no-browser
```

Environment defaults:

```bash
HARN_GIBSON_VIEW_HOST=127.0.0.1
HARN_GIBSON_VIEW_PORT=0
HARN_GIBSON_VIEW_BROWSER=1
HARN_GIBSON_VIEW_RENDERER=default
HARN_GIBSON_RECENT_EVENTS=100
```

If `HARN_GIBSON_RENDERER_COMMAND`, `HARN_GIBSON_RENDERER_MODEL_COMMAND`, or `HARN_GIBSON_RENDERER` is already set, `/gibson-view` preserves that environment unless `HARN_GIBSON_VIEW_RENDERER` or an explicit command option overrides it.

Manual wiring is still available when you want separate process ownership:

```bash
uv run harn-gibson serve --host 127.0.0.1 --port 8765
HARN_GIBSON_ENDPOINT=http://127.0.0.1:8765/events harn -e /path/to/harn-gibson
```

### Browser as Primary Interface

In this mode Gibson owns process lifecycle: start the display server, open the browser, launch harn, and forward browser input through the extension's input poller.

This should remain a Gibson CLI launcher because the browser must exist before the first user prompt and the launcher must keep the display alive if harn exits with an error.

Current support:

```bash
uv run harn-gibson run
```

### Non-Interactive harn, Visualized

This is for prompted or scripted harn runs where the user still wants to watch the session.

This should also remain a Gibson CLI launcher. The launcher wires `HARN_GIBSON_ENDPOINT`, optional projection/style variables, event logging, and harn arguments in one process tree.

Current support:

```bash
uv run harn-gibson run -- -p "summarize this repo"
```

### Direct Replay and Review

Replay does not need harn. It feeds captured events, saved render plans, or scene mutations through the same scene pipeline and browser backend.

Current support:

```bash
uv run harn-gibson watch-replay examples/dogfood-replays/repo-map-trajectory.json
```

Add `--playback-timing real-time` to use source timestamp deltas, or `--renderer classic` / `--renderer stress` to call a built-in visualization live during event replay.

For deterministic checks:

```bash
uv run harn-gibson replay examples/claude-gibson-replays/linkjar-live-session.json \
  --renderer default \
  --screenshot test-artifacts/replay.png
```

## Package Boundary

`harn-gibson` is a harn package and a standalone CLI package.

The harn package entry point is `extensions/gibson.py`. That file adds `src/` to `sys.path` and exports `default = extension_factory`, so it works from a git checkout even when the Python package has not been installed into harn's environment.

The Python CLI entry point is `harn-gibson = harn_gibson.cli:main`. It owns server/browser/harn process orchestration for primary-browser, non-interactive, capture, and replay workflows.

Both package manifests declare the same extension path:

- `pyproject.toml` uses `[tool.harn]` for the documented harn package shape.
- `package.json` keeps compatibility with current harn resource discovery paths that still read harn manifests from `package.json`.

## Lifecycle Rule

The extension should be fail-open for harn progress. If no display server is running, event publish failures become diagnostics or sink state, not harn failures. The CLI launchers can be stricter because they own the display lifecycle and can keep the browser open to show harn errors.

## Future Work

- Promote `/gibson-view` from a bounded recent-event attach to fuller historical session bootstrap when harn exposes enough session history cheaply.
- Add a harn upstream follow-up if pyproject-only harn package manifests remain documented but unsupported in current discovery code.
