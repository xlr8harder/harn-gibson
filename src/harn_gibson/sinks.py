"""Event sinks used by the harn extension and display server."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harn_gibson.events import GibsonEvent
from harn_gibson.hooks import HookDecision

DEFAULT_ENDPOINT = "http://127.0.0.1:8765/events"


class EventSink(Protocol):
    async def publish(self, event: GibsonEvent, decisions: Iterable[HookDecision] = ()) -> None: ...


@dataclass(slots=True)
class NoopSink:
    async def publish(self, _event: GibsonEvent, _decisions: Iterable[HookDecision] = ()) -> None:
        return None


@dataclass(slots=True)
class CompositeSink:
    sinks: list[EventSink]

    async def publish(self, event: GibsonEvent, decisions: Iterable[HookDecision] = ()) -> None:
        cached = list(decisions)
        for sink in self.sinks:
            await sink.publish(event, cached)


@dataclass(slots=True)
class JsonlEventSink:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def publish(self, event: GibsonEvent, decisions: Iterable[HookDecision] = ()) -> None:
        payload = event_payload(event, decisions)
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)


@dataclass(slots=True)
class HttpEventSink:
    endpoint: str
    timeout: float = 0.15
    last_error: str | None = None

    async def publish(self, event: GibsonEvent, decisions: Iterable[HookDecision] = ()) -> None:
        payload = event_payload(event, decisions)
        try:
            await asyncio.to_thread(self._post, payload)
        except OSError as error:
            self.last_error = str(error)

    def _post(self, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            response.read()


@dataclass(slots=True)
class EventBuffer:
    max_events: int = 200
    _events: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, payload: Mapping[str, Any]) -> None:
        event = dict(payload)
        with self._lock:
            self._events.append(event)
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def subscribe(self) -> tuple[queue.Queue[dict[str, Any]], Callable[[], None]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            for event in self._events:
                subscriber.put(event)
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return subscriber, unsubscribe

def event_payload(event: GibsonEvent, decisions: Iterable[HookDecision] = ()) -> dict[str, Any]:
    payload = event.to_dict()
    rendered_decisions = [decision.to_dict() for decision in decisions]
    if rendered_decisions:
        payload["decisions"] = rendered_decisions
    return payload


def build_sink_from_env(environ: Mapping[str, str] | None = None) -> EventSink:
    env = os.environ if environ is None else environ
    sinks: list[EventSink] = []
    endpoint = env.get("HARN_GIBSON_ENDPOINT", DEFAULT_ENDPOINT)
    if endpoint.lower() not in {"", "0", "false", "none"}:
        sinks.append(HttpEventSink(endpoint))
    event_log = env.get("HARN_GIBSON_EVENT_LOG")
    if event_log:
        sinks.append(JsonlEventSink(Path(event_log)))
    if not sinks:
        return NoopSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)
