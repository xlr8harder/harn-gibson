"""harn extension entry point."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harn_gibson.events import GibsonEvent
from harn_gibson.hooks import HookDispatcher, load_hook_module, result_for_harn
from harn_gibson.sinks import EventSink, build_sink_from_env

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
        decisions = await self.dispatcher.dispatch(event)
        _update_harn_ui(ctx, event)
        await self.sink.publish(event, decisions)
        return result_for_harn(event.event_type, decisions)


def extension_factory(harn: Any) -> None:
    relay = GibsonRelay(build_sink_from_env(), build_dispatcher_from_env())
    for event_type in HARN_EVENTS:
        harn.on(event_type, _handler_for(relay))


def build_dispatcher_from_env(environ: Mapping[str, str] | None = None) -> HookDispatcher:
    env = os.environ if environ is None else environ
    dispatcher = HookDispatcher()
    hooks = env.get("HARN_GIBSON_HOOKS", "")
    for hook_path in _split_paths(hooks):
        load_hook_module(hook_path, dispatcher)
    return dispatcher


def extension_path() -> str:
    return str(Path(__file__).resolve())


def _handler_for(relay: GibsonRelay) -> Any:
    async def handler(event: Any, ctx: Any = None) -> Any:
        return await relay.handle(event, ctx)

    return handler


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
