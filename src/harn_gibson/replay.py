"""Replay historical harn events, renderer plans, and scene mutations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.rendering import RenderPlan, RenderRequest, RenderStep, RenderSubmitResult
from harn_gibson.scene import SceneMutation, SceneState, mutation_from_mapping
from harn_gibson.server import GibsonServerState, event_from_payload, submit_event_to_renderer

ReplayStepKind = Literal["event", "raw_event", "render_plan", "mutations"]


@dataclass(frozen=True, slots=True)
class ReplayStepResult:
    index: int
    kind: ReplayStepKind
    scene_revision: int
    updates: int
    route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "sceneRevision": self.scene_revision,
            "updates": self.updates,
        }
        if self.route is not None:
            payload["route"] = self.route
        return payload


@dataclass(frozen=True, slots=True)
class ReplayResult:
    schema: str
    name: str
    steps: tuple[ReplayStepResult, ...]
    scene: SceneState
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.replay-result.v1",
            "replaySchema": self.schema,
            "name": self.name,
            "steps": [step.to_dict() for step in self.steps],
            "scene": self.scene.to_dict(),
            "metadata": self.metadata,
        }


def run_replay_file(path: str | Path, state: GibsonServerState | None = None) -> ReplayResult:
    return run_replay_data(load_replay_file(path), state)


def load_replay_file(path: str | Path) -> dict[str, Any]:
    replay_path = Path(path)
    payload = json.loads(replay_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("replay file must contain a JSON object")
    return payload


def run_replay_data(data: Mapping[str, Any], state: GibsonServerState | None = None) -> ReplayResult:
    replay_state = state or GibsonServerState()
    schema = str(data.get("schema") or "harn-gibson.replay.v1")
    name = str(data.get("name") or "unnamed replay")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise ValueError("replay must contain a steps list")

    results: list[ReplayStepResult] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise ValueError(f"replay step {index} must be an object")
        results.append(_run_step(index, step, replay_state))
    return ReplayResult(
        schema=schema,
        name=name,
        steps=tuple(results),
        scene=replay_state.scene.state,
        metadata=dict(data.get("metadata") or {}),
    )


def _run_step(index: int, step: Mapping[str, Any], state: GibsonServerState) -> ReplayStepResult:
    kind = step.get("type", step.get("kind"))
    if kind == "event":
        return _run_event_step(index, step, state)
    if kind == "raw_event":
        return _run_raw_event_step(index, step, state)
    if kind == "render_plan":
        return _run_render_plan_step(index, step, state)
    if kind == "mutations":
        return _run_mutations_step(index, step, state)
    raise ValueError(f"unsupported replay step type at index {index}: {kind}")


def _run_event_step(index: int, step: Mapping[str, Any], state: GibsonServerState) -> ReplayStepResult:
    payload = step.get("event")
    if not isinstance(payload, Mapping):
        raise ValueError(f"event replay step {index} must include event object")
    result = submit_event_to_renderer(dict(payload), state)
    return _step_result(index, "event", result, state)


def _run_raw_event_step(index: int, step: Mapping[str, Any], state: GibsonServerState) -> ReplayStepResult:
    raw = step.get("raw")
    if raw is None:
        raise ValueError(f"raw_event replay step {index} must include raw")
    sequence = int(step.get("sequence", index + 1))
    event = GibsonEvent.from_raw(
        raw,
        sequence,
        source=str(step.get("source", "replay")),
        timestamp_ms=_optional_int(step.get("timestampMs", step.get("timestamp_ms"))),
        recent_context=_string_tuple(step.get("recentContext", ())),
        visualization_context=_string_tuple(step.get("visualizationContext", ())),
    )
    payload = event.to_dict()
    decisions = step.get("decisions")
    if isinstance(decisions, list):
        payload["decisions"] = [decision for decision in decisions if isinstance(decision, dict)]
    result = submit_event_to_renderer(payload, state)
    return _step_result(index, "raw_event", result, state)


def _run_render_plan_step(index: int, step: Mapping[str, Any], state: GibsonServerState) -> ReplayStepResult:
    plan = render_plan_from_mapping(step)
    result = state.pipeline.apply_plan(plan)
    return _step_result(index, "render_plan", result, state)


def _run_mutations_step(index: int, step: Mapping[str, Any], state: GibsonServerState) -> ReplayStepResult:
    mutations = mutations_from_value(step.get("mutations"), index)
    event_payload = step.get("event")
    if isinstance(event_payload, Mapping):
        event = event_from_payload(dict(event_payload))
    else:
        event = diagnostic_event(
            index + 1,
            event_type="replay_mutations",
            source="replay",
            message=str(step.get("summary") or "manual scene mutations"),
            timestamp_ms=_optional_int(step.get("timestampMs", step.get("timestamp_ms"))),
        )
    request = RenderRequest(event, route="direct_scene", metadata={"replayStep": index})
    result = state.pipeline.apply_direct(
        request,
        mutations,
        metadata={"renderer": "replay", "stepType": "mutations"},
    )
    return _step_result(index, "mutations", result, state)


def render_plan_from_mapping(value: Mapping[str, Any]) -> RenderPlan:
    nested_plan = value.get("plan")
    plan_payload = nested_plan if isinstance(nested_plan, Mapping) else value
    requests_value = plan_payload.get("requests")
    if not isinstance(requests_value, list) or not requests_value:
        raise ValueError("render_plan replay step must include non-empty requests")
    steps_value = plan_payload.get("steps")
    if not isinstance(steps_value, list):
        raise ValueError("render_plan replay step must include steps list")
    requests = []
    for request in requests_value:
        if not isinstance(request, Mapping):
            raise ValueError("render request must be an object")
        requests.append(render_request_from_mapping(request))
    steps = []
    for step in steps_value:
        if not isinstance(step, Mapping):
            raise ValueError("render step must be an object")
        steps.append(render_step_from_mapping(step))
    return RenderPlan(requests=tuple(requests), steps=tuple(steps), metadata=dict(plan_payload.get("metadata") or {}))


def render_request_from_mapping(value: Mapping[str, Any]) -> RenderRequest:
    event_value = value.get("event")
    if not isinstance(event_value, Mapping):
        raise ValueError("render request must include event object")
    return RenderRequest(
        event=event_from_payload(dict(event_value)),
        decisions=tuple(item for item in value.get("decisions", ()) if isinstance(item, dict)),
        route=str(value.get("route") or "renderer_agent"),
        timeline_offset_ms=int(value.get("timelineOffsetMs", value.get("timeline_offset_ms", 0))),
        coalesced_count=int(value.get("coalescedCount", value.get("coalesced_count", 1))),
        metadata=dict(value.get("metadata") or {}),
    )


def render_step_from_mapping(value: Mapping[str, Any]) -> RenderStep:
    return RenderStep(
        mutations=mutations_from_value(value.get("mutations"), -1),
        delay_ms=int(value.get("delayMs", value.get("delay_ms", 0))),
        start_offset_ms=int(value.get("startOffsetMs", value.get("start_offset_ms", 0))),
        event_index=_optional_int(value.get("eventIndex", value.get("event_index"))),
    )


def mutations_from_value(value: Any, step_index: int) -> tuple[SceneMutation, ...]:
    if not isinstance(value, list):
        raise ValueError(f"replay step {step_index} mutations must be a list")
    mutations = []
    for mutation in value:
        if isinstance(mutation, SceneMutation):
            mutations.append(mutation)
        elif isinstance(mutation, Mapping):
            mutations.append(mutation_from_mapping(mutation))
        else:
            raise ValueError(f"replay step {step_index} mutation must be an object")
    return tuple(mutations)


def write_scene(path: str | Path, scene: SceneState) -> None:
    scene_path = Path(path)
    scene_path.parent.mkdir(parents=True, exist_ok=True)
    scene_path.write_text(json.dumps(scene.to_dict(), indent=2) + "\n", encoding="utf-8")


def write_replay_result(path: str | Path, result: ReplayResult) -> None:
    result_path = Path(path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


def _step_result(
    index: int,
    kind: ReplayStepKind,
    result: RenderSubmitResult,
    state: GibsonServerState,
) -> ReplayStepResult:
    route = None
    if result.updates:
        metadata = result.updates[-1].get("renderPlan", {}).get("metadata", {})
        route_payload = metadata.get("route") if isinstance(metadata, Mapping) else None
        if isinstance(route_payload, Mapping):
            route_value = route_payload.get("route")
            route = route_value if isinstance(route_value, str) else None
    return ReplayStepResult(
        index=index,
        kind=kind,
        scene_revision=result.scene_revision if result.scene_revision is not None else state.scene.state.revision,
        updates=len(result.updates),
        route=route,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes):
        return ()
    return tuple(str(item) for item in value)


__all__ = [
    "ReplayResult",
    "ReplayStepKind",
    "ReplayStepResult",
    "load_replay_file",
    "mutations_from_value",
    "render_plan_from_mapping",
    "render_request_from_mapping",
    "render_step_from_mapping",
    "run_replay_data",
    "run_replay_file",
    "write_replay_result",
    "write_scene",
]
