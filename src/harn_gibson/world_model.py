"""Event-sourced world model for renderer perception."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from harn_gibson.events import GibsonEvent

WorldFactSource = Literal["observed", "inferred", "assumed", "stale"]
WORLD_MODEL_SCHEMA = "harn-gibson.world-model.v1"
_COMMAND_KEYS = frozenset({"cmd", "command", "shellCommand"})
_COMMAND_START_EVENTS = frozenset({"tool_call", "tool_execution_start", "user_bash"})
_COMMAND_END_EVENTS = frozenset({"tool_result", "tool_execution_end"})
_MAX_COMMAND_PREVIEW_CHARS = 240
_MAX_COMMAND_SUMMARY_CHARS = 200
_MAX_COMMAND_TOUCHED_PATHS = 12


@dataclass(frozen=True, slots=True)
class CommandObservation:
    tool_name: str
    command: str
    source: str


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


@dataclass(slots=True)
class CommandWorldEntity:
    id: str
    tool_name: str
    command_preview: str
    command_source: str
    first_sequence: int
    first_seen_ms: int
    last_sequence: int
    last_seen_ms: int
    status: str = "running"
    started_sequence: int | None = None
    started_ms: int | None = None
    completed_sequence: int | None = None
    completed_ms: int | None = None
    duration_ms: int | None = None
    event_types: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    touched_paths: list[str] = field(default_factory=list)
    last_summary: str = ""
    last_outcome: dict[str, Any] | None = None

    def record_event(
        self,
        event: GibsonEvent,
        observation: CommandObservation,
        touches: Sequence[Mapping[str, Any]],
    ) -> None:
        self.last_sequence = event.sequence
        self.last_seen_ms = event.timestamp_ms
        self.last_summary = _clip_text(event.summary, _MAX_COMMAND_SUMMARY_CHARS)
        _append_unique(self.event_types, event.event_type)
        _append_unique(self.phases, event.phase)
        _append_unique(self.sources, observation.source)
        for touch in touches:
            path = str(touch.get("path") or "")
            _append_unique(self.touched_paths, path)

    def record_start(
        self,
        event: GibsonEvent,
        observation: CommandObservation,
        touches: Sequence[Mapping[str, Any]],
    ) -> None:
        self.started_sequence = event.sequence
        self.started_ms = event.timestamp_ms
        self.status = "running"
        self.record_event(event, observation, touches)

    def record_finish(
        self,
        event: GibsonEvent,
        observation: CommandObservation,
        touches: Sequence[Mapping[str, Any]],
        outcome: Mapping[str, Any],
    ) -> None:
        self.completed_sequence = event.sequence
        self.completed_ms = event.timestamp_ms
        self.status = str(outcome.get("status"))
        if self.started_ms is not None:
            self.duration_ms = max(0, event.timestamp_ms - self.started_ms)
        self.last_outcome = {
            "status": outcome.get("status"),
            "eventSequence": outcome.get("eventSequence"),
            "eventType": outcome.get("eventType"),
            "toolName": outcome.get("toolName"),
        }
        self.record_event(event, observation, touches)

    def to_dict(self) -> dict[str, Any]:
        touched_paths = self.touched_paths[:_MAX_COMMAND_TOUCHED_PATHS]
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "command",
            "toolName": self.tool_name,
            "commandPreview": self.command_preview,
            "commandSource": self.command_source,
            "status": self.status,
            "firstSequence": self.first_sequence,
            "lastSequence": self.last_sequence,
            "firstSeenMs": self.first_seen_ms,
            "lastSeenMs": self.last_seen_ms,
            "eventTypes": list(self.event_types),
            "phases": list(self.phases),
            "sources": list(self.sources),
            "touchedPaths": touched_paths,
            "touchedPathCount": len(self.touched_paths),
            "touchedPathsTruncated": len(self.touched_paths) > len(touched_paths),
            "lastSummary": self.last_summary,
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "lastConfirmedSequence": self.last_sequence,
                "lastConfirmedMs": self.last_seen_ms,
            },
        }
        if self.started_sequence is not None:
            payload["startedSequence"] = self.started_sequence
        if self.completed_sequence is not None:
            payload["completedSequence"] = self.completed_sequence
        if self.duration_ms is not None:
            payload["durationMs"] = self.duration_ms
        if self.last_outcome is not None:
            payload["lastOutcome"] = dict(self.last_outcome)
        return payload


class WorldModel:
    """Accumulates durable facts derived from the agent event stream."""

    def __init__(self, *, max_recent_outcomes: int = 16) -> None:
        self.revision = 0
        self._files: dict[str, FileWorldEntity] = {}
        self._commands: dict[str, CommandWorldEntity] = {}
        self._pending_commands: dict[tuple[str, str], list[str]] = {}
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
            observation = command_from_event(event)
            if observation is not None:
                self._record_command(event, observation, touches, outcome)
        if changed:
            self.revision += 1

    def to_dict(self, *, max_entities: int = 24) -> dict[str, Any]:
        limit = max(0, max_entities)
        files = sorted(self._files.values(), key=lambda item: (-item.activity_count, item.path))
        commands = sorted(self._commands.values(), key=lambda item: (-item.last_sequence, item.id))
        rendered_files = [entity.to_dict() for entity in files[:limit]]
        rendered_commands = [entity.to_dict() for entity in commands[:limit]]
        return {
            "schema": WORLD_MODEL_SCHEMA,
            "revision": self.revision,
            "entityCount": len(files) + len(commands),
            "truncated": len(files) > limit or len(commands) > limit,
            "counts": {
                "files": len(files),
                "commands": len(commands),
            },
            "entities": {
                "files": rendered_files,
                "commands": rendered_commands,
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

    def _record_command(
        self,
        event: GibsonEvent,
        observation: CommandObservation,
        touches: Sequence[Mapping[str, Any]],
        outcome: Mapping[str, Any] | None,
    ) -> None:
        if event.event_type in _COMMAND_START_EVENTS:
            entity = self._new_command_entity(event, observation)
            entity.record_start(event, observation, touches)
            self._pending_commands.setdefault((observation.tool_name, observation.command), []).append(entity.id)
            return
        entity = self._command_for_finish(event, observation)
        entity.record_finish(event, observation, touches, outcome)

    def _new_command_entity(self, event: GibsonEvent, observation: CommandObservation) -> CommandWorldEntity:
        entity = CommandWorldEntity(
            id=f"command:{event.sequence}",
            tool_name=observation.tool_name,
            command_preview=_clip_text(observation.command, _MAX_COMMAND_PREVIEW_CHARS),
            command_source=observation.source,
            first_sequence=event.sequence,
            first_seen_ms=event.timestamp_ms,
            last_sequence=event.sequence,
            last_seen_ms=event.timestamp_ms,
        )
        self._commands[entity.id] = entity
        return entity

    def _command_for_finish(self, event: GibsonEvent, observation: CommandObservation) -> CommandWorldEntity:
        pending_key = (observation.tool_name, observation.command)
        pending_ids = self._pending_commands.get(pending_key, [])
        if pending_ids:
            entity_id = pending_ids.pop(0)
            return self._commands[entity_id]
        return self._new_command_entity(event, observation)


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


def command_from_event(event: GibsonEvent) -> CommandObservation | None:
    if event.event_type not in _COMMAND_START_EVENTS and event.event_type not in _COMMAND_END_EVENTS:
        return None
    found = _command_text_from_value(event.payload, ())
    if found is None:
        return None
    command, source = found
    tool_name = event.payload.get("toolName")
    rendered_tool = tool_name if isinstance(tool_name, str) and tool_name else event.event_type
    return CommandObservation(rendered_tool, command, source)


def _command_text_from_value(value: Any, key_path: tuple[str, ...]) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered_key = str(key)
            child_path = (*key_path, rendered_key)
            if rendered_key in _COMMAND_KEYS and isinstance(child, str) and child.strip():
                return (child.strip(), ".".join(child_path))
            found = _command_text_from_value(child, child_path)
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found = _command_text_from_value(child, (*key_path, str(index)))
            if found is not None:
                return found
    return None


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


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


__all__ = [
    "WORLD_MODEL_SCHEMA",
    "CommandObservation",
    "CommandWorldEntity",
    "FileWorldEntity",
    "WorldFactSource",
    "WorldModel",
    "command_from_event",
    "outcome_from_event",
]
