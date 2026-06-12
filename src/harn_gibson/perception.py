"""Perception model: a temporal entity-relation graph for renderers.

Implements the direction recorded in ``docs/decision_point.md``: one perception
model that all displays project from, fed by two sources only.

* **Harn events are the primary clock and causality source.** Commands, checks,
  touches, focus, and outcomes come from the normalized event stream as it
  happens.
* **Git state is the workspace reconciliation source.** Tracked/dirty/untracked
  paths, file sizes, measured change data, and commit milestones come from cheap
  git invocations (``ls-files``, ``status --porcelain``, ``diff --numstat``,
  ``log``). When the project root is not a git repository, a bounded filesystem
  walk stands in for the tree.

The governing rule: the perception layer observes *activity* and *structure*.
It never interprets code -- no parsing, no AST, no import graphs.

Shape (``harn-gibson.perception-model.v1``):

* ``entities`` -- typed nodes (``file``, ``dir``, ``command``, ``check``,
  ``commit``, ``agent``) with small, literal attribute bags and provenance.
* ``relations`` -- typed edges (``contains``, ``touched``, ``produced``,
  ``focused_on``) with provenance.
* ``events`` -- the recent timeline of transition facts (``file_changed``,
  ``command_completed``, ``check_completed``, ``commit_created``).

Renderers receive a *bounded snapshot*, not a whole-repo dump: the root and
top-level entries are always present (directories carry aggregate facts), and
detail is expanded around touched, dirty, and focused areas. Truncation is
explicit metadata.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import GibsonEvent
from .world_model import (
    _touched_files_by_sequence,
    changes_from_event,
    command_from_event,
    health_category_from_command,
    outcome_from_event,
)

PERCEPTION_MODEL_SCHEMA = "harn-gibson.perception-model.v1"

_GIT_TIMEOUT_SECONDS = 3.0
_MAX_GIT_FILES = 4000
_MAX_UNTRACKED_FILES = 200
_MAX_COMMAND_PREVIEW_CHARS = 200
_MAX_NARRATION_CHARS = 6000

_EXCLUDED_NAMES = {
    ".coverage",
    ".git",
    ".harn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "test-artifacts",
}
_SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "auth.json",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "tokens",
    "token",
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


def _path_visible(path: str) -> bool:
    parts = path.split("/")
    for part in parts:
        lowered = part.lower()
        if lowered in _EXCLUDED_NAMES or lowered in _SENSITIVE_NAMES:
            return False
        if lowered.endswith(_SENSITIVE_SUFFIXES):
            return False
    return True


def _plausible_touch_path(path: str) -> bool:
    """Touched-path extraction upstream is over-eager and will surface tool
    OUTPUT text ('Successfully replaced 1 block(s) in src/x.py.') or commit
    subjects as paths. A real workspace path has no spaces, is reasonably
    short, and contains a separator or extension."""
    if not path or len(path) > 200 or " " in path or "\t" in path:
        return False
    return "/" in path or "." in path


# --- git reconciliation ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    available: bool
    branch: str = ""
    head_sha: str = ""
    head_subject: str = ""
    tracked: tuple[str, ...] = ()
    dirty: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    numstat: Mapping[str, tuple[int, int]] = field(default_factory=dict)


def _run_git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def capture_git_snapshot(root: Path) -> GitSnapshot:
    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return GitSnapshot(available=False)
    # When the project root is a subdirectory of the repository, ls-files is
    # cwd-relative but status/diff report repo-root-relative paths; strip the
    # prefix so every fact uses project-root-relative paths.
    prefix = (_run_git(root, "rev-parse", "--show-prefix") or "").strip()
    branch = (_run_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    head = (_run_git(root, "log", "-1", "--format=%H%x00%s") or "").strip()
    head_sha, _, head_subject = head.partition("\x00")
    ls_files = _run_git(root, "ls-files", "-z") or ""
    tracked = tuple(
        path for path in ls_files.split("\x00") if path and _path_visible(path)
    )[:_MAX_GIT_FILES]
    status = _run_git(root, "status", "--porcelain", "-z") or ""
    dirty: list[str] = []
    untracked: list[str] = []
    for entry in status.split("\x00"):
        if len(entry) < 4:
            continue
        code, path = entry[:2], _strip_prefix(entry[3:], prefix)
        if not path or path.endswith("/") or not _path_visible(path):
            continue
        if code == "??":
            if len(untracked) < _MAX_UNTRACKED_FILES:
                untracked.append(path)
        else:
            dirty.append(path)
    numstat: dict[str, tuple[int, int]] = {}
    if head_sha:
        diff = _run_git(root, "diff", "--numstat", "HEAD", "--") or ""
        for line in diff.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            added, removed, raw_path = fields
            path = _strip_prefix(raw_path, prefix)
            if not path or not _path_visible(path):
                continue
            try:
                numstat[path] = (int(added), int(removed))
            except ValueError:
                continue  # binary files report "-"
    return GitSnapshot(
        available=True,
        branch=branch,
        head_sha=head_sha,
        head_subject=head_subject,
        tracked=tracked,
        dirty=tuple(dirty),
        untracked=tuple(untracked),
        numstat=numstat,
    )


def _message_delta(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Extract a narration delta and its channel: "thinking" (inner
    monologue), "text" (spoken), or "plain" (untyped legacy payloads).
    Tool-call argument streams return empty."""
    nested = payload.get("assistantMessageEvent")
    if isinstance(nested, Mapping):
        if _is_tool_call_delta(nested):
            return "", ""  # streamed tool-call argument JSON is not narration
        nested_type = str(nested.get("type") or "")
        if nested_type.endswith("_start") or nested_type.endswith("_end"):
            # block lifecycle markers: *_end events carry the COMPLETE block
            # content, which the deltas already streamed -- appending it
            # doubles every block verbatim
            return "", ""
        for key in ("text", "delta", "content"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value, _delta_channel(nested)
    for key in ("text", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value, "plain"
    return "", ""


def _delta_channel(nested: Mapping[str, Any]) -> str:
    index = nested.get("contentIndex")
    partial = nested.get("partial")
    content = partial.get("content") if isinstance(partial, Mapping) else None
    if isinstance(content, list) and isinstance(index, int) and 0 <= index < len(content):
        entry = content[index]
        if isinstance(entry, Mapping):
            entry_type = str(entry.get("type") or "")
            if entry_type == "thinking" or "thinking" in entry:
                return "thinking"
            if entry_type == "text":
                return "text"
    return "plain"


# sanity caps only: the full diff scrolling past is the show, so these
# trigger on genuinely insane payloads, not ordinary large edits
_MAX_DIFF_PREVIEW_LINES = 2000
_MAX_DIFF_LINE_CHARS = 240
_MAX_DIFF_LINES_PER_SIDE = 800


def _diff_preview_from_payload(payload: Mapping[str, Any]) -> list[str]:
    """A bounded, display-safe excerpt of what an edit actually changed:
    -/+ prefixed lines from edit-tool oldText/newText pairs (or the head of
    written content), clipped per line and capped overall."""
    tool = payload.get("toolName")
    inner = payload.get("input")
    if not isinstance(inner, Mapping):
        return []
    lines: list[str] = []

    def take(text: Any, prefix: str, cap: int) -> None:
        if not isinstance(text, str) or not text:
            return
        for raw in text.splitlines()[:cap]:
            if len(lines) >= _MAX_DIFF_PREVIEW_LINES:
                return
            lines.append(f"{prefix}{raw[:_MAX_DIFF_LINE_CHARS]}")

    edits = inner.get("edits")
    if isinstance(edits, list):
        for edit in edits[:64]:
            if isinstance(edit, Mapping):
                take(edit.get("oldText"), "-", _MAX_DIFF_LINES_PER_SIDE)
                take(edit.get("newText"), "+", _MAX_DIFF_LINES_PER_SIDE)
    elif tool in {"write", "create", "file_write", "write_file"}:
        take(inner.get("content") or inner.get("text"), "+", _MAX_DIFF_LINES_PER_SIDE)
    return lines


def _is_tool_call_delta(nested: Mapping[str, Any]) -> bool:
    """Message streams interleave thinking/text deltas with tool-call argument
    deltas (raw JSON); only the former are the agent's voice."""
    index = nested.get("contentIndex")
    partial = nested.get("partial")
    content = partial.get("content") if isinstance(partial, Mapping) else None
    if isinstance(content, list) and isinstance(index, int) and 0 <= index < len(content):
        entry = content[index]
        if isinstance(entry, Mapping):
            entry_type = str(entry.get("type") or "")
            if entry_type:
                return entry_type.lower().startswith("tool")
            return "thinking" not in entry and ("name" in entry or "arguments" in entry)
    return False


def _strip_prefix(path: str, prefix: str) -> str:
    """Reduce a repo-root-relative path to project-root-relative; paths outside
    the project root are dropped (returned empty)."""
    if not prefix:
        return path
    if path.startswith(prefix):
        return path[len(prefix):]
    return ""


def _walk_workspace(root: Path, *, max_files: int) -> tuple[str, ...]:
    """Bounded filesystem fallback when the root is not a git repository."""
    found: list[str] = []
    pending = [root]
    while pending and len(found) < max_files:
        directory = pending.pop(0)
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if not _path_visible(relative):
                continue
            if entry.is_dir() and not entry.is_symlink():
                pending.append(entry)
            elif entry.is_file():
                found.append(relative)
                if len(found) >= max_files:
                    break
    return tuple(found)


# --- entity state ----------------------------------------------------------------


@dataclass(slots=True)
class _FileState:
    path: str
    exists: bool = True
    tracked: bool | None = None
    dirty: bool = False
    size_bytes: int | None = None
    touch_count: int = 0
    last_touched_seq: int = 0
    basis: str = "harn-events"


@dataclass(slots=True)
class _CommandState:
    id: str
    preview: str
    tool_name: str
    status: str = "running"
    start_seq: int = 0
    end_seq: int = 0


@dataclass(slots=True)
class _CheckState:
    id: str
    category: str
    status: str
    command_id: str
    seq: int = 0
    exit_code: int | None = None


@dataclass(slots=True)
class _CommitState:
    id: str
    sha: str
    subject: str
    seq: int = 0
    files_changed: int | None = None


class PerceptionModel:
    """Temporal entity-relation graph fed by harn events + git reconciliation."""

    def __init__(
        self,
        *,
        project_root: str | None = None,
        max_events: int = 24,
        max_stat_files: int = 600,
        git_enabled: bool = True,
        discovery: str = "workspace",
    ) -> None:
        # discovery="workspace": the tree is known from git/fs immediately.
        # discovery="stream": files enter the world only when the event
        # stream touches them -- replays of recorded sessions grow the way
        # the session actually did, instead of showing the final disk state
        # at step zero (replay cannot rewind the workspace).
        self.discovery = discovery if discovery in {"workspace", "stream"} else "workspace"
        self.project_root = (
            Path(project_root).expanduser().resolve() if project_root else Path.cwd().resolve()
        )
        self.max_events = max(1, max_events)
        self.max_stat_files = max(0, max_stat_files)
        self.git_enabled = git_enabled
        self.revision = 0
        self.latest_sequence = 0
        self.latest_seen_ms = 0
        self._files: dict[str, _FileState] = {}
        self._commands: dict[str, _CommandState] = {}
        self._checks: dict[str, _CheckState] = {}
        self._commits: dict[str, _CommitState] = {}
        self._pending_commands: dict[tuple[str, str], str] = {}
        self._agent_action: dict[str, Any] = {}
        self._narration: str = ""
        self._narration_seq: int = 0
        self._narration_complete: bool = False
        self._narration_channel: str = ""
        self._narration_message_index: int = 0
        self._focused_path: str | None = None
        self._relations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._seen_event_keys: set[tuple[str, int, int, str]] = set()
        self._git: GitSnapshot = GitSnapshot(available=False)
        self._last_head_sha: str | None = None
        self._reconciled_once = False
        self._workspace_paths: tuple[str, ...] = ()

    # -- ingestion -----------------------------------------------------------

    def apply_batch(self, events: Sequence[GibsonEvent], touched_files: Mapping[str, Any]) -> None:
        touched_by_sequence = _touched_files_by_sequence(touched_files)
        changed = False
        saw_after_phase = False
        for event in events:
            key = (event.source, event.sequence, event.timestamp_ms, event.event_type)
            if key in self._seen_event_keys:
                continue
            self._seen_event_keys.add(key)
            changed = True
            self.latest_sequence = max(self.latest_sequence, event.sequence)
            self.latest_seen_ms = max(self.latest_seen_ms, event.timestamp_ms)
            if event.phase == "after":
                saw_after_phase = True
            touches = touched_by_sequence.get(event.sequence, ())
            self._observe_narration(event)
            if event.event_type in {"agent_end", "session_shutdown", "harn_exit"}:
                # closing pose: the work is done; attention returns to the
                # whole project instead of lingering on the last touched file
                self._focused_path = None
                self._upsert_relation(
                    "focused_on", "agent", "dir:.",
                    seq=event.sequence, provenance="inferred", confidence=0.7,
                    exclusive=True,
                )
            check_launched = self._observe_command(event)
            command_id = self._command_for_sequence(event.sequence)
            # narrative attention: launching a check means attending to the
            # whole project, not the file paths named in the command line --
            # the cursor travels to the root BEFORE the verdict lands there
            if check_launched:
                self._upsert_relation(
                    "focused_on", "agent", "dir:.",
                    seq=event.sequence, provenance="inferred", confidence=0.7,
                    exclusive=True,
                )
            self._observe_touches(event, touches, command_id, suppress_focus=check_launched)
            outcome = outcome_from_event(event)
            if outcome is not None:
                self._observe_outcome(event, outcome, command_id)
            self._observe_payload_changes(event, touches)
        if changed and (saw_after_phase or not self._reconciled_once):
            self._reconcile()
        if changed:
            self.revision += 1

    def _observe_narration(self, event: GibsonEvent) -> None:
        """Track what the agent is saying. Streamed chunks accumulate into the
        current message (bounded to a tail); message_end seals it. This makes
        the assistant's voice a perception fact instead of page chrome."""
        if event.event_type not in {"message_update", "message_end"}:
            return
        text, channel = _message_delta(event.payload)
        if event.event_type == "message_update" and text:
            if self._narration_complete:
                self._narration = ""
                self._narration_channel = ""
                self._narration_complete = False
                self._narration_message_index += 1
            # a message streams its inner monologue (thinking) and then its
            # spoken text; blinking the monologue away mid-read is jarring, so
            # the spoken text CONTINUES beneath it in the same window -- one
            # message, one scroll
            if channel == "text" and self._narration_channel == "thinking" and self._narration:
                self._narration = self._narration.rstrip() + "\n\n"
            if channel in {"thinking", "text"}:
                self._narration_channel = channel
            # keep the HEAD on overflow: the thesis of a message lives at the
            # start; displays scroll forward through it
            self._narration = (self._narration + text)[:_MAX_NARRATION_CHARS]
            self._narration_seq = event.sequence
        elif event.event_type == "message_end":
            if text:
                self._narration = text[:_MAX_NARRATION_CHARS]
            self._narration_seq = event.sequence
            self._narration_complete = True
            self._narration_channel = ""

    def _observe_command(self, event: GibsonEvent) -> bool:
        """Returns True when this event launches a health check (test/build)."""
        observation = command_from_event(event)
        if observation is None:
            return False
        pending_key = (observation.tool_name, observation.command)
        if event.phase == "before":
            prior_id = self._pending_commands.get(pending_key)
            prior = self._commands.get(prior_id) if prior_id else None
            if (
                prior is not None
                and prior.status == "running"
                and event.sequence - prior.start_seq <= 2
            ):
                # one run announces itself twice (tool_execution_start, then
                # tool_call); a second running command here would leave an
                # orphan for tool_execution_end to settle -- doubling the
                # check, the breach, and the check_started beat
                return False
            command_id = f"command:{event.sequence}"
            self._commands[command_id] = _CommandState(
                id=command_id,
                preview=observation.command[:_MAX_COMMAND_PREVIEW_CHARS],
                tool_name=observation.tool_name,
                status="running",
                start_seq=event.sequence,
            )
            self._pending_commands[pending_key] = command_id
            # a check launching is itself a story beat (the analysis prelude)
            category = health_category_from_command(observation.command)
            if category is not None:
                self._append_event({
                    "seq": event.sequence,
                    "ts": event.timestamp_ms,
                    "kind": "check_started",
                    "entity": command_id,
                    "category": category,
                })
                return True
        else:
            command_id = self._pending_commands.get(pending_key)
            existing = self._commands.get(command_id) if command_id else None
            verdict = "error" if bool(event.payload.get("isError")) else "ok"
            if (
                existing is not None
                and existing.status == verdict
                and event.sequence - existing.end_seq <= 2
            ):
                # the same run reports completion twice (tool_result then
                # tool_execution_end) with the same verdict; minting a
                # "re-run" here would double every check -- and every breach.
                # A nearby completion with a DIFFERENT verdict is a real
                # retry and falls through to mint a fresh command.
                return False
            if existing is None or existing.status != "running":
                # No live command for this text: either the call event was never
                # seen, or this is a re-run of an already-settled command (the
                # retry-after-failure loop). Mint a fresh command entity so the
                # new outcome is attributed to the new run, not the old one.
                replacement_id = f"command:{event.sequence}"
                if replacement_id not in self._commands:
                    self._commands[replacement_id] = _CommandState(
                        id=replacement_id,
                        preview=observation.command[:_MAX_COMMAND_PREVIEW_CHARS],
                        tool_name=observation.tool_name,
                        status="running",
                        start_seq=event.sequence,
                    )
                    self._pending_commands[pending_key] = replacement_id
        return False

    def _command_for_sequence(self, sequence: int) -> str | None:
        # insertion order is ascending start_seq, so the last match is the
        # most recent command covering this sequence
        best: _CommandState | None = None
        for command in self._commands.values():
            if command.start_seq <= sequence and (command.status == "running" or command.end_seq >= sequence):
                best = command
        return best.id if best else None

    def _observe_touches(
        self,
        event: GibsonEvent,
        touches: Sequence[Mapping[str, Any]],
        command_id: str | None,
        *,
        suppress_focus: bool = False,
    ) -> None:
        primary: str | None = None
        primary_is_write = False
        for touch in touches:
            path = str(touch.get("path") or "")
            if not path or not _path_visible(path) or not _plausible_touch_path(path):
                continue
            # during-phase-only touches are streamed tool-call arguments caught
            # mid-flight (path prefixes like "src/m", "src/mid", ...): the
            # completed call re-reports the real path with a before/after phase
            phases = touch.get("phases")
            if isinstance(phases, list) and phases and all(p == "during" for p in phases):
                continue
            state = self._files.get(path)
            if state is None:
                state = _FileState(path=path)
                self._files[path] = state
            state.touch_count += 1
            state.last_touched_seq = event.sequence
            # focus prefers files the agent WRITES over files merely named on a
            # command line (uv sync mentioning pyproject.toml is not attention)
            operation = str(touch.get("operation") or "")
            is_write = operation.split(":", 1)[0] in {"edit", "write", "create", "file_write", "write_file"}
            if primary is None or (is_write and not primary_is_write):
                primary = path
                primary_is_write = is_write
            source = command_id or f"event:{event.sequence}"
            self._upsert_relation(
                "touched", source, f"file:{path}",
                seq=event.sequence, provenance="observed", confidence=1.0,
            )
        if primary is not None and not suppress_focus:
            # paths named in a check's command line are arguments, not where
            # the attention is -- focus stays on the project while it runs
            self._focused_path = primary
            self._agent_action = {
                "eventType": event.event_type,
                "phase": event.phase,
                "sequence": event.sequence,
            }
            self._upsert_relation(
                "focused_on", "agent", f"file:{primary}",
                seq=event.sequence, provenance="inferred", confidence=0.7,
                exclusive=True,
            )

    def _observe_outcome(
        self, event: GibsonEvent, outcome: Mapping[str, Any], command_id: str | None
    ) -> None:
        status = str(outcome.get("status") or "ok")
        if command_id is not None:
            command = self._commands.get(command_id)
            if command is not None and command.status == "running":
                command.status = status
                command.end_seq = event.sequence
                self._append_event({
                    "seq": event.sequence,
                    "ts": event.timestamp_ms,
                    "kind": "command_completed",
                    "entity": command_id,
                    "status": status,
                })
                category = health_category_from_command(command.preview)
                if category is not None:
                    check_id = f"check:{category}:{event.sequence}"
                    exit_code = outcome.get("exitCode")
                    self._checks[check_id] = _CheckState(
                        id=check_id,
                        category=category,
                        status=status,
                        command_id=command_id,
                        seq=event.sequence,
                        exit_code=exit_code if isinstance(exit_code, int) else None,
                    )
                    self._upsert_relation(
                        "produced", command_id, check_id,
                        seq=event.sequence, provenance="observed", confidence=1.0,
                    )
                    self._append_event({
                        "seq": event.sequence,
                        "ts": event.timestamp_ms,
                        "kind": "check_completed",
                        "entity": check_id,
                        "category": category,
                        "status": status,
                    })

    def _observe_payload_changes(self, event: GibsonEvent, touches: Sequence[Mapping[str, Any]]) -> None:
        # Event-local change facts from tool payloads (e.g. edit deltas). Sizes
        # are unknown at this point, so churn follows the "observed but
        # unmeasured" rule; git reconciliation supplies measured sizes later.
        diff_preview = _diff_preview_from_payload(event.payload)
        for change in changes_from_event(event, touches, None):
            if not _path_visible(change.path) or not _plausible_touch_path(change.path):
                continue
            payload: dict[str, Any] = {
                "seq": event.sequence,
                "ts": event.timestamp_ms,
                "kind": "file_changed",
                "entity": f"file:{change.path}",
                "churnFraction": 1.0,
                "basis": "harn-payload",
            }
            if change.added_lines or change.removed_lines:
                payload["addedLines"] = change.added_lines
                payload["removedLines"] = change.removed_lines
            # the tool_result repeats the tool_call's input verbatim; attach
            # the preview once, at the canonical edit moment
            if diff_preview and event.phase == "before":
                payload["diffPreview"] = diff_preview
            self._append_event(payload)

    # -- reconciliation --------------------------------------------------------

    def _reconcile(self) -> None:
        self._reconciled_once = True
        if self.git_enabled:
            self._git = capture_git_snapshot(self.project_root)
        else:
            self._git = GitSnapshot(available=False)
        if self._git.available:
            workspace = list(self._git.tracked)
            workspace.extend(p for p in self._git.untracked if p not in self._git.tracked)
            self._workspace_paths = tuple(workspace)
            tracked = set(self._git.tracked)
            dirty = set(self._git.dirty)
            untracked = set(self._git.untracked)
            basis = "git"
        else:
            self._workspace_paths = _walk_workspace(self.project_root, max_files=_MAX_GIT_FILES)
            tracked = set()
            dirty = set()
            untracked = set()
            basis = "filesystem"

        if self.discovery == "stream":
            self._workspace_paths = tuple(
                path for path in self._workspace_paths if path in self._files
            )
        known = set(self._workspace_paths)
        for path in known:
            state = self._files.get(path)
            if state is None:
                state = _FileState(path=path)
                self._files[path] = state
            state.exists = True
            state.tracked = (path in tracked) if self._git.available else None
            state.dirty = path in dirty or path in untracked
            state.basis = basis
        for path, state in self._files.items():
            if path not in known and state.basis in {"git", "filesystem"}:
                state.exists = False
        # sweep streamed-argument debris: a touch-only entity that never existed
        # on disk and is a strict prefix of a real path is a caught-mid-flight
        # fragment, not a file
        fragments = [
            path for path, state in self._files.items()
            if state.basis == "harn-events" and path not in known
            and any(real != path and real.startswith(path) for real in known)
        ]
        for path in fragments:
            del self._files[path]
            self._relations = {
                key: value for key, value in self._relations.items()
                if key[1] != f"file:{path}" and key[2] != f"file:{path}"
            }
            if self._focused_path == path:
                self._focused_path = None

        self._stat_sizes()
        self._detect_commit()

    def _stat_sizes(self) -> None:
        """Refresh sizes for the interesting slice; emit measured file_changed
        events when a known size moved. Bounded by max_stat_files."""
        interesting: list[str] = []
        seen: set[str] = set()
        for path in self._interesting_paths():
            if path not in seen:
                seen.add(path)
                interesting.append(path)
        budget = self.max_stat_files
        for path in interesting:
            if budget <= 0:
                break
            state = self._files.get(path)
            if state is None or not state.exists:
                continue
            budget -= 1
            try:
                size = (self.project_root / path).stat().st_size
            except OSError:
                state.exists = False
                continue
            before = state.size_bytes
            state.size_bytes = size
            if before is not None and before != size:
                churn = abs(size - before) / max(size, before, 1)
                payload: dict[str, Any] = {
                    "seq": self.latest_sequence,
                    "ts": self.latest_seen_ms,
                    "kind": "file_changed",
                    "entity": f"file:{path}",
                    "sizeBefore": before,
                    "sizeAfter": size,
                    "churnFraction": round(min(1.0, churn), 4),
                    "basis": "git" if self._git.available else "filesystem",
                }
                numstat = self._git.numstat.get(path)
                if numstat is not None:
                    payload["addedLines"], payload["removedLines"] = numstat
                self._append_event(payload)

    def _interesting_paths(self) -> list[str]:
        """Touched, dirty, focused, and top-level files -- the expanded slice."""
        paths = [p for p, s in self._files.items() if s.touch_count > 0]
        paths.extend(self._git.dirty)
        paths.extend(self._git.untracked)
        if self._focused_path:
            paths.append(self._focused_path)
        paths.extend(p for p in self._workspace_paths if "/" not in p)
        return [p for p in paths if _path_visible(p)]

    def _detect_commit(self) -> None:
        head = self._git.head_sha
        if not head:
            return
        if self._last_head_sha is not None and head != self._last_head_sha:
            commit_id = f"commit:{head[:12]}"
            files_changed = self._commit_files_changed()
            self._commits[commit_id] = _CommitState(
                id=commit_id,
                sha=head,
                subject=self._git.head_subject,
                seq=self.latest_sequence,
                files_changed=files_changed,
            )
            self._append_event({
                "seq": self.latest_sequence,
                "ts": self.latest_seen_ms,
                "kind": "commit_created",
                "entity": commit_id,
                "subject": self._git.head_subject[:120],
                "filesChanged": files_changed,
            })
            checkpoint = self._latest_checkpoint_command()
            if checkpoint is not None:
                self._upsert_relation(
                    "produced", checkpoint, commit_id,
                    seq=self.latest_sequence, provenance="inferred", confidence=0.8,
                )
        self._last_head_sha = head

    def _commit_files_changed(self) -> int | None:
        listing = _run_git(self.project_root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        if listing is None:
            return None
        return sum(1 for line in listing.splitlines() if line.strip())

    def _latest_checkpoint_command(self) -> str | None:
        best: _CommandState | None = None
        for command in self._commands.values():
            if "git commit" in command.preview and (best is None or command.start_seq > best.start_seq):
                best = command
        return best.id if best else None

    # -- bookkeeping -------------------------------------------------------------

    def _upsert_relation(
        self,
        relation_type: str,
        from_id: str,
        to_id: str,
        *,
        seq: int,
        provenance: str,
        confidence: float,
        exclusive: bool = False,
    ) -> None:
        if exclusive:
            stale = [key for key in self._relations if key[0] == relation_type and key[1] == from_id]
            for key in stale:
                del self._relations[key]
        self._relations[(relation_type, from_id, to_id)] = {
            "type": relation_type,
            "from": from_id,
            "to": to_id,
            "lastSeq": seq,
            "provenance": {"source": provenance, "confidence": confidence},
        }

    def _append_event(self, payload: dict[str, Any]) -> None:
        payload.setdefault("provenance", {"source": "observed", "confidence": 1.0})
        self._events.append(payload)
        if len(self._events) > self.max_events * 4:
            del self._events[: len(self._events) - self.max_events * 4]

    # -- snapshot ------------------------------------------------------------------

    def to_dict(self, *, max_entities: int = 96, max_relations: int = 144) -> dict[str, Any]:
        visible_files, file_truncated = self._visible_files(max_entities)
        dirs = self._dir_aggregates(visible_files)
        agent_attrs: dict[str, Any] = {k: v for k, v in self._agent_action.items()}
        if self._narration:
            agent_attrs["narration"] = self._narration
            agent_attrs["narrationSeq"] = self._narration_seq
            agent_attrs["narrationComplete"] = self._narration_complete
            agent_attrs["narrationMessageIndex"] = self._narration_message_index
        entities: list[dict[str, Any]] = []
        entities.append({
            "id": "agent",
            "type": "agent",
            "attrs": agent_attrs,
            "provenance": {"source": "inferred", "confidence": 0.7},
        })
        for dir_payload in dirs:
            entities.append(dir_payload)
        for path in visible_files:
            entities.append(self._file_payload(self._files[path]))
        for command in sorted(self._commands.values(), key=lambda c: -c.start_seq)[:12]:
            entities.append({
                "id": command.id,
                "type": "command",
                "attrs": {
                    "preview": command.preview,
                    "toolName": command.tool_name,
                    "status": command.status,
                    "startSeq": command.start_seq,
                    "endSeq": command.end_seq or None,
                },
                "provenance": {"source": "observed", "confidence": 1.0},
            })
        for check in sorted(self._checks.values(), key=lambda c: -c.seq)[:8]:
            attrs: dict[str, Any] = {"category": check.category, "status": check.status, "seq": check.seq}
            if check.exit_code is not None:
                attrs["exitCode"] = check.exit_code
            entities.append({
                "id": check.id, "type": "check", "attrs": attrs,
                "provenance": {"source": "observed", "confidence": 1.0},
            })
        for commit in sorted(self._commits.values(), key=lambda c: -c.seq)[:6]:
            attrs = {"sha": commit.sha[:12], "subject": commit.subject[:120], "seq": commit.seq}
            if commit.files_changed is not None:
                attrs["filesChanged"] = commit.files_changed
            entities.append({
                "id": commit.id, "type": "commit", "attrs": attrs,
                "provenance": {"source": "observed", "confidence": 1.0},
            })

        entity_ids = {entity["id"] for entity in entities}
        relations = self._visible_relations(entity_ids, visible_files, max_relations)

        counts_by_type: dict[str, int] = {}
        for entity in entities:
            counts_by_type[entity["type"]] = counts_by_type.get(entity["type"], 0) + 1
        relation_counts: dict[str, int] = {}
        for relation in relations:
            relation_counts[relation["type"]] = relation_counts.get(relation["type"], 0) + 1

        return {
            "schema": PERCEPTION_MODEL_SCHEMA,
            "revision": self.revision,
            "latestSequence": self.latest_sequence,
            "latestSeenMs": self.latest_seen_ms,
            "workspace": {
                "rootName": self.project_root.name,
                "git": {
                    "available": self._git.available,
                    "branch": self._git.branch,
                    "headSha": self._git.head_sha[:12],
                    "dirtyPathCount": len(self._git.dirty),
                    "untrackedPathCount": len(self._git.untracked),
                },
                "fileCount": len(self._workspace_paths),
                "basis": "git" if self._git.available else "filesystem",
            },
            "entities": entities,
            "relations": relations,
            "events": self._events[-self.max_events:],
            "counts": {
                "entitiesByType": dict(sorted(counts_by_type.items())),
                "relationsByType": dict(sorted(relation_counts.items())),
                "events": len(self._events),
            },
            "truncation": {
                "files": file_truncated,
                "events": len(self._events) > self.max_events,
                "workspaceFileCount": len(self._workspace_paths),
                "renderedFileCount": len(visible_files),
            },
            "provenance": {
                "sources": ["harn-events", "git"] if self._git.available else ["harn-events", "filesystem"],
                "notes": [
                    "Harn events are the primary clock and causality source.",
                    "Git snapshots reconcile workspace state; sizes and change deltas are measured, not parsed.",
                    "No code interpretation occurs in this layer.",
                ],
            },
        }

    def _visible_files(self, max_entities: int) -> tuple[list[str], bool]:
        """Bounded snapshot: active slice first, then top-level files, then the
        rest of the tree until the budget runs out."""
        budget = max(8, max_entities)
        ordered: list[str] = []
        seen: set[str] = set()

        def take(path: str) -> None:
            if path not in seen and path in self._files and _path_visible(path):
                seen.add(path)
                ordered.append(path)

        active = sorted(
            (p for p, s in self._files.items() if s.touch_count > 0 or s.dirty),
            key=lambda p: -(self._files[p].last_touched_seq),
        )
        for path in active:
            take(path)
        if self._focused_path:
            take(self._focused_path)
        for path in self._workspace_paths:
            if "/" not in path:
                take(path)
        for path in self._workspace_paths:
            take(path)
        truncated = len(ordered) > budget
        return ordered[:budget], truncated

    def _file_payload(self, state: _FileState) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "exists": state.exists,
            "touchCount": state.touch_count,
            "lastTouchedSeq": state.last_touched_seq,
        }
        if state.size_bytes is not None:
            attrs["sizeBytes"] = state.size_bytes
        if state.tracked is not None:
            attrs["tracked"] = state.tracked
        if state.dirty:
            attrs["dirty"] = True
        return {
            "id": f"file:{state.path}",
            "type": "file",
            "attrs": attrs,
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "basis": state.basis,
            },
        }

    def _dir_aggregates(self, visible_files: list[str]) -> list[dict[str, Any]]:
        """Directory entities: every ancestor of a visible file, plus all
        top-level directories with aggregate facts (counts always; sizes when
        the stat budget covered them)."""
        wanted: set[str] = set()
        for path in visible_files:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                wanted.add("/".join(parts[:depth]))
        for path in self._workspace_paths:
            if "/" in path:
                wanted.add(path.split("/", 1)[0])

        file_counts: dict[str, int] = {}
        size_sums: dict[str, int] = {}
        size_complete: dict[str, bool] = {}
        for path in self._workspace_paths:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                prefix = "/".join(parts[:depth])
                if prefix not in wanted:
                    continue
                file_counts[prefix] = file_counts.get(prefix, 0) + 1
                state = self._files.get(path)
                if state is not None and state.size_bytes is not None:
                    size_sums[prefix] = size_sums.get(prefix, 0) + state.size_bytes
                else:
                    size_complete[prefix] = False

        payloads: list[dict[str, Any]] = [{
            "id": "dir:.",
            "type": "dir",
            "attrs": {"fileCount": len(self._workspace_paths), "root": True},
            "provenance": {
                "source": "observed",
                "confidence": 1.0,
                "basis": "git" if self._git.available else "filesystem",
            },
        }]
        for directory in sorted(wanted):
            attrs: dict[str, Any] = {"fileCount": file_counts.get(directory, 0)}
            if size_complete.get(directory, True) and directory in size_sums:
                attrs["sizeBytes"] = size_sums[directory]
            payloads.append({
                "id": f"dir:{directory}",
                "type": "dir",
                "attrs": attrs,
                "provenance": {
                    "source": "observed",
                    "confidence": 1.0,
                    "basis": "git" if self._git.available else "filesystem",
                },
            })
        return payloads

    def _visible_relations(
        self, entity_ids: set[str], visible_files: list[str], max_relations: int
    ) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        # contains: derived from paths for the visible slice (the tree relation
        # is implicit in ids; rendered explicitly so displays never re-derive it).
        for entity_id in sorted(entity_ids):
            if entity_id.startswith("file:"):
                path = entity_id[5:]
                parent = path.rsplit("/", 1)[0] if "/" in path else None
                parent_id = f"dir:{parent}" if parent else "dir:."
            elif entity_id.startswith("dir:") and entity_id != "dir:.":
                directory = entity_id[4:]
                parent = directory.rsplit("/", 1)[0] if "/" in directory else None
                parent_id = f"dir:{parent}" if parent else "dir:."
            else:
                continue
            # every ancestor of a visible entity is guaranteed by _dir_aggregates
            relations.append({
                "type": "contains",
                "from": parent_id,
                "to": entity_id,
                "provenance": {"source": "observed", "confidence": 1.0},
            })
        for record in sorted(self._relations.values(), key=lambda r: -int(r.get("lastSeq", 0))):
            to_id = record["to"]
            if to_id.startswith("file:") and to_id[5:] not in visible_files:
                continue
            relations.append(dict(record))
        return relations[: max(8, max_relations)]


__all__ = [
    "PERCEPTION_MODEL_SCHEMA",
    "GitSnapshot",
    "PerceptionModel",
    "capture_git_snapshot",
]
