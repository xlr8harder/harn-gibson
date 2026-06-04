from __future__ import annotations

from harn_gibson.events import GibsonEvent
from harn_gibson.world_model import WORLD_MODEL_SCHEMA, WorldModel, outcome_from_event


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
    assert payload["entityCount"] == 1
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
