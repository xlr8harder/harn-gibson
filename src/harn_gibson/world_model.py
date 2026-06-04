"""Event-sourced world model for renderer perception."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from harn_gibson.events import GibsonEvent

WorldFactSource = Literal["observed", "inferred", "assumed", "stale"]
WORLD_MODEL_SCHEMA = "harn-gibson.world-model.v1"


@dataclass(slots=True)
class FileWorldEntity:
    path: str
    first_sequence: int
    first_seen_ms: int
    activity_count: int = 0
    last_sequence: int = 0
    last_seen_ms: int = 0
    phases: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    last_outcome: dict[str, Any] | None = None

    def record_touch(self, event: GibsonEvent, touch: Mapping[str, Any]) -> None:
        self.activity_count += 1
        self.last_sequence = event.sequence
        self.last_seen_ms = event.timestamp_ms
        _append_unique(self.phases, event.phase)
        operation = str(touch.get("operation") or _operation_for_event(event))
        _append_unique(self.operations, operation)
        for source in touch.get("sources", ()):
            if isinstance(source, str):
                _append_unique(self.sources, source)

    def record_outcome(self, outcome: Mapping[str, Any]) -> None:
        self.last_outcome = {
            "status": outcome.get("status"),
            "eventSequence": outcome.get("eventSequence"),
            "eventType": outcome.get("eventType"),
            "toolName": outcome.get("toolName"),
        }

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": f"file:{self.path}",
            "kind": "file",
            "path": self.path,
            "activityCount": self.activity_count,
            "firstSequence": self.first_sequence,
            "lastSequence": self.last_sequence,
            "firstSeenMs": self.first_seen_ms,
            "lastSeenMs": self.last_seen_ms,
            "phases": list(self.phases),
            "operations": list(self.operations),
            "sources": list(self.sources),
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "lastConfirmedSequence": self.last_sequence,
                "lastConfirmedMs": self.last_seen_ms,
            },
        }
        if self.last_outcome is not None:
            payload["lastOutcome"] = dict(self.last_outcome)
        return payload


class WorldModel:
    """Accumulates durable facts derived from the agent event stream."""

    def __init__(self, *, max_recent_outcomes: int = 16) -> None:
        self.revision = 0
        self._files: dict[str, FileWorldEntity] = {}
        self._recent_outcomes: list[dict[str, Any]] = []
        self._seen_event_keys: set[tuple[str, int, int, str]] = set()
        self.max_recent_outcomes = max(1, max_recent_outcomes)

    def apply_batch(self, events: Sequence[GibsonEvent], touched_files: Mapping[str, Any]) -> None:
        touched_by_sequence = _touched_files_by_sequence(touched_files)
        changed = False
        for event in events:
            key = (event.source, event.sequence, event.timestamp_ms, event.event_type)
            if key in self._seen_event_keys:
                continue
            self._seen_event_keys.add(key)
            changed = True
            touches = touched_by_sequence.get(event.sequence, ())
            for touch in touches:
                path = str(touch.get("path") or "")
                if not path:
                    continue
                entity = self._files.get(path)
                if entity is None:
                    entity = FileWorldEntity(
                        path=path,
                        first_sequence=event.sequence,
                        first_seen_ms=event.timestamp_ms,
                        last_sequence=event.sequence,
                        last_seen_ms=event.timestamp_ms,
                    )
                    self._files[path] = entity
                entity.record_touch(event, touch)
            outcome = outcome_from_event(event)
            if outcome is not None:
                self._recent_outcomes.append(outcome)
                if len(self._recent_outcomes) > self.max_recent_outcomes:
                    del self._recent_outcomes[: len(self._recent_outcomes) - self.max_recent_outcomes]
                for touch in touches:
                    path = str(touch.get("path") or "")
                    entity = self._files.get(path)
                    if entity is not None:
                        entity.record_outcome(outcome)
        if changed:
            self.revision += 1

    def to_dict(self, *, max_entities: int = 24) -> dict[str, Any]:
        limit = max(0, max_entities)
        files = sorted(self._files.values(), key=lambda item: (-item.activity_count, item.path))
        rendered_files = [entity.to_dict() for entity in files[:limit]]
        return {
            "schema": WORLD_MODEL_SCHEMA,
            "revision": self.revision,
            "entityCount": len(files),
            "truncated": len(files) > limit,
            "entities": {
                "files": rendered_files,
            },
            "recentOutcomes": list(self._recent_outcomes),
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "notes": [
                    "Derived from normalized harn events and touched-file batches.",
                    "Semantic graph and agent intent are not yet modeled.",
                ],
            },
        }


def outcome_from_event(event: GibsonEvent) -> dict[str, Any] | None:
    if event.event_type == "runtime_error":
        return _outcome_payload(event, status="error")
    if event.event_type in {"tool_result", "tool_execution_end"}:
        status = "error" if bool(event.payload.get("isError")) else "ok"
        return _outcome_payload(event, status=status)
    if event.event_type == "harn_exit":
        exit_code = event.payload.get("exitCode", event.payload.get("returnCode"))
        status = "error" if isinstance(exit_code, int) and exit_code != 0 else "ok"
        return _outcome_payload(event, status=status)
    return None


def _outcome_payload(event: GibsonEvent, *, status: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventSequence": event.sequence,
        "timestampMs": event.timestamp_ms,
        "eventType": event.event_type,
        "status": status,
        "summary": event.summary,
        "provenance": {
            "source": "observed",
            "confidence": 1.0,
            "lastConfirmedSequence": event.sequence,
            "lastConfirmedMs": event.timestamp_ms,
        },
    }
    tool_name = event.payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        payload["toolName"] = tool_name
    exit_code = event.payload.get("exitCode", event.payload.get("returnCode"))
    if isinstance(exit_code, int):
        payload["exitCode"] = exit_code
    return payload


def _touched_files_by_sequence(touched_files: Mapping[str, Any]) -> dict[int, tuple[Mapping[str, Any], ...]]:
    by_sequence: dict[int, list[Mapping[str, Any]]] = {}
    files = touched_files.get("files")
    if not isinstance(files, list):
        return {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        first_sequence = item.get("firstSequence")
        last_sequence = item.get("lastSequence")
        if not isinstance(first_sequence, int) or not isinstance(last_sequence, int):
            continue
        for sequence in range(first_sequence, last_sequence + 1):
            by_sequence.setdefault(sequence, []).append(item)
    return {key: tuple(value) for key, value in by_sequence.items()}


def _operation_for_event(event: GibsonEvent) -> str:
    tool_name = event.payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        return f"{tool_name}:{event.phase}"
    return f"{event.event_type}:{event.phase}"


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


__all__ = [
    "WORLD_MODEL_SCHEMA",
    "FileWorldEntity",
    "WorldFactSource",
    "WorldModel",
    "outcome_from_event",
]
