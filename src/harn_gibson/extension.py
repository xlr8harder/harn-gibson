"""harn extension entry point."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.hooks import HookDispatcher, load_hook_module, result_for_harn
from harn_gibson.renderers import (
    DEFAULT_RENDERER,
    RENDERERS,
    direct_renderer_command,
    normalize_renderer,
    renderer_listing,
)
from harn_gibson.sinks import (
    DEFAULT_ENDPOINT,
    CompositeSink,
    EventSink,
    HttpEventSink,
    JsonlEventSink,
    build_sink_from_env,
)

HARN_EVENTS = (
    "resources_discover",
    "session_start",
    "session_before_switch",
    "session_before_fork",
    "session_before_compact",
    "session_compact",
    "session_shutdown",
    "session_before_tree",
    "session_tree",
    "context",
    "before_provider_request",
    "after_provider_response",
    "before_agent_start",
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "model_select",
    "thinking_level_select",
    "tool_call",
    "tool_result",
    "user_bash",
    "input",
)


class GibsonRelay:
    def __init__(
        self,
        sink: EventSink,
        dispatcher: HookDispatcher | None = None,
        *,
        max_recent_events: int = 100,
    ) -> None:
        self.sink = sink
        self.dispatcher = dispatcher or HookDispatcher()
        self.sequence = 0
        self.max_recent_events = max_recent_events
        self.recent_events: list[tuple[GibsonEvent, list[Any]]] = []

    async def handle(self, raw_event: Any, ctx: Any = None) -> Any:
        self.sequence += 1
        event = GibsonEvent.from_raw(raw_event, self.sequence)
        try:
            decisions = await self.dispatcher.dispatch(event)
        except Exception as error:  # noqa: BLE001
            await self.publish_exception(
                error,
                "Gibson hook dispatch failed",
                details=f"event={event.event_type} phase={event.phase}",
            )
            decisions = []
        _update_harn_ui(ctx, event)
        await self.publish(event, decisions)
        return result_for_harn(event.event_type, decisions)

    async def publish(self, event: GibsonEvent, decisions: Iterable[Any] = ()) -> None:
        cached = list(decisions)
        self._remember(event, cached)
        await self.sink.publish(event, cached)

    async def publish_diagnostic(
        self,
        *,
        event_type: str,
        severity: str,
        title: str,
        message: str,
        details: str | None = None,
    ) -> None:
        self.sequence += 1
        event = diagnostic_event(
            self.sequence,
            event_type=event_type,
            source="harn-gibson",
            severity=severity,
            title=title,
            message=message,
            details=details,
        )
        await self.publish(event, ())

    async def publish_exception(self, error: BaseException, message: str, *, details: str | None = None) -> None:
        self.sequence += 1
        event = diagnostic_event(
            self.sequence,
            event_type="runtime_error",
            source="harn-gibson",
            severity="error",
            title="Runtime error",
            message=f"{message}: {error}",
            details=details,
            traceback_text="".join(traceback.format_exception(error)),
        )
        await self.publish(event, ())

    def attach_http_endpoint(self, endpoint: str, environ: Mapping[str, str] | None = None) -> EventSink:
        env = os.environ if environ is None else environ
        http_sink = HttpEventSink(endpoint)
        sinks: list[EventSink] = [http_sink]
        event_log = env.get("HARN_GIBSON_EVENT_LOG")
        if event_log:
            sinks.append(JsonlEventSink(Path(event_log)))
        self.sink = sinks[0] if len(sinks) == 1 else CompositeSink(sinks)
        return http_sink

    async def flush_recent(self, sink: EventSink) -> None:
        for event, decisions in list(self.recent_events):
            await sink.publish(event, decisions)

    def _remember(self, event: GibsonEvent, decisions: list[Any]) -> None:
        if self.max_recent_events <= 0:
            return
        self.recent_events.append((event, list(decisions)))
        if len(self.recent_events) > self.max_recent_events:
            del self.recent_events[: len(self.recent_events) - self.max_recent_events]


class BrowserInputPoller:
    # Deliberately NOT a dataclass: released harn versions execute extension
    # files without registering them in sys.modules, and dataclass machinery can
    # crash while resolving string annotations. Keep this entry path loader-safe.
    def __init__(
        self,
        harn: Any,
        endpoint: str | None,
        diagnostic_relay: GibsonRelay | None = None,
        poll_interval: float = 0.25,
        timeout: float = 0.2,
    ) -> None:
        self.harn = harn
        self.endpoint = endpoint
        self.diagnostic_relay = diagnostic_relay
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.endpoint is None:
            return
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def set_endpoint(self, endpoint: str | None) -> None:
        self.endpoint = endpoint

    async def poll_once(self) -> bool:
        if self.endpoint is None:
            return False
        item = await fetch_browser_input(self.endpoint, self.timeout)
        if item is None:
            return False
        message = item.get("message")
        if not isinstance(message, str) or not message.strip():
            return False
        deliver_as = item.get("deliverAs")
        options = {"deliverAs": deliver_as if deliver_as in {"followUp", "steer"} else "followUp"}
        send_user_message = getattr(self.harn, "sendUserMessage", None)
        if not callable(send_user_message):
            return False
        try:
            send_user_message(message, options)
        except Exception as error:  # noqa: BLE001
            if self.diagnostic_relay is not None:
                await self.diagnostic_relay.publish_exception(
                    error,
                    "Browser input delivery failed",
                    details=f"input={item.get('id', 'unknown')} deliverAs={options['deliverAs']}",
                )
            return False
        return True

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_interval)


class GibsonViewController:
    def __init__(
        self,
        harn: Any,
        relay: GibsonRelay,
        poller: BrowserInputPoller,
        *,
        environ: Mapping[str, str] | None = None,
        viewer_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.harn = harn
        self.relay = relay
        self.poller = poller
        self.environ = os.environ if environ is None else environ
        self.viewer_factory = viewer_factory
        self.viewer: Any | None = None

    async def handle(self, raw_args: str = "", ctx: Any = None) -> None:
        try:
            options = _parse_view_command_args(raw_args, self.environ)
            if options["list_renderers"]:
                message = renderer_listing()
                _notify_command_context(ctx, message)
                _append_harn_entry(self.harn, message, {"renderers": list(RENDERERS)})
                await self.relay.publish_diagnostic(
                    event_type="viewer_renderers",
                    severity="info",
                    title="Gibson renderers",
                    message=message,
                )
                return
            viewer = self._start_or_reuse_viewer(options)
            http_sink = self.relay.attach_http_endpoint(viewer.endpoint, self.environ)
            self.poller.set_endpoint(viewer.input_endpoint)
            self.poller.start()
            await self.relay.flush_recent(http_sink)
            message = f"Gibson viewer attached at {viewer.display_url}"
            _notify_command_context(ctx, message)
            _append_harn_entry(
                self.harn,
                message,
                {
                    "url": viewer.display_url,
                    "renderer": options["renderer"],
                },
            )
            await self.relay.publish_diagnostic(
                event_type="viewer_attach",
                severity="info",
                title="Gibson viewer",
                message=message,
                details=f"endpoint={viewer.endpoint} renderer={options['renderer']}",
            )
        except Exception as error:  # noqa: BLE001
            _notify_command_context(ctx, f"Gibson viewer failed: {error}", level="error")
            await self.relay.publish_exception(error, "Gibson viewer failed", details=f"args={raw_args!r}")

    def stop(self) -> None:
        viewer = self.viewer
        self.viewer = None
        if viewer is None:
            return
        close = getattr(viewer, "close", None)
        if callable(close):
            close()

    def _start_or_reuse_viewer(self, options: Mapping[str, Any]) -> Any:
        viewer = self.viewer
        if viewer is not None and not getattr(viewer, "closed", False):
            if options["browser"]:
                open_browser = getattr(viewer, "open_browser", None)
                if callable(open_browser):
                    open_browser()
            return viewer
        factory = self.viewer_factory
        if factory is None:
            from harn_gibson.viewer import start_viewer

            factory = start_viewer
        viewer_env = _viewer_env_for_options(self.environ, options)
        viewer = factory(
            options["host"],
            options["port"],
            env=viewer_env,
            launch_browser=options["browser"],
        )
        self.viewer = viewer
        return viewer


def extension_factory(harn: Any) -> None:
    relay = GibsonRelay(
        build_sink_from_env(),
        build_dispatcher_from_env(),
        max_recent_events=_recent_event_limit_from_env(),
    )
    poller = BrowserInputPoller(
        harn=harn,
        endpoint=build_input_endpoint_from_env(),
        diagnostic_relay=relay,
        poll_interval=_poll_interval_from_env(),
    )
    view_controller = GibsonViewController(harn, relay, poller)
    _register_view_command(harn, view_controller)
    _register_renderer_command(harn, view_controller)
    for event_type in HARN_EVENTS:
        harn.on(event_type, _handler_for(relay, poller, view_controller))


default = extension_factory


def build_dispatcher_from_env(environ: Mapping[str, str] | None = None) -> HookDispatcher:
    env = os.environ if environ is None else environ
    dispatcher = HookDispatcher()
    hooks = env.get("HARN_GIBSON_HOOKS", "")
    for hook_path in _split_paths(hooks):
        load_hook_module(hook_path, dispatcher)
    return dispatcher


def extension_path() -> str:
    return str(Path(__file__).resolve())


async def fetch_browser_input(endpoint: str, timeout: float = 0.2) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(_fetch_browser_input_sync, endpoint, timeout)
    except (OSError, json.JSONDecodeError):
        return None


def build_input_endpoint_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("HARN_GIBSON_INPUT_ENDPOINT")
    if explicit:
        return explicit
    endpoint = env.get("HARN_GIBSON_ENDPOINT", DEFAULT_ENDPOINT)
    if endpoint.lower() in {"", "0", "false", "none"}:
        return None
    return _event_endpoint_to_input_endpoint(endpoint)


def _handler_for(relay: GibsonRelay, poller: BrowserInputPoller, view_controller: GibsonViewController) -> Any:
    async def handler(event: Any, ctx: Any = None) -> Any:
        result = await relay.handle(event, ctx)
        event_type = event.get("type") if isinstance(event, Mapping) else getattr(event, "type", None)
        if event_type == "session_start":
            poller.start()
        elif event_type == "session_shutdown":
            poller.stop()
            view_controller.stop()
        return result

    return handler


def _fetch_browser_input_sync(endpoint: str, timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status == 204:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 204:
            return None
        raise
    return payload if isinstance(payload, dict) else None


def _event_endpoint_to_input_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/events"):
        base = base[: -len("/events")]
    return f"{base}/input/next"


def _poll_interval_from_env(environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    raw_value = env.get("HARN_GIBSON_INPUT_POLL_MS", "250")
    try:
        milliseconds = max(50, int(raw_value))
    except ValueError:
        milliseconds = 250
    return milliseconds / 1000


def _recent_event_limit_from_env(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw_value = env.get("HARN_GIBSON_RECENT_EVENTS", "100")
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 100


def _parse_view_command_args(raw_args: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    host = env.get("HARN_GIBSON_VIEW_HOST", "127.0.0.1")
    port = _coerce_port(env.get("HARN_GIBSON_VIEW_PORT"), default=0)
    browser = env.get("HARN_GIBSON_VIEW_BROWSER", "1").lower() not in {"0", "false", "no", "off"}
    renderer = _default_view_renderer(env)
    renderer_command: str | None = None
    style: str | None = None
    renderer_timeout_ms: str | None = None
    list_renderers = False
    tokens = shlex.split(raw_args)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--no-browser":
            browser = False
        elif token == "--browser":
            browser = True
        elif token in {"--list-renderers", "--renderers"}:
            list_renderers = True
        elif token == "--host" and index + 1 < len(tokens):
            index += 1
            host = tokens[index]
        elif token.startswith("--host="):
            host = token.split("=", 1)[1]
        elif token == "--port" and index + 1 < len(tokens):
            index += 1
            port = _coerce_port(tokens[index], default=port)
        elif token.startswith("--port="):
            port = _coerce_port(token.split("=", 1)[1], default=port)
        elif token == "--renderer" and index + 1 < len(tokens):
            index += 1
            renderer = _coerce_view_renderer(tokens[index], default=renderer)
        elif token.startswith("--renderer="):
            renderer = _coerce_view_renderer(token.split("=", 1)[1], default=renderer)
        elif token == "--renderer-command" and index + 1 < len(tokens):
            index += 1
            renderer_command = tokens[index]
            renderer = "command"
        elif token.startswith("--renderer-command="):
            renderer_command = token.split("=", 1)[1]
            renderer = "command"
        elif token == "--style" and index + 1 < len(tokens):
            index += 1
            style = tokens[index]
        elif token.startswith("--style="):
            style = token.split("=", 1)[1]
        elif token == "--renderer-timeout-ms" and index + 1 < len(tokens):
            index += 1
            renderer_timeout_ms = tokens[index]
        elif token.startswith("--renderer-timeout-ms="):
            renderer_timeout_ms = token.split("=", 1)[1]
        index += 1
    return {
        "host": host,
        "port": port,
        "browser": browser,
        "renderer": renderer,
        "renderer_command": renderer_command,
        "style": style,
        "renderer_timeout_ms": renderer_timeout_ms,
        "list_renderers": list_renderers,
    }


def _default_view_renderer(env: Mapping[str, str]) -> str | None:
    configured = (
        env.get("HARN_GIBSON_RENDERER_COMMAND")
        or env.get("HARN_GIBSON_RENDERER_MODEL_COMMAND")
        or env.get("HARN_GIBSON_RENDERER")
    )
    if configured and not env.get("HARN_GIBSON_VIEW_RENDERER"):
        return None
    return _coerce_view_renderer(env.get("HARN_GIBSON_VIEW_RENDERER"), default=DEFAULT_RENDERER)


def _coerce_view_renderer(value: str | None, *, default: str | None) -> str | None:
    if value is None:
        return default
    return normalize_renderer(value, default=default)


def _viewer_env_for_options(environ: Mapping[str, str], options: Mapping[str, Any]) -> dict[str, str]:
    env = dict(environ)
    style = options.get("style")
    if style:
        env["HARN_GIBSON_STYLE"] = str(style)
    renderer_timeout_ms = options.get("renderer_timeout_ms")
    if renderer_timeout_ms:
        env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
    renderer = options.get("renderer")
    renderer_command = options.get("renderer_command")
    if renderer_command:
        env["HARN_GIBSON_RENDERER_COMMAND"] = str(renderer_command)
        env.pop("HARN_GIBSON_RENDERER", None)
        env.pop("HARN_GIBSON_RENDERER_MODEL_COMMAND", None)
    elif isinstance(renderer, str):
        env.pop("HARN_GIBSON_RENDERER_MODEL_COMMAND", None)
        renderer_command_value = direct_renderer_command(renderer)
        if renderer == "none":
            env["HARN_GIBSON_RENDERER"] = "none"
            env.pop("HARN_GIBSON_RENDERER_COMMAND", None)
            env.pop("HARN_GIBSON_RENDERER_TIMEOUT_MS", None)
        elif renderer_command_value is None:
            env["HARN_GIBSON_RENDERER"] = renderer
            env.pop("HARN_GIBSON_RENDERER_COMMAND", None)
            env.pop("HARN_GIBSON_RENDERER_TIMEOUT_MS", None)
        else:
            env["HARN_GIBSON_RENDERER_COMMAND"] = renderer_command_value
            env.setdefault("HARN_GIBSON_RENDERER_TIMEOUT_MS", "10000")
            env.pop("HARN_GIBSON_RENDERER", None)
    return env


def _coerce_port(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default


def _split_paths(value: str) -> list[str]:
    return [part for part in value.split(os.pathsep) if part]


def _register_view_command(harn: Any, view_controller: GibsonViewController) -> None:
    register_command = getattr(harn, "registerCommand", None)
    if not callable(register_command):
        return
    register_command(
        "gibson-view",
        {
            "description": "Open the Gibson visualizer",
            "handler": view_controller.handle,
        },
    )


def _register_renderer_command(harn: Any, view_controller: GibsonViewController) -> None:
    register_command = getattr(harn, "registerCommand", None)
    if not callable(register_command):
        return

    async def handle_renderers(_raw_args: str = "", ctx: Any = None) -> None:
        await view_controller.handle("--list-renderers", ctx)

    register_command(
        "gibson-renderers",
        {
            "description": "List Gibson viewer renderers",
            "handler": handle_renderers,
        },
    )


def _notify_command_context(ctx: Any, message: str, *, level: str = "info") -> None:
    ui = getattr(ctx, "ui", None)
    if ui is None:
        return
    notify = getattr(ui, "notify", None)
    if callable(notify):
        notify(message, level)
    _call_first(ui, ("setStatus", "set_status"), "gibson", message)


def _append_harn_entry(harn: Any, message: str, data: Mapping[str, Any]) -> None:
    append_entry = getattr(harn, "appendEntry", None)
    if callable(append_entry):
        append_entry("gibson_view", {"message": message, **dict(data)})


def _update_harn_ui(ctx: Any, event: GibsonEvent) -> None:
    ui = getattr(ctx, "ui", None)
    if ui is None:
        return
    if event.event_type == "session_start":
        _call_first(ui, ("setTitle", "set_title"), "harn // gibson link")
    status = None if event.event_type == "session_shutdown" else f"{event.phase}:{event.event_type}"
    _call_first(ui, ("setStatus", "set_status"), "gibson", status)


def _call_first(target: Any, names: tuple[str, ...], *args: Any) -> None:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            method(*args)
            return
