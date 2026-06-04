from __future__ import annotations

from harn_gibson.events import GibsonEvent
from harn_gibson.world_model import (
    WORLD_MODEL_SCHEMA,
    WorldModel,
    changes_from_event,
    command_from_event,
    outcome_from_event,
)


def test_world_model_tracks_file_activity_outcomes_and_provenance() -> None:
    model = WorldModel(max_recent_outcomes=2)
    call = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "bash",
            "input": {"command": "pytest tests/test_world_model.py"},
        },
        1,
        timestamp_ms=100,
    )
    result = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "bash",
            "isError": False,
            "input": {"command": "pytest tests/test_world_model.py"},
            "content": [{"text": "ok"}],
        },
        2,
        timestamp_ms=200,
    )
    runtime_error = GibsonEvent.from_raw(
        {"type": "runtime_error", "message": "boom"},
        3,
        timestamp_ms=300,
    )
    harn_exit = GibsonEvent.from_raw(
        {"type": "harn_exit", "exitCode": 7, "message": "failed"},
        4,
        timestamp_ms=400,
    )
    touched_files = {
        "schema": "harn-gibson.touched-files.v1",
        "files": [
            {
                "path": "tests/test_world_model.py",
                "operation": "bash:before",
                "firstSequence": 1,
                "lastSequence": 2,
                "phases": ["before", "after"],
                "sources": ["input.command"],
            },
            {"path": "bad.py", "firstSequence": "bad", "lastSequence": 4},
            "ignored",
        ],
        "count": 2,
        "truncated": False,
    }

    model.apply_batch((call, result, runtime_error, harn_exit), touched_files)
    model.apply_batch((call,), touched_files)
    payload = model.to_dict(max_entities=1)

    assert payload["schema"] == WORLD_MODEL_SCHEMA
    assert payload["revision"] == 1
    assert payload["entityCount"] == 2
    assert payload["counts"] == {"files": 1, "commands": 1, "changes": 0}
    assert payload["truncated"] is False
    assert payload["provenance"] == {
        "source": "observed",
        "confidence": 1.0,
        "notes": [
            "Derived from normalized harn events and touched-file batches.",
            "Semantic graph and agent intent are not yet modeled.",
        ],
    }
    file_entity = payload["entities"]["files"][0]
    assert file_entity["id"] == "file:tests/test_world_model.py"
    assert file_entity["kind"] == "file"
    assert file_entity["activityCount"] == 2
    assert file_entity["firstSequence"] == 1
    assert file_entity["lastSequence"] == 2
    assert file_entity["phases"] == ["before", "after"]
    assert file_entity["operations"] == ["bash:before"]
    assert file_entity["sources"] == ["input.command"]
    assert file_entity["lastOutcome"] == {
        "status": "ok",
        "eventSequence": 2,
        "eventType": "tool_result",
        "toolName": "bash",
    }
    assert file_entity["provenance"] == {
        "source": "observed",
        "confidence": 1.0,
        "lastConfirmedSequence": 2,
        "lastConfirmedMs": 200,
    }
    assert [item["status"] for item in payload["recentOutcomes"]] == ["error", "error"]
    assert payload["recentOutcomes"][0]["eventType"] == "runtime_error"
    assert payload["recentOutcomes"][1]["eventType"] == "harn_exit"
    assert payload["recentOutcomes"][1]["exitCode"] == 7
    command_entity = payload["entities"]["commands"][0]
    assert command_entity["id"] == "command:1"
    assert command_entity["kind"] == "command"
    assert command_entity["toolName"] == "bash"
    assert command_entity["commandPreview"] == "pytest tests/test_world_model.py"
    assert command_entity["commandSource"] == "input.command"
    assert command_entity["status"] == "ok"
    assert command_entity["startedSequence"] == 1
    assert command_entity["completedSequence"] == 2
    assert command_entity["durationMs"] == 100
    assert command_entity["eventTypes"] == ["tool_call", "tool_result"]
    assert command_entity["phases"] == ["before", "after"]
    assert command_entity["sources"] == ["input.command"]
    assert command_entity["touchedPaths"] == ["tests/test_world_model.py"]
    assert command_entity["touchedPathCount"] == 1
    assert command_entity["touchedPathsTruncated"] is False
    assert command_entity["lastOutcome"]["status"] == "ok"
    assert command_entity["provenance"]["source"] == "observed"
    assert model.to_dict(max_entities=0)["truncated"] is True


def test_world_model_handles_empty_or_malformed_touched_files_and_outcome_shapes() -> None:
    model = WorldModel()
    ok_exit = GibsonEvent.from_raw({"type": "harn_exit", "returnCode": 0}, 1, timestamp_ms=10)
    failed_tool = GibsonEvent.from_raw(
        {"type": "tool_execution_end", "toolName": "write", "isError": True},
        2,
        timestamp_ms=20,
    )
    no_outcome = GibsonEvent.from_raw({"type": "message_update"}, 3, timestamp_ms=30)

    model.apply_batch((ok_exit, failed_tool, no_outcome), {"files": {"bad": True}})
    payload = model.to_dict()

    assert payload["entityCount"] == 0
    assert [item["status"] for item in payload["recentOutcomes"]] == ["ok", "error"]
    assert outcome_from_event(no_outcome) is None


def test_world_model_tracks_command_entities_and_command_extraction() -> None:
    model = WorldModel()
    touched_files = {
        "files": [
            *[
                {
                    "path": f"src/file_{index}.py",
                    "firstSequence": 1,
                    "lastSequence": 2,
                    "sources": ["input.command"],
                }
                for index in range(13)
            ],
            {"path": "README.md", "firstSequence": 3, "lastSequence": 3, "sources": ["input.command"]},
        ]
    }
    paired_call = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "bash", "input": {"command": "python -m pytest"}},
        1,
        timestamp_ms=100,
    )
    paired_result = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "bash", "isError": True, "input": {"command": "python -m pytest"}},
        2,
        timestamp_ms=340,
    )
    result_only = GibsonEvent.from_raw(
        {"type": "tool_execution_end", "toolName": "bash", "input": {"command": "git status --short"}},
        3,
        timestamp_ms=450,
    )
    running = GibsonEvent.from_raw({"type": "user_bash", "command": "ls docs"}, 4, timestamp_ms=460)
    nested = GibsonEvent.from_raw({"type": "tool_call", "args": [{"shellCommand": "make test"}]}, 5)
    empty_command = GibsonEvent.from_raw({"type": "tool_call", "input": {"command": ""}}, 6)

    model.apply_batch((paired_call, paired_result, result_only, running, nested, empty_command), touched_files)
    payload = model.to_dict(max_entities=2)
    commands = {item["id"]: item for item in payload["entities"]["commands"]}

    assert payload["counts"] == {"files": 14, "commands": 4, "changes": 0}
    assert payload["entityCount"] == 18
    assert payload["truncated"] is True
    assert list(commands) == ["command:5", "command:4"]

    full_commands = {item["id"]: item for item in model.to_dict(max_entities=20)["entities"]["commands"]}
    paired = full_commands["command:1"]
    assert paired["status"] == "error"
    assert paired["durationMs"] == 240
    assert paired["completedSequence"] == 2
    assert paired["lastOutcome"] == {
        "status": "error",
        "eventSequence": 2,
        "eventType": "tool_result",
        "toolName": "bash",
    }
    assert paired["touchedPathCount"] == 13
    assert paired["touchedPathsTruncated"] is True
    assert paired["touchedPaths"] == [f"src/file_{index}.py" for index in range(12)]

    result = full_commands["command:3"]
    assert result["status"] == "ok"
    assert result["completedSequence"] == 3
    assert "startedSequence" not in result
    assert "durationMs" not in result
    assert result["touchedPaths"] == ["README.md"]

    assert full_commands["command:4"]["status"] == "running"
    assert "completedSequence" not in full_commands["command:4"]
    assert "lastOutcome" not in full_commands["command:4"]

    nested_observation = command_from_event(nested)
    assert nested_observation is not None
    assert nested_observation.tool_name == "tool_call"
    assert nested_observation.command == "make test"
    assert nested_observation.source == "args.0.shellCommand"
    assert command_from_event(empty_command) is None


def test_world_model_tracks_structured_change_facts() -> None:
    model = WorldModel()
    edit_call = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "edit",
            "input": {
                "filePath": "src/app.py",
                "old_string": "one\ntwo\n",
                "new_string": "one\n2\nthree\n",
                "startLine": 4,
                "endLine": 5,
            },
        },
        1,
        timestamp_ms=100,
    )
    write_call = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "write", "input": {"filePath": "src/new.py", "content": "a\nb\n"}},
        2,
        timestamp_ms=200,
    )
    patch_result = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "patch",
            "isError": True,
            "filePath": "src/app.py",
            "patch": "--- a/src/app.py\n+++ b/src/app.py\n context\n-old\n+new\n+two",
        },
        3,
        timestamp_ms=300,
    )
    explicit_stats = GibsonEvent.from_raw(
        {
            "type": "tool_result",
            "toolName": "edit",
            "input": {"filePath": "src/stats.py", "addedLines": 3, "removedLines": 1, "lineStart": 10, "lineEnd": 12},
        },
        4,
        timestamp_ms=400,
    )
    nested_write = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "write", "args": [{"path": "docs/nested.md", "text": "title\nbody"}]},
        5,
        timestamp_ms=500,
    )
    no_delta = GibsonEvent.from_raw({"type": "tool_call", "toolName": "edit", "input": {"filePath": "src/noop.py"}}, 6)
    touched_files = {
        "files": [
            {"path": "src/app.py", "operation": "edit:before", "firstSequence": 1, "lastSequence": 1},
            {"path": "src/new.py", "operation": "write:before", "firstSequence": 2, "lastSequence": 2},
            {"path": "src/app.py", "operation": "patch:after", "firstSequence": 3, "lastSequence": 3},
            {"path": "src/stats.py", "operation": "edit:after", "firstSequence": 4, "lastSequence": 4},
            {"path": "docs/nested.md", "operation": "write:before", "firstSequence": 5, "lastSequence": 5},
            {"path": "src/noop.py", "operation": "edit:before", "firstSequence": 6, "lastSequence": 6},
        ]
    }

    model.apply_batch((edit_call, write_call, patch_result, explicit_stats, nested_write, no_delta), touched_files)
    payload = model.to_dict(max_entities=10)
    changes = {item["id"]: item for item in payload["entities"]["changes"]}

    assert payload["counts"] == {"files": 5, "commands": 0, "changes": 5}
    assert payload["entityCount"] == 10
    assert changes["change:1:0"] == {
        "id": "change:1:0",
        "kind": "change",
        "path": "src/app.py",
        "operation": "edit",
        "status": "planned",
        "eventSequence": 1,
        "timestampMs": 100,
        "eventType": "tool_call",
        "phase": "before",
        "source": "input.old_string/input.new_string",
        "toolName": "edit",
        "addedLines": 3,
        "removedLines": 2,
        "magnitudeLines": 5,
        "startLine": 4,
        "endLine": 5,
        "provenance": {
            "source": "observed",
            "confidence": 1.0,
            "lastConfirmedSequence": 1,
            "lastConfirmedMs": 100,
        },
    }
    assert changes["change:2:0"]["operation"] == "write"
    assert changes["change:2:0"]["source"] == "input.content"
    assert changes["change:2:0"]["addedLines"] == 2
    assert changes["change:2:0"]["removedLines"] == 0
    assert changes["change:3:0"]["operation"] == "patch"
    assert changes["change:3:0"]["status"] == "error"
    assert changes["change:3:0"]["lastOutcome"]["status"] == "error"
    assert changes["change:3:0"]["addedLines"] == 2
    assert changes["change:3:0"]["removedLines"] == 1
    assert changes["change:4:0"]["source"] == "input.lineCounts"
    assert changes["change:4:0"]["status"] == "ok"
    assert changes["change:4:0"]["startLine"] == 10
    assert changes["change:4:0"]["endLine"] == 12
    assert changes["change:5:0"]["path"] == "docs/nested.md"
    assert changes["change:5:0"]["source"] == "args.0.text"
    assert changes_from_event(no_delta, ({"path": "src/noop.py"},), None) == ()


def test_world_model_change_facts_handle_optional_shapes() -> None:
    model = WorldModel()
    custom = GibsonEvent.from_raw(
        {"type": "tool_call", "input": {"filePath": "src/custom.py", "old": "a", "new": "b"}},
        1,
    )
    fallback = GibsonEvent.from_raw(
        {"type": "tool_call", "input": {"filePath": "src/fallback.py", "old": "a", "new": "b"}},
        2,
    )
    explicit_counts = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "edit", "input": {"filePath": "src/counts.py", "addedLines": 1}},
        3,
    )
    empty_write = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "write", "input": {"filePath": "src/empty.py", "content": ""}},
        4,
    )
    list_write = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "write", "args": [{"meta": {}}, {"wrapper": {"text": "later"}}]},
        5,
    )
    nested_replace = GibsonEvent.from_raw(
        {
            "type": "tool_call",
            "toolName": "edit",
            "input": {"edits": [{"filePath": "src/nested.py", "old": "x", "new": "y"}]},
        },
        6,
    )
    touched_files = {
        "files": [
            {"path": "src/custom.py", "operation": "custom:before", "firstSequence": 1, "lastSequence": 1},
            {"path": "src/fallback.py", "firstSequence": 2, "lastSequence": 2},
            {"path": "src/counts.py", "operation": "edit:after", "firstSequence": 3, "lastSequence": 3},
            {"path": "src/empty.py", "operation": "write:before", "firstSequence": 4, "lastSequence": 4},
            {"path": "src/list.py", "operation": "write:before", "firstSequence": 5, "lastSequence": 5},
            {"path": "src/nested.py", "operation": "edit:before", "firstSequence": 6, "lastSequence": 6},
        ]
    }

    model.apply_batch((custom, fallback, explicit_counts, empty_write, list_write, nested_replace), touched_files)
    changes = {item["id"]: item for item in model.to_dict(max_entities=10)["entities"]["changes"]}

    assert "toolName" not in changes["change:1:0"]
    assert changes["change:1:0"]["operation"] == "custom"
    assert changes["change:2:0"]["operation"] == "tool_call"
    assert changes["change:3:0"]["addedLines"] == 1
    assert changes["change:3:0"]["removedLines"] == 0
    assert "startLine" not in changes["change:3:0"]
    assert "endLine" not in changes["change:3:0"]
    assert changes["change:4:0"]["magnitudeLines"] == 0
    assert changes["change:5:0"]["source"] == "args.1.wrapper.text"
    assert changes["change:6:0"]["source"] == "input.edits.0.old/input.edits.0.new"

    no_op_diff = GibsonEvent.from_raw(
        {"type": "tool_result", "toolName": "patch", "patch": "--- a\n+++ b\n context"},
        7,
    )
    empty_write_args = GibsonEvent.from_raw(
        {"type": "tool_call", "toolName": "write", "args": [{"meta": {}}]},
        8,
    )
    assert changes_from_event(no_op_diff, ({"path": "src/noop.py"},), outcome_from_event(no_op_diff)) == ()
    assert changes_from_event(empty_write_args, ({"path": "src/noop.py"},), None) == ()


def test_world_model_clips_long_command_previews() -> None:
    model = WorldModel()
    command = "x" * 260
    model.apply_batch(
        (GibsonEvent.from_raw({"type": "tool_call", "toolName": "bash", "input": {"command": command}}, 1),),
        {"files": []},
    )

    preview = model.to_dict()["entities"]["commands"][0]["commandPreview"]
    assert preview == f"{'x' * 239}..."


def test_world_model_ignores_blank_paths_and_uses_event_operation_fallback() -> None:
    model = WorldModel()
    runtime_error = GibsonEvent.from_raw({"type": "runtime_error", "path": "README.md"}, 1, timestamp_ms=10)
    tool_call = GibsonEvent.from_raw({"type": "tool_call", "toolName": "bash", "path": "pyproject.toml"}, 2)

    model.apply_batch(
        (runtime_error, tool_call),
        {
            "files": [
                {"path": "", "firstSequence": 1, "lastSequence": 1},
                {"path": "README.md", "firstSequence": 1, "lastSequence": 1, "sources": ["path", 7]},
                {"path": "pyproject.toml", "firstSequence": 2, "lastSequence": 2},
            ]
        },
    )

    files = {item["path"]: item for item in model.to_dict()["entities"]["files"]}
    assert model.to_dict()["entityCount"] == 2
    assert files["README.md"]["operations"] == ["runtime_error:after"]
    assert files["README.md"]["sources"] == ["path"]
    assert files["README.md"]["lastOutcome"]["status"] == "error"
    assert files["pyproject.toml"]["operations"] == ["bash:before"]
