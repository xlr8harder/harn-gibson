# harn-gibson

`harn-gibson` is a first-pass display fixture for [secemp9/harn](https://github.com/secemp9/harn). It provides:

- a harn extension that hooks before, during, and after core agent events;
- a normalized JSON event stream for graphical displays;
- a persistent scene engine with primitives, animations, and mutations;
- a browser-based local display server with SSE updates;
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
HARN_GIBSON_ENDPOINT=http://127.0.0.1:8765/events \
harn -e "$(uv run harn-gibson extension-path)"
```

For offline inspection, write normalized events to JSONL:

```bash
HARN_GIBSON_EVENT_LOG=.harn-gibson.jsonl \
harn -e "$(uv run harn-gibson extension-path)"
```

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
