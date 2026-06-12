"""Tests for the perception model (harn-gibson.perception-model.v1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import harn_gibson.perception as perception_module
from harn_gibson.events import GibsonEvent
from harn_gibson.perception import (
    PERCEPTION_MODEL_SCHEMA,
    PerceptionModel,
    capture_git_snapshot,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _git_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "app_pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app_pkg" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "src" / "app_pkg" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _touched(path: str, sequence: int) -> dict:
    return {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": path,
                "operation": "bash:after",
                "firstSequence": sequence,
                "lastSequence": sequence,
                "phases": ["after"],
                "sources": ["input.command"],
            }
        ],
        "count": 1,
        "truncated": False,
    }


def _command_pair(command: str, *, start_seq: int, path: str) -> tuple:
    call = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "bash", "input": {"command": command}},
        sequence=start_seq,
        timestamp_ms=start_seq * 100,
    )
    result = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": command}, "isError": False},
        sequence=start_seq + 1,
        timestamp_ms=(start_seq + 1) * 100,
    )
    touched = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": path,
                "operation": "bash:after",
                "firstSequence": start_seq,
                "lastSequence": start_seq + 1,
                "phases": ["before", "after"],
                "sources": ["input.command"],
            }
        ],
        "count": 1,
        "truncated": False,
    }
    return call, result, touched


def test_perception_builds_entities_relations_and_events_from_git_and_stream(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    call, result, touched = _command_pair("uv run pytest tests", start_seq=1, path="tests/test_app.py")
    model.apply_batch((call, result), touched)
    payload = model.to_dict()

    assert payload["schema"] == PERCEPTION_MODEL_SCHEMA
    assert payload["workspace"]["git"]["available"] is True
    assert payload["workspace"]["basis"] == "git"
    assert payload["workspace"]["fileCount"] == 4

    by_id = {entity["id"]: entity for entity in payload["entities"]}
    assert "agent" in by_id
    assert by_id["dir:."]["attrs"]["root"] is True
    assert by_id["dir:src"]["type"] == "dir"
    assert by_id["dir:src/app_pkg"]["attrs"]["fileCount"] == 2
    touched_file = by_id["file:tests/test_app.py"]
    assert touched_file["attrs"]["touchCount"] == 2
    assert touched_file["attrs"]["tracked"] is True
    assert touched_file["attrs"]["exists"] is True
    assert touched_file["attrs"]["sizeBytes"] > 0
    assert touched_file["provenance"]["basis"] == "git"
    command = by_id["command:1"]
    assert command["attrs"]["status"] == "ok"
    assert command["attrs"]["preview"] == "uv run pytest tests"
    checks = [entity for entity in payload["entities"] if entity["type"] == "check"]
    assert len(checks) == 1
    assert checks[0]["attrs"]["category"] == "test"
    assert checks[0]["attrs"]["status"] == "ok"

    relations = payload["relations"]
    relation_set = {(r["type"], r["from"], r["to"]) for r in relations}
    assert ("contains", "dir:.", "dir:src") in relation_set
    assert ("contains", "dir:src", "dir:src/app_pkg") in relation_set
    assert ("contains", "dir:tests", "file:tests/test_app.py") in relation_set
    assert ("touched", "command:1", "file:tests/test_app.py") in relation_set
    assert ("produced", "command:1", checks[0]["id"]) in relation_set
    assert ("focused_on", "agent", "file:tests/test_app.py") in relation_set
    focused = next(r for r in relations if r["type"] == "focused_on")
    assert focused["provenance"]["source"] == "inferred"

    kinds = [event["kind"] for event in payload["events"]]
    assert "command_completed" in kinds
    assert "check_completed" in kinds


def test_perception_emits_measured_file_changed_events(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    first = _command_pair("cat src/app_pkg/app.py", start_seq=1, path="src/app_pkg/app.py")
    model.apply_batch(first[:2], first[2])

    target = root / "src" / "app_pkg" / "app.py"
    before = target.stat().st_size
    target.write_text("print('hello')\nprint('world, much longer now')\n", encoding="utf-8")
    after = target.stat().st_size

    second = _command_pair("uv run pytest tests", start_seq=4, path="src/app_pkg/app.py")
    model.apply_batch(second[:2], second[2])
    payload = model.to_dict()

    measured = [
        event for event in payload["events"]
        if event["kind"] == "file_changed" and event.get("sizeBefore") is not None
    ]
    assert len(measured) == 1
    event = measured[0]
    assert event["entity"] == "file:src/app_pkg/app.py"
    assert event["sizeBefore"] == before
    assert event["sizeAfter"] == after
    assert 0.0 < event["churnFraction"] <= 1.0
    assert event["basis"] == "git"
    # the dirty flag from git status survives onto the entity
    by_id = {entity["id"]: entity for entity in payload["entities"]}
    assert by_id["file:src/app_pkg/app.py"]["attrs"]["dirty"] is True


def test_perception_records_commit_milestones(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    first = _command_pair("cat README.md", start_seq=1, path="README.md")
    model.apply_batch(first[:2], first[2])

    (root / "README.md").write_text("# fixture\n\nupdated\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "update readme")

    second = _command_pair("git commit -m 'update readme'", start_seq=4, path="README.md")
    model.apply_batch(second[:2], second[2])
    payload = model.to_dict()

    commits = [entity for entity in payload["entities"] if entity["type"] == "commit"]
    assert len(commits) == 1
    assert commits[0]["attrs"]["subject"] == "update readme"
    assert commits[0]["attrs"]["filesChanged"] == 1
    commit_events = [event for event in payload["events"] if event["kind"] == "commit_created"]
    assert len(commit_events) == 1
    assert commit_events[0]["entity"] == commits[0]["id"]
    produced = {
        (r["from"], r["to"]) for r in payload["relations"] if r["type"] == "produced"
    }
    assert ("command:4", commits[0]["id"]) in produced


def test_perception_falls_back_to_filesystem_walk_without_git(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "notes.md").write_text("notes\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("ignored\n", encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "topsecret.txt").write_text("ignored\n", encoding="utf-8")

    model = PerceptionModel(project_root=str(root))
    call, result, touched = _command_pair("cat src/main.py", start_seq=1, path="src/main.py")
    model.apply_batch((call, result), touched)
    payload = model.to_dict()

    assert payload["workspace"]["git"]["available"] is False
    assert payload["workspace"]["basis"] == "filesystem"
    paths = {entity["id"] for entity in payload["entities"] if entity["type"] == "file"}
    assert "file:src/main.py" in paths
    assert "file:notes.md" in paths
    assert not any(".venv" in path or "secrets" in path for path in paths)
    main = next(e for e in payload["entities"] if e["id"] == "file:src/main.py")
    assert main["provenance"]["basis"] == "filesystem"
    assert "tracked" not in main["attrs"]


def test_perception_snapshot_is_bounded_with_truncation_metadata(tmp_path: Path) -> None:
    root = tmp_path / "wide"
    (root / "pkg" / "sub").mkdir(parents=True)
    for index in range(40):
        (root / "pkg" / f"module_{index:02d}.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "hot.py").write_text("y = 2\n", encoding="utf-8")
    # deep file that falls outside the bounded snapshot: its directory is not
    # expanded, so per-dir aggregation skips it beyond the top level
    (root / "pkg" / "sub" / "zz_deep.py").write_text("z = 3\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")

    model = PerceptionModel(project_root=str(root))
    call, result, touched = _command_pair("cat pkg/hot.py", start_seq=1, path="pkg/hot.py")
    model.apply_batch((call, result), touched)
    payload = model.to_dict(max_entities=10)

    files = [entity for entity in payload["entities"] if entity["type"] == "file"]
    assert len(files) == 10
    assert payload["truncation"]["files"] is True
    assert payload["truncation"]["workspaceFileCount"] == 42
    assert payload["truncation"]["renderedFileCount"] == 10
    # the touched file wins a slot ahead of the alphabetical tail
    assert any(entity["id"] == "file:pkg/hot.py" for entity in files)
    # the directory aggregate still reports the full count
    pkg = next(entity for entity in payload["entities"] if entity["id"] == "dir:pkg")
    assert pkg["attrs"]["fileCount"] == 42
    # the unexpanded subdirectory is not an entity of its own
    assert not any(entity["id"] == "dir:pkg/sub" for entity in payload["entities"])


def test_perception_tracks_agent_narration(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    chunk_one = GibsonEvent.from_raw(
        {"type": "message_update", "text": "scanning the cli module "}, sequence=1, timestamp_ms=100
    )
    chunk_two = GibsonEvent.from_raw(
        {"type": "message_update", "assistantMessageEvent": {"delta": "and patching the exit code"}},
        sequence=2,
        timestamp_ms=200,
    )
    empty = {"schema": "harn-gibson.touched-files.v1", "files": [], "count": 0, "truncated": False}
    model.apply_batch((chunk_one, chunk_two), empty)
    agent = next(e for e in model.to_dict()["entities"] if e["id"] == "agent")
    assert agent["attrs"]["narration"] == "scanning the cli module and patching the exit code"
    assert agent["attrs"]["narrationComplete"] is False
    assert agent["attrs"]["narrationSeq"] == 2

    done = GibsonEvent.from_raw(
        {"type": "message_end", "content": "patched the exit code"}, sequence=3, timestamp_ms=300
    )
    model.apply_batch((done,), empty)
    agent = next(e for e in model.to_dict()["entities"] if e["id"] == "agent")
    assert agent["attrs"]["narration"] == "patched the exit code"
    assert agent["attrs"]["narrationComplete"] is True

    # a new message after completion starts fresh instead of appending,
    # and the buffer keeps only a bounded tail of very long messages
    flood = GibsonEvent.from_raw(
        {"type": "message_update", "text": "x" * 1000}, sequence=4, timestamp_ms=400
    )
    model.apply_batch((flood,), empty)
    agent = next(e for e in model.to_dict()["entities"] if e["id"] == "agent")
    assert agent["attrs"]["narration"] == "x" * 400
    assert agent["attrs"]["narrationComplete"] is False

    # message_end without text keeps the accumulated narration, sealed
    silent_end = GibsonEvent.from_raw({"type": "message_end"}, sequence=5, timestamp_ms=500)
    model.apply_batch((silent_end,), empty)
    agent = next(e for e in model.to_dict()["entities"] if e["id"] == "agent")
    assert agent["attrs"]["narration"] == "x" * 400
    assert agent["attrs"]["narrationComplete"] is True

    # streamed tool-call argument deltas are not the agent's voice
    tool_delta = GibsonEvent.from_raw(
        {"type": "message_update", "assistantMessageEvent": {
            "contentIndex": 1, "delta": '{"command": "uv run',
            "partial": {"content": [{"thinking": "planning"}, {"type": "toolCall", "name": "bash"}]},
        }},
        sequence=6,
        timestamp_ms=600,
    )
    untyped_tool_delta = GibsonEvent.from_raw(
        {"type": "message_update", "assistantMessageEvent": {
            "contentIndex": 1, "delta": ' pytest"}',
            "partial": {"content": [{"thinking": "planning"}, {"name": "bash", "arguments": "{"}]},
        }},
        sequence=7,
        timestamp_ms=700,
    )
    thinking_delta = GibsonEvent.from_raw(
        {"type": "message_update", "assistantMessageEvent": {
            "contentIndex": 0, "delta": "running the tests now",
            "partial": {"content": [{"type": "thinking", "thinking": "running"}]},
        }},
        sequence=8,
        timestamp_ms=800,
    )
    model.apply_batch((tool_delta, untyped_tool_delta, thinking_delta), empty)
    agent = next(e for e in model.to_dict()["entities"] if e["id"] == "agent")
    assert agent["attrs"]["narration"] == "running the tests now"
    assert '{"command"' not in agent["attrs"]["narration"]


def test_perception_is_idempotent_across_repeated_batches(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    call, result, touched = _command_pair("uv run pytest tests", start_seq=1, path="tests/test_app.py")
    model.apply_batch((call, result), touched)
    first_revision = model.revision
    first = model.to_dict()
    model.apply_batch((call, result), touched)
    second = model.to_dict()
    assert model.revision == first_revision
    assert first["counts"] == second["counts"]
    by_id = {entity["id"]: entity for entity in second["entities"]}
    assert by_id["file:tests/test_app.py"]["attrs"]["touchCount"] == 2


def test_git_snapshot_handles_subprocess_failures(monkeypatch, tmp_path: Path) -> None:
    def _boom(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(perception_module.subprocess, "run", _boom)
    snapshot = capture_git_snapshot(tmp_path)
    assert snapshot.available is False


def test_git_snapshot_filters_status_and_numstat_edge_cases(monkeypatch, tmp_path: Path) -> None:
    flood = [f"?? extra_{index:03d}.txt" for index in range(perception_module._MAX_UNTRACKED_FILES + 5)]
    outputs = {
        ("rev-parse", "--is-inside-work-tree"): "true\n",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main\n",
        ("log", "-1", "--format=%H%x00%s"): "abc123\x00subject\n",
        ("ls-files", "-z"): "src/app.py\x00.env\x00server.pem\x00",
        ("status", "--porcelain", "-z"): "\x00".join((
            " M src/app.py",
            "?? newdir/",
            "?? untracked.py",
            "?? secrets",
            "?? server.pem",
            "x",  # malformed short entry
            *flood,
            "",
        )),
        ("diff", "--numstat", "HEAD", "--"): "garbage-line\n1\t2\t.env\n-\t-\tassets/logo.png\n3\t1\tsrc/app.py\n",
    }

    def _fake_run_git(root: Path, *args: str) -> str | None:
        return outputs.get(args)

    monkeypatch.setattr(perception_module, "_run_git", _fake_run_git)
    snapshot = capture_git_snapshot(tmp_path)
    assert snapshot.available is True
    assert snapshot.tracked == ("src/app.py",)  # .env name and .pem suffix are sensitive
    assert snapshot.dirty == ("src/app.py",)
    assert "untracked.py" in snapshot.untracked  # dir entry and sensitive entries skipped
    assert not any("secret" in path or path.endswith(".pem") for path in snapshot.untracked)
    assert len(snapshot.untracked) == perception_module._MAX_UNTRACKED_FILES
    assert dict(snapshot.numstat) == {"src/app.py": (3, 1)}  # binary and sensitive skipped


def test_git_snapshot_normalizes_paths_for_subdirectory_roots(tmp_path: Path) -> None:
    repo = tmp_path / "outer"
    workspace = repo / "examples" / "workspace"
    workspace.mkdir(parents=True)
    (repo / "top.py").write_text("top = 1\n", encoding="utf-8")
    (workspace / "inner.py").write_text("inner = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    (workspace / "inner.py").write_text("inner = 2\nmore = 3\n", encoding="utf-8")
    (repo / "top.py").write_text("top = 2\n", encoding="utf-8")
    (workspace / "fresh.py").write_text("fresh = 1\n", encoding="utf-8")

    snapshot = capture_git_snapshot(workspace)
    assert snapshot.available is True
    assert snapshot.tracked == ("inner.py",)
    # repo-root-relative status/diff paths are reduced to the workspace and
    # paths outside the workspace are dropped
    assert snapshot.dirty == ("inner.py",)
    assert snapshot.untracked == ("fresh.py",)
    assert dict(snapshot.numstat) == {"inner.py": (2, 1)}


def test_git_snapshot_skips_diff_before_first_commit(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    snapshot = capture_git_snapshot(root)
    assert snapshot.available is True
    assert snapshot.head_sha == ""
    assert dict(snapshot.numstat) == {}
    assert snapshot.untracked == ("a.py",)


def test_workspace_walk_is_bounded_and_resilient(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "walk"
    (root / "locked").mkdir(parents=True)
    (root / "a.py").write_text("a\n", encoding="utf-8")
    (root / "b.py").write_text("b\n", encoding="utf-8")
    (root / "dangling").symlink_to(root / "missing-target")

    original_iterdir = Path.iterdir

    def _iterdir(self):
        if self.name == "locked":
            raise OSError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    assert perception_module._walk_workspace(root, max_files=1) == ("a.py",)
    assert perception_module._walk_workspace(root, max_files=10) == ("a.py", "b.py")


def test_perception_handles_eventless_commands_and_runtime_errors(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))

    # A runtime error before any command exists: outcome with no command.
    runtime_error = GibsonEvent.from_raw(
        {"type": "runtime_error", "message": "boom"}, sequence=1, timestamp_ms=100
    )
    # A tool_result with no matching tool_call: the command is created late.
    lone_result = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "uv run pytest"}, "isError": True,
         "exitCode": 2},
        sequence=2,
        timestamp_ms=200,
    )
    # A later result for the same command text: a re-run, attributed to a fresh
    # command entity (the retry-after-failure loop).
    repeat_result = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "uv run pytest"}, "isError": False},
        sequence=3,
        timestamp_ms=300,
    )
    # A second outcome at the same sequence (tool_result after tool_execution_end):
    # the command is already settled and is not re-completed.
    duplicate_seq = GibsonEvent.from_raw(
        {"type": "tool_execution_end", "toolName": "bash", "input": {"command": "uv run pytest"}},
        sequence=2,
        timestamp_ms=200,
    )
    empty_touched = {"schema": "harn-gibson.touched-files.v1", "files": [], "count": 0, "truncated": False}
    model.apply_batch((runtime_error, lone_result, duplicate_seq, repeat_result), empty_touched)
    payload = model.to_dict()

    commands = {entity["id"]: entity for entity in payload["entities"] if entity["type"] == "command"}
    assert set(commands) == {"command:2", "command:3"}
    assert commands["command:2"]["attrs"]["status"] == "error"
    assert commands["command:3"]["attrs"]["status"] == "ok"
    checks = {entity["id"]: entity for entity in payload["entities"] if entity["type"] == "check"}
    assert set(checks) == {"check:test:2", "check:test:3"}
    assert checks["check:test:2"]["attrs"]["exitCode"] == 2
    assert checks["check:test:3"]["attrs"]["status"] == "ok"
    completed = [event for event in payload["events"] if event["kind"] == "command_completed"]
    assert len(completed) == 2


def test_perception_ignores_invisible_and_empty_touches(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "cat secrets/x"}, "isError": False},
        sequence=1,
        timestamp_ms=100,
    )
    touched = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {"path": "", "operation": "bash:after", "firstSequence": 1, "lastSequence": 1,
             "phases": ["after"], "sources": ["input.command"]},
            {"path": "secrets/topsecret.txt", "operation": "bash:after", "firstSequence": 1, "lastSequence": 1,
             "phases": ["after"], "sources": ["input.command"]},
            # tool OUTPUT text mis-extracted as a path by the upstream extractor
            {"path": "Successfully replaced 1 block(s) in src/app.py.", "operation": "edit:after",
             "firstSequence": 1, "lastSequence": 1, "phases": ["after"], "sources": ["output"]},
            {"path": "6fae274 seed linkjar: store works", "operation": "bash:after",
             "firstSequence": 1, "lastSequence": 1, "phases": ["after"], "sources": ["output"]},
        ],
        "count": 4,
        "truncated": False,
    }
    model.apply_batch((event,), touched)
    payload = model.to_dict()
    assert not any("secret" in entity["id"] for entity in payload["entities"])
    assert not any("Successfully" in entity["id"] or "seed linkjar" in entity["id"]
                   for entity in payload["entities"])
    assert not any(r["type"] == "focused_on" for r in payload["relations"])


def test_perception_keeps_primary_focus_for_multi_path_touches(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "cat src/app_pkg/*.py"}, "isError": False},
        sequence=1,
        timestamp_ms=100,
    )
    touched = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {"path": "src/app_pkg/app.py", "operation": "bash:after", "firstSequence": 1, "lastSequence": 1,
             "phases": ["after"], "sources": ["input.command"]},
            {"path": "src/app_pkg/util.py", "operation": "bash:after", "firstSequence": 1, "lastSequence": 1,
             "phases": ["after"], "sources": ["input.command"]},
        ],
        "count": 2,
        "truncated": False,
    }
    model.apply_batch((event,), touched)
    focused = [r for r in model.to_dict()["relations"] if r["type"] == "focused_on"]
    assert [(r["from"], r["to"]) for r in focused] == [("agent", "file:src/app_pkg/app.py")]


def test_perception_replaces_focus_when_attention_moves(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    first = _command_pair("cat src/app_pkg/app.py", start_seq=1, path="src/app_pkg/app.py")
    model.apply_batch(first[:2], first[2])
    second = _command_pair("cat tests/test_app.py", start_seq=4, path="tests/test_app.py")
    model.apply_batch(second[:2], second[2])
    focused = [r for r in model.to_dict()["relations"] if r["type"] == "focused_on"]
    assert [(r["from"], r["to"]) for r in focused] == [("agent", "file:tests/test_app.py")]


def test_perception_emits_payload_change_events_with_line_deltas(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    edit = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "edit",
            "input": {"filePath": "src/app_pkg/app.py", "addedLines": 3, "removedLines": 1},
        },
        sequence=1,
        timestamp_ms=100,
    )
    hidden_delta = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "edit",
            "input": {"filePath": "secrets/topsecret.txt", "addedLines": 2, "removedLines": 0},
        },
        sequence=2,
        timestamp_ms=200,
    )
    zero_delta = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "edit",
            "input": {"filePath": "README.md", "addedLines": 0, "removedLines": 0},
        },
        sequence=3,
        timestamp_ms=300,
    )
    touched = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {"path": "src/app_pkg/app.py", "operation": "edit:after", "firstSequence": 1, "lastSequence": 1,
             "phases": ["after"], "sources": ["input.filePath"]},
            {"path": "secrets/topsecret.txt", "operation": "edit:after", "firstSequence": 2, "lastSequence": 2,
             "phases": ["after"], "sources": ["input.filePath"]},
            {"path": "README.md", "operation": "edit:after", "firstSequence": 3, "lastSequence": 3,
             "phases": ["after"], "sources": ["input.filePath"]},
        ],
        "count": 3,
        "truncated": False,
    }
    model.apply_batch((edit, hidden_delta, zero_delta), touched)
    events = [event for event in model.to_dict()["events"] if event.get("basis") == "harn-payload"]
    assert [event["entity"] for event in events] == ["file:src/app_pkg/app.py", "file:README.md"]
    assert events[0]["addedLines"] == 3
    assert events[0]["removedLines"] == 1
    assert events[0]["churnFraction"] == 1.0
    assert "addedLines" not in events[1]  # change observed, magnitude unknown


def test_diff_preview_rides_payload_change_events(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    edit = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "edit",
            "input": {
                "path": "src/app_pkg/app.py",
                "edits": [{"oldText": "        return 2\n", "newText": "        return 0\n"}],
                "addedLines": 1,
                "removedLines": 1,
            },
        },
        sequence=1,
        timestamp_ms=100,
    )
    touched = _touched("src/app_pkg/app.py", 1)
    model.apply_batch((edit,), touched)
    changed = [e for e in model.to_dict()["events"] if e["kind"] == "file_changed"]
    assert changed
    assert changed[0]["diffPreview"] == ["-        return 2", "+        return 0"]

    # write tools preview the head of the written content
    write = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "write",
            "input": {"path": "src/app_pkg/new.py", "content": "x = 1\ny = 2\n" + "z = 3\n" * 20,
                      "addedLines": 22, "removedLines": 0},
        },
        sequence=2,
        timestamp_ms=200,
    )
    model.apply_batch((write,), _touched("src/app_pkg/new.py", 2))
    changed = [e for e in model.to_dict()["events"]
               if e["kind"] == "file_changed" and e["entity"].endswith("new.py")]
    assert changed[0]["diffPreview"][0] == "+x = 1"
    assert len(changed[0]["diffPreview"]) <= 12


def test_diff_preview_caps_total_lines_and_skips_malformed_edits() -> None:
    from harn_gibson.perception import _diff_preview_from_payload

    block = "\n".join(f"line {i}" for i in range(6))
    preview = _diff_preview_from_payload({
        "toolName": "edit",
        "input": {"path": "x.py", "edits": [
            {"oldText": block, "newText": block},
            {"oldText": block, "newText": block},
        ]},
    })
    assert len(preview) == 12  # hard cap across all edits
    skipped = _diff_preview_from_payload({
        "toolName": "edit",
        "input": {"path": "x.py", "edits": ["not-a-mapping", {"newText": "y = 1\n"}]},
    })
    assert skipped == ["+y = 1"]
    assert _diff_preview_from_payload({"toolName": "edit", "input": "junk"}) == []


def test_tool_call_delta_detection_tolerates_malformed_content() -> None:
    from harn_gibson.perception import _is_tool_call_delta

    assert _is_tool_call_delta({"contentIndex": 0, "partial": {"content": ["not-a-mapping"]}}) is False
    assert _is_tool_call_delta({"contentIndex": 5, "partial": {"content": []}}) is False
    assert _is_tool_call_delta({}) is False


def test_perception_with_git_disabled_uses_filesystem_basis(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root), git_enabled=False)
    call, result, touched = _command_pair("cat README.md", start_seq=1, path="README.md")
    model.apply_batch((call, result), touched)
    payload = model.to_dict()
    assert payload["workspace"]["git"]["available"] is False
    assert payload["workspace"]["basis"] == "filesystem"


def test_perception_marks_deleted_and_phantom_files(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    first = _command_pair("cat src/app_pkg/util.py", start_seq=1, path="src/app_pkg/util.py")
    model.apply_batch(first[:2], first[2])

    (root / "src" / "app_pkg" / "util.py").unlink()
    _git(root, "add", "-A")
    # touch a path that never existed on disk: stat fails, exists flips off
    second = _command_pair("cat ghost.py", start_seq=4, path="ghost.py")
    model.apply_batch(second[:2], second[2])
    payload = model.to_dict()

    by_id = {entity["id"]: entity for entity in payload["entities"]}
    assert by_id["file:src/app_pkg/util.py"]["attrs"]["exists"] is False
    assert by_id["file:ghost.py"]["attrs"]["exists"] is False


def test_perception_respects_stat_budget(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root), max_stat_files=1)
    call, result, touched = _command_pair("uv run pytest tests", start_seq=1, path="tests/test_app.py")
    model.apply_batch((call, result), touched)
    payload = model.to_dict()
    sized = [
        entity for entity in payload["entities"]
        if entity["type"] == "file" and "sizeBytes" in entity["attrs"]
    ]
    assert len(sized) == 1


def test_perception_commit_without_checkpoint_command(monkeypatch, tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root))
    first = _command_pair("cat README.md", start_seq=1, path="README.md")
    model.apply_batch(first[:2], first[2])

    (root / "README.md").write_text("# changed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "external change")

    original_run_git = perception_module._run_git

    def _no_diff_tree(root_arg: Path, *args: str) -> str | None:
        if args and args[0] == "diff-tree":
            return None
        return original_run_git(root_arg, *args)

    monkeypatch.setattr(perception_module, "_run_git", _no_diff_tree)
    second = _command_pair("cat README.md", start_seq=4, path="README.md")
    model.apply_batch(second[:2], second[2])
    payload = model.to_dict()

    commits = [entity for entity in payload["entities"] if entity["type"] == "commit"]
    assert len(commits) == 1
    assert "filesChanged" not in commits[0]["attrs"]
    produced = [r for r in payload["relations"] if r["type"] == "produced" and r["to"].startswith("commit:")]
    assert produced == []


def test_perception_trims_event_backlog(tmp_path: Path) -> None:
    root = _git_fixture(tmp_path)
    model = PerceptionModel(project_root=str(root), max_events=1)
    for index in range(6):
        start = index * 3 + 1
        batch = _command_pair(f"uv run pytest tests -k case{index}", start_seq=start, path="tests/test_app.py")
        model.apply_batch(batch[:2], batch[2])
    payload = model.to_dict()
    assert len(payload["events"]) == 1
    assert payload["truncation"]["events"] is True
    assert payload["counts"]["events"] <= 4


def test_perception_drops_relations_to_truncated_files(tmp_path: Path) -> None:
    root = tmp_path / "many"
    (root / "pkg").mkdir(parents=True)
    paths = [f"pkg/mod_{index:02d}.py" for index in range(12)]
    for path in paths:
        (root / path).write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")

    model = PerceptionModel(project_root=str(root))
    events = []
    for index, path in enumerate(paths):
        events.append(GibsonEvent.from_raw(
            {"type": "tool_result", "toolName": "bash", "input": {"command": f"cat {path}"}, "isError": False},
            sequence=index + 1,
            timestamp_ms=(index + 1) * 100,
        ))
    touched = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {"path": path, "operation": "bash:after", "firstSequence": index + 1, "lastSequence": index + 1,
             "phases": ["after"], "sources": ["input.command"]}
            for index, path in enumerate(paths)
        ],
        "count": len(paths),
        "truncated": False,
    }
    model.apply_batch(tuple(events), touched)
    payload = model.to_dict(max_entities=8)

    visible_files = {entity["id"] for entity in payload["entities"] if entity["type"] == "file"}
    assert len(visible_files) == 8
    for relation in payload["relations"]:
        if relation["to"].startswith("file:"):
            assert relation["to"] in visible_files


def test_renderer_context_includes_perception_model(tmp_path: Path) -> None:
    from harn_gibson.catalog import default_visual_catalog
    from harn_gibson.rendering import (
        RendererContextBuilder,
        RendererContextConfig,
        RenderInputBatch,
        RenderRequest,
    )
    from harn_gibson.scene import SceneEngine

    root = _git_fixture(tmp_path)
    event = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "input": {"command": "uv run pytest tests"},
         "isError": False},
        sequence=1,
        timestamp_ms=100,
    )
    batch = RenderInputBatch.from_requests((RenderRequest(event),))
    builder = RendererContextBuilder(RendererContextConfig(project_root=str(root)))
    builder.observe_batch(batch)
    context = builder.build(batch, SceneEngine().state, default_visual_catalog())

    perception = context.project["perceptionModel"]
    assert perception["schema"] == PERCEPTION_MODEL_SCHEMA
    assert perception["workspace"]["git"]["available"] is True
    assert context.project["schemas"]["perceptionModel"] == PERCEPTION_MODEL_SCHEMA
    # the semantic graph is no longer default context
    assert context.project["semanticGraph"]["available"] is False
    assert context.project["semanticGraph"]["enabled"] is False


def test_renderer_prompt_metadata_counts_perception_entities() -> None:
    from harn_gibson.renderer_prompt import _context_metadata

    context = {
        "project": {
            "perceptionModel": {
                "entities": [{"id": "agent"}, {"id": "file:a.py"}],
                "counts": {"events": 3},
            },
        },
        "renderInput": {"requests": []},
    }
    metadata = _context_metadata(context)
    assert metadata["perceptionEntityCount"] == 2
    assert metadata["perceptionEventCount"] == 3
