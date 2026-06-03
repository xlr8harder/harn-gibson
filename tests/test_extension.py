from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harn_gibson.extension import (
    HARN_EVENTS,
    GibsonRelay,
    build_dispatcher_from_env,
    extension_factory,
    extension_path,
)
from harn_gibson.hooks import HookDecision


class FakeSink:
    def __init__(self) -> None:
        self.published: list[tuple[object, list[HookDecision]]] = []

    async def publish(self, event: object, decisions: list[HookDecision] | None = None) -> None:
        self.published.append((event, list(decisions or [])))


class FakeHarn:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event_type: str, handler: object) -> None:
        self.handlers[event_type] = handler


class CamelUi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def setTitle(self, *args: object) -> None:
        self.calls.append(("setTitle", args))

    def setStatus(self, *args: object) -> None:
        self.calls.append(("setStatus", args))


class SnakeUi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def set_title(self, *args: object) -> None:
        self.calls.append(("set_title", args))

    def set_status(self, *args: object) -> None:
        self.calls.append(("set_status", args))


class FakeCtx:
    def __init__(self, ui: object | None) -> None:
        self.ui = ui


class NoMethodUi:
    pass


def test_relay_publishes_and_returns_hook_result() -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink)
    relay.dispatcher.on("tool_call", "before", lambda _event: HookDecision(block=True, reason="no"))

    result = asyncio.run(relay.handle({"type": "tool_call", "toolName": "bash"}, None))

    assert result == {"block": True, "reason": "no"}
    assert sink.published[0][0].event_type == "tool_call"  # type: ignore[attr-defined]
    assert sink.published[0][1] == [HookDecision(block=True, reason="no")]


def test_relay_updates_camel_and_snake_ui() -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink)
    camel = CamelUi()
    snake = SnakeUi()

    asyncio.run(relay.handle({"type": "session_start", "reason": "startup"}, FakeCtx(camel)))
    asyncio.run(relay.handle({"type": "session_shutdown", "reason": "quit"}, FakeCtx(snake)))
    asyncio.run(relay.handle({"type": "agent_start"}, FakeCtx(NoMethodUi())))

    assert ("setTitle", ("harn // gibson link",)) in camel.calls
    assert ("setStatus", ("gibson", "lifecycle:session_start")) in camel.calls
    assert ("set_status", ("gibson", None)) in snake.calls


def test_extension_factory_registers_every_event(monkeypatch: Any) -> None:
    monkeypatch.setenv("HARN_GIBSON_ENDPOINT", "none")
    harn = FakeHarn()

    extension_factory(harn)

    assert set(harn.handlers) == set(HARN_EVENTS)
    handler = harn.handlers["input"]
    result = asyncio.run(handler({"type": "input", "text": "hi", "source": "interactive"}, FakeCtx(None)))  # type: ignore[operator]
    assert result is None


def test_build_dispatcher_from_env_loads_paths(tmp_path: Path) -> None:
    hook = tmp_path / "hook.py"
    hook.write_text(
        "from harn_gibson import HookDecision\n"
        "def register_gibson_hooks(dispatcher):\n"
        "    dispatcher.on('input', 'before', lambda event: HookDecision(action='handled'))\n",
        encoding="utf-8",
    )
    dispatcher = build_dispatcher_from_env({"HARN_GIBSON_HOOKS": f"{hook}:"})
    relay = GibsonRelay(FakeSink(), dispatcher)

    assert asyncio.run(relay.handle({"type": "input", "text": "x", "source": "rpc"})) == {"action": "handled"}
    assert extension_path().endswith("extension.py")
