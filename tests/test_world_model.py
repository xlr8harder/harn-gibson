from __future__ import annotations

from harn_gibson.events import GibsonEvent
from harn_gibson.world_model import WORLD_MODEL_SCHEMA, WorldModel, command_from_event, outcome_from_event


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
    assert payload["counts"] == {"files": 1, "commands": 1}
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

    assert payload["counts"] == {"files": 14, "commands": 4}
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
