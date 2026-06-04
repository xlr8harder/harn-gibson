"""Inferred agent attention and current intent for renderer context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harn_gibson.events import GibsonEvent
from harn_gibson.shell_commands import shell_command_has_in_place_edit

AGENT_ATTENTION_SCHEMA = "harn-gibson.agent-attention.v1"

_COMMAND_KEYS = frozenset({"cmd", "command", "shellCommand"})
_MAX_OBJECTIVE_CHARS = 180
_MAX_SUMMARY_CHARS = 240


def agent_attention_from_context(
    events: Sequence[GibsonEvent],
    touched_files: Mapping[str, Any],
    world_model: Mapping[str, Any],
    *,
    max_focus_paths: int = 8,
) -> dict[str, Any]:
    """Build a compact inferred attention payload for renderer agents."""

    latest = events[-1] if events else None
    rendered_touched_files = _mapping(touched_files)
    rendered_world_model = _mapping(world_model)
    focus_paths, focus_truncated = _focus_paths(rendered_touched_files, rendered_world_model, max_focus_paths)
    action = _action_from_event(latest)
    objective = _objective_from_event(latest, action)
    payload: dict[str, Any] = {
        "schema": AGENT_ATTENTION_SCHEMA,
        "action": action,
        "focus": {
            "primaryPath": focus_paths[0] if focus_paths else None,
            "paths": focus_paths,
            "entities": [
                {
                    "id": f"file:{path}",
                    "kind": "file",
                    "path": path,
                    "reason": _focus_reason(path, rendered_touched_files),
                }
                for path in focus_paths
            ],
            "truncated": focus_truncated,
        },
        "signals": _attention_signals(latest, rendered_touched_files, rendered_world_model),
        "provenance": {
            "source": "inferred",
            "confidence": action["confidence"],
            "basis": "Derived from current normalized harn events, touched-file batches, and world-model health.",
        },
    }
    if objective is not None:
        payload["objective"] = objective
    health_focus = _health_focus(rendered_world_model)
    if health_focus is not None:
        payload["healthFocus"] = health_focus
    return payload


def _action_from_event(event: GibsonEvent | None) -> dict[str, Any]:
    if event is None:
        return _action_payload("idle", "Await agent activity", "", 0, "", "", 0.5)
    command = _command_text_from_value(event.payload)
    if event.event_type == "runtime_error":
        return _action_payload("diagnose", "Diagnose runtime failure", event.summary, event, confidence=0.88)
    if event.event_type in {"tool_result", "tool_execution_end"}:
        kind = "diagnose" if bool(event.payload.get("isError")) else "observe_result"
        label = "Diagnose failed tool result" if kind == "diagnose" else "Observe completed tool result"
        confidence = 0.86 if kind == "diagnose" else 0.72
        return _action_payload(kind, label, event.summary, event, confidence=confidence)
    if command:
        kind, label, confidence = _command_action(command)
        return _action_payload(kind, label, command, event, confidence=confidence)
    if event.event_type in {"input", "browser_input"}:
        return _action_payload("follow_user", "Handle user input", event.summary, event, confidence=0.74)
    if event.event_type in {"message_update", "message_end"}:
        return _action_payload("respond", "Stream or finalize response", event.summary, event, confidence=0.7)
    if event.phase == "lifecycle":
        return _action_payload("coordinate", "Coordinate session lifecycle", event.summary, event, confidence=0.62)
    return _action_payload("operate", "Handle current event", event.summary, event, confidence=0.58)


def _action_payload(
    kind: str,
    label: str,
    summary: str,
    event: GibsonEvent | int,
    event_type: str | None = None,
    phase: str | None = None,
    confidence: float = 0.6,
) -> dict[str, Any]:
    if isinstance(event, GibsonEvent):
        sequence = event.sequence
        rendered_event_type = event.event_type
        rendered_phase = event.phase
    else:
        sequence = event
        rendered_event_type = event_type or ""
        rendered_phase = phase or ""
    return {
        "kind": kind,
        "label": label,
        "summary": _clip_text(summary, _MAX_SUMMARY_CHARS),
        "eventType": rendered_event_type,
        "phase": rendered_phase,
        "sequence": sequence,
        "confidence": confidence,
    }


def _command_action(command: str) -> tuple[str, str, float]:
    normalized = " ".join(command.lower().split())
    if _looks_like_test_command(normalized):
        return "verify", "Verify current work", 0.86
    if _looks_like_build_command(normalized):
        return "build", "Build project artifacts", 0.82
    if shell_command_has_in_place_edit(command) or _looks_like_edit_command(normalized):
        return "edit", "Edit focused files", 0.82
    if _looks_like_inspection_command(normalized):
        return "inspect", "Inspect project state", 0.78
    if "git commit" in normalized or "git add" in normalized:
        return "checkpoint", "Checkpoint repository state", 0.78
    if "git " in normalized:
        return "version_control", "Review repository state", 0.72
    return "command", "Run shell command", 0.64


def _looks_like_test_command(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "pytest",
            "tox",
            "nox",
            "npm test",
            "npm run test",
            "pnpm test",
            "yarn test",
            "cargo test",
            "go test",
            "make test",
        )
    )


def _looks_like_build_command(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "python -m build",
            "uv build",
            "npm run build",
            "pnpm build",
            "yarn build",
            "cargo build",
            "go build",
            "make build",
        )
    )


def _looks_like_edit_command(normalized: str) -> bool:
    return any(marker in normalized for marker in ("apply_patch", " write ", " edit ", " replace "))


def _looks_like_inspection_command(normalized: str) -> bool:
    first = normalized.split(" ", 1)[0] if normalized else ""
    return first in {"cat", "find", "grep", "ls", "rg", "sed", "tree"} or normalized.startswith("git status")


def _objective_from_event(event: GibsonEvent | None, action: Mapping[str, Any]) -> dict[str, Any] | None:
    if event is None:
        return None
    if event.event_type in {"input", "browser_input"}:
        input_text = _input_text_from_event(event)
        if input_text:
            return {"text": _clip_text(input_text, _MAX_OBJECTIVE_CHARS), "source": "input.text"}
    command = _command_text_from_value(event.payload)
    if command:
        return {
            "text": f"{action.get('label', 'Run command')}: {_clip_text(command, 132)}",
            "source": "command",
        }
    if event.event_type == "runtime_error":
        return {"text": _clip_text(event.summary, _MAX_OBJECTIVE_CHARS), "source": "runtime_error"}
    if event.event_type in {"tool_result", "tool_execution_end"} and bool(event.payload.get("isError")):
        return {"text": _clip_text(event.summary, _MAX_OBJECTIVE_CHARS), "source": "tool_result"}
    return None


def _input_text_from_event(event: GibsonEvent) -> str | None:
    for key in ("text", "message"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = event.payload.get("payload")
    if isinstance(payload, Mapping):
        value = payload.get("message")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _focus_paths(
    touched_files: Mapping[str, Any],
    world_model: Mapping[str, Any],
    max_focus_paths: int,
) -> tuple[list[str], bool]:
    paths: list[str] = []
    truncated = False
    for candidates in (_touched_paths(touched_files), _health_paths(world_model), _world_file_paths(world_model)):
        missing = list(dict.fromkeys(path for path in candidates if path and path not in paths))
        before = len(paths)
        _extend_paths(paths, candidates, max_focus_paths)
        truncated = truncated or before + len(missing) > len(paths)
        if len(paths) >= max(0, max_focus_paths):
            break
    return paths, truncated


def _extend_paths(paths: list[str], candidates: Sequence[str], max_focus_paths: int) -> None:
    for path in candidates:
        if len(paths) >= max(0, max_focus_paths):
            break
        if path and path not in paths:
            paths.append(path)


def _touched_paths(touched_files: Mapping[str, Any]) -> list[str]:
    files = touched_files.get("files")
    if not isinstance(files, list):
        return []
    return [path for item in files if isinstance(item, Mapping) and isinstance(path := item.get("path"), str) and path]


def _health_paths(world_model: Mapping[str, Any]) -> list[str]:
    entities = _mapping(world_model.get("entities"))
    health_items = entities.get("health")
    if not isinstance(health_items, list):
        return []
    paths: list[str] = []
    for item in health_items:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status not in {"error", "running"}:
            continue
        touched_paths = item.get("touchedPaths")
        if isinstance(touched_paths, list):
            paths.extend(path for path in touched_paths if isinstance(path, str) and path)
    return paths


def _world_file_paths(world_model: Mapping[str, Any]) -> list[str]:
    entities = _mapping(world_model.get("entities"))
    file_items = entities.get("files")
    if not isinstance(file_items, list):
        return []
    return [path for item in file_items if isinstance(item, Mapping) and isinstance(path := item.get("path"), str)]


def _focus_reason(path: str, touched_files: Mapping[str, Any]) -> str:
    return "currentBatch" if path in _touched_paths(touched_files) else "worldModel"


def _health_focus(world_model: Mapping[str, Any]) -> dict[str, Any] | None:
    entities = _mapping(world_model.get("entities"))
    health_items = entities.get("health")
    if not isinstance(health_items, list):
        return None
    for item in health_items:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status not in {"error", "running"}:
            continue
        if not item.get("sourceCommandId"):
            continue
        return {
            "category": str(item.get("category") or "health"),
            "status": str(status),
            "sourceCommandId": str(item.get("sourceCommandId") or ""),
            "provenance": dict(item.get("provenance")) if isinstance(item.get("provenance"), Mapping) else {},
        }
    return None


def _attention_signals(
    event: GibsonEvent | None,
    touched_files: Mapping[str, Any],
    world_model: Mapping[str, Any],
) -> list[str]:
    signals = []
    if event is not None:
        signals.append("currentEvent")
    if _touched_paths(touched_files):
        signals.append("touchedFiles")
    counts = world_model.get("counts")
    if isinstance(counts, Mapping) and any(_positive_int(value) for value in counts.values()):
        signals.append("worldModel")
    return signals


def _command_text_from_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered_key = str(key)
            if rendered_key in _COMMAND_KEYS and isinstance(child, str) and child.strip():
                return child.strip()
            found = _command_text_from_value(child)
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for child in value:
            found = _command_text_from_value(child)
            if found is not None:
                return found
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _clip_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


__all__ = ["AGENT_ATTENTION_SCHEMA", "agent_attention_from_context"]
