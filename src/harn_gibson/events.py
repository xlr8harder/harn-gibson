"""Event normalization for harn-gibson display and hook layers."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal

EventPhase = Literal["before", "during", "after", "lifecycle"]

_BEFORE_EVENTS = {
    "before_agent_start",
    "before_provider_request",
    "context",
    "input",
    "session_before_compact",
    "session_before_fork",
    "session_before_switch",
    "session_before_tree",
    "tool_call",
    "tool_execution_start",
    "user_bash",
}
_DURING_EVENTS = {"message_update", "tool_execution_update"}
_AFTER_EVENTS = {
    "after_provider_response",
    "agent_end",
    "message_end",
    "session_compact",
    "session_shutdown",
    "session_tree",
    "tool_execution_end",
    "tool_result",
    "turn_end",
}
_TITLE_OVERRIDES = {
    "before_agent_start": "Agent preflight",
    "input": "Input intercept",
    "message_update": "Stream update",
    "tool_call": "Tool preflight",
    "tool_result": "Tool result",
}


@dataclass(frozen=True, slots=True)
class GibsonEvent:
    """Normalized event passed to displays and hook handlers."""

    sequence: int
    timestamp_ms: int
    source: str
    event_type: str
    phase: EventPhase
    title: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    recent_context: tuple[str, ...] = ()
    visualization_context: tuple[str, ...] = ()

    @classmethod
    def from_raw(
        cls,
        raw_event: Any,
        sequence: int,
        *,
        source: str = "harn",
        timestamp_ms: int | None = None,
        recent_context: Sequence[str] = (),
        visualization_context: Sequence[str] = (),
    ) -> GibsonEvent:
        payload = _payload_from_raw(raw_event)
        raw_type = payload.get("type", "unknown")
        event_type = raw_type if isinstance(raw_type, str) and raw_type else "unknown"
        phase = phase_for_event(event_type)
        return cls(
            sequence=sequence,
            timestamp_ms=timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
            source=source,
            event_type=event_type,
            phase=phase,
            title=title_for_event(event_type),
            summary=summarize_event(event_type, payload),
            payload=payload,
            recent_context=tuple(recent_context),
            visualization_context=tuple(visualization_context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.event.v1",
            "sequence": self.sequence,
            "timestampMs": self.timestamp_ms,
            "source": self.source,
            "eventType": self.event_type,
            "phase": self.phase,
            "title": self.title,
            "summary": self.summary,
            "payload": self.payload,
            "recentContext": list(self.recent_context),
            "visualizationContext": list(self.visualization_context),
        }


def phase_for_event(event_type: str) -> EventPhase:
    if event_type in _BEFORE_EVENTS:
        return "before"
    if event_type in _DURING_EVENTS:
        return "during"
    if event_type in _AFTER_EVENTS:
        return "after"
    return "lifecycle"


def title_for_event(event_type: str) -> str:
    if event_type in _TITLE_OVERRIDES:
        return _TITLE_OVERRIDES[event_type]
    return event_type.replace("_", " ").strip().capitalize() or "Unknown event"


def summarize_event(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type == "input":
        source = str(payload.get("source") or "unknown")
        return f"{source} input: {_clip(str(payload.get('text') or ''), 96)}"
    if event_type in {"tool_call", "tool_execution_start"}:
        tool_name = str(payload.get("toolName") or "tool")
        tool_input = payload.get("input", payload.get("args", {}))
        return f"{tool_name} starting with {_shape(tool_input)}"
    if event_type in {"tool_result", "tool_execution_end"}:
        tool_name = str(payload.get("toolName") or "tool")
        status = "error" if bool(payload.get("isError")) else "ok"
        return f"{tool_name} completed: {status}; {_first_content_text(payload)}"
    if event_type == "message_update":
        update = payload.get("assistantMessageEvent", {})
        return f"assistant stream {_shape(update)}"
    if event_type in {"message_start", "message_end"}:
        message = payload.get("message", {})
        role = _field(message, "role", "message")
        return f"{role} {event_type.rsplit('_', 1)[-1]}: {_first_message_text(message)}"
    if event_type == "model_select":
        model = payload.get("model", {})
        return f"model selected: {_field(model, 'provider', '?')}/{_field(model, 'id', '?')}"
    if event_type == "session_start":
        return f"session start: {payload.get('reason', 'startup')}"
    if event_type == "session_shutdown":
        return f"session shutdown: {payload.get('reason', 'quit')}"
    return _shape(payload)


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clip(value, 4000)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [to_jsonable(item) for item in sorted(value, key=repr)]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    return repr(value)


def _payload_from_raw(raw_event: Any) -> dict[str, Any]:
    payload = to_jsonable(raw_event)
    if isinstance(payload, dict):
        return payload
    return {"type": "unknown", "value": payload}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _shape(value: Any) -> str:
    if isinstance(value, Mapping):
        keys = ", ".join(str(key) for key in list(value)[:5])
        suffix = ", ..." if len(value) > 5 else ""
        return "{" + keys + suffix + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    text = str(value)
    return _clip(text, 80)


def _first_content_text(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if _field(item, "type") == "text":
                return _clip(str(_field(item, "text", "")), 120)
    result = payload.get("result")
    if isinstance(result, Mapping):
        return _first_content_text(result)
    return ""


def _first_message_text(message: Any) -> str:
    content = _field(message, "content", "")
    if isinstance(content, str):
        return _clip(content, 120)
    if isinstance(content, list):
        for item in content:
            text = _field(item, "text")
            if text:
                return _clip(str(text), 120)
    return ""


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."
