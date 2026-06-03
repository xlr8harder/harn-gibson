"""harn extension entry point."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.hooks import HookDispatcher, load_hook_module, result_for_harn
from harn_gibson.sinks import DEFAULT_ENDPOINT, EventSink, build_sink_from_env

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
    def __init__(self, sink: EventSink, dispatcher: HookDispatcher | None = None) -> None:
        self.sink = sink
        self.dispatcher = dispatcher or HookDispatcher()
        self.sequence = 0

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
        await self.sink.publish(event, decisions)
        return result_for_harn(event.event_type, decisions)

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
        await self.sink.publish(event, ())


@dataclass(slots=True)
class BrowserInputPoller:
    harn: Any
    endpoint: str | None
    diagnostic_relay: GibsonRelay | None = None
    poll_interval: float = 0.25
    timeout: float = 0.2
    task: asyncio.Task[None] | None = None

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


def extension_factory(harn: Any) -> None:
    relay = GibsonRelay(build_sink_from_env(), build_dispatcher_from_env())
    poller = BrowserInputPoller(
        harn=harn,
        endpoint=build_input_endpoint_from_env(),
        diagnostic_relay=relay,
        poll_interval=_poll_interval_from_env(),
    )
    for event_type in HARN_EVENTS:
        harn.on(event_type, _handler_for(relay, poller))


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


def _handler_for(relay: GibsonRelay, poller: BrowserInputPoller) -> Any:
    async def handler(event: Any, ctx: Any = None) -> Any:
        result = await relay.handle(event, ctx)
        event_type = event.get("type") if isinstance(event, Mapping) else getattr(event, "type", None)
        if event_type == "session_start":
            poller.start()
        elif event_type == "session_shutdown":
            poller.stop()
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


def _split_paths(value: str) -> list[str]:
    return [part for part in value.split(os.pathsep) if part]


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
