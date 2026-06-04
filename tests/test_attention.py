from __future__ import annotations

import pytest

from harn_gibson.attention import AGENT_ATTENTION_SCHEMA, agent_attention_from_context
from harn_gibson.events import GibsonEvent


def _event(sequence: int, payload: dict[str, object]) -> GibsonEvent:
    return GibsonEvent.from_raw(payload, sequence, timestamp_ms=sequence * 100)


def test_agent_attention_tracks_command_focus_health_and_objective() -> None:
    event = _event(
        7,
        {
            "type": "tool_call",
            "toolName": "bash",
            "input": {"command": "uv run pytest tests/test_cli.py src/app.py"},
        },
    )
    touched_files = {
        "files": [
            {"path": "tests/test_cli.py"},
            {"path": "src/app.py"},
            {"path": "src/app.py"},
        ]
    }
    world_model = {
        "counts": {"files": 2, "commands": 1, "health": 1},
        "entities": {
            "files": [{"path": "tests/test_cli.py"}, {"path": "src/app.py"}],
            "health": [
                {
                    "category": "test",
                    "status": "error",
                    "sourceCommandId": "command:7",
                    "touchedPaths": ["tests/test_cli.py"],
                    "provenance": {"source": "inferred", "confidence": 0.85},
                }
            ],
        },
    }

    attention = agent_attention_from_context((event,), touched_files, world_model)

    assert attention["schema"] == AGENT_ATTENTION_SCHEMA
    assert attention["action"]["kind"] == "verify"
    assert attention["action"]["label"] == "Verify current work"
    assert attention["action"]["eventType"] == "tool_call"
    assert attention["objective"] == {
        "text": "Verify current work: uv run pytest tests/test_cli.py src/app.py",
        "source": "command",
    }
    assert attention["focus"]["primaryPath"] == "tests/test_cli.py"
    assert attention["focus"]["paths"] == ["tests/test_cli.py", "src/app.py"]
    assert attention["focus"]["entities"][0] == {
        "id": "file:tests/test_cli.py",
        "kind": "file",
        "path": "tests/test_cli.py",
        "reason": "currentBatch",
    }
    assert attention["focus"]["truncated"] is False
    assert attention["healthFocus"]["category"] == "test"
    assert attention["healthFocus"]["status"] == "error"
    assert attention["healthFocus"]["provenance"] == {"source": "inferred", "confidence": 0.85}
    assert attention["signals"] == ["currentEvent", "touchedFiles", "worldModel"]
    assert attention["provenance"]["source"] == "inferred"
    assert attention["provenance"]["confidence"] == 0.86


@pytest.mark.parametrize(
    ("command", "kind", "label"),
    (
        ("python -m build", "build", "Build project artifacts"),
        ("sed -i 's/a/b/' src/app.py", "edit", "Edit focused files"),
        ("python apply_patch.py src/app.py", "edit", "Edit focused files"),
        ("cat src/app.py", "inspect", "Inspect project state"),
        ("git status --short", "inspect", "Inspect project state"),
        ("git add src/app.py && git commit -m test", "checkpoint", "Checkpoint repository state"),
        ("git diff -- src/app.py", "version_control", "Review repository state"),
        ("python scripts/run.py", "command", "Run shell command"),
    ),
)
def test_agent_attention_classifies_command_intent(command: str, kind: str, label: str) -> None:
    event = _event(3, {"type": "tool_call", "toolName": "bash", "input": [{"command": command}]})

    attention = agent_attention_from_context((event,), {"files": []}, {"counts": {"files": 0}})

    assert attention["action"]["kind"] == kind
    assert attention["action"]["label"] == label
    assert attention["objective"]["source"] == "command"


def test_agent_attention_handles_result_runtime_input_stream_lifecycle_and_fallback_events() -> None:
    cases = [
        (_event(1, {"type": "tool_result", "toolName": "bash", "isError": True}), "diagnose", "tool_result"),
        (_event(2, {"type": "tool_result", "toolName": "bash", "isError": False}), "observe_result", None),
        (_event(3, {"type": "runtime_error", "message": "boom"}), "diagnose", "runtime_error"),
        (_event(4, {"type": "input", "text": "please inspect src/app.py"}), "follow_user", "input.text"),
        (
            _event(5, {"type": "browser_input", "payload": {"message": "steer toward tests"}}),
            "follow_user",
            "input.text",
        ),
        (_event(6, {"type": "message_update", "assistantMessageEvent": {"delta": "hi"}}), "respond", None),
        (_event(7, {"type": "session_start"}), "coordinate", None),
        (_event(8, {"type": "before_agent_start"}), "operate", None),
    ]

    for event, kind, objective_source in cases:
        attention = agent_attention_from_context((event,), {"files": "bad"}, {"counts": {"files": False}})
        assert attention["action"]["kind"] == kind
        if objective_source is None:
            assert "objective" not in attention
        else:
            assert attention["objective"]["source"] == objective_source

    blank_input = _event(9, {"type": "input", "text": " ", "payload": {"message": 7}})
    sparse_input = _event(10, {"type": "input", "payload": "bad"})
    assert "objective" not in agent_attention_from_context((blank_input,), {}, {})
    assert "objective" not in agent_attention_from_context((sparse_input,), {}, {})


def test_agent_attention_uses_health_and_world_files_when_current_batch_has_no_paths() -> None:
    event = _event(9, {"type": "tool_result", "toolName": "bash", "isError": True})
    world_model = {
        "counts": {"files": 3},
        "entities": {
            "files": [
                {"path": "src/fallback.py"},
                {"path": ""},
                {"bad": True},
            ],
            "health": [
                "bad",
                {"status": "ok", "touchedPaths": ["src/healthy.py"]},
                {"status": "running", "touchedPaths": "bad"},
                {
                    "category": "build",
                    "status": "running",
                    "sourceCommandId": "command:9",
                    "touchedPaths": ["src/build.py", "src/fallback.py"],
                },
            ],
        },
    }

    attention = agent_attention_from_context((event,), {}, world_model, max_focus_paths=2)

    assert attention["focus"]["paths"] == ["src/build.py", "src/fallback.py"]
    assert attention["focus"]["entities"][0]["reason"] == "worldModel"
    assert attention["focus"]["truncated"] is False
    assert attention["healthFocus"] == {
        "category": "build",
        "status": "running",
        "sourceCommandId": "command:9",
        "provenance": {},
    }
    assert attention["signals"] == ["currentEvent", "worldModel"]


def test_agent_attention_handles_empty_and_malformed_context() -> None:
    long_text = "please " + "inspect " * 80
    input_event = _event(2, {"type": "input", "text": long_text})

    idle = agent_attention_from_context((), {}, "bad")  # type: ignore[arg-type]
    clipped = agent_attention_from_context((input_event,), {}, {"counts": {"files": 0}})
    zero_focus = agent_attention_from_context(
        (_event(3, {"type": "tool_call", "toolName": "bash", "input": {"command": "pytest"}}),),
        {"files": [{"path": "a.py"}]},
        {"entities": {"files": [{"path": "b.py"}]}},
        max_focus_paths=0,
    )

    assert idle["action"]["kind"] == "idle"
    assert "objective" not in idle
    assert idle["signals"] == []
    assert clipped["objective"]["text"].endswith("...")
    assert len(clipped["objective"]["text"]) <= 180
    assert zero_focus["focus"]["paths"] == []
    assert zero_focus["focus"]["truncated"] is True
