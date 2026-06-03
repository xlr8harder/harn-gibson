from __future__ import annotations

from dataclasses import dataclass

from harn_gibson.events import (
    GibsonEvent,
    diagnostic_event,
    phase_for_event,
    summarize_event,
    title_for_event,
    to_jsonable,
)


@dataclass
class SampleData:
    value: int


class Dumpable:
    def model_dump(self) -> dict[str, object]:
        return {"dumped": True}


class ReprOnly:
    def __repr__(self) -> str:
        return "<repr-only>"


def test_gibson_event_from_raw_and_to_dict() -> None:
    event = GibsonEvent.from_raw(
        {"type": "input", "text": "hello\nworld", "source": "interactive"},
        7,
        source="unit",
        timestamp_ms=123,
        recent_context=("last user prompt",),
        visualization_context=("grid online",),
    )

    assert event.phase == "before"
    assert event.title == "Input intercept"
    assert event.summary == "interactive input: hello world"
    assert event.to_dict() == {
        "schema": "harn-gibson.event.v1",
        "sequence": 7,
        "timestampMs": 123,
        "source": "unit",
        "eventType": "input",
        "phase": "before",
        "title": "Input intercept",
        "summary": "interactive input: hello world",
        "payload": {"type": "input", "text": "hello world", "source": "interactive"},
        "recentContext": ["last user prompt"],
        "visualizationContext": ["grid online"],
    }


def test_gibson_event_handles_unknown_raw_values() -> None:
    event = GibsonEvent.from_raw("not a dict", 1)

    assert event.event_type == "unknown"
    assert event.phase == "lifecycle"
    assert event.payload == {"type": "unknown", "value": "not a dict"}


def test_phase_and_title_variants() -> None:
    assert phase_for_event("message_update") == "during"
    assert phase_for_event("tool_result") == "after"
    assert phase_for_event("session_start") == "lifecycle"
    assert title_for_event("tool_call") == "Tool preflight"
    assert title_for_event("session_start") == "Session start"
    assert title_for_event("") == "Unknown event"


def test_summarize_event_variants() -> None:
    assert summarize_event("tool_call", {"toolName": "bash", "input": {"command": "ls"}}) == (
        "bash starting with {command}"
    )
    many_args = {"type": "tool_execution_start", "toolName": "grep", "args": {str(i): i for i in range(6)}}
    assert summarize_event("tool_execution_start", many_args) == "grep starting with {0, 1, 2, 3, 4, ...}"
    assert summarize_event(
        "tool_result",
        {"toolName": "read", "isError": True, "content": [{"type": "text", "text": "failed\nbadly"}]},
    ) == "read completed: error; failed badly"
    assert summarize_event(
        "tool_execution_end",
        {"toolName": "bash", "result": {"content": [{"type": "text", "text": "done"}]}},
    ) == "bash completed: ok; done"
    assert summarize_event("message_update", {"assistantMessageEvent": {"type": "text_delta", "delta": "x"}}) == (
        "assistant stream {type, delta}"
    )
    assert summarize_event("message_start", {"message": {"role": "assistant", "content": "hi"}}) == (
        "assistant start: hi"
    )
    assert summarize_event(
        "message_end",
        {"message": {"role": "user", "content": [{"type": "text", "text": "bye"}]}},
    ) == "user end: bye"
    assert summarize_event("message_end", {"message": {"role": "custom", "content": []}}) == "custom end: "
    assert summarize_event("message_end", {"message": {"role": "custom", "content": [{"type": "image"}]}}) == (
        "custom end: "
    )
    assert summarize_event("message_end", {"message": {"role": "custom", "content": [{"text": ""}]}}) == (
        "custom end: "
    )
    assert summarize_event("message_end", {"message": {"role": "custom", "content": None}}) == "custom end: "
    assert summarize_event("model_select", {"model": {"provider": "openai", "id": "gpt"}}) == (
        "model selected: openai/gpt"
    )
    assert summarize_event("session_start", {"reason": "reload"}) == "session start: reload"
    assert summarize_event("session_shutdown", {"reason": "quit"}) == "session shutdown: quit"
    assert summarize_event("runtime_error", {"severity": "error", "message": "boom"}) == "error: boom"
    assert summarize_event("unknown", {"items": [1, 2, 3]}) == "{items}"
    assert summarize_event("unknown", ["a", "b"]) == "[2 items]"
    assert summarize_event("unknown", "scalar") == "scalar"
    assert summarize_event("tool_result", {"toolName": "read", "content": [{"type": "image"}]}) == (
        "read completed: ok; "
    )
    assert summarize_event("tool_result", {"toolName": "read", "content": [{"type": "image"}], "result": []}) == (
        "read completed: ok; "
    )


def test_summarize_event_with_object_fields() -> None:
    class Message:
        role = "assistant"
        content = [{"type": "text", "text": "object message"}]

    class Model:
        provider = "provider"
        id = "model"

    assert summarize_event("message_end", {"message": Message()}) == "assistant end: object message"
    assert summarize_event("model_select", {"model": Model()}) == "model selected: provider/model"


def test_to_jsonable_variants() -> None:
    long_text = "x" * 4010

    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(3.5) == 3.5
    assert to_jsonable(long_text).endswith("...")
    assert to_jsonable({"a": SampleData(1), 2: ("x",)}) == {"a": {"value": 1}, "2": ["x"]}
    assert to_jsonable({"b", "a"}) == ["a", "b"]
    assert to_jsonable(Dumpable()) == {"dumped": True}
    assert to_jsonable(ReprOnly()) == "<repr-only>"


def test_long_scalar_summary_is_clipped() -> None:
    assert summarize_event("unknown", {"value": "x" * 200}) == "{value}"
    event = GibsonEvent.from_raw({"type": "unknown", "payload": "x" * 200}, 2)
    assert event.summary == "{type, payload}"


def test_diagnostic_event_payload() -> None:
    event = diagnostic_event(
        9,
        message="boom",
        event_type="runtime_error",
        severity="error",
        details="details",
        traceback_text="trace",
        timestamp_ms=99,
    )

    assert event.sequence == 9
    assert event.phase == "after"
    assert event.title == "Runtime error"
    assert event.summary == "error: boom"
    assert event.payload == {
        "type": "runtime_error",
        "severity": "error",
        "message": "boom",
        "details": "details",
        "traceback": "trace",
    }
