"""Hook dispatch and harn-result adaptation."""

from __future__ import annotations

import asyncio
import importlib.util
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from harn_gibson.events import EventPhase, GibsonEvent

HookHandler = Callable[[GibsonEvent], "HookResult | Awaitable[HookResult]"]
HookResult = "HookDecision | Mapping[str, Any] | None"
HookAction = Literal["continue", "transform", "handled"]


@dataclass(frozen=True, slots=True)
class HookDecision:
    """Decision returned by a hook handler."""

    block: bool = False
    reason: str | None = None
    action: HookAction | None = None
    replacement: dict[str, Any] = field(default_factory=dict)
    display: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: HookResult) -> HookDecision | None:
        if result is None:
            return None
        if isinstance(result, HookDecision):
            return result
        return cls(
            block=bool(result.get("block", False)),
            reason=_optional_str(result.get("reason")),
            action=_optional_action(result.get("action")),
            replacement=dict(result.get("replacement") or {}),
            display=bool(result.get("display", True)),
            metadata=dict(result.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "reason": self.reason,
            "action": self.action,
            "replacement": self.replacement,
            "display": self.display,
            "metadata": self.metadata,
        }


class HookDispatcher:
    """Phase-aware event dispatcher for external hook modules."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, EventPhase | Literal["*"]], list[HookHandler]] = defaultdict(list)

    def on(
        self,
        event_type: str = "*",
        phase: EventPhase | Literal["*"] = "*",
        handler: HookHandler | None = None,
    ) -> Callable[[HookHandler], HookHandler] | HookHandler:
        def register(current: HookHandler) -> HookHandler:
            self._handlers[(event_type, phase)].append(current)
            return current

        if handler is not None:
            return register(handler)
        return register

    async def dispatch(self, event: GibsonEvent) -> list[HookDecision]:
        decisions: list[HookDecision] = []
        for handler in self.handlers_for(event):
            result = handler(event)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
            decision = HookDecision.from_result(result)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def handlers_for(self, event: GibsonEvent) -> list[HookHandler]:
        handlers: list[HookHandler] = []
        for key in (("*", "*"), (event.event_type, "*"), ("*", event.phase), (event.event_type, event.phase)):
            handlers.extend(self._handlers.get(key, ()))
        return handlers


def result_for_harn(event_type: str, decisions: Iterable[HookDecision]) -> dict[str, Any] | None:
    collected = list(decisions)
    blocking = next((decision for decision in collected if decision.block), None)
    if event_type.startswith("session_before_") and blocking is not None:
        return {"cancel": True}
    if event_type == "tool_call" and blocking is not None:
        return {"block": True, "reason": blocking.reason or "Blocked by harn-gibson hook"}
    if event_type == "input":
        return _input_result(collected)
    if event_type == "tool_result":
        return _replacement_result(collected, ("content", "details", "isError"))
    if event_type == "message_end":
        return _replacement_result(collected, ("message",))
    if event_type == "before_agent_start":
        return _replacement_result(collected, ("message", "systemPrompt"))
    if event_type == "before_provider_request":
        replacement = _replacement_result(collected, ("payload",))
        return None if replacement is None else replacement["payload"]
    return None


def load_hook_module(path: str | Path, dispatcher: HookDispatcher) -> None:
    resolved = Path(path).expanduser().resolve()
    spec = importlib.util.spec_from_file_location(f"harn_gibson_hook_{abs(hash(resolved))}", resolved)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load hook module: {resolved}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register_gibson_hooks", None)
    if not callable(register):
        raise ValueError(f"Hook module must define register_gibson_hooks(dispatcher): {resolved}")
    register(dispatcher)


def _input_result(decisions: list[HookDecision]) -> dict[str, Any] | None:
    for decision in reversed(decisions):
        if decision.action == "handled":
            return {"action": "handled"}
        if decision.action == "transform":
            result = {"action": "transform"}
            result.update(decision.replacement)
            return result
    return None


def _replacement_result(decisions: list[HookDecision], fields: tuple[str, ...]) -> dict[str, Any] | None:
    replacement: dict[str, Any] = {}
    for decision in decisions:
        for field_name in fields:
            if field_name in decision.replacement:
                replacement[field_name] = decision.replacement[field_name]
    return replacement or None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_action(value: Any) -> HookAction | None:
    return value if value in {"continue", "transform", "handled"} else None
