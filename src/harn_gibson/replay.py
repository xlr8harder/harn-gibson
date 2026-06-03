"""Replay historical harn events, renderer plans, and scene mutations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import unified_diff
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from harn_gibson.events import GibsonEvent, diagnostic_event
from harn_gibson.renderer_prompt import renderer_prompt_from_context
from harn_gibson.rendering import RendererContext, RenderPlan, RenderRequest, RenderStep, RenderSubmitResult
from harn_gibson.scene import SceneMutation, SceneState, mutation_from_mapping, scene_state_from_mapping
from harn_gibson.server import GibsonServerState, event_from_payload, submit_event_to_renderer
from harn_gibson.styles import style_pack_from_name

ReplayStepKind = Literal["event", "raw_event", "render_plan", "mutations"]
ReplayExpectationOp = Literal["equals", "contains", "exists", "min", "max"]
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
    screenshot_expectations: int = 0
    screenshot_expectation_failures: tuple[ReplayExpectationResult, ...] = ()
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
        if self.screenshot_expectations:
            payload["screenshotExpectations"] = self.screenshot_expectations
        if self.screenshot_expectation_failures:
            payload["screenshotExpectationFailures"] = [
                failure.to_dict() for failure in self.screenshot_expectation_failures
            ]
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
    state_factory: Callable[[], GibsonServerState] | None = None,
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
        state = state_factory() if state_factory is not None else GibsonServerState(style_pack=style_pack)
        result: ReplayResult | None = None
        baseline = None
        screenshot = None
        screenshot_expectations: tuple[ReplayExpectationResult, ...] = ()
        screenshot_failures: tuple[ReplayExpectationResult, ...] = ()
        try:
            replay_data = load_replay_file(replay_file)
            result = run_replay_data(replay_data, state)
            if screenshot_root is not None:
                screenshot = _capture_suite_screenshot(
                    root,
                    replay_file,
                    screenshot_root,
                    state,
                    width=screenshot_width,
                    height=screenshot_height,
                )
                screenshot_expectations = evaluate_screenshot_expectations(
                    screenshot,
                    replay_data.get("screenshotExpect"),
                )
                screenshot_failures = tuple(
                    expectation for expectation in screenshot_expectations if not expectation.passed
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
            ok = (baseline is None or baseline.ok) and not screenshot_failures
            error = ""
            if baseline is not None and not baseline.ok:
                error = baseline.error
            elif screenshot_failures:
                error = _screenshot_expectation_error(screenshot_failures)
            results.append(
                ReplayFileResult(
                    path=_suite_path(root, replay_file),
                    ok=ok,
                    steps=len(result.steps),
                    scene_revision=result.scene.revision,
                    expectations=len(result.expectations),
                    error=error,
                    screenshot_expectations=len(screenshot_expectations),
                    screenshot_expectation_failures=screenshot_failures,
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


def evaluate_screenshot_expectations(
    screenshot: Mapping[str, Any],
    value: Any,
) -> tuple[ReplayExpectationResult, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("replay screenshotExpect must be an object")
    checks: list[ReplayExpectationResult] = []
    if "nonblank" in value:
        checks.append(_evaluate_expectation(screenshot, "canvasMetrics.nonblank", "equals", value["nonblank"]))
    checks_value = value.get("checks", [])
    if not isinstance(checks_value, list):
        raise ValueError("replay screenshotExpect checks must be a list")
    for index, check in enumerate(checks_value):
        if not isinstance(check, Mapping):
            raise ValueError(f"replay screenshotExpect check {index} must be an object")
        checks.append(_expectation_from_mapping(screenshot, check, index))
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
    ops = [op for op in ("equals", "contains", "exists", "min", "max") if op in check]
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
    elif op == "min":
        passed = (
            actual is not MISSING
            and _is_replay_number(actual)
            and _is_replay_number(expected)
            and actual >= expected
        )
    elif op == "max":
        passed = (
            actual is not MISSING
            and _is_replay_number(actual)
            and _is_replay_number(expected)
            and actual <= expected
        )
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


def _is_replay_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


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


def _screenshot_expectation_error(failures: tuple[ReplayExpectationResult, ...]) -> str:
    details = "; ".join(failure.message for failure in failures)
    return f"replay screenshot expectations failed: {details}"


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


def replay_renderer_prompts_from_result(result: ReplayResult) -> dict[str, Any]:
    prompts = [
        renderer_prompt_from_context(context.context, context_index=context.index)
        for context in result.renderer_contexts
    ]
    return {
        "schema": "harn-gibson.replay-renderer-prompts.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "promptCount": len(prompts),
        "prompts": prompts,
        "metadata": result.metadata,
    }


def write_replay_renderer_prompts(path: str | Path, result: ReplayResult) -> None:
    prompts_path = Path(path)
    prompts_path.parent.mkdir(parents=True, exist_ok=True)
    prompts_path.write_text(
        json.dumps(replay_renderer_prompts_from_result(result), indent=2) + "\n",
        encoding="utf-8",
    )


def replay_renderer_chunks_from_result(result: ReplayResult, *, chunk_size: int = 4) -> dict[str, Any]:
    size = _positive_render_chunk_size(chunk_size)
    contexts = tuple(result.renderer_contexts)
    chunks = [
        _renderer_context_chunk_payload(index, contexts[start : start + size])
        for index, start in enumerate(range(0, len(contexts), size))
    ]
    return {
        "schema": "harn-gibson.replay-renderer-chunks.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "contextCount": len(contexts),
        "chunkCount": len(chunks),
        "chunkSize": size,
        "chunks": chunks,
        "metadata": result.metadata,
    }


def write_replay_renderer_chunks(path: str | Path, result: ReplayResult, *, chunk_size: int = 4) -> None:
    chunks_path = Path(path)
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_path.write_text(
        json.dumps(replay_renderer_chunks_from_result(result, chunk_size=chunk_size), indent=2) + "\n",
        encoding="utf-8",
    )


def replay_renderer_chunks_review_html(payload: Mapping[str, Any]) -> str:
    chunks = payload.get("chunks")
    rendered_chunks = [chunk for chunk in chunks if isinstance(chunk, Mapping)] if isinstance(chunks, list) else []
    title = str(payload.get("replayName") or "replay renderer chunks")
    schema = escape(str(payload.get("schema", "")))
    chunk_count = escape(str(payload.get("chunkCount", len(rendered_chunks))))
    context_count = escape(str(payload.get("contextCount", 0)))
    chunk_size = escape(str(payload.get("chunkSize", "")))
    cards = "\n".join(_renderer_chunk_review_card(chunk) for chunk in rendered_chunks)
    if not cards:
        cards = '    <section class="empty">No renderer chunks were captured for this replay.</section>'
    embedded_chunks = _html_script_json(rendered_chunks)
    meta_text = f"{chunk_count} chunks &middot; {context_count} contexts &middot; chunk size {chunk_size}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} renderer chunk review</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    body {{ margin: 0; background: #020608; color: #d9fff7; }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 18px 22px;
      background: rgba(2, 6, 8, 0.94);
      border-bottom: 1px solid rgba(35, 255, 214, 0.30);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .meta {{ color: #7ee8d0; font-size: 13px; }}
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 18px;
      padding: 20px;
    }}
    article {{
      display: grid;
      gap: 13px;
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(5, 16, 19, 0.88);
      padding: 16px;
    }}
    h2 {{ margin: 0; color: #ffcf63; font-size: 15px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px;
      color: #b8fff3;
      font-size: 12px;
    }}
    code {{ color: #ffcf63; }}
    .badge-set {{ display: grid; gap: 5px; }}
    .badge-label {{ color: #7ee8d0; font-size: 11px; text-transform: uppercase; }}
    .badge-row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(2, 8, 10, 0.92);
      color: #d9fff7;
      padding: 4px 7px;
      font-size: 11px;
    }}
    .prompt-preview {{
      display: grid;
      gap: 8px;
      border-top: 1px solid rgba(35, 255, 214, 0.16);
      padding-top: 12px;
    }}
    .prompt-preview strong {{ color: #7ee8d0; font-size: 11px; text-transform: uppercase; }}
    pre {{
      margin: 0;
      max-height: 260px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: #d9fff7;
      font-size: 11px;
      line-height: 1.42;
    }}
    .empty {{
      border: 1px solid rgba(255, 207, 99, 0.30);
      padding: 18px;
      color: #ffcf63;
      background: rgba(6, 12, 14, 0.86);
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)} renderer chunk review</h1>
    <div class="meta">{meta_text} &middot; schema {schema}</div>
  </header>
  <main>
{cards}
  </main>
  <script>
    window.__gibsonRendererChunks = {embedded_chunks};
  </script>
</body>
</html>
"""


def write_replay_renderer_chunks_review_html(path: str | Path, payload: Mapping[str, Any]) -> None:
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(replay_renderer_chunks_review_html(payload), encoding="utf-8")


def replay_render_intents_from_result(result: ReplayResult) -> dict[str, Any]:
    intents = _render_intents_from_scene_metadata(result.scene.metadata)
    return {
        "schema": "harn-gibson.replay-render-intents.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "intentCount": len(intents),
        "intents": [{"index": index, "intent": intent} for index, intent in enumerate(intents)],
        "metadata": result.metadata,
    }


def write_replay_render_intents(path: str | Path, result: ReplayResult) -> None:
    intents_path = Path(path)
    intents_path.parent.mkdir(parents=True, exist_ok=True)
    intents_path.write_text(
        json.dumps(replay_render_intents_from_result(result), indent=2) + "\n",
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


def replay_render_intents_review_html(payload: Mapping[str, Any]) -> str:
    entries = payload.get("intents")
    rendered_entries = [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    title = str(payload.get("replayName") or "replay render intents")
    schema = escape(str(payload.get("schema", "")))
    intent_count = escape(str(payload.get("intentCount", len(rendered_entries))))
    review_entries = [_render_intent_review_entry(entry) for entry in rendered_entries]
    embedded_entries = _html_script_json(review_entries)
    cards = "\n".join(_render_intent_review_card(entry) for entry in review_entries)
    if not cards:
        cards = '    <section class="empty">No render intents were recorded for this replay.</section>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} render intent review</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    body {{ margin: 0; background: #030507; color: #d9fff7; }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 18px 22px;
      background: rgba(3, 5, 7, 0.94);
      border-bottom: 1px solid rgba(35, 255, 214, 0.30);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
    .meta {{ color: #7ee8d0; font-size: 13px; }}
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 18px;
      padding: 20px;
    }}
    article {{
      display: grid;
      gap: 12px;
      border: 1px solid rgba(35, 255, 214, 0.26);
      background: rgba(5, 16, 19, 0.88);
      padding: 16px;
    }}
    article[data-renderer="direct"] {{ border-color: rgba(255, 207, 99, 0.34); }}
    h2 {{ margin: 0; color: #ffcf63; font-size: 15px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      color: #b8fff3;
      font-size: 12px;
    }}
    .summary span {{ overflow-wrap: anywhere; }}
    code {{ color: #ffcf63; }}
    .badge-set {{ display: grid; gap: 5px; }}
    .badge-label {{ color: #7ee8d0; font-size: 11px; text-transform: uppercase; }}
    .badge-row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(2, 8, 10, 0.92);
      color: #d9fff7;
      padding: 4px 7px;
      font-size: 11px;
    }}
    .badge.effect {{ border-color: rgba(255, 95, 157, 0.36); color: #ffc2dc; }}
    .badge.target {{ border-color: rgba(126, 232, 208, 0.36); color: #b8fff3; }}
    pre {{
      margin: 0;
      max-height: 260px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border-top: 1px solid rgba(35, 255, 214, 0.18);
      padding-top: 10px;
      color: #9bd7ca;
      font-size: 11px;
      line-height: 1.42;
    }}
    .empty {{
      border: 1px solid rgba(255, 207, 99, 0.30);
      padding: 18px;
      color: #ffcf63;
      background: rgba(6, 12, 14, 0.86);
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)} render intent review</h1>
    <div class="meta">{intent_count} intents &middot; schema {schema}</div>
  </header>
  <main>
{cards}
  </main>
  <script>
    window.__gibsonRenderIntents = {embedded_entries};
  </script>
</body>
</html>
"""


def write_replay_render_intents_review_html(path: str | Path, payload: Mapping[str, Any]) -> None:
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(replay_render_intents_review_html(payload), encoding="utf-8")


def replay_renderer_prompts_review_html(payload: Mapping[str, Any]) -> str:
    entries = payload.get("prompts")
    rendered_entries = [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    title = str(payload.get("replayName") or "replay renderer prompts")
    schema = escape(str(payload.get("schema", "")))
    prompt_count = escape(str(payload.get("promptCount", len(rendered_entries))))
    cards = "\n".join(_renderer_prompt_review_card(entry) for entry in rendered_entries)
    if not cards:
        cards = '    <section class="empty">No renderer prompts were captured for this replay.</section>'
    embedded_entries = _html_script_json(rendered_entries)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} renderer prompt review</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    body {{ margin: 0; background: #020608; color: #d9fff7; }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 18px 22px;
      background: rgba(2, 6, 8, 0.94);
      border-bottom: 1px solid rgba(35, 255, 214, 0.30);
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
    .meta {{ color: #7ee8d0; font-size: 13px; }}
    main {{ display: grid; gap: 18px; padding: 20px; }}
    article {{
      display: grid;
      gap: 13px;
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(5, 16, 19, 0.88);
      padding: 16px;
    }}
    h2 {{ margin: 0; color: #ffcf63; font-size: 15px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 8px;
      color: #b8fff3;
      font-size: 12px;
    }}
    code {{ color: #ffcf63; }}
    .message {{
      display: grid;
      gap: 8px;
      border-top: 1px solid rgba(35, 255, 214, 0.16);
      padding-top: 12px;
    }}
    .role {{ color: #7ee8d0; font-size: 11px; text-transform: uppercase; }}
    pre {{
      margin: 0;
      max-height: 420px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: #d9fff7;
      font-size: 11px;
      line-height: 1.42;
    }}
    .empty {{
      border: 1px solid rgba(255, 207, 99, 0.30);
      padding: 18px;
      color: #ffcf63;
      background: rgba(6, 12, 14, 0.86);
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)} renderer prompt review</h1>
    <div class="meta">{prompt_count} prompts &middot; schema {schema}</div>
  </header>
  <main>
{cards}
  </main>
  <script>
    window.__gibsonRendererPrompts = {embedded_entries};
  </script>
</body>
</html>
"""


def write_replay_renderer_prompts_review_html(path: str | Path, payload: Mapping[str, Any]) -> None:
    review_path = Path(path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(replay_renderer_prompts_review_html(payload), encoding="utf-8")


def replay_review_bundle_manifest(
    result: ReplayResult,
    screenshots: Iterable[ReplayFrameScreenshot],
    artifacts: Mapping[str, str],
    *,
    render_chunk_size: int = 4,
) -> dict[str, Any]:
    rendered_screenshots = tuple(screenshots)
    render_intents = replay_render_intents_from_result(result)
    renderer_prompts = replay_renderer_prompts_from_result(result)
    renderer_chunks = replay_renderer_chunks_from_result(result, chunk_size=render_chunk_size)
    return {
        "schema": "harn-gibson.replay-review-bundle.v1",
        "replayName": result.name,
        "replaySchema": result.schema,
        "stepCount": len(result.steps),
        "sceneRevision": result.scene.revision,
        "frameCount": len(result.frames),
        "screenshotCount": len(rendered_screenshots),
        "contextCount": len(result.renderer_contexts),
        "intentCount": int(render_intents["intentCount"]),
        "promptCount": int(renderer_prompts["promptCount"]),
        "chunkCount": int(renderer_chunks["chunkCount"]),
        "renderChunkSize": int(renderer_chunks["chunkSize"]),
        "artifacts": dict(artifacts),
        "metadata": result.metadata,
    }


def replay_review_bundle_index_html(manifest: Mapping[str, Any]) -> str:
    title = str(manifest.get("replayName") or "replay review")
    schema = escape(str(manifest.get("schema", "")))
    cards = "\n".join(
        _replay_review_metric(label, manifest.get(key, 0))
        for label, key in (
            ("steps", "stepCount"),
            ("scene revision", "sceneRevision"),
            ("frames", "frameCount"),
            ("screenshots", "screenshotCount"),
            ("renderer contexts", "contextCount"),
            ("render intents", "intentCount"),
            ("renderer prompts", "promptCount"),
            ("renderer chunks", "chunkCount"),
        )
    )
    artifact_links = "\n".join(
        _replay_review_artifact_link(label, href)
        for label, href in _replay_review_artifacts(manifest.get("artifacts"))
    )
    manifest_data = _html_script_json(manifest)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} replay review</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    body {{ margin: 0; background: #020608; color: #d9fff7; }}
    header {{
      padding: 20px 22px;
      border-bottom: 1px solid rgba(35, 255, 214, 0.32);
      background: rgba(2, 6, 8, 0.94);
    }}
    h1 {{ margin: 0 0 7px; font-size: 23px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .meta {{ color: #7ee8d0; font-size: 13px; }}
    main {{ display: grid; gap: 22px; padding: 20px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(5, 16, 19, 0.88);
      padding: 14px;
    }}
    .metric strong {{ display: block; color: #ffcf63; font-size: 24px; line-height: 1; }}
    .metric span {{ display: block; margin-top: 7px; color: #b8fff3; font-size: 12px; text-transform: uppercase; }}
    .links {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
    }}
    a {{
      display: block;
      border: 1px solid rgba(255, 207, 99, 0.28);
      background: rgba(6, 12, 14, 0.86);
      color: #d9fff7;
      padding: 13px;
      text-decoration: none;
      overflow-wrap: anywhere;
    }}
    a:hover {{ border-color: rgba(255, 207, 99, 0.76); box-shadow: 0 0 20px rgba(255, 207, 99, 0.12); }}
    code {{ color: #ffcf63; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)} replay review</h1>
    <div class="meta">schema {schema}</div>
  </header>
  <main>
    <section class="metrics" aria-label="Replay review metrics">
{cards}
    </section>
    <section class="links" aria-label="Replay review artifacts">
{artifact_links}
    </section>
  </main>
  <script>
    window.__gibsonReplayReview = {manifest_data};
  </script>
</body>
</html>
"""


def write_replay_review_bundle(
    path: str | Path,
    result: ReplayResult,
    screenshots: Iterable[ReplayFrameScreenshot],
    *,
    render_chunk_size: int = 4,
) -> dict[str, Any]:
    bundle_path = Path(path)
    frames_path = bundle_path / "frames"
    bundle_path.mkdir(parents=True, exist_ok=True)
    frames_path.mkdir(parents=True, exist_ok=True)
    rendered_screenshots = tuple(screenshots)
    artifacts = {
        "overview": "index.html",
        "manifest": "manifest.json",
        "scene": "scene.json",
        "result": "result.json",
        "timeline": "timeline.json",
        "rendererContexts": "renderer-contexts.json",
        "rendererPrompts": "renderer-prompts.json",
        "rendererChunks": "renderer-chunks.json",
        "rendererChunkReview": "renderer-chunks.html",
        "rendererPromptReview": "renderer-prompts.html",
        "renderIntents": "render-intents.json",
        "renderIntentReview": "render-intents.html",
        "frameManifest": "frames/manifest.json",
        "frameReview": "frames/index.html",
    }
    write_scene(bundle_path / artifacts["scene"], result.scene)
    write_replay_result(bundle_path / artifacts["result"], result)
    write_replay_timeline(bundle_path / artifacts["timeline"], result)
    write_replay_renderer_contexts(bundle_path / artifacts["rendererContexts"], result)
    write_replay_renderer_prompts(bundle_path / artifacts["rendererPrompts"], result)
    renderer_chunks = replay_renderer_chunks_from_result(result, chunk_size=render_chunk_size)
    write_replay_renderer_chunks(bundle_path / artifacts["rendererChunks"], result, chunk_size=render_chunk_size)
    write_replay_renderer_chunks_review_html(bundle_path / artifacts["rendererChunkReview"], renderer_chunks)
    write_replay_renderer_prompts_review_html(
        bundle_path / artifacts["rendererPromptReview"],
        replay_renderer_prompts_from_result(result),
    )
    write_replay_render_intents(bundle_path / artifacts["renderIntents"], result)
    write_replay_render_intents_review_html(
        bundle_path / artifacts["renderIntentReview"],
        replay_render_intents_from_result(result),
    )
    write_replay_frame_screenshot_manifest(frames_path / "manifest.json", result, rendered_screenshots)
    write_replay_frame_review_html(
        frames_path / "index.html",
        replay_frame_screenshot_manifest(result, rendered_screenshots),
    )
    manifest = replay_review_bundle_manifest(
        result,
        rendered_screenshots,
        artifacts,
        render_chunk_size=render_chunk_size,
    )
    (bundle_path / artifacts["manifest"]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (bundle_path / artifacts["overview"]).write_text(replay_review_bundle_index_html(manifest), encoding="utf-8")
    return manifest


def _baseline_mismatch_error(expected_scene: Mapping[str, Any], actual_scene: Mapping[str, Any]) -> str:
    expected = json.dumps(expected_scene, indent=2, sort_keys=True).splitlines()
    actual = json.dumps(actual_scene, indent=2, sort_keys=True).splitlines()
    diff = "\n".join(islice(unified_diff(expected, actual, fromfile="baseline", tofile="actual", lineterm=""), 80))
    return f"baseline scene mismatch\n{diff}"


def _replay_review_metric(label: str, value: Any) -> str:
    return f"""      <div class="metric">
        <strong>{escape(str(value))}</strong>
        <span>{escape(label)}</span>
      </div>"""


def _replay_review_artifacts(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    labels = {
        "frameReview": "Timeline Frame Review",
        "renderIntentReview": "Render Intent Review",
        "rendererChunkReview": "Renderer Chunk Review",
        "rendererPromptReview": "Renderer Prompt Review",
        "rendererChunks": "Renderer Chunks JSON",
        "scene": "Final Scene JSON",
        "result": "Replay Result JSON",
        "timeline": "Timeline JSON",
        "rendererContexts": "Renderer Contexts JSON",
        "rendererPrompts": "Renderer Prompts JSON",
        "renderIntents": "Render Intents JSON",
        "frameManifest": "Frame Screenshot Manifest",
        "manifest": "Bundle Manifest",
    }
    return tuple((label, str(value[key])) for key, label in labels.items() if isinstance(value.get(key), str))


def _replay_review_artifact_link(label: str, href: str) -> str:
    return f'      <a href="{escape(href)}">{escape(label)}<br><code>{escape(href)}</code></a>'


def _renderer_context_chunk_payload(index: int, contexts: tuple[ReplayRendererContext, ...]) -> dict[str, Any]:
    prompts = [renderer_prompt_from_context(context.context, context_index=context.index) for context in contexts]
    summary = _renderer_context_chunk_summary(prompts)
    return {
        "index": index,
        "contextStart": contexts[0].index,
        "contextEnd": contexts[-1].index,
        "contextIndexes": [context.index for context in contexts],
        "contextCount": len(contexts),
        "promptCount": len(prompts),
        **summary,
        "contexts": [context.to_dict() for context in contexts],
        "prompts": prompts,
    }


def _renderer_context_chunk_summary(prompts: list[dict[str, Any]]) -> dict[str, Any]:
    event_types: list[str] = []
    routes: list[str] = []
    modes: list[str] = []
    display_styles: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    durations: list[int] = []
    message_chars = 0
    context_chars = 0
    request_count = 0
    for prompt in prompts:
        metadata = prompt["metadata"]
        timeline = metadata["timeline"]
        _append_unique(modes, str(prompt["mode"]))
        _extend_unique(event_types, metadata["eventTypes"])
        _extend_unique(routes, metadata["routes"])
        _append_unique(display_styles, str(metadata["displayStyle"]))
        starts.append(_coerce_int(timeline["startMs"], 0))
        ends.append(_coerce_int(timeline["endMs"], 0))
        durations.append(_coerce_int(timeline["durationMs"], 0))
        message_chars += _coerce_int(metadata["messageChars"], 0)
        context_chars += _coerce_int(metadata["contextChars"], 0)
        request_count += _coerce_int(metadata["requestCount"], 0)
    start_ms = min(starts)
    end_ms = max(ends)
    return {
        "modes": modes,
        "displayStyles": display_styles,
        "eventTypes": event_types,
        "routes": routes,
        "requestCount": request_count,
        "timeline": {
            "startMs": start_ms,
            "endMs": end_ms,
            "durationMs": max(max(0, end_ms - start_ms), sum(durations)),
        },
        "messageChars": message_chars,
        "contextChars": context_chars,
    }


def _extend_unique(items: list[str], values: Iterable[Any]) -> None:
    for value in values:
        _append_unique(items, str(value))


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _positive_render_chunk_size(value: int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("render chunk size must be positive") from error
    if size <= 0:
        raise ValueError("render chunk size must be positive")
    return size


def _render_intents_from_scene_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    value = metadata.get("renderIntents")
    if isinstance(value, list):
        intents = [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
        if intents:
            return tuple(intents)
    last = metadata.get("lastRenderIntent")
    if isinstance(last, Mapping):
        return (deepcopy(dict(last)),)
    return ()


def _render_intent_review_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw_intent = entry.get("intent")
    intent = dict(raw_intent) if isinstance(raw_intent, Mapping) else dict(entry)
    timeline = intent.get("timeline")
    timeline_payload = dict(timeline) if isinstance(timeline, Mapping) else {}
    metadata = intent.get("metadata")
    return {
        "index": _coerce_int(entry.get("index"), 0),
        "renderer": str(intent.get("renderer") or "unknown"),
        "intent": str(intent.get("intent") or "render scene"),
        "requestCount": _coerce_int(intent.get("requestCount"), 0),
        "stepCount": _coerce_int(intent.get("stepCount"), 0),
        "mutationCount": _coerce_int(intent.get("mutationCount"), 0),
        "eventTypes": _string_list(intent.get("eventTypes")),
        "routes": _string_list(intent.get("routes")),
        "effects": _string_list(intent.get("effects")),
        "targets": _string_list(intent.get("targets")),
        "timeline": {
            "startMs": _coerce_int(timeline_payload.get("startMs"), 0),
            "endMs": _coerce_int(timeline_payload.get("endMs"), 0),
            "durationMs": _coerce_int(timeline_payload.get("durationMs"), 0),
        },
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
    }


def _render_intent_review_card(entry: Mapping[str, Any]) -> str:
    index = escape(str(entry.get("index", "")))
    renderer = escape(str(entry.get("renderer", "unknown")))
    intent = escape(str(entry.get("intent", "render scene")))
    timeline = entry.get("timeline") if isinstance(entry.get("timeline"), Mapping) else {}
    timeline_text = (
        f"{escape(str(timeline.get('startMs', 0)))}ms -> "
        f"{escape(str(timeline.get('endMs', 0)))}ms "
        f"({escape(str(timeline.get('durationMs', 0)))}ms)"
    )
    event_types = _badge_row(entry.get("eventTypes"), "event")
    routes = _badge_row(entry.get("routes"), "route")
    effects = _badge_row(entry.get("effects"), "effect")
    targets = _badge_row(entry.get("targets"), "target")
    metadata = escape(json.dumps(entry.get("metadata", {}), indent=2, sort_keys=True))
    return f"""    <article data-renderer="{renderer}">
      <h2>#{index} {intent}</h2>
      <div class="summary">
        <span>renderer <code>{renderer}</code></span>
        <span>timeline <code>{timeline_text}</code></span>
        <span>requests <code>{escape(str(entry.get("requestCount", 0)))}</code></span>
        <span>steps <code>{escape(str(entry.get("stepCount", 0)))}</code></span>
        <span>mutations <code>{escape(str(entry.get("mutationCount", 0)))}</code></span>
      </div>
      <div class="badge-set"><span class="badge-label">events</span><div class="badge-row">{event_types}</div></div>
      <div class="badge-set"><span class="badge-label">routes</span><div class="badge-row">{routes}</div></div>
      <div class="badge-set"><span class="badge-label">effects</span><div class="badge-row">{effects}</div></div>
      <div class="badge-set"><span class="badge-label">targets</span><div class="badge-row">{targets}</div></div>
      <pre>{metadata}</pre>
    </article>"""


def _renderer_prompt_review_card(entry: Mapping[str, Any]) -> str:
    context_index = escape(str(entry.get("contextIndex", "")))
    mode = escape(str(entry.get("mode", "rolling")))
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), Mapping) else {}
    event_types = ", ".join(_string_list(metadata.get("eventTypes"))) or "none"
    routes = ", ".join(_string_list(metadata.get("routes"))) or "none"
    messages = entry.get("messages")
    rendered_messages = (
        [message for message in messages if isinstance(message, Mapping)] if isinstance(messages, list) else []
    )
    message_cards = "\n".join(_renderer_prompt_message_card(message) for message in rendered_messages)
    return f"""    <article>
      <h2>context #{context_index} / {mode}</h2>
      <div class="summary">
        <span>events <code>{escape(event_types)}</code></span>
        <span>routes <code>{escape(routes)}</code></span>
        <span>requests <code>{escape(str(metadata.get("requestCount", 0)))}</code></span>
        <span>chars <code>{escape(str(metadata.get("messageChars", 0)))}</code></span>
      </div>
{message_cards}
    </article>"""


def _renderer_prompt_message_card(message: Mapping[str, Any]) -> str:
    role = escape(str(message.get("role") or "user"))
    content = escape(str(message.get("content") or ""))
    return f"""      <section class="message">
        <div class="role">{role}</div>
        <pre>{content}</pre>
      </section>"""


def _renderer_chunk_review_card(chunk: Mapping[str, Any]) -> str:
    index = escape(str(chunk.get("index", "")))
    context_range = f"{escape(str(chunk.get('contextStart', '')))}..{escape(str(chunk.get('contextEnd', '')))}"
    timeline = chunk.get("timeline") if isinstance(chunk.get("timeline"), Mapping) else {}
    timeline_text = (
        f"{escape(str(timeline.get('startMs', 0)))}ms -> "
        f"{escape(str(timeline.get('endMs', 0)))}ms "
        f"({escape(str(timeline.get('durationMs', 0)))}ms)"
    )
    event_types = _badge_row(chunk.get("eventTypes"), "event")
    routes = _badge_row(chunk.get("routes"), "route")
    modes = _badge_row(chunk.get("modes"), "mode")
    display_styles = _badge_row(chunk.get("displayStyles"), "style")
    prompt_preview = _renderer_chunk_prompt_preview(chunk)
    return f"""    <article>
      <h2>chunk #{index} / contexts <code>{context_range}</code></h2>
      <div class="summary">
        <span>contexts <code>{escape(str(chunk.get("contextCount", 0)))}</code></span>
        <span>prompts <code>{escape(str(chunk.get("promptCount", 0)))}</code></span>
        <span>requests <code>{escape(str(chunk.get("requestCount", 0)))}</code></span>
        <span>timeline <code>{timeline_text}</code></span>
        <span>prompt chars <code>{escape(str(chunk.get("messageChars", 0)))}</code></span>
        <span>context chars <code>{escape(str(chunk.get("contextChars", 0)))}</code></span>
      </div>
      <div class="badge-set"><span class="badge-label">modes</span><div class="badge-row">{modes}</div></div>
      <div class="badge-set"><span class="badge-label">styles</span><div class="badge-row">{display_styles}</div></div>
      <div class="badge-set"><span class="badge-label">events</span><div class="badge-row">{event_types}</div></div>
      <div class="badge-set"><span class="badge-label">routes</span><div class="badge-row">{routes}</div></div>
{prompt_preview}
    </article>"""


def _renderer_chunk_prompt_preview(chunk: Mapping[str, Any]) -> str:
    prompts = chunk.get("prompts")
    prompt_items = [prompt for prompt in prompts if isinstance(prompt, Mapping)] if isinstance(prompts, list) else []
    if not prompt_items:
        return ""
    first_prompt = prompt_items[0]
    messages = first_prompt.get("messages")
    rendered_messages = (
        [message for message in messages if isinstance(message, Mapping)] if isinstance(messages, list) else []
    )
    user_message = next((message for message in rendered_messages if message.get("role") == "user"), None)
    content = str(user_message.get("content") if isinstance(user_message, Mapping) else "")
    preview = content[:1800] + ("..." if len(content) > 1800 else "")
    return f"""      <section class="prompt-preview">
        <strong>first prompt user message</strong>
        <pre>{escape(preview)}</pre>
      </section>"""


def _badge_row(value: Any, badge_class: str) -> str:
    items = _string_list(value)
    if not items:
        return f'<span class="badge {badge_class}">none</span>'
    return "".join(f'<span class="badge {badge_class}">{escape(item)}</span>' for item in items)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str | int | float | bool)]


def _coerce_int(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


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
    "evaluate_screenshot_expectations",
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
    "replay_renderer_chunks_from_result",
    "replay_renderer_prompts_from_result",
    "replay_renderer_prompts_review_html",
    "replay_render_intents_from_result",
    "replay_render_intents_review_html",
    "replay_review_bundle_index_html",
    "replay_review_bundle_manifest",
    "replay_timeline_from_result",
    "run_replay_data",
    "run_replay_file",
    "run_replay_suite",
    "write_replay_result",
    "write_replay_frame_screenshot_manifest",
    "write_replay_frame_review_html",
    "write_replay_renderer_contexts",
    "write_replay_renderer_chunks",
    "write_replay_renderer_prompts",
    "write_replay_renderer_prompts_review_html",
    "write_replay_render_intents",
    "write_replay_render_intents_review_html",
    "write_replay_review_bundle",
    "write_replay_timeline",
    "write_replay_baseline",
    "write_scene",
]
