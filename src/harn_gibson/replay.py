"""Replay historical harn events, renderer plans, and scene mutations."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import unified_diff
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.rendering import RendererContext, RenderPlan, RenderRequest, RenderStep, RenderSubmitResult
from harn_gibson.scene import SceneMutation, SceneState, mutation_from_mapping, scene_state_from_mapping
from harn_gibson.server import GibsonServerState, event_from_payload, submit_event_to_renderer
from harn_gibson.styles import style_pack_from_name

ReplayStepKind = Literal["event", "raw_event", "render_plan", "mutations"]
ReplayExpectationOp = Literal["equals", "contains", "exists"]
MISSING = object()


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
class ReplayFrame:
    index: int
    step: ReplayStepResult
    scene: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "step": self.step.to_dict(),
            "scene": self.scene,
        }


@dataclass(frozen=True, slots=True)
class ReplayFrameScreenshot:
    index: int
    step: ReplayStepResult
    screenshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "step": self.step.to_dict(),
            "screenshot": self.screenshot,
        }


@dataclass(frozen=True, slots=True)
class ReplayRendererContext:
    index: int
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "context": self.context,
        }


@dataclass(frozen=True, slots=True)
class ReplayExpectationResult:
    path: str
    op: ReplayExpectationOp
    passed: bool
    expected: Any = None
    actual: Any = MISSING
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "op": self.op,
            "passed": self.passed,
        }
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not MISSING:
            payload["actual"] = self.actual
        if self.message:
            payload["message"] = self.message
        return payload


class ReplayExpectationError(AssertionError):
    def __init__(self, failures: tuple[ReplayExpectationResult, ...]) -> None:
        self.failures = failures
        details = "; ".join(failure.message for failure in failures)
        super().__init__(f"replay expectations failed: {details}")


@dataclass(frozen=True, slots=True)
class ReplayBaselineResult:
    path: str
    ok: bool
    updated: bool = False
    error: str = ""
    checked: tuple[str, ...] = ("scene",)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "ok": self.ok,
            "updated": self.updated,
            "checked": list(self.checked),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class ReplayFileResult:
    path: str
    ok: bool
    steps: int = 0
    scene_revision: int | None = None
    expectations: int = 0
    error: str = ""
    expectation_failures: tuple[ReplayExpectationResult, ...] = ()
    screenshot: dict[str, Any] | None = None
    baseline: ReplayBaselineResult | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "ok": self.ok,
            "steps": self.steps,
            "expectations": self.expectations,
        }
        if self.scene_revision is not None:
            payload["sceneRevision"] = self.scene_revision
        if self.error:
            payload["error"] = self.error
        if self.expectation_failures:
            payload["expectationFailures"] = [failure.to_dict() for failure in self.expectation_failures]
        if self.screenshot is not None:
            payload["screenshot"] = self.screenshot
        if self.baseline is not None:
            payload["baseline"] = self.baseline.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ReplaySuiteResult:
    root: str
    files: tuple[ReplayFileResult, ...]

    @property
    def total(self) -> int:
        return len(self.files)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.files if not result.ok)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.replay-suite-result.v1",
            "root": self.root,
            "ok": self.ok,
            "total": self.total,
            "failed": self.failed,
            "files": [result.to_dict() for result in self.files],
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    schema: str
    name: str
    steps: tuple[ReplayStepResult, ...]
    scene: SceneState
    metadata: dict[str, Any] = field(default_factory=dict)
    expectations: tuple[ReplayExpectationResult, ...] = ()
    frames: tuple[ReplayFrame, ...] = ()
    renderer_contexts: tuple[ReplayRendererContext, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": "harn-gibson.replay-result.v1",
            "replaySchema": self.schema,
            "name": self.name,
            "steps": [step.to_dict() for step in self.steps],
            "scene": self.scene.to_dict(),
            "metadata": self.metadata,
            "expectations": [expectation.to_dict() for expectation in self.expectations],
        }
        if self.frames:
            payload["frames"] = [frame.to_dict() for frame in self.frames]
        if self.renderer_contexts:
            payload["rendererContexts"] = [context.to_dict() for context in self.renderer_contexts]
        return payload


def run_replay_file(
    path: str | Path,
    state: GibsonServerState | None = None,
    *,
    capture_frames: bool = False,
    capture_renderer_contexts: bool = False,
) -> ReplayResult:
    return run_replay_data(
        load_replay_file(path),
        state,
        capture_frames=capture_frames,
        capture_renderer_contexts=capture_renderer_contexts,
    )


def run_replay_suite(
    path: str | Path,
    *,
    screenshot_dir: str | Path | None = None,
    screenshot_width: int = 1280,
    screenshot_height: int = 900,
    baseline_dir: str | Path | None = None,
    update_baselines: bool = False,
    style: str | None = None,
) -> ReplaySuiteResult:
    if update_baselines and baseline_dir is None:
        raise ValueError("update_baselines requires baseline_dir")
    root = Path(path)
    files = discover_replay_files(root)
    screenshot_root = Path(screenshot_dir) if screenshot_dir is not None else None
    baseline_root = Path(baseline_dir) if baseline_dir is not None else None
    style_pack = style_pack_from_name(style)
    results = []
    for replay_file in files:
        state = GibsonServerState(style_pack=style_pack)
        result: ReplayResult | None = None
        baseline = None
        try:
            result = run_replay_file(replay_file, state)
            screenshot = None
            if screenshot_root is not None:
                screenshot = _capture_suite_screenshot(
                    root,
                    replay_file,
                    screenshot_root,
                    state,
                    width=screenshot_width,
                    height=screenshot_height,
                )
            if baseline_root is not None:
                baseline_path = _suite_baseline_path(root, replay_file, baseline_root)
                if update_baselines:
                    write_replay_baseline(baseline_path, result)
                    baseline = ReplayBaselineResult(path=baseline_path.as_posix(), ok=True, updated=True)
                else:
                    baseline = compare_replay_baseline(baseline_path, result)
        except ReplayExpectationError as error:
            results.append(
                ReplayFileResult(
                    path=_suite_path(root, replay_file),
                    ok=False,
                    scene_revision=state.scene.state.revision,
                    error=str(error),
                    expectation_failures=error.failures,
                )
            )
        except Exception as error:
            results.append(
                ReplayFileResult(
                    path=_suite_path(root, replay_file),
                    ok=False,
                    steps=len(result.steps) if result is not None else 0,
                    scene_revision=result.scene.revision if result is not None else state.scene.state.revision,
                    expectations=len(result.expectations) if result is not None else 0,
                    error=str(error),
                    baseline=baseline,
                )
            )
        else:
            ok = baseline is None or baseline.ok
            results.append(
                ReplayFileResult(
                    path=_suite_path(root, replay_file),
                    ok=ok,
                    steps=len(result.steps),
                    scene_revision=result.scene.revision,
                    expectations=len(result.expectations),
                    error="" if ok else baseline.error,
                    screenshot=screenshot,
                    baseline=baseline,
                )
            )
        finally:
            state.pipeline.stop()
    return ReplaySuiteResult(root=str(root), files=tuple(results))


def discover_replay_files(path: str | Path) -> tuple[Path, ...]:
    root = Path(path)
    if root.is_file():
        return (root,)
    if not root.is_dir():
        raise FileNotFoundError(f"replay path not found: {root}")
    files = tuple(sorted(item for item in root.rglob("*.json") if item.is_file()))
    if not files:
        raise ValueError(f"no replay JSON files found under {root}")
    return files


def load_replay_file(path: str | Path) -> dict[str, Any]:
    replay_path = Path(path)
    payload = json.loads(replay_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("replay file must contain a JSON object")
    return payload


def replay_data_from_event_log(path: str | Path, *, name: str | None = None) -> dict[str, Any]:
    event_log_path = Path(path)
    steps: list[dict[str, Any]] = []
    with event_log_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            event = json.loads(stripped)
            if not isinstance(event, dict):
                raise ValueError(f"event log line {line_number} must contain a JSON object")
            steps.append({"type": "event", "event": event})
    return {
        "schema": "harn-gibson.replay.v1",
        "name": name if name is not None else f"event log: {event_log_path.name}",
        "metadata": {
            "sourceEventLog": event_log_path.as_posix(),
            "eventCount": len(steps),
        },
        "steps": steps,
    }


def replay_baseline_from_result(result: ReplayResult) -> dict[str, Any]:
    return {
        "schema": "harn-gibson.replay-baseline.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "scene": replay_baseline_scene(result.scene),
        "metadata": result.metadata,
    }


def write_replay_baseline(path: str | Path, result: ReplayResult) -> None:
    baseline_path = Path(path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(replay_baseline_from_result(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compare_replay_baseline(path: str | Path, result: ReplayResult) -> ReplayBaselineResult:
    baseline_path = Path(path)
    if not baseline_path.exists():
        return ReplayBaselineResult(path=baseline_path.as_posix(), ok=False, error=f"baseline missing: {baseline_path}")
    try:
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("baseline file must contain a JSON object")
        expected_scene = payload.get("scene")
        if not isinstance(expected_scene, Mapping):
            raise ValueError("baseline scene must be a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return ReplayBaselineResult(path=baseline_path.as_posix(), ok=False, error=f"baseline invalid: {error}")
    actual_scene = replay_baseline_scene(result.scene)
    if dict(expected_scene) != actual_scene:
        return ReplayBaselineResult(
            path=baseline_path.as_posix(),
            ok=False,
            error=_baseline_mismatch_error(dict(expected_scene), actual_scene),
        )
    return ReplayBaselineResult(path=baseline_path.as_posix(), ok=True)


def replay_baseline_scene(scene: SceneState) -> dict[str, Any]:
    payload = deepcopy(scene.to_dict())
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        _normalize_render_intent(metadata.get("lastRenderIntent"))
        render_intents = metadata.get("renderIntents")
        if isinstance(render_intents, list):
            for intent in render_intents:
                _normalize_render_intent(intent)
    return payload


def run_replay_data(
    data: Mapping[str, Any],
    state: GibsonServerState | None = None,
    *,
    capture_frames: bool = False,
    capture_renderer_contexts: bool = False,
) -> ReplayResult:
    replay_state = state or GibsonServerState()
    schema = str(data.get("schema") or "harn-gibson.replay.v1")
    name = str(data.get("name") or "unnamed replay")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise ValueError("replay must contain a steps list")

    results: list[ReplayStepResult] = []
    frames: list[ReplayFrame] = []
    renderer_contexts: list[ReplayRendererContext] = []
    previous_context_recorder = replay_state.pipeline.context_recorder

    def capture_context(context: RendererContext) -> None:
        if previous_context_recorder is not None:
            previous_context_recorder(context)
        renderer_contexts.append(ReplayRendererContext(len(renderer_contexts), deepcopy(context.to_dict())))

    if capture_renderer_contexts:
        replay_state.pipeline.context_recorder = capture_context
    try:
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                raise ValueError(f"replay step {index} must be an object")
            result = _run_step(index, step, replay_state)
            results.append(result)
            if capture_frames:
                frames.append(
                    ReplayFrame(
                        index=index,
                        step=result,
                        scene=deepcopy(replay_state.scene.state.to_dict()),
                    )
                )
    finally:
        if capture_renderer_contexts:
            replay_state.pipeline.context_recorder = previous_context_recorder
    expectations = evaluate_replay_expectations(replay_state.scene.state, data.get("expect"))
    failures = tuple(expectation for expectation in expectations if not expectation.passed)
    if failures:
        raise ReplayExpectationError(failures)
    return ReplayResult(
        schema=schema,
        name=name,
        steps=tuple(results),
        scene=replay_state.scene.state,
        metadata=dict(data.get("metadata") or {}),
        expectations=expectations,
        frames=tuple(frames),
        renderer_contexts=tuple(renderer_contexts),
    )


def evaluate_replay_expectations(scene: SceneState, value: Any) -> tuple[ReplayExpectationResult, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("replay expect must be an object")
    checks: list[ReplayExpectationResult] = []
    scene_payload = scene.to_dict()
    if "sceneRevision" in value:
        checks.append(_evaluate_expectation(scene_payload, "revision", "equals", value["sceneRevision"]))
    checks_value = value.get("checks", [])
    if not isinstance(checks_value, list):
        raise ValueError("replay expect checks must be a list")
    for index, check in enumerate(checks_value):
        if not isinstance(check, Mapping):
            raise ValueError(f"replay expect check {index} must be an object")
        checks.append(_expectation_from_mapping(scene_payload, check, index))
    return tuple(checks)


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


def _expectation_from_mapping(
    scene_payload: Mapping[str, Any],
    check: Mapping[str, Any],
    index: int,
) -> ReplayExpectationResult:
    path = check.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError(f"replay expect check {index} must include path")
    ops = [op for op in ("equals", "contains", "exists") if op in check]
    if len(ops) != 1:
        raise ValueError(f"replay expect check {index} must include exactly one operation")
    op = ops[0]
    return _evaluate_expectation(scene_payload, path, op, check[op])


def _evaluate_expectation(
    scene_payload: Mapping[str, Any],
    path: str,
    op: ReplayExpectationOp,
    expected: Any,
) -> ReplayExpectationResult:
    actual = _value_at_path(scene_payload, path)
    if op == "equals":
        passed = actual is not MISSING and actual == expected
    elif op == "contains":
        passed = actual is not MISSING and _contains(actual, expected)
    else:
        passed = (actual is not MISSING) is bool(expected)
    return ReplayExpectationResult(
        path=path,
        op=op,
        passed=passed,
        expected=expected,
        actual=actual,
        message="" if passed else _expectation_message(path, op, expected, actual),
    )


def _value_at_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return any(_matches(item, expected) for item in actual)
    if isinstance(actual, str) and isinstance(expected, str):
        return expected in actual
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return _mapping_contains(actual, expected)
    return False


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return _mapping_contains(actual, expected)
    return actual == expected


def _mapping_contains(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for key, expected_value in expected.items():
        if key not in actual or not _matches(actual[key], expected_value):
            return False
    return True


def _expectation_message(path: str, op: ReplayExpectationOp, expected: Any, actual: Any) -> str:
    actual_text = "<missing>" if actual is MISSING else repr(actual)
    return f"{path} expected to {op} {expected!r}, got {actual_text}"


def _suite_path(root: Path, path: Path) -> str:
    if root.is_dir():
        return path.relative_to(root).as_posix()
    return path.as_posix()


def _suite_screenshot_path(root: Path, path: Path, screenshot_root: Path) -> Path:
    if root.is_dir():
        relative_path = path.relative_to(root)
    else:
        relative_path = Path(path.name)
    return screenshot_root / relative_path.with_suffix(".png")


def _suite_baseline_path(root: Path, path: Path, baseline_root: Path) -> Path:
    if root.is_dir():
        relative_path = path.relative_to(root)
    else:
        relative_path = Path(path.name)
    return baseline_root / relative_path.with_suffix(".json")


def _capture_suite_screenshot(
    root: Path,
    path: Path,
    screenshot_root: Path,
    state: GibsonServerState,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    from harn_gibson.browser_capture import capture_scene_screenshot

    screenshot_path = _suite_screenshot_path(root, path, screenshot_root)
    return capture_scene_screenshot(state, screenshot_path, width=width, height=height).to_dict()


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


def replay_timeline_from_result(result: ReplayResult) -> dict[str, Any]:
    return {
        "schema": "harn-gibson.replay-timeline.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "frameCount": len(result.frames),
        "frames": [frame.to_dict() for frame in result.frames],
        "metadata": result.metadata,
    }


def write_replay_timeline(path: str | Path, result: ReplayResult) -> None:
    timeline_path = Path(path)
    timeline_path.parent.mkdir(parents=True, exist_ok=True)
    timeline_path.write_text(json.dumps(replay_timeline_from_result(result), indent=2) + "\n", encoding="utf-8")


def replay_renderer_contexts_from_result(result: ReplayResult) -> dict[str, Any]:
    return {
        "schema": "harn-gibson.replay-renderer-contexts.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "contextCount": len(result.renderer_contexts),
        "contexts": [context.to_dict() for context in result.renderer_contexts],
        "metadata": result.metadata,
    }


def write_replay_renderer_contexts(path: str | Path, result: ReplayResult) -> None:
    contexts_path = Path(path)
    contexts_path.parent.mkdir(parents=True, exist_ok=True)
    contexts_path.write_text(
        json.dumps(replay_renderer_contexts_from_result(result), indent=2) + "\n",
        encoding="utf-8",
    )


def capture_replay_frame_screenshots(
    result: ReplayResult,
    output_dir: str | Path,
    *,
    width: int = 1280,
    height: int = 900,
) -> tuple[ReplayFrameScreenshot, ...]:
    from harn_gibson.browser_capture import capture_scene_screenshot

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    captures: list[ReplayFrameScreenshot] = []
    for frame in result.frames:
        state = GibsonServerState()
        state.scene.state = scene_state_from_mapping(frame.scene)
        try:
            screenshot = capture_scene_screenshot(
                state,
                output_root / f"frame-{frame.index:04d}.png",
                width=width,
                height=height,
            )
        finally:
            state.pipeline.stop()
        captures.append(ReplayFrameScreenshot(frame.index, frame.step, screenshot.to_dict()))
    return tuple(captures)


def replay_frame_screenshot_manifest(
    result: ReplayResult,
    screenshots: Iterable[ReplayFrameScreenshot],
) -> dict[str, Any]:
    rendered_screenshots = tuple(screenshots)
    return {
        "schema": "harn-gibson.replay-frame-screenshots.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "frameCount": len(result.frames),
        "screenshotCount": len(rendered_screenshots),
        "frames": [screenshot.to_dict() for screenshot in rendered_screenshots],
        "metadata": result.metadata,
    }


def write_replay_frame_screenshot_manifest(
    path: str | Path,
    result: ReplayResult,
    screenshots: Iterable[ReplayFrameScreenshot],
) -> None:
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(replay_frame_screenshot_manifest(result, screenshots), indent=2) + "\n",
        encoding="utf-8",
    )


def replay_frame_review_html(manifest: Mapping[str, Any], *, output_path: str | Path | None = None) -> str:
    frames = manifest.get("frames")
    rendered_frames = [frame for frame in frames if isinstance(frame, Mapping)] if isinstance(frames, list) else []
    title = str(manifest.get("replayName") or "replay timeline")
    frame_count = escape(str(manifest.get("screenshotCount", len(rendered_frames))))
    schema = escape(str(manifest.get("schema", "")))
    player_frames = _replay_frame_review_player_frames(rendered_frames, output_path)
    player_data = _html_script_json(player_frames)
    initial_frame = player_frames[0] if player_frames else {}
    frame_max = max(0, len(player_frames) - 1)
    cards = "\n".join(
        _replay_frame_review_card(position, frame, output_path) for position, frame in enumerate(rendered_frames)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} timeline review</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    body {{ margin: 0; background: #020608; color: #d9fff7; }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 18px 22px;
      background: rgba(2, 6, 8, 0.92);
      border-bottom: 1px solid rgba(35, 255, 214, 0.32);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
    .meta {{ color: #7ee8d0; font-size: 13px; }}
    .player {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, 0.32fr);
      gap: 18px;
      padding: 20px 20px 4px;
      align-items: stretch;
    }}
    .active-frame {{
      margin: 0;
      min-width: 0;
      border: 1px solid rgba(35, 255, 214, 0.36);
      background: #000;
      box-shadow: 0 0 34px rgba(35, 255, 214, 0.16);
    }}
    .active-frame img {{
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: contain;
      background: #000;
    }}
    .player-panel {{
      display: grid;
      align-content: start;
      gap: 12px;
      border: 1px solid rgba(255, 207, 99, 0.30);
      background: rgba(6, 16, 18, 0.86);
      padding: 14px;
    }}
    .player-panel h2 {{ margin: 0; font-size: 14px; color: #ffcf63; letter-spacing: 0; }}
    .frame-meta,
    .frame-health {{
      min-height: 38px;
      color: #b8fff3;
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    .frame-health[data-ok="false"] {{ color: #ff5f9d; }}
    .controls {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    button {{
      min-height: 36px;
      border: 1px solid rgba(35, 255, 214, 0.34);
      background: rgba(2, 8, 10, 0.92);
      color: #d9fff7;
      font: inherit;
      cursor: pointer;
    }}
    button:hover,
    .frame-card.active {{
      border-color: rgba(255, 207, 99, 0.72);
      box-shadow: 0 0 22px rgba(255, 207, 99, 0.16);
    }}
    .scrubber-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }}
    input[type="range"] {{ width: 100%; accent-color: #ffcf63; }}
    #timelineCounter {{ color: #7ee8d0; font-size: 12px; }}
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
      padding: 20px;
    }}
    figure {{
      margin: 0;
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(4, 16, 18, 0.86);
    }}
    .frame-select {{ display: block; width: 100%; padding: 0; border: 0; background: transparent; }}
    img {{ display: block; width: 100%; height: auto; background: #000; }}
    figcaption {{ display: grid; gap: 5px; padding: 12px; font-size: 12px; color: #b8fff3; }}
    code {{ color: #ffcf63; }}
    .bad {{ color: #ff5f9d; }}
    @media (max-width: 860px) {{
      .player {{ grid-template-columns: 1fr; }}
      .controls {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)} timeline review</h1>
    <div class="meta">{frame_count} frames &middot; schema {schema}</div>
  </header>
  <section class="player" aria-label="Replay timeline player">
    <figure class="active-frame">
      <img id="activeFrame" src="{escape(str(initial_frame.get("src", "")))}" alt="Active replay frame">
    </figure>
    <aside class="player-panel">
      <h2>Timeline Playback</h2>
      <div id="frameMeta" class="frame-meta">loading timeline frames</div>
      <div id="frameHealth" class="frame-health" data-ok="true"></div>
      <div class="controls">
        <button id="previousFrame" type="button">PREV</button>
        <button id="playPause" type="button">PLAY</button>
        <button id="nextFrame" type="button">NEXT</button>
      </div>
      <div class="scrubber-row">
        <input id="timelineScrubber" type="range" min="0" max="{frame_max}" value="0" step="1">
        <span id="timelineCounter">0 / {len(player_frames)}</span>
      </div>
    </aside>
  </section>
  <main>
{cards}
  </main>
  <script>
    window.__gibsonReplayFrames = {player_data};
    (() => {{
      const frames = window.__gibsonReplayFrames;
      const activeFrame = document.getElementById("activeFrame");
      const frameMeta = document.getElementById("frameMeta");
      const frameHealth = document.getElementById("frameHealth");
      const scrubber = document.getElementById("timelineScrubber");
      const counter = document.getElementById("timelineCounter");
      const playPause = document.getElementById("playPause");
      const previousFrame = document.getElementById("previousFrame");
      const nextFrame = document.getElementById("nextFrame");
      let current = 0;
      let timer = null;

      function stopPlayback() {{
        if (timer !== null) window.clearInterval(timer);
        timer = null;
        playPause.textContent = "PLAY";
      }}

      function selectFrame(index) {{
        if (!frames.length) {{
          frameMeta.textContent = "no frames captured";
          frameHealth.textContent = "";
          counter.textContent = "0 / 0";
          return;
        }}
        current = Math.max(0, Math.min(frames.length - 1, Number(index) || 0));
        const frame = frames[current];
        activeFrame.src = frame.src || "";
        activeFrame.alt = `Replay frame ${{frame.index}}`;
        frameMeta.textContent = [
          `frame ${{frame.index}}`,
          `step ${{frame.kind || "unknown"}}`,
          `revision ${{frame.revision || "unknown"}}`,
          `updates ${{frame.updates || "0"}}`,
          `route ${{frame.route || "n/a"}}`,
        ].join(" / ");
        frameHealth.textContent = `canvas nonblank: ${{frame.nonblankText}}`;
        frameHealth.dataset.ok = frame.nonblank ? "true" : "false";
        scrubber.value = String(current);
        counter.textContent = `${{current + 1}} / ${{frames.length}}`;
        document.querySelectorAll("[data-frame-card]").forEach((card) => {{
          card.classList.toggle("active", Number(card.dataset.frameCard) === current);
        }});
      }}

      function stepFrame(delta) {{
        if (!frames.length) return;
        selectFrame((current + delta + frames.length) % frames.length);
      }}

      playPause.addEventListener("click", () => {{
        if (timer !== null) {{
          stopPlayback();
          return;
        }}
        playPause.textContent = "PAUSE";
        timer = window.setInterval(() => stepFrame(1), 900);
      }});
      previousFrame.addEventListener("click", () => {{ stopPlayback(); stepFrame(-1); }});
      nextFrame.addEventListener("click", () => {{ stopPlayback(); stepFrame(1); }});
      scrubber.addEventListener("input", () => {{ stopPlayback(); selectFrame(scrubber.value); }});
      document.querySelectorAll("[data-frame-select]").forEach((button) => {{
        button.addEventListener("click", () => {{
          stopPlayback();
          selectFrame(button.dataset.frameSelect);
        }});
      }});
      selectFrame(0);
    }})();
  </script>
</body>
</html>
"""


def write_replay_frame_review_html(path: str | Path, manifest: Mapping[str, Any]) -> None:
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(replay_frame_review_html(manifest, output_path=review_path), encoding="utf-8")


def _baseline_mismatch_error(expected_scene: Mapping[str, Any], actual_scene: Mapping[str, Any]) -> str:
    expected = json.dumps(expected_scene, indent=2, sort_keys=True).splitlines()
    actual = json.dumps(actual_scene, indent=2, sort_keys=True).splitlines()
    diff = "\n".join(islice(unified_diff(expected, actual, fromfile="baseline", tofile="actual", lineterm=""), 80))
    return f"baseline scene mismatch\n{diff}"


def _replay_frame_review_card(position: int, frame: Mapping[str, Any], output_path: str | Path | None) -> str:
    frame_data = _replay_frame_review_player_frame(frame, output_path)
    index = frame_data["index"]
    image_src = frame_data["src"]
    nonblank = frame_data["nonblank"]
    nonblank_class = "" if nonblank is True else ' class="bad"'
    kind = escape(str(frame_data["kind"]))
    revision = escape(str(frame_data["revision"]))
    updates = escape(str(frame_data["updates"]))
    route = escape(str(frame_data["route"]))
    step_line = (
        f"frame <code>{escape(str(index))}</code> &middot; "
        f"step <code>{kind}</code> &middot; revision <code>{revision}</code>"
    )
    route_line = f"updates <code>{updates}</code> &middot; route <code>{route}</code>"
    button_label = escape(str(index))
    button_open = (
        f'<button class="frame-select" type="button" data-frame-select="{position}" '
        f'aria-label="Show replay frame {button_label}">'
    )
    return f"""    <figure class="frame-card" data-frame-card="{position}">
      {button_open}
        <img src="{escape(image_src)}" alt="Replay frame {escape(str(index))}">
      </button>
      <figcaption>
        <span>{step_line}</span>
        <span>{route_line}</span>
        <span{nonblank_class}>canvas nonblank: <code>{escape(str(frame_data["nonblankText"]))}</code></span>
      </figcaption>
    </figure>"""


def _replay_frame_review_player_frames(
    frames: Iterable[Mapping[str, Any]],
    output_path: str | Path | None,
) -> list[dict[str, Any]]:
    return [_replay_frame_review_player_frame(frame, output_path) for frame in frames]


def _replay_frame_review_player_frame(frame: Mapping[str, Any], output_path: str | Path | None) -> dict[str, Any]:
    step = frame.get("step") if isinstance(frame.get("step"), Mapping) else {}
    screenshot = frame.get("screenshot") if isinstance(frame.get("screenshot"), Mapping) else {}
    index = frame.get("index", step.get("index") if isinstance(step, Mapping) else "")
    canvas_metrics = screenshot.get("canvasMetrics")
    nonblank = canvas_metrics.get("nonblank") if isinstance(canvas_metrics, Mapping) else None
    return {
        "index": str(index),
        "src": _replay_frame_image_src(screenshot.get("path"), output_path),
        "kind": str(step.get("kind", "")),
        "revision": str(step.get("sceneRevision", screenshot.get("sceneRevision", ""))),
        "updates": str(step.get("updates", "")),
        "route": str(step.get("route", "n/a")),
        "nonblank": nonblank is True,
        "nonblankText": str(nonblank),
    }


def _html_script_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":")).replace("</", "<\\/")


def _replay_frame_image_src(value: Any, output_path: str | Path | None) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if output_path is None:
        return Path(value).as_posix()
    output_parent = Path(output_path).parent.resolve()
    image_path = Path(value)
    if not image_path.is_absolute():
        image_path = (Path.cwd() / image_path).resolve()
    return Path(os.path.relpath(image_path, output_parent)).as_posix()


def _normalize_render_intent(value: Any) -> None:
    if not isinstance(value, dict):
        return
    timeline = value.get("timeline")
    if isinstance(timeline, dict):
        value["timeline"] = {"durationMs": timeline.get("durationMs", 0)}


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
    "ReplayBaselineResult",
    "ReplayFileResult",
    "ReplayExpectationError",
    "ReplayExpectationOp",
    "ReplayExpectationResult",
    "ReplayFrame",
    "ReplayFrameScreenshot",
    "ReplayRendererContext",
    "ReplayResult",
    "ReplayStepKind",
    "ReplayStepResult",
    "ReplaySuiteResult",
    "capture_replay_frame_screenshots",
    "discover_replay_files",
    "compare_replay_baseline",
    "evaluate_replay_expectations",
    "load_replay_file",
    "mutations_from_value",
    "render_plan_from_mapping",
    "render_request_from_mapping",
    "render_step_from_mapping",
    "replay_data_from_event_log",
    "replay_baseline_from_result",
    "replay_baseline_scene",
    "replay_frame_screenshot_manifest",
    "replay_frame_review_html",
    "replay_renderer_contexts_from_result",
    "replay_timeline_from_result",
    "run_replay_data",
    "run_replay_file",
    "run_replay_suite",
    "write_replay_result",
    "write_replay_frame_screenshot_manifest",
    "write_replay_frame_review_html",
    "write_replay_renderer_contexts",
    "write_replay_timeline",
    "write_replay_baseline",
    "write_scene",
]
