from __future__ import annotations

import json
from pathlib import Path

import pytest

from harn_gibson import ReplayResult, ReplayStepResult, run_replay_data, run_replay_file
from harn_gibson.events import GibsonEvent
from harn_gibson.replay import (
    load_replay_file,
    mutations_from_value,
    render_plan_from_mapping,
    render_request_from_mapping,
    render_step_from_mapping,
    write_replay_result,
    write_scene,
)
from harn_gibson.scene import SceneMutation
from harn_gibson.server import GibsonServerState


def event_payload(
    sequence: int = 1,
    event_type: str = "tool_call",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    raw = {"type": event_type, **dict(payload or {})}
    return GibsonEvent.from_raw(raw, sequence, source="unit", timestamp_ms=1000 + sequence).to_dict()


def test_replay_event_steps_file_io_and_writers(tmp_path: Path) -> None:
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harn-gibson.replay.v1",
                "name": "event replay",
                "metadata": {"fixture": True},
                "steps": [
                    {
                        "type": "event",
                        "event": event_payload(
                            1,
                            "tool_call",
                            {"toolName": "bash", "input": {"command": "pwd"}},
                        ),
                    },
                    {
                        "kind": "event",
                        "event": event_payload(
                            2,
                            "message_update",
                            {"assistantMessageEvent": {"delta": "loading"}},
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert load_replay_file(path)["name"] == "event replay"
    result = run_replay_file(path)

    assert isinstance(result, ReplayResult)
    assert result.schema == "harn-gibson.replay.v1"
    assert result.name == "event replay"
    assert result.metadata == {"fixture": True}
    assert result.steps[0].to_dict() == {
        "index": 0,
        "kind": "event",
        "sceneRevision": 1,
        "updates": 1,
    }
    assert result.steps[1].route == "stream_buffer"
    assert result.scene.primitives["assistant-stream"].props["text"] == "loading"
    assert result.to_dict()["steps"][1]["route"] == "stream_buffer"

    scene_path = tmp_path / "out" / "scene.json"
    result_path = tmp_path / "out" / "result.json"
    write_scene(scene_path, result.scene)
    write_replay_result(result_path, result)

    assert json.loads(scene_path.read_text(encoding="utf-8"))["revision"] == 2
    assert json.loads(result_path.read_text(encoding="utf-8"))["name"] == "event replay"


def test_replay_raw_events_render_plans_and_mutations() -> None:
    explicit_event = event_payload(12, "browser_input", {"id": "input-1", "message": "go"})
    state = GibsonServerState()

    result = run_replay_data(
        {
            "name": "mixed replay",
            "steps": [
                {
                    "type": "raw_event",
                    "raw": {"type": "input", "text": "raw input", "source": "replay"},
                    "sequence": 10,
                    "source": "raw-fixture",
                    "timestampMs": 2222,
                    "recentContext": ["user asked for status"],
                    "visualizationContext": "ignored",
                    "decisions": [{"block": False}, "not a decision"],
                },
                {
                    "type": "render_plan",
                    "plan": {
                        "requests": [
                            {
                                "event": event_payload(11, "tool_result", {"toolName": "bash"}),
                                "route": "direct_scene",
                                "timelineOffsetMs": 5,
                                "coalescedCount": 2,
                                "decisions": [{"reviewed": True}],
                                "metadata": {"source": "fixture"},
                            }
                        ],
                        "steps": [
                            {
                                "startOffsetMs": 5,
                                "eventIndex": 0,
                                "mutations": [
                                    {"op": "patch", "targetId": "status", "props": {"text": "saved plan"}},
                                    {"op": "append_log", "entry": {"sequence": 11, "eventType": "saved_plan"}},
                                ],
                            }
                        ],
                        "metadata": {"renderer": "saved", "route": {"route": "saved_plan"}},
                    },
                },
                {
                    "type": "mutations",
                    "event": explicit_event,
                    "mutations": [SceneMutation("patch", target_id="status", props={"text": "manual event"})],
                },
                {
                    "type": "mutations",
                    "summary": "manual diagnostic",
                    "timestamp_ms": 3333,
                    "mutations": [{"op": "append_log", "entry": {"eventType": "manual"}}],
                },
            ],
        },
        state,
    )

    assert [step.kind for step in result.steps] == ["raw_event", "render_plan", "mutations", "mutations"]
    assert result.steps[1].route == "saved_plan"
    assert result.steps[2].scene_revision == 3
    assert result.scene.revision == 4
    assert result.scene.primitives["status"].props["text"] == "manual event"
    assert result.scene.log[-1]["eventType"] == "manual"


def test_replay_raw_event_without_decisions_and_empty_plan() -> None:
    result = run_replay_data(
        {
            "steps": [
                {"type": "raw_event", "raw": {"type": "input", "text": "raw input"}},
                {
                    "type": "render_plan",
                    "requests": [{"event": event_payload(20, "tool_call", {"toolName": "bash"})}],
                    "steps": [],
                },
            ]
        }
    )

    assert result.steps[0].updates == 1
    assert result.steps[1].updates == 0
    assert result.steps[1].scene_revision == result.scene.revision


def test_render_plan_parsers_accept_direct_mappings() -> None:
    request_event = event_payload(7, "tool_result", {"toolName": "bash"})
    request = render_request_from_mapping(
        {
            "event": request_event,
            "route": "direct_scene",
            "timeline_offset_ms": 12,
            "coalesced_count": 3,
            "metadata": {"x": 1},
        }
    )
    step = render_step_from_mapping(
        {
            "delay_ms": 4,
            "start_offset_ms": 8,
            "event_index": 0,
            "mutations": [{"op": "append_log", "entry": {"ok": True}}],
        }
    )
    plan = render_plan_from_mapping(
        {
            "requests": [request.to_dict()],
            "steps": [step.to_dict()],
            "metadata": {"renderer": "direct-test"},
        }
    )

    assert request.timeline_offset_ms == 12
    assert request.coalesced_count == 3
    assert step.delay_ms == 4
    assert plan.metadata == {"renderer": "direct-test"}
    assert plan.steps[0].mutations[0].entry == {"ok": True}

    no_index_step = render_step_from_mapping({"mutations": []})
    assert no_index_step.event_index is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "replay file must contain a JSON object"),
    ],
)
def test_load_replay_file_validation(tmp_path: Path, payload: object, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_replay_file(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "replay must contain a steps list"),
        ({"steps": [{}]}, "unsupported replay step type"),
        ({"steps": ["bad"]}, "replay step 0 must be an object"),
        ({"steps": [{"type": "event"}]}, "event replay step 0 must include event object"),
        ({"steps": [{"type": "raw_event"}]}, "raw_event replay step 0 must include raw"),
        (
            {"steps": [{"type": "render_plan", "requests": [], "steps": []}]},
            "render_plan replay step must include non-empty requests",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": [{"event": event_payload()}]}]},
            "render_plan replay step must include steps list",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": ["bad"], "steps": []}]},
            "render request must be an object",
        ),
        (
            {"steps": [{"type": "render_plan", "requests": [{"event": event_payload()}], "steps": ["bad"]}]},
            "render step must be an object",
        ),
        (
            {"steps": [{"type": "mutations", "mutations": "bad"}]},
            "replay step 0 mutations must be a list",
        ),
        (
            {"steps": [{"type": "mutations", "mutations": ["bad"]}]},
            "replay step 0 mutation must be an object",
        ),
    ],
)
def test_run_replay_data_validation(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_replay_data(payload)


def test_parser_validation_helpers() -> None:
    with pytest.raises(ValueError, match="render request must include event object"):
        render_request_from_mapping({})
    with pytest.raises(ValueError, match="replay step -1 mutations must be a list"):
        render_step_from_mapping({"mutations": None})
    with pytest.raises(ValueError, match="replay step 3 mutation must be an object"):
        mutations_from_value([object()], 3)

    step = ReplayStepResult(1, "event", 5, 0)
    assert step.to_dict() == {"index": 1, "kind": "event", "sceneRevision": 5, "updates": 0}
