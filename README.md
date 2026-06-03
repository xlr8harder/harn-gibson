# harn-gibson

`harn-gibson` is a first-pass display fixture for [secemp9/harn](https://github.com/secemp9/harn). It provides:

- a harn extension that hooks before, during, and after core agent events;
- a normalized JSON event stream for graphical displays;
- a persistent scene engine with primitives, animations, and mutations;
- a browser-based local display server with SSE updates and an input composer;
- a hook dispatcher so future policies can block, transform, or post-process events.

The current display agent is deterministic. The later LLM-driven visualization layer can consume the same event stream and emit scene mutations without changing the harn integration.

## Development

```bash
uv sync
uv run pytest
```

Coverage is enforced at 100% for the Python package.

Install `harn` separately when you want to run against a live agent.

## Run The Display

Start the graphical display server:

```bash
uv run harn-gibson serve --host 127.0.0.1 --port 8765
```

In another terminal, run harn with the extension:

```bash
harn
```

Run that from the repo root. Project-local `.harn/settings.json` selects the Codex provider/model and points harn at `.harn/extensions/gibson.py`; that shim adds `src/` to `sys.path` and loads the real `harn_gibson.extension` module. Because the shim lives in `.harn/extensions/`, `/reload` can reload it during development.

For an isolated quick test that ignores other discovered extensions:

```bash
harn --no-extensions -e .harn/extensions/gibson.py
```

The browser page can be used as the primary input surface. Submitted text is queued on the display server at `/input`; the harn extension polls `/input/next` and forwards messages via `harn.sendUserMessage`.

Delivery modes:

- `queue`: default. Runs immediately if harn is idle, or queues as a follow-up if harn is streaming.
- `steer`: queues steering input for the active agent run.

The raw event details, event feed, and hook decisions are in the debug drawer. Use `DEBUG` to open it and `CLOSE` inside the drawer to collapse it.

Project-local harn settings in `.harn/settings.json` select the `openai-codex` provider, `gpt-5.5`, and this extension. Run `/login` in harn and choose ChatGPT Plus/Pro (Codex) if credentials are not already stored.

Render mode is configurable:

```bash
HARN_GIBSON_RENDER_MODE=blocking  # default
HARN_GIBSON_RENDER_MODE=async HARN_GIBSON_RENDER_BATCH_MS=40
```

For offline inspection, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn --no-extensions -e .harn/extensions/gibson.py
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
