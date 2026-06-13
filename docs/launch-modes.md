# Launch Modes

Gibson has one display protocol and several launch modes. The right owner depends on who controls the harn process.

## Recommended Shape

### Interactive harn, Second Visualizer

This is the default user story: harn is already running in a terminal and Gibson attaches as a parallel display.

Target UX:

```text
/gibson-view
```

The harn extension should own this mode. The command should start or reuse a local Gibson display server, open the browser, publish a bootstrap event from the extension's recent-event buffer, and then stream future events to the display endpoint. Harn remains the primary input surface, while the browser composer can still enqueue small follow-up or steering messages.

Current support:

```bash
uv run harn-gibson serve --host 127.0.0.1 --port 8765
HARN_GIBSON_ENDPOINT=http://127.0.0.1:8765/events harn -e /path/to/harn-gibson
```

The `/gibson-view` command is not implemented yet. It should be added after the embedded server lifecycle is small enough to expose safely from the extension.

### Browser as Primary Interface

In this mode Gibson owns process lifecycle: start the display server, open the browser, launch harn, and forward browser input through the extension's input poller.

This should remain a Gibson CLI launcher because the browser must exist before the first user prompt and the launcher must keep the display alive if harn exits with an error.

Current support:

```bash
uv run harn-gibson dogfood
```

### Non-Interactive harn, Visualized

This is for prompted or scripted harn runs where the user still wants to watch the session.

This should also remain a Gibson CLI launcher. The launcher wires `HARN_GIBSON_ENDPOINT`, optional projection/style variables, event logging, and harn arguments in one process tree.

Current support:

```bash
uv run harn-gibson dogfood -- -p "summarize this repo"
```

### Direct Replay and Review

Replay does not need harn. It feeds captured events, saved render plans, or scene mutations through the same scene pipeline and browser backend.

Current support:

```bash
uv run harn-gibson watch-replay examples/claude-gibson-replays/linkjar-live-session.json \
  --projection examples/projections/gibson-organic.json \
  --playback-timing real-time
```

For deterministic checks:

```bash
uv run harn-gibson replay examples/claude-gibson-replays/linkjar-live-session.json \
  --projection examples/projections/gibson-organic.json \
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

- Add `/gibson-view` or `/gibson view` once harn command naming and argument parsing are confirmed.
- Keep a bounded in-extension event buffer so late viewer attach can show recent context.
- Factor embedded display-server startup out of the CLI so the extension command and CLI launchers share one implementation.
- Add a harn upstream follow-up if pyproject-only harn package manifests remain documented but unsupported in current discovery code.
