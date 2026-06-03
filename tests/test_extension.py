from __future__ import annotations

import asyncio
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.extension import (
    HARN_EVENTS,
    BrowserInputPoller,
    GibsonRelay,
    _poll_interval_from_env,
    build_dispatcher_from_env,
    build_input_endpoint_from_env,
    extension_factory,
    extension_path,
    fetch_browser_input,
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
        self.sent: list[tuple[str, dict[str, str]]] = []

    def on(self, event_type: str, handler: object) -> None:
        self.handlers[event_type] = handler

    def sendUserMessage(self, message: str, options: dict[str, str]) -> None:
        self.sent.append((message, options))


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

    start = harn.handlers["session_start"]
    shutdown = harn.handlers["session_shutdown"]
    asyncio.run(start({"type": "session_start", "reason": "startup"}, FakeCtx(None)))  # type: ignore[operator]
    asyncio.run(shutdown({"type": "session_shutdown", "reason": "quit"}, FakeCtx(None)))  # type: ignore[operator]


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


def test_input_endpoint_from_env() -> None:
    assert build_input_endpoint_from_env({"HARN_GIBSON_INPUT_ENDPOINT": "http://x/custom"}) == "http://x/custom"
    assert build_input_endpoint_from_env({"HARN_GIBSON_ENDPOINT": "none"}) is None
    assert build_input_endpoint_from_env({"HARN_GIBSON_ENDPOINT": "http://x/events"}) == "http://x/input/next"
    assert build_input_endpoint_from_env({"HARN_GIBSON_ENDPOINT": "http://x/base/"}) == "http://x/base/input/next"


def test_browser_input_poller_poll_once(monkeypatch: Any) -> None:
    harn = FakeHarn()
    poller = BrowserInputPoller(harn, "http://x/input/next")
    responses = [
        {"message": "first", "deliverAs": "steer"},
        {"message": "second", "deliverAs": "bad"},
        {"message": ""},
        None,
    ]

    async def fake_fetch(_endpoint: str, _timeout: float) -> dict[str, object] | None:
        return responses.pop(0)

    monkeypatch.setattr("harn_gibson.extension.fetch_browser_input", fake_fetch)

    assert asyncio.run(poller.poll_once()) is True
    assert asyncio.run(poller.poll_once()) is True
    assert asyncio.run(poller.poll_once()) is False
    assert asyncio.run(poller.poll_once()) is False
    assert harn.sent == [
        ("first", {"deliverAs": "steer"}),
        ("second", {"deliverAs": "followUp"}),
    ]
    assert asyncio.run(BrowserInputPoller(harn, None).poll_once()) is False
    responses.append({"message": "lost"})
    assert asyncio.run(BrowserInputPoller(object(), "http://x").poll_once()) is False


def test_browser_input_poller_start_stop_and_run(monkeypatch: Any) -> None:
    class OneLoopPoller(BrowserInputPoller):
        async def poll_once(self) -> bool:
            return False

    async def fake_sleep(_interval: float) -> None:
        raise asyncio.CancelledError

    async def run_case() -> None:
        harn = FakeHarn()
        poller = OneLoopPoller(harn, None)
        poller.start()
        assert poller.task is None
        poller.endpoint = "http://x"
        poller.start()
        task = poller.task
        assert task is not None
        poller.start()
        assert poller.task is task
        poller.stop()
        assert poller.task is None

        monkeypatch.setattr("harn_gibson.extension.asyncio.sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await OneLoopPoller(harn, "http://x")._run()

    asyncio.run(run_case())


def test_fetch_browser_input(monkeypatch: Any) -> None:
    class Response:
        def __init__(self, status: int, body: object = None) -> None:
            self.status = status
            self.body = body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.body).encode("utf-8")

    class InvalidJsonResponse(Response):
        def read(self) -> bytes:
            return b"{"

    responses: list[object] = [
        Response(204),
        Response(200, {"message": "hi"}),
        Response(200, []),
        urllib.error.HTTPError("http://x", 204, "none", {}, None),
        urllib.error.HTTPError("http://x", 500, "bad", {}, None),
        OSError("down"),
        InvalidJsonResponse(200),
    ]

    def fake_urlopen(_request: object, *, timeout: float) -> object:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert asyncio.run(fetch_browser_input("http://x")) is None
    assert asyncio.run(fetch_browser_input("http://x")) == {"message": "hi"}
    assert asyncio.run(fetch_browser_input("http://x")) is None
    assert asyncio.run(fetch_browser_input("http://x")) is None
    assert asyncio.run(fetch_browser_input("http://x")) is None


def test_poll_interval_from_env() -> None:
    assert _poll_interval_from_env({"HARN_GIBSON_INPUT_POLL_MS": "10"}) == 0.05
    assert _poll_interval_from_env({"HARN_GIBSON_INPUT_POLL_MS": "500"}) == 0.5
    assert _poll_interval_from_env({"HARN_GIBSON_INPUT_POLL_MS": "bad"}) == 0.25
    assert asyncio.run(fetch_browser_input("http://x")) is None
    assert asyncio.run(fetch_browser_input("http://x")) is None
