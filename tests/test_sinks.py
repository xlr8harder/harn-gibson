from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.hooks import HookDecision
from harn_gibson.sinks import (
    CompositeSink,
    EventBuffer,
    HttpEventSink,
    JsonlEventSink,
    NoopSink,
    build_sink_from_env,
    event_payload,
)


def sample_event() -> GibsonEvent:
    return GibsonEvent.from_raw({"type": "tool_call", "toolName": "bash"}, 1, timestamp_ms=10)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[GibsonEvent, list[HookDecision]]] = []

    async def publish(self, event: GibsonEvent, decisions: list[HookDecision] | None = None) -> None:
        self.events.append((event, list(decisions or [])))


def test_event_payload_and_noop_sink() -> None:
    decision = HookDecision(block=True, reason="no")
    payload = event_payload(sample_event(), [decision])

    assert payload["eventType"] == "tool_call"
    assert payload["decisions"] == [decision.to_dict()]
    assert "decisions" not in event_payload(sample_event())
    assert asyncio.run(NoopSink().publish(sample_event())) is None


def test_composite_sink_reuses_decisions() -> None:
    first = RecordingSink()
    second = RecordingSink()
    decision = HookDecision(metadata={"x": 1})

    asyncio.run(CompositeSink([first, second]).publish(sample_event(), [decision]))

    assert first.events[0][1] == [decision]
    assert second.events[0][1] == [decision]


def test_jsonl_event_sink_writes_sorted_payload(tmp_path: Path) -> None:
    path = tmp_path / "events" / "gibson.jsonl"
    asyncio.run(JsonlEventSink(path).publish(sample_event(), [HookDecision(reason="logged")]))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eventType"] == "tool_call"
    assert data["decisions"][0]["reason"] == "logged"


def test_http_event_sink_post_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def fake_urlopen(request: Any, timeout: float) -> Response:
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sink = HttpEventSink("http://example.test/events", timeout=0.5)
    sink._post({"b": 2})
    assert json.loads(captured["data"]) == {"b": 2}
    asyncio.run(sink.publish(sample_event()))

    assert captured["url"] == "http://example.test/events"
    assert json.loads(captured["data"])["eventType"] == "tool_call"
    assert captured["timeout"] == 0.5

    def broken(_request: object, *, timeout: float) -> object:
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", broken)
    asyncio.run(sink.publish(sample_event()))
    assert sink.last_error == "down"


def test_event_buffer_publish_snapshot_subscribe_and_trim() -> None:
    buffer = EventBuffer(max_events=2)
    buffer.publish({"sequence": 1})
    buffer.publish({"sequence": 2})
    subscriber, unsubscribe = buffer.subscribe()
    buffer.publish({"sequence": 3})

    assert buffer.snapshot() == [{"sequence": 2}, {"sequence": 3}]
    assert subscriber.get_nowait() == {"sequence": 1}
    assert subscriber.get_nowait() == {"sequence": 2}
    assert subscriber.get_nowait() == {"sequence": 3}
    unsubscribe()
    buffer.publish({"sequence": 4})
    with pytest.raises(queue.Empty):
        subscriber.get_nowait()
    unsubscribe()


def test_build_sink_from_env_variants(tmp_path: Path) -> None:
    assert isinstance(build_sink_from_env({"HARN_GIBSON_ENDPOINT": "none"}), NoopSink)
    assert isinstance(build_sink_from_env({}), HttpEventSink)
    assert isinstance(
        build_sink_from_env({"HARN_GIBSON_ENDPOINT": "0", "HARN_GIBSON_EVENT_LOG": str(tmp_path / "events.jsonl")}),
        JsonlEventSink,
    )
    assert isinstance(
        build_sink_from_env({"HARN_GIBSON_ENDPOINT": "http://x", "HARN_GIBSON_EVENT_LOG": str(tmp_path / "e.jsonl")}),
        CompositeSink,
    )
