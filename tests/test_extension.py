from __future__ import annotations

import asyncio
import json
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.extension import (
    HARN_EVENTS,
    BrowserInputPoller,
    GibsonRelay,
    GibsonViewController,
    _append_harn_entry,
    _notify_command_context,
    _parse_view_command_args,
    _poll_interval_from_env,
    _recent_event_limit_from_env,
    _register_renderer_command,
    _register_view_command,
    _viewer_env_for_options,
    build_dispatcher_from_env,
    build_input_endpoint_from_env,
    default,
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
        self.commands: dict[str, dict[str, object]] = {}
        self.sent: list[tuple[str, dict[str, str]]] = []
        self.entries: list[tuple[str, object]] = []

    def on(self, event_type: str, handler: object) -> None:
        self.handlers[event_type] = handler

    def registerCommand(self, name: str, options: dict[str, object]) -> None:
        self.commands[name] = options

    def appendEntry(self, custom_type: str, data: object = None) -> None:
        self.entries.append((custom_type, data))

    def sendUserMessage(self, message: str, options: dict[str, str]) -> None:
        self.sent.append((message, options))


class FailingHarn(FakeHarn):
    def sendUserMessage(self, _message: str, _options: dict[str, str]) -> None:
        raise RuntimeError("delivery failed")


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


class CommandUi:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.statuses: list[tuple[str, str | None]] = []

    def notify(self, message: str, level: str) -> None:
        self.notifications.append((message, level))

    def setStatus(self, key: str, text: str | None) -> None:
        self.statuses.append((key, text))


def test_relay_publishes_and_returns_hook_result() -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink)
    relay.dispatcher.on("tool_call", "before", lambda _event: HookDecision(block=True, reason="no"))

    result = asyncio.run(relay.handle({"type": "tool_call", "toolName": "bash"}, None))

    assert result == {"block": True, "reason": "no"}
    assert sink.published[0][0].event_type == "tool_call"  # type: ignore[attr-defined]
    assert sink.published[0][1] == [HookDecision(block=True, reason="no")]


def test_relay_publishes_hook_exceptions_as_runtime_errors() -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink)

    def broken(_event: object) -> None:
        raise RuntimeError("hook failed")

    relay.dispatcher.on("tool_call", "before", broken)

    result = asyncio.run(relay.handle({"type": "tool_call", "toolName": "bash"}, None))

    assert result is None
    assert sink.published[0][0].event_type == "runtime_error"  # type: ignore[attr-defined]
    assert "hook failed" in sink.published[0][0].payload["traceback"]  # type: ignore[attr-defined]
    assert sink.published[1][0].event_type == "tool_call"  # type: ignore[attr-defined]


def test_relay_buffers_recent_events_and_flushes_to_sink() -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink, max_recent_events=2)
    flush_sink = FakeSink()

    asyncio.run(relay.handle({"type": "input", "text": "one"}))
    asyncio.run(relay.handle({"type": "tool_call", "toolName": "bash"}))
    asyncio.run(relay.handle({"type": "tool_result", "toolName": "bash"}))
    asyncio.run(relay.flush_recent(flush_sink))

    assert [event.sequence for event, _decisions in relay.recent_events] == [2, 3]
    assert [event.sequence for event, _decisions in flush_sink.published] == [2, 3]  # type: ignore[attr-defined]
    relay.max_recent_events = 0
    asyncio.run(relay.handle({"type": "turn_end"}))
    assert [event.sequence for event, _decisions in relay.recent_events] == [2, 3]


def test_relay_attaches_http_endpoint_without_event_log(monkeypatch: Any) -> None:
    class FakeHttpSink:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint

        async def publish(self, _event: object, _decisions: list[object] | None = None) -> None:
            return None

    relay = GibsonRelay(FakeSink())
    monkeypatch.setattr("harn_gibson.extension.HttpEventSink", FakeHttpSink)

    sink = relay.attach_http_endpoint("http://viewer/events", {})

    assert sink is relay.sink
    assert sink.endpoint == "http://viewer/events"  # type: ignore[attr-defined]


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


def test_extension_file_loads_the_way_harn_loads_it(monkeypatch: Any) -> None:
    # harn executes the entry file as a synthetic module WITHOUT registering it
    # in sys.modules; module-level dataclasses crash that path because the
    # dataclass machinery resolves annotations via sys.modules[__module__].
    import importlib.util

    monkeypatch.setenv("HARN_GIBSON_ENDPOINT", "none")
    spec = importlib.util.spec_from_file_location("harn_extension_synthetic", extension_path())
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not raise
    harn = FakeHarn()
    module.default(harn)
    assert set(harn.handlers) == set(HARN_EVENTS)


def test_extension_factory_registers_every_event(monkeypatch: Any) -> None:
    monkeypatch.setenv("HARN_GIBSON_ENDPOINT", "none")
    harn = FakeHarn()

    extension_factory(harn)

    assert set(harn.handlers) == set(HARN_EVENTS)
    assert sorted(harn.commands) == ["gibson-renderers", "gibson-view"]
    assert default is extension_factory
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


def test_view_command_arg_parsing_and_recent_limit() -> None:
    parsed = _parse_view_command_args(
        "--host 0.0.0.0 --port 777 --no-browser",
        {
            "HARN_GIBSON_VIEW_HOST": "localhost",
            "HARN_GIBSON_VIEW_PORT": "8888",
            "HARN_GIBSON_VIEW_BROWSER": "true",
        },
    )
    assert {key: parsed[key] for key in ("host", "port", "browser", "renderer")} == {
        "host": "0.0.0.0",
        "port": 777,
        "browser": False,
        "renderer": "default",
    }
    parsed = _parse_view_command_args(
        "--host=::1 --port=bad --browser --renderer stress --style mainframe --renderer-timeout-ms 1234",
        {
            "HARN_GIBSON_VIEW_PORT": "1234",
            "HARN_GIBSON_VIEW_BROWSER": "0",
        },
    )
    assert parsed == {
        "host": "::1",
        "port": 1234,
        "browser": True,
        "renderer": "stress",
        "renderer_command": None,
        "style": "mainframe",
        "renderer_timeout_ms": "1234",
        "list_renderers": False,
    }
    assert _parse_view_command_args("--port -5", {})["port"] == 0
    parsed = _parse_view_command_args("--port", {"HARN_GIBSON_VIEW_PORT": "bad"})
    assert {key: parsed[key] for key in ("host", "port", "browser", "renderer")} == {
        "host": "127.0.0.1",
        "port": 0,
        "browser": True,
        "renderer": "default",
    }
    assert _parse_view_command_args("--unknown", {"HARN_GIBSON_VIEW_BROWSER": "no"})["browser"] is False
    assert _parse_view_command_args(
        "--renderer=bad --renderer classic --renderer-command='python renderer.py'",
        {},
    )["renderer"] == "command"
    assert _parse_view_command_args("--renderer=default --list-renderers", {})["list_renderers"] is True
    parsed = _parse_view_command_args(
        "--renderer-command python-custom --renderer examples/renderer.json "
        "--style=neon-noir --renderer-timeout-ms=333",
        {},
    )
    assert parsed["renderer"] == "examples/renderer.json"
    assert parsed["renderer_command"] == "python-custom"
    assert parsed["style"] == "neon-noir"
    assert parsed["renderer_timeout_ms"] == "333"
    assert _parse_view_command_args(
        "",
        {"HARN_GIBSON_RENDERER_COMMAND": "python renderer.py"},
    )["renderer"] is None
    assert _recent_event_limit_from_env({"HARN_GIBSON_RECENT_EVENTS": "2"}) == 2
    assert _recent_event_limit_from_env({"HARN_GIBSON_RECENT_EVENTS": "-1"}) == 0
    assert _recent_event_limit_from_env({"HARN_GIBSON_RECENT_EVENTS": "bad"}) == 100


def test_viewer_env_for_renderer_options() -> None:
    base_env = {
        "HARN_GIBSON_RENDERER_COMMAND": "python old.py",
        "HARN_GIBSON_RENDERER_MODEL_COMMAND": "python model.py",
        "HARN_GIBSON_RENDERER": "old",
    }

    classic_env = _viewer_env_for_options(
        base_env,
        {"renderer": "classic", "renderer_command": None, "style": "mainframe"},
    )
    assert "gibson1_renderer.py" in classic_env["HARN_GIBSON_RENDERER_COMMAND"]
    assert classic_env["HARN_GIBSON_STYLE"] == "mainframe"
    assert "HARN_GIBSON_RENDERER_MODEL_COMMAND" not in classic_env
    assert "HARN_GIBSON_RENDERER" not in classic_env

    default_env = _viewer_env_for_options(
        base_env,
        {"renderer": "default", "renderer_command": None, "renderer_timeout_ms": "500"},
    )
    assert default_env["HARN_GIBSON_RENDERER"] == "default"
    assert "HARN_GIBSON_RENDERER_COMMAND" not in default_env
    assert "HARN_GIBSON_RENDERER_TIMEOUT_MS" not in default_env

    command_env = _viewer_env_for_options(
        base_env,
        {"renderer": "command", "renderer_command": "python custom.py"},
    )
    assert command_env["HARN_GIBSON_RENDERER_COMMAND"] == "python custom.py"
    assert "HARN_GIBSON_RENDERER" not in command_env

    spec_env = _viewer_env_for_options(
        base_env,
        {"renderer": "examples/renderer.json", "renderer_command": None},
    )
    assert spec_env["HARN_GIBSON_RENDERER"] == "examples/renderer.json"
    assert "HARN_GIBSON_RENDERER_COMMAND" not in spec_env


def test_gibson_view_controller_attaches_viewer_and_reuses_it(tmp_path: Path, monkeypatch: Any) -> None:
    class FakeHttpSink:
        instances: list[FakeHttpSink] = []

        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.published: list[tuple[object, list[object]]] = []
            self.instances.append(self)

        async def publish(self, event: object, decisions: list[object] | None = None) -> None:
            self.published.append((event, list(decisions or [])))

    class FakeViewer:
        display_url = "http://127.0.0.1:9900"
        endpoint = "http://127.0.0.1:9900/events"
        input_endpoint = "http://127.0.0.1:9900/input/next"
        closed = False

        def __init__(self) -> None:
            self.opened = 0
            self.closed_count = 0

        def open_browser(self) -> None:
            self.opened += 1

        def close(self) -> None:
            self.closed = True
            self.closed_count += 1

    created: list[tuple[str, int, dict[str, str], bool]] = []
    viewer = FakeViewer()

    def fake_viewer_factory(host: str, port: int, *, env: dict[str, str], launch_browser: bool) -> FakeViewer:
        created.append((host, port, env, launch_browser))
        return viewer

    monkeypatch.setattr("harn_gibson.extension.HttpEventSink", FakeHttpSink)
    event_log = tmp_path / "events.jsonl"
    harn = FakeHarn()
    relay = GibsonRelay(FakeSink())
    asyncio.run(relay.handle({"type": "input", "text": "before attach"}))
    poller = BrowserInputPoller(harn, None)
    ui = CommandUi()
    controller = GibsonViewController(
        harn,
        relay,
        poller,
        environ={"HARN_GIBSON_EVENT_LOG": str(event_log)},
        viewer_factory=fake_viewer_factory,
    )

    asyncio.run(controller.handle("--host 127.0.0.2 --port 9900 --no-browser", FakeCtx(ui)))
    asyncio.run(controller.handle("--browser", FakeCtx(ui)))
    controller.stop()
    controller.stop()

    assert created[0][0:2] == ("127.0.0.2", 9900)
    assert created[0][3] is False
    assert created[0][2]["HARN_GIBSON_EVENT_LOG"] == str(event_log)
    assert created[0][2]["HARN_GIBSON_RENDERER"] == "default"
    assert "HARN_GIBSON_RENDERER_COMMAND" not in created[0][2]
    assert viewer.opened == 1
    assert viewer.closed_count == 1
    assert poller.endpoint == "http://127.0.0.1:9900/input/next"
    assert harn.entries[0][0] == "gibson_view"
    assert "Gibson viewer attached" in ui.notifications[0][0]
    assert [event.event_type for event, _decisions in FakeHttpSink.instances[0].published] == [  # type: ignore[attr-defined]
        "input",
        "viewer_attach",
    ]
    assert [event.event_type for event, _decisions in FakeHttpSink.instances[1].published][-1] == "viewer_attach"  # type: ignore[attr-defined]
    assert "viewer_attach" in event_log.read_text(encoding="utf-8")


def test_gibson_renderer_list_command_reports_renderers() -> None:
    harn = FakeHarn()
    relay = GibsonRelay(FakeSink())
    poller = BrowserInputPoller(harn, None)
    controller = GibsonViewController(harn, relay, poller)
    ui = CommandUi()

    _register_renderer_command(harn, controller)
    handler = harn.commands["gibson-renderers"]["handler"]
    asyncio.run(handler("", FakeCtx(ui)))  # type: ignore[operator]

    assert "default" in ui.notifications[0][0]
    assert harn.entries[0][0] == "gibson_view"
    assert relay.recent_events[0][0].event_type == "viewer_renderers"


def test_gibson_view_controller_reports_startup_errors(monkeypatch: Any) -> None:
    def broken_viewer_factory(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("viewer boom")

    harn = FakeHarn()
    sink = FakeSink()
    relay = GibsonRelay(sink)
    poller = BrowserInputPoller(harn, None)
    ui = CommandUi()
    controller = GibsonViewController(harn, relay, poller, viewer_factory=broken_viewer_factory)

    asyncio.run(controller.handle("", FakeCtx(ui)))

    assert ui.notifications == [("Gibson viewer failed: viewer boom", "error")]
    assert sink.published[0][0].event_type == "runtime_error"  # type: ignore[attr-defined]
    assert "viewer boom" in sink.published[0][0].payload["message"]  # type: ignore[attr-defined]


def test_gibson_view_controller_default_factory_and_stop_without_close(monkeypatch: Any) -> None:
    class FakeViewer:
        display_url = "http://viewer"
        endpoint = "http://viewer/events"
        input_endpoint = "http://viewer/input/next"
        closed = False

    calls: list[tuple[str, int, dict[str, str], bool]] = []

    def fake_start_viewer(host: str, port: int, *, env: dict[str, str], launch_browser: bool) -> FakeViewer:
        calls.append((host, port, env, launch_browser))
        return FakeViewer()

    monkeypatch.setattr("harn_gibson.viewer.start_viewer", fake_start_viewer)
    controller = GibsonViewController(FakeHarn(), GibsonRelay(FakeSink()), BrowserInputPoller(FakeHarn(), None))
    options = {"host": "127.0.0.9", "port": 9999, "browser": False, "renderer": None}
    viewer = controller._start_or_reuse_viewer(options)
    reused = controller._start_or_reuse_viewer({"host": "127.0.0.8", "port": 8888, "browser": True, "renderer": None})
    reused_without_browser = controller._start_or_reuse_viewer(
        {"host": "127.0.0.7", "port": 7777, "browser": False, "renderer": None}
    )
    controller.viewer = object()
    controller.stop()

    assert viewer.display_url == "http://viewer"
    assert reused is viewer
    assert reused_without_browser is viewer
    assert calls == [("127.0.0.9", 9999, dict(os.environ), False)]


def test_optional_harn_command_helpers_handle_missing_capabilities() -> None:
    ui = SnakeUi()
    controller = GibsonViewController(object(), GibsonRelay(FakeSink()), BrowserInputPoller(object(), None))

    _register_view_command(object(), controller)
    _register_renderer_command(object(), controller)
    _notify_command_context(FakeCtx(None), "hidden")
    _notify_command_context(FakeCtx(ui), "visible")
    _append_harn_entry(object(), "attached", {"url": "http://viewer"})

    assert ("set_status", ("gibson", "visible")) in ui.calls


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


def test_browser_input_poller_reports_delivery_exceptions(monkeypatch: Any) -> None:
    sink = FakeSink()
    relay = GibsonRelay(sink)
    poller = BrowserInputPoller(FailingHarn(), "http://x/input/next", diagnostic_relay=relay)

    async def fake_fetch(_endpoint: str, _timeout: float) -> dict[str, object]:
        return {"id": "input-1", "message": "first", "deliverAs": "steer"}

    monkeypatch.setattr("harn_gibson.extension.fetch_browser_input", fake_fetch)

    assert asyncio.run(poller.poll_once()) is False
    assert sink.published[0][0].event_type == "runtime_error"  # type: ignore[attr-defined]
    assert "delivery failed" in sink.published[0][0].payload["message"]  # type: ignore[attr-defined]
    assert "input=input-1" in sink.published[0][0].payload["details"]  # type: ignore[attr-defined]


def test_browser_input_poller_ignores_delivery_exception_without_relay(monkeypatch: Any) -> None:
    poller = BrowserInputPoller(FailingHarn(), "http://x/input/next")

    async def fake_fetch(_endpoint: str, _timeout: float) -> dict[str, object]:
        return {"id": "input-1", "message": "first", "deliverAs": "steer"}

    monkeypatch.setattr("harn_gibson.extension.fetch_browser_input", fake_fetch)

    assert asyncio.run(poller.poll_once()) is False


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
