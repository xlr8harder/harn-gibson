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
_MAX_CHANGE_SOURCE_CHARS = 120
_HEALTH_COMMAND_PATTERNS = (
    ("test", "pytest"),
    ("test", "tox"),
    ("test", "nox"),
    ("test", "npm test"),
    ("test", "npm run test"),
    ("test", "pnpm test"),
    ("test", "pnpm run test"),
    ("test", "yarn test"),
    ("test", "yarn run test"),
    ("test", "cargo test"),
    ("test", "go test"),
    ("test", "make test"),
    ("build", "python -m build"),
    ("build", "uv build"),
    ("build", "npm run build"),
    ("build", "pnpm build"),
    ("build", "pnpm run build"),
    ("build", "yarn build"),
    ("build", "yarn run build"),
    ("build", "cargo build"),
    ("build", "go build"),
    ("build", "make build"),
)
_PATH_KEYS = frozenset(
    {
        "destinationPath",
        "file",
        "fileName",
        "filePath",
        "filename",
        "filepath",
        "output",
        "outputPath",
        "path",
        "sourcePath",
        "targetPath",
    }
)
_OLD_TEXT_KEYS = frozenset({"before", "old", "oldString", "oldText", "old_string", "old_text", "previous"})
_NEW_TEXT_KEYS = frozenset(
    {"after", "insert", "new", "newString", "newText", "new_string", "new_text", "replacement"}
)
_WRITE_TEXT_KEYS = frozenset({"body", "content", "data", "text"})
_WRITE_TOOL_NAMES = frozenset({"create", "file_write", "write", "write_file"})
_ADDED_LINE_KEYS = frozenset({"added", "addedLines", "added_lines", "linesAdded", "lines_added"})
_REMOVED_LINE_KEYS = frozenset({"deleted", "linesRemoved", "lines_removed", "removed", "removedLines", "removed_lines"})
_START_LINE_KEYS = frozenset({"line", "lineNumber", "lineStart", "line_start", "startLine", "start_line"})
_END_LINE_KEYS = frozenset({"endLine", "end_line", "lineEnd", "line_end"})
_DIFF_KEYS = frozenset({"diff", "patch", "unifiedDiff", "unified_diff"})


@dataclass(frozen=True, slots=True)
class CommandObservation:
    tool_name: str
    command: str
    source: str


@dataclass(frozen=True, slots=True)
class ChangeObservation:
    path: str
    operation: str
    source: str
    added_lines: int
    removed_lines: int
    start_line: int | None = None
    end_line: int | None = None


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


@dataclass(frozen=True, slots=True)
class ChangeWorldEntity:
    id: str
    path: str
    operation: str
    event_sequence: int
    timestamp_ms: int
    event_type: str
    phase: str
    source: str
    tool_name: str | None = None
    status: str = "observed"
    added_lines: int = 0
    removed_lines: int = 0
    start_line: int | None = None
    end_line: int | None = None
    last_outcome: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "change",
            "path": self.path,
            "operation": self.operation,
            "status": self.status,
            "eventSequence": self.event_sequence,
            "timestampMs": self.timestamp_ms,
            "eventType": self.event_type,
            "phase": self.phase,
            "source": self.source,
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "lastConfirmedSequence": self.event_sequence,
                "lastConfirmedMs": self.timestamp_ms,
            },
        }
        if self.tool_name is not None:
            payload["toolName"] = self.tool_name
        payload["addedLines"] = self.added_lines
        payload["removedLines"] = self.removed_lines
        payload["magnitudeLines"] = max(0, self.added_lines) + max(0, self.removed_lines)
        if self.start_line is not None:
            payload["startLine"] = self.start_line
        if self.end_line is not None:
            payload["endLine"] = self.end_line
        if self.last_outcome is not None:
            payload["lastOutcome"] = dict(self.last_outcome)
        return payload


@dataclass(slots=True)
class HealthWorldEntity:
    id: str
    category: str
    source_command_id: str
    tool_name: str
    command_preview: str
    command_source: str
    first_sequence: int
    first_seen_ms: int
    last_sequence: int
    last_seen_ms: int
    status: str = "running"
    status_source: str = "observed_command_start"
    started_sequence: int | None = None
    completed_sequence: int | None = None
    duration_ms: int | None = None
    event_types: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    touched_paths: list[str] = field(default_factory=list)
    last_summary: str = ""
    last_outcome: dict[str, Any] | None = None

    def record_command(self, command: CommandWorldEntity) -> None:
        self.tool_name = command.tool_name
        self.command_preview = command.command_preview
        self.command_source = command.command_source
        self.last_sequence = command.last_sequence
        self.last_seen_ms = command.last_seen_ms
        self.status = command.status
        self.status_source = (
            "observed_command_outcome" if command.last_outcome is not None else "observed_command_start"
        )
        self.started_sequence = command.started_sequence
        self.completed_sequence = command.completed_sequence
        self.duration_ms = command.duration_ms
        self.event_types = list(command.event_types)
        self.phases = list(command.phases)
        self.sources = list(command.sources)
        self.touched_paths = list(command.touched_paths)
        self.last_summary = command.last_summary
        self.last_outcome = dict(command.last_outcome) if command.last_outcome is not None else None

    def to_dict(self) -> dict[str, Any]:
        touched_paths = self.touched_paths[:_MAX_COMMAND_TOUCHED_PATHS]
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "health",
            "category": self.category,
            "sourceCommandId": self.source_command_id,
            "toolName": self.tool_name,
            "commandPreview": self.command_preview,
            "commandSource": self.command_source,
            "status": self.status,
            "statusSource": self.status_source,
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
                "source": "inferred",
                "confidence": 0.85,
                "lastConfirmedSequence": self.last_sequence,
                "lastConfirmedMs": self.last_seen_ms,
                "basis": (
                    "Command text classified as a test/build health check; "
                    "status comes from observed command state."
                ),
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
        self._changes: dict[str, ChangeWorldEntity] = {}
        self._health: dict[str, HealthWorldEntity] = {}
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
            for change in changes_from_event(event, touches, outcome):
                self._changes[change.id] = change
        if changed:
            self.revision += 1

    def to_dict(self, *, max_entities: int = 24) -> dict[str, Any]:
        limit = max(0, max_entities)
        files = sorted(self._files.values(), key=lambda item: (-item.activity_count, item.path))
        commands = sorted(self._commands.values(), key=lambda item: (-item.last_sequence, item.id))
        changes = sorted(self._changes.values(), key=lambda item: (-item.event_sequence, item.id))
        health = sorted(self._health.values(), key=lambda item: (-item.last_sequence, item.id))
        rendered_files = [entity.to_dict() for entity in files[:limit]]
        rendered_commands = [entity.to_dict() for entity in commands[:limit]]
        rendered_changes = [entity.to_dict() for entity in changes[:limit]]
        rendered_health = [entity.to_dict() for entity in health[:limit]]
        return {
            "schema": WORLD_MODEL_SCHEMA,
            "revision": self.revision,
            "entityCount": len(files) + len(commands) + len(changes) + len(health),
            "truncated": len(files) > limit or len(commands) > limit or len(changes) > limit or len(health) > limit,
            "counts": {
                "files": len(files),
                "commands": len(commands),
                "changes": len(changes),
                "health": len(health),
            },
            "entities": {
                "files": rendered_files,
                "commands": rendered_commands,
                "changes": rendered_changes,
                "health": rendered_health,
            },
            "recentOutcomes": list(self._recent_outcomes),
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "notes": [
                    "Derived from normalized harn events and touched-file batches.",
                    "Health categories are inferred from observed command text; command status remains observed.",
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
            self._record_health_from_command(entity, observation.command)
            return
        entity = self._command_for_finish(event, observation)
        entity.record_finish(event, observation, touches, outcome)
        self._record_health_from_command(entity, observation.command)

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

    def _record_health_from_command(self, command: CommandWorldEntity, command_text: str) -> None:
        category = health_category_from_command(command_text)
        if category is None:
            return
        entity_id = f"health:{command.id}"
        entity = self._health.get(entity_id)
        if entity is None:
            entity = HealthWorldEntity(
                id=entity_id,
                category=category,
                source_command_id=command.id,
                tool_name=command.tool_name,
                command_preview=command.command_preview,
                command_source=command.command_source,
                first_sequence=command.first_sequence,
                first_seen_ms=command.first_seen_ms,
                last_sequence=command.last_sequence,
                last_seen_ms=command.last_seen_ms,
            )
            self._health[entity_id] = entity
        entity.record_command(command)


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


def health_category_from_command(command: str) -> str | None:
    normalized = " ".join(command.lower().split())
    if not normalized:
        return None
    for category, marker in _HEALTH_COMMAND_PATTERNS:
        if marker in normalized:
            return category
    return None


def changes_from_event(
    event: GibsonEvent,
    touches: Sequence[Mapping[str, Any]],
    outcome: Mapping[str, Any] | None,
) -> tuple[ChangeWorldEntity, ...]:
    delta = _change_delta_from_event(event)
    if delta is None:
        return ()
    observations = [
        ChangeObservation(
            path=path,
            operation=_change_operation(event, touch),
            source=delta["source"],
            added_lines=delta.get("addedLines"),
            removed_lines=delta.get("removedLines"),
            start_line=delta.get("startLine"),
            end_line=delta.get("endLine"),
        )
        for touch in touches
        if (path := str(touch.get("path") or ""))
    ]
    return tuple(_change_entity(event, observation, index, outcome) for index, observation in enumerate(observations))


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


def _change_delta_from_event(event: GibsonEvent) -> dict[str, Any] | None:
    explicit = _change_delta_from_value(event.payload, ())
    if explicit is not None:
        return explicit
    if _tool_name(event) in _WRITE_TOOL_NAMES:
        written = _write_text_delta_from_payload(event.payload)
        if written is not None:
            return written
    return None


def _change_delta_from_value(value: Any, key_path: tuple[str, ...]) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        explicit = _explicit_line_delta(value, key_path)
        if explicit is not None:
            return explicit
        replacement = _replacement_delta(value, key_path)
        if replacement is not None:
            return replacement
        diff = _diff_delta(value, key_path)
        if diff is not None:
            return diff
        for key, child in value.items():
            if str(key) in _PATH_KEYS:
                continue
            found = _change_delta_from_value(child, (*key_path, str(key)))
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found = _change_delta_from_value(child, (*key_path, str(index)))
            if found is not None:
                return found
    return None


def _explicit_line_delta(value: Mapping[str, Any], key_path: tuple[str, ...]) -> dict[str, Any] | None:
    added = _int_from_keys(value, _ADDED_LINE_KEYS)
    removed = _int_from_keys(value, _REMOVED_LINE_KEYS)
    if added is None and removed is None:
        return None
    payload: dict[str, Any] = {
        "source": _source_path(key_path, "lineCounts"),
        "addedLines": max(0, added or 0),
        "removedLines": max(0, removed or 0),
    }
    start_line = _int_from_keys(value, _START_LINE_KEYS)
    end_line = _int_from_keys(value, _END_LINE_KEYS)
    if start_line is not None:
        payload["startLine"] = start_line
    if end_line is not None:
        payload["endLine"] = end_line
    return payload


def _replacement_delta(value: Mapping[str, Any], key_path: tuple[str, ...]) -> dict[str, Any] | None:
    old_item = _string_item_for_keys(value, _OLD_TEXT_KEYS)
    new_item = _string_item_for_keys(value, _NEW_TEXT_KEYS)
    if old_item is None or new_item is None:
        return None
    old_key, old_text = old_item
    new_key, new_text = new_item
    source = f"{_source_path(key_path, old_key)}/{_source_path(key_path, new_key)}"
    payload: dict[str, Any] = {
        "source": _clip_text(source, _MAX_CHANGE_SOURCE_CHARS),
        "addedLines": _line_count(new_text),
        "removedLines": _line_count(old_text),
    }
    start_line = _int_from_keys(value, _START_LINE_KEYS)
    end_line = _int_from_keys(value, _END_LINE_KEYS)
    if start_line is not None:
        payload["startLine"] = start_line
    if end_line is not None:
        payload["endLine"] = end_line
    return payload


def _diff_delta(value: Mapping[str, Any], key_path: tuple[str, ...]) -> dict[str, Any] | None:
    item = _string_item_for_keys(value, _DIFF_KEYS)
    if item is None:
        return None
    key, diff = item
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    if added == 0 and removed == 0:
        return None
    return {
        "source": _source_path(key_path, key),
        "addedLines": added,
        "removedLines": removed,
    }


def _write_text_delta_from_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in ("input", "args"):
        value = payload.get(key)
        found = _write_text_item(value, (key,))
        if found is not None:
            source, text = found
            return {"source": source, "addedLines": _line_count(text), "removedLines": 0}
    return None


def _write_text_item(value: Any, key_path: tuple[str, ...]) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered_key = str(key)
            if rendered_key in _WRITE_TEXT_KEYS and isinstance(child, str):
                return (_source_path(key_path, rendered_key), child)
            found = _write_text_item(child, (*key_path, rendered_key))
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found = _write_text_item(child, (*key_path, str(index)))
            if found is not None:
                return found
    return None


def _change_entity(
    event: GibsonEvent,
    observation: ChangeObservation,
    index: int,
    outcome: Mapping[str, Any] | None,
) -> ChangeWorldEntity:
    rendered_tool = event.payload.get("toolName")
    tool_name = rendered_tool if isinstance(rendered_tool, str) and rendered_tool else None
    status = (
        str(outcome.get("status"))
        if outcome is not None
        else ("planned" if event.phase == "before" else "observed")
    )
    last_outcome = None
    if outcome is not None:
        last_outcome = {
            "status": outcome.get("status"),
            "eventSequence": outcome.get("eventSequence"),
            "eventType": outcome.get("eventType"),
            "toolName": outcome.get("toolName"),
        }
    return ChangeWorldEntity(
        id=f"change:{event.sequence}:{index}",
        path=observation.path,
        operation=observation.operation,
        event_sequence=event.sequence,
        timestamp_ms=event.timestamp_ms,
        event_type=event.event_type,
        phase=event.phase,
        source=observation.source,
        tool_name=tool_name,
        status=status,
        added_lines=observation.added_lines,
        removed_lines=observation.removed_lines,
        start_line=observation.start_line,
        end_line=observation.end_line,
        last_outcome=last_outcome,
    )


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


def _change_operation(event: GibsonEvent, touch: Mapping[str, Any]) -> str:
    tool_name = _tool_name(event)
    if tool_name in _WRITE_TOOL_NAMES:
        return "write"
    if "patch" in tool_name:
        return "patch"
    if "edit" in tool_name or "replace" in tool_name:
        return "edit"
    operation = touch.get("operation")
    if isinstance(operation, str) and operation:
        return operation.split(":", 1)[0]
    return event.event_type


def _tool_name(event: GibsonEvent) -> str:
    tool_name = event.payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        return tool_name.lower()
    return event.event_type.lower()


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def _source_path(key_path: tuple[str, ...], leaf: str) -> str:
    if key_path:
        return _clip_text(".".join((*key_path, leaf)), _MAX_CHANGE_SOURCE_CHARS)
    return leaf


def _string_item_for_keys(value: Mapping[str, Any], keys: frozenset[str]) -> tuple[str, str] | None:
    for key in value:
        rendered_key = str(key)
        if rendered_key in keys and isinstance(value[key], str):
            return rendered_key, value[key]
    return None


def _int_from_keys(value: Mapping[str, Any], keys: frozenset[str]) -> int | None:
    for key in value:
        rendered_key = str(key)
        item = value[key]
        if rendered_key in keys and isinstance(item, int) and not isinstance(item, bool):
            return item
    return None


def _line_count(value: str) -> int:
    if not value:
        return 0
    return value.count("\n") + (0 if value.endswith("\n") else 1)


__all__ = [
    "WORLD_MODEL_SCHEMA",
    "ChangeObservation",
    "ChangeWorldEntity",
    "CommandObservation",
    "CommandWorldEntity",
    "FileWorldEntity",
    "HealthWorldEntity",
    "WorldFactSource",
    "WorldModel",
    "changes_from_event",
    "command_from_event",
    "health_category_from_command",
    "outcome_from_event",
]
