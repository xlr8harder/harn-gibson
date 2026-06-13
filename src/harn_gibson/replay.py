"""Replay historical harn events, renderer plans, and scene mutations."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from difflib import unified_diff
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any, Literal, cast

from harn_gibson.events import EventPhase, GibsonEvent, diagnostic_event, phase_for_event
from harn_gibson.renderer_prompt import renderer_prompt_from_context
from harn_gibson.rendering import (
    RendererContext,
    RendererContextConfig,
    RenderPlan,
    RenderRequest,
    RenderStep,
    RenderSubmitResult,
    touched_files_context_from_events,
)
from harn_gibson.scene import SceneMutation, SceneState, mutation_from_mapping, scene_state_from_mapping
from harn_gibson.server import GibsonServerState, event_from_payload, submit_event_to_renderer
from harn_gibson.styles import style_pack_from_name
from harn_gibson.world_bindings import world_bindings_from_props

ReplayStepKind = Literal["event", "raw_event", "render_plan", "mutations"]
ReplayExpectationOp = Literal["equals", "contains", "exists", "min", "max"]
ReplayPlaybackTiming = Literal["fixed", "real-time"]
MISSING = object()
SENSITIVE_REDACTION = "[redacted]"
EVENT_SUMMARY_TOUCHED_FILE_LIMIT = 16
_SENSITIVE_EVENT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
        "tokens",
    }
)
_SENSITIVE_TEXT_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(([A-Z0-9_]*TOKEN|[A-Z0-9_]*SECRET|PASSWORD|PASSWD|API[_-]?KEY|"
            r"[A-Z0-9_]*API[_-]?KEY)\s*[:=]\s*)[^\s,'\"}]{4,}"
        ),
        rf"\1{SENSITIVE_REDACTION}",
    ),
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {SENSITIVE_REDACTION}"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"), SENSITIVE_REDACTION),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), SENSITIVE_REDACTION),
    (re.compile(r"\bgh[opsu]_[A-Za-z0-9_]{16,}\b"), SENSITIVE_REDACTION),
)


@dataclass(frozen=True, slots=True)
class ReplayStepResult:
    index: int
    kind: ReplayStepKind
    scene_revision: int
    updates: int
    route: str | None = None
    timestamp_ms: int | None = None
    delay_ms_to_next: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "sceneRevision": self.scene_revision,
            "updates": self.updates,
        }
        if self.route is not None:
            payload["route"] = self.route
        if self.timestamp_ms is not None:
            payload["timestampMs"] = self.timestamp_ms
        if self.delay_ms_to_next is not None:
            payload["delayMsToNext"] = self.delay_ms_to_next
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
    event_summary: dict[str, Any] = field(default_factory=dict)
    route_counts: dict[str, int] = field(default_factory=dict)
    renderer_counts: dict[str, int] = field(default_factory=dict)
    visual_continuity_summary: dict[str, Any] = field(default_factory=dict)

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
        if self.event_summary:
            payload["eventSummary"] = self.event_summary
        if self.route_counts:
            payload["routes"] = sorted(self.route_counts)
            payload["routeCounts"] = dict(sorted(self.route_counts.items()))
        if self.renderer_counts:
            payload["renderers"] = sorted(self.renderer_counts)
            payload["rendererCounts"] = dict(sorted(self.renderer_counts.items()))
        if self.visual_continuity_summary:
            payload["visualContinuitySummary"] = self.visual_continuity_summary
        return payload


@dataclass(frozen=True, slots=True)
class ReplaySuiteResult:
    root: str
    files: tuple[ReplayFileResult, ...]
    split_manifest: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        return len(self.files)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.files if not result.ok)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def summary(self) -> dict[str, Any]:
        return _replay_suite_result_summary(self.files)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "harn-gibson.replay-suite-result.v1",
            "root": self.root,
            "ok": self.ok,
            "total": self.total,
            "failed": self.failed,
            "summary": self.summary,
            "files": [result.to_dict() for result in self.files],
        }
        if isinstance(self.split_manifest, Mapping):
            payload["splitManifest"] = dict(self.split_manifest)
            capture_summary = self.split_manifest.get("captureSummary")
            if isinstance(capture_summary, Mapping):
                payload["captureSummary"] = dict(capture_summary)
        return payload


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


ReplayProgressCallback = Callable[[ReplayStepResult, int, int, SceneState], None]


def play_replay_file(
    path: str | Path,
    state: GibsonServerState | None = None,
    *,
    start_delay_ms: int = 0,
    step_delay_ms: int = 900,
    playback_timing: ReplayPlaybackTiming = "fixed",
    time_scale: float = 1.0,
    max_step_delay_ms: int | None = None,
    quiet_step_delay_ms: int | None = None,
    min_step_delay_ms: int | None = None,
    start_index: int = 0,
    end_index: int | None = None,
    check_expectations: bool = True,
    progress: ReplayProgressCallback | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ReplayResult:
    return play_replay_data(
        load_replay_file(path),
        state,
        start_delay_ms=start_delay_ms,
        step_delay_ms=step_delay_ms,
        playback_timing=playback_timing,
        time_scale=time_scale,
        max_step_delay_ms=max_step_delay_ms,
        quiet_step_delay_ms=quiet_step_delay_ms,
        min_step_delay_ms=min_step_delay_ms,
        start_index=start_index,
        end_index=end_index,
        check_expectations=check_expectations,
        progress=progress,
        sleep_fn=sleep_fn,
    )


def play_replay_data(
    data: Mapping[str, Any],
    state: GibsonServerState | None = None,
    *,
    start_delay_ms: int = 0,
    step_delay_ms: int = 900,
    playback_timing: ReplayPlaybackTiming = "fixed",
    time_scale: float = 1.0,
    max_step_delay_ms: int | None = None,
    quiet_step_delay_ms: int | None = None,
    min_step_delay_ms: int | None = None,
    start_index: int = 0,
    end_index: int | None = None,
    check_expectations: bool = True,
    progress: ReplayProgressCallback | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ReplayResult:
    if playback_timing not in ("fixed", "real-time"):
        raise ValueError("playback_timing must be 'fixed' or 'real-time'")
    if time_scale <= 0 or not math.isfinite(time_scale):
        raise ValueError("time_scale must be positive")
    if max_step_delay_ms is not None and max_step_delay_ms < 0:
        raise ValueError("max_step_delay_ms must be non-negative")
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if end_index is not None and end_index < start_index:
        raise ValueError("end_index must be greater than or equal to start_index")
    replay_state = state or GibsonServerState()
    _apply_replay_project_metadata(replay_state, data)
    schema = str(data.get("schema") or "harn-gibson.replay.v1")
    name = str(data.get("name") or "unnamed replay")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise ValueError("replay must contain a steps list")

    results: list[ReplayStepResult] = []
    selected_steps = tuple(enumerate(steps[start_index:end_index], start_index))
    selected_payloads = tuple(step for _, step in selected_steps)
    timestamps = _replay_step_timestamps(selected_payloads)
    quiet_flags = _replay_quiet_flags(selected_payloads)
    if selected_steps and start_delay_ms > 0:
        sleep_fn(start_delay_ms / 1000)
    for position, (index, step) in enumerate(selected_steps):
        if not isinstance(step, Mapping):
            raise ValueError(f"replay step {index} must be an object")
        result = _run_step(index, step, replay_state)
        result = _step_result_with_timing(result, timestamps, position)
        results.append(result)
        if progress is not None:
            progress(result, position + 1, len(selected_steps), replay_state.scene.state)
        if position < len(selected_steps) - 1:
            delay_ms = _replay_step_delay_ms(
                position,
                timestamps=timestamps,
                playback_timing=playback_timing,
                step_delay_ms=step_delay_ms,
                time_scale=time_scale,
                max_step_delay_ms=max_step_delay_ms,
                quiet_step_delay_ms=quiet_step_delay_ms,
                min_step_delay_ms=min_step_delay_ms,
                quiet_flags=quiet_flags,
            )
            if delay_ms > 0:
                sleep_fn(delay_ms / 1000)

    expectations = (
        evaluate_replay_expectations(replay_state.scene.state, data.get("expect")) if check_expectations else ()
    )
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
        event_summary: dict[str, Any] = {}
        route_counts: dict[str, int] = {}
        renderer_counts: dict[str, int] = {}
        visual_continuity_summary: dict[str, Any] = {}
        screenshot_expectations: tuple[ReplayExpectationResult, ...] = ()
        screenshot_failures: tuple[ReplayExpectationResult, ...] = ()
        try:
            replay_data = load_replay_file(replay_file)
            event_summary = _replay_data_event_summary(replay_data)
            result = run_replay_data(replay_data, state)
            render_summary = _replay_result_render_summary(result)
            route_counts = render_summary["routeCounts"]
            renderer_counts = render_summary["rendererCounts"]
            visual_continuity_summary = _replay_scene_visual_continuity_summary(result.scene)
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
                    event_summary=event_summary,
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
                    event_summary=event_summary,
                    route_counts=route_counts,
                    renderer_counts=renderer_counts,
                    visual_continuity_summary=visual_continuity_summary,
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
                    event_summary=event_summary,
                    route_counts=route_counts,
                    renderer_counts=renderer_counts,
                    visual_continuity_summary=visual_continuity_summary,
                )
            )
        finally:
            state.pipeline.stop()
    return ReplaySuiteResult(root=str(root), files=tuple(results), split_manifest=_load_split_manifest(root))


def discover_replay_files(path: str | Path) -> tuple[Path, ...]:
    root = Path(path)
    if root.is_file():
        return (root,)
    if not root.is_dir():
        raise FileNotFoundError(f"replay path not found: {root}")
    files = tuple(sorted(item for item in root.rglob("*.json") if item.is_file() and item.name != "manifest.json"))
    if not files:
        raise ValueError(f"no replay JSON files found under {root}")
    return files


def _replay_suite_result_summary(files: Sequence[ReplayFileResult]) -> dict[str, Any]:
    file_results = tuple(files)
    summary: dict[str, Any] = {
        "fileCount": len(file_results),
        "okCount": sum(1 for result in file_results if result.ok),
        "failedCount": sum(1 for result in file_results if not result.ok),
        "stepCount": sum(result.steps for result in file_results),
        "expectationCount": sum(result.expectations for result in file_results),
        "screenshotCount": sum(1 for result in file_results if result.screenshot is not None),
        "screenshotExpectationCount": sum(result.screenshot_expectations for result in file_results),
    }
    baseline_count = sum(1 for result in file_results if result.baseline is not None)
    if baseline_count:
        summary["baselineCount"] = baseline_count
        summary["baselineUpdatedCount"] = sum(
            1 for result in file_results if result.baseline is not None and result.baseline.updated
        )
        summary["baselineFailedCount"] = sum(
            1 for result in file_results if result.baseline is not None and not result.baseline.ok
        )
    event_summary = _merge_replay_event_summaries(result.event_summary for result in file_results)
    if event_summary:
        summary["eventSummary"] = event_summary
    route_counts = _merge_count_mappings(result.route_counts for result in file_results)
    if route_counts:
        summary["routes"] = sorted(route_counts)
        summary["routeCounts"] = route_counts
    renderer_counts = _merge_count_mappings(result.renderer_counts for result in file_results)
    if renderer_counts:
        summary["renderers"] = sorted(renderer_counts)
        summary["rendererCounts"] = renderer_counts
    visual_continuity_summary = _merge_visual_continuity_summaries(
        result.visual_continuity_summary for result in file_results
    )
    if visual_continuity_summary:
        summary["visualContinuitySummary"] = visual_continuity_summary
    trajectory_coverage = _trajectory_coverage_from_summary(summary)
    if trajectory_coverage:
        summary["trajectoryCoverage"] = trajectory_coverage
    return summary


def _replay_data_event_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    steps = data.get("steps")
    if not isinstance(steps, list):
        return {}
    events: list[Mapping[str, Any]] = []
    for step in steps:
        if isinstance(step, Mapping):
            events.extend(_replay_step_events(step))
    if not events:
        return {}
    return {
        "eventCount": len(events),
        **_event_log_capture_summary(events),
    }


def _replay_step_events(step: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    kind = step.get("type", step.get("kind"))
    if kind == "event":
        event = step.get("event")
        return (event,) if isinstance(event, Mapping) else ()
    if kind == "raw_event":
        raw = step.get("raw")
        return (raw,) if isinstance(raw, Mapping) else ()
    if kind == "mutations":
        event = step.get("event")
        return (event,) if isinstance(event, Mapping) else ()
    if kind != "render_plan":
        return ()
    plan = step.get("plan")
    if not isinstance(plan, Mapping):
        plan = step
    requests = plan.get("requests")
    if not isinstance(requests, list):
        return ()
    events = []
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        event = request.get("event")
        if isinstance(event, Mapping):
            events.append(event)
    return tuple(events)


def _replay_result_render_summary(result: ReplayResult) -> dict[str, dict[str, int]]:
    route_counts: dict[str, int] = {}
    renderer_counts: dict[str, int] = {}
    intents = result.scene.metadata.get("renderIntents")
    if isinstance(intents, list):
        for intent in intents:
            if not isinstance(intent, Mapping):
                continue
            renderer = intent.get("renderer")
            if isinstance(renderer, str) and renderer:
                renderer_counts[renderer] = renderer_counts.get(renderer, 0) + 1
            routes = intent.get("routes")
            if isinstance(routes, str):
                route_counts[routes] = route_counts.get(routes, 0) + 1
            elif isinstance(routes, list):
                for route in routes:
                    if isinstance(route, str) and route:
                        route_counts[route] = route_counts.get(route, 0) + 1
    if not route_counts:
        route_counts = _event_log_counts(step.route for step in result.steps)
    return {
        "routeCounts": dict(sorted(route_counts.items())),
        "rendererCounts": dict(sorted(renderer_counts.items())),
    }


def _replay_scene_visual_continuity_summary(scene: SceneState) -> dict[str, Any]:
    anchors = [
        primitive.id
        for primitive in sorted(scene.primitives.values(), key=lambda item: item.id)
        if primitive.region == "stage"
    ]
    world_binding_count = sum(
        len(world_bindings_from_props(primitive.props, target_id=primitive.id, max_bindings=32))
        for primitive in scene.primitives.values()
    )
    active_animation_targets = sorted(
        {animation.target_id for animation in scene.animations.values() if animation.target_id}
    )
    active_animation_effects = [
        f"animation:{animation.kind}" for animation in sorted(scene.animations.values(), key=lambda item: item.id)
    ]
    effects: list[str] = []
    targets: list[str] = []
    renderers: list[str] = []
    for intent in _render_intents_from_scene_metadata(scene.metadata):
        renderer = intent.get("renderer")
        if isinstance(renderer, str) and renderer:
            _append_bounded_unique(renderers, renderer)
        _extend_bounded_unique(effects, intent.get("effects"))
        _extend_bounded_unique(targets, intent.get("targets"))
    _extend_bounded_unique(effects, active_animation_effects)
    _extend_bounded_unique(targets, active_animation_targets)
    return _merge_visual_continuity_summaries(
        (
            {
                "maxVisualAnchorCount": len(anchors),
                "maxWorldBindingCount": world_binding_count,
                "maxActiveAnimationCount": len(scene.animations),
                "anchors": anchors,
                "effects": effects,
                "targets": targets,
                "renderers": renderers,
                "styleMotifs": _scene_style_motifs(scene),
            },
        )
    )


def _scene_style_motifs(scene: SceneState) -> list[str]:
    metadata_style = scene.metadata.get("stylePack")
    if isinstance(metadata_style, Mapping):
        motifs = metadata_style.get("motifs")
        if isinstance(motifs, list):
            return _string_list(motifs)
    stage = scene.primitives.get("stage")
    if stage is not None:
        stage_style = stage.props.get("stylePack")
        if isinstance(stage_style, Mapping):
            motifs = stage_style.get("motifs")
            if isinstance(motifs, list):
                return _string_list(motifs)
    return []


def _merge_replay_event_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    summary_items = tuple(summary for summary in summaries if isinstance(summary, Mapping))
    event_count = 0
    event_type_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    first_sequences: list[int] = []
    last_sequences: list[int] = []
    first_timestamps: list[int] = []
    last_timestamps: list[int] = []
    for summary in summary_items:
        event_count += _coerce_int(summary.get("eventCount"), 0)
        _merge_count_mapping(event_type_counts, summary.get("eventTypeCounts"))
        _merge_count_mapping(phase_counts, summary.get("phaseCounts"))
        _merge_count_mapping(source_counts, summary.get("sourceCounts"))
        _append_int(first_sequences, summary.get("firstSequence"))
        _append_int(last_sequences, summary.get("lastSequence"))
        _append_int(first_timestamps, summary.get("firstTimestampMs"))
        _append_int(last_timestamps, summary.get("lastTimestampMs"))
    if not event_count and not event_type_counts and not phase_counts and not source_counts:
        return {}
    merged: dict[str, Any] = {
        "eventCount": event_count,
        "eventTypes": sorted(event_type_counts),
        "eventTypeCounts": dict(sorted(event_type_counts.items())),
        "phases": sorted(phase_counts),
        "phaseCounts": dict(sorted(phase_counts.items())),
        "sources": sorted(source_counts),
        "sourceCounts": dict(sorted(source_counts.items())),
    }
    if first_sequences:
        merged["firstSequence"] = min(first_sequences)
    if last_sequences:
        merged["lastSequence"] = max(last_sequences)
    if first_timestamps and last_timestamps:
        first_timestamp = min(first_timestamps)
        last_timestamp = max(last_timestamps)
        merged["firstTimestampMs"] = first_timestamp
        merged["lastTimestampMs"] = last_timestamp
        merged["durationMs"] = max(0, last_timestamp - first_timestamp)
    tool_summary = _merge_event_tool_summaries(
        summary.get("tools") for summary in summary_items if isinstance(summary.get("tools"), Mapping)
    )
    if tool_summary:
        merged["tools"] = tool_summary
    touched_summary = _merge_event_touched_file_summaries(
        summary.get("touchedFiles") for summary in summary_items if isinstance(summary.get("touchedFiles"), Mapping)
    )
    if touched_summary:
        merged["touchedFiles"] = touched_summary
    return merged


def _merge_event_tool_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    command_count = 0
    failed_tool_result_count = 0
    for summary in summaries:
        _merge_count_mapping(tool_counts, summary.get("toolCounts"))
        command_count += _coerce_int(summary.get("commandCount"), 0)
        failed_tool_result_count += _coerce_int(summary.get("failedToolResultCount"), 0)
    payload: dict[str, Any] = {}
    if tool_counts:
        payload["toolNames"] = sorted(tool_counts)
        payload["toolCounts"] = dict(sorted(tool_counts.items()))
    if command_count:
        payload["commandCount"] = command_count
    if failed_tool_result_count:
        payload["failedToolResultCount"] = failed_tool_result_count
    return payload


def _merge_event_touched_file_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, dict[str, Any]] = {}
    any_truncated = False
    for summary in summaries:
        any_truncated = any_truncated or bool(summary.get("truncated"))
        files = summary.get("files")
        if not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                continue
            current = by_path.get(path)
            if current is None:
                current = {
                    "path": path,
                    "operation": str(item.get("operation") or "touched"),
                    "phases": [],
                    "sources": [],
                }
                by_path[path] = current
            _merge_optional_sequence_range(current, item)
            phases = item.get("phases")
            if isinstance(phases, list):
                _extend_unique(current["phases"], phases)
            sources = item.get("sources")
            if isinstance(sources, list):
                _extend_unique(current["sources"], sources)
    if not by_path:
        return {}
    files = tuple(sorted(by_path.values(), key=lambda item: str(item["path"])))
    bounded_files = files[:EVENT_SUMMARY_TOUCHED_FILE_LIMIT]
    return {
        "schema": "harn-gibson.touched-files.v1",
        "files": [dict(item) for item in bounded_files],
        "paths": [str(item["path"]) for item in bounded_files],
        "count": len(files),
        "truncated": any_truncated or len(files) > EVENT_SUMMARY_TOUCHED_FILE_LIMIT,
        "topLevelCounts": _touched_file_top_level_counts(files),
    }


def _merge_optional_sequence_range(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    first_sequence = source.get("firstSequence")
    if isinstance(first_sequence, int) and not isinstance(first_sequence, bool):
        current = target.get("firstSequence")
        target["firstSequence"] = min(current, first_sequence) if isinstance(current, int) else first_sequence
    last_sequence = source.get("lastSequence")
    if isinstance(last_sequence, int) and not isinstance(last_sequence, bool):
        current = target.get("lastSequence")
        target["lastSequence"] = max(current, last_sequence) if isinstance(current, int) else last_sequence


def _merge_count_mappings(summaries: Iterable[Mapping[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for summary in summaries:
        _merge_count_mapping(merged, summary)
    return dict(sorted(merged.items()))


def _merge_count_mapping(target: dict[str, int], value: Any) -> None:
    if not isinstance(value, Mapping):
        return
    for key, count in value.items():
        if not isinstance(key, str) or not key:
            continue
        target[key] = target.get(key, 0) + _coerce_int(count, 0)


def _append_int(items: list[int], value: Any) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        items.append(value)


def load_replay_file(path: str | Path) -> dict[str, Any]:
    replay_path = Path(path)
    payload = json.loads(replay_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("replay file must contain a JSON object")
    return payload


def replay_data_from_event_log(
    path: str | Path,
    *,
    name: str | None = None,
    visual_fixture: bool = False,
    screenshot_lit_min: float = 0.02,
    screenshot_max_channel_min: int = 60,
    redact_sensitive: bool = True,
) -> dict[str, Any]:
    event_log_path = Path(path)
    events = _read_event_log_events(event_log_path)
    events, redaction_count, _ = _event_log_events_for_fixture(events, redact_sensitive=redact_sensitive)
    return _replay_data_from_events(
        event_log_path,
        events,
        name=name if name is not None else f"event log: {event_log_path.name}",
        visual_fixture=visual_fixture,
        screenshot_lit_min=screenshot_lit_min,
        screenshot_max_channel_min=screenshot_max_channel_min,
        redact_sensitive=redact_sensitive,
        redaction_count=redaction_count,
    )


def split_replay_data_from_event_log(
    path: str | Path,
    *,
    events_per_fixture: int,
    name: str | None = None,
    visual_fixture: bool = False,
    screenshot_lit_min: float = 0.02,
    screenshot_max_channel_min: int = 60,
    redact_sensitive: bool = True,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    if events_per_fixture <= 0:
        raise ValueError("events_per_fixture must be positive")
    event_log_path = Path(path)
    raw_events = _read_event_log_events(event_log_path)
    events, redaction_count, event_redaction_counts = _event_log_events_for_fixture(
        raw_events,
        redact_sensitive=redact_sensitive,
    )
    chunks = _event_chunks(events, events_per_fixture)
    chunk_count = len(chunks)
    base_name = name if name is not None else f"event log: {event_log_path.name}"
    fixtures: list[dict[str, Any]] = []
    fixture_entries: list[dict[str, Any]] = []
    for index, chunk_events in enumerate(chunks, start=1):
        start_offset = (index - 1) * events_per_fixture
        end_offset = start_offset + len(chunk_events) - 1 if chunk_events else None
        chunk_metadata = {
            "eventLogChunk": {
                "chunkIndex": index,
                "chunkCount": chunk_count,
                "eventsPerFixture": events_per_fixture,
                "startEventOffset": start_offset,
                "endEventOffset": end_offset,
                "totalEventCount": len(events),
            }
        }
        fixture = _replay_data_from_events(
            event_log_path,
            chunk_events,
            name=f"{base_name} chunk {index}/{chunk_count}",
            visual_fixture=visual_fixture,
            screenshot_lit_min=screenshot_lit_min,
            screenshot_max_channel_min=screenshot_max_channel_min,
            metadata=chunk_metadata,
            redact_sensitive=redact_sensitive,
            redaction_count=sum(event_redaction_counts[start_offset : start_offset + len(chunk_events)]),
        )
        fixtures.append(fixture)
        entry: dict[str, Any] = {
            "path": _split_replay_fixture_filename(base_name, index),
            "chunkIndex": index,
            "eventCount": len(chunk_events),
            "startEventOffset": start_offset,
            "endEventOffset": end_offset,
        }
        summary = fixture.get("metadata", {}).get("captureSummary")
        if isinstance(summary, Mapping):
            for key in ("firstSequence", "lastSequence", "firstTimestampMs", "lastTimestampMs", "durationMs"):
                if key in summary:
                    entry[key] = summary[key]
        fixture_entries.append(entry)
    manifest: dict[str, Any] = {
        "schema": "harn-gibson.event-log-split.v1",
        "name": base_name,
        "sourceEventLog": event_log_path.as_posix(),
        "eventCount": len(events),
        "eventsPerFixture": events_per_fixture,
        "chunkCount": chunk_count,
        "visualFixture": visual_fixture,
        "redaction": {"enabled": redact_sensitive, "count": redaction_count},
        "captureSummary": _event_log_capture_summary(events),
        "fixtures": fixture_entries,
    }
    return tuple(fixtures), manifest


def split_replay_fixture_filename(name: str, index: int) -> str:
    return _split_replay_fixture_filename(name, index)


def _read_event_log_events(event_log_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with event_log_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            event = json.loads(stripped)
            if not isinstance(event, dict):
                raise ValueError(f"event log line {line_number} must contain a JSON object")
            events.append(event)
    return events


def _event_log_events_for_fixture(
    events: Sequence[dict[str, Any]],
    *,
    redact_sensitive: bool,
) -> tuple[list[dict[str, Any]], int, tuple[int, ...]]:
    if not redact_sensitive:
        return [dict(event) for event in events], 0, tuple(0 for _ in events)
    redacted: list[dict[str, Any]] = []
    event_redaction_counts: list[int] = []
    redaction_count = 0
    for event in events:
        redacted_event, event_redaction_count = _redact_sensitive_event_data(event)
        redacted.append(redacted_event)
        event_redaction_counts.append(event_redaction_count)
        redaction_count += event_redaction_count
    return redacted, redaction_count, tuple(event_redaction_counts)


def _redact_sensitive_event_data(value: Any) -> tuple[Any, int]:
    if isinstance(value, Mapping):
        return _redact_sensitive_event_mapping(value)
    if isinstance(value, list):
        return _redact_sensitive_event_list(value)
    if isinstance(value, str):
        return _redact_sensitive_event_text(value)
    return value, 0


def _redact_sensitive_event_mapping(value: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    redacted: dict[str, Any] = {}
    redaction_count = 0
    for key, child in value.items():
        if _is_sensitive_event_key(key):
            redacted[key] = SENSITIVE_REDACTION
            redaction_count += 1
        else:
            redacted_child, child_redaction_count = _redact_sensitive_event_data(child)
            redacted[key] = redacted_child
            redaction_count += child_redaction_count
    return redacted, redaction_count


def _redact_sensitive_event_list(value: Sequence[Any]) -> tuple[list[Any], int]:
    redacted: list[Any] = []
    redaction_count = 0
    for child in value:
        redacted_child, child_redaction_count = _redact_sensitive_event_data(child)
        redacted.append(redacted_child)
        redaction_count += child_redaction_count
    return redacted, redaction_count


def _redact_sensitive_event_text(value: str) -> tuple[str, int]:
    redacted = value
    redaction_count = 0
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        redaction_count += count
    return redacted, redaction_count


def _is_sensitive_event_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_EVENT_KEYS


def _replay_data_from_events(
    event_log_path: Path,
    events: Sequence[dict[str, Any]],
    *,
    name: str,
    visual_fixture: bool,
    screenshot_lit_min: float,
    screenshot_max_channel_min: int,
    metadata: Mapping[str, Any] | None = None,
    redact_sensitive: bool,
    redaction_count: int,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for event in events:
        steps.append({"type": "event", "event": event})
    fixture_metadata: dict[str, Any] = {
        "sourceEventLog": event_log_path.as_posix(),
        "eventCount": len(steps),
        "redaction": {"enabled": redact_sensitive, "count": redaction_count},
    }
    if metadata is not None:
        fixture_metadata.update(dict(metadata))
    if visual_fixture:
        fixture_metadata["visualFixture"] = True
        fixture_metadata["captureSummary"] = _event_log_capture_summary(events)
    fixture: dict[str, Any] = {
        "schema": "harn-gibson.replay.v1",
        "name": name,
        "metadata": fixture_metadata,
        "steps": steps,
    }
    if visual_fixture:
        fixture["screenshotExpect"] = {
            "nonblank": True,
            "checks": [
                {"path": "canvasMetrics.litRatio", "min": screenshot_lit_min},
                {"path": "canvasMetrics.maxChannelTotal", "min": screenshot_max_channel_min},
            ],
        }
    return fixture


def _event_chunks(events: Sequence[dict[str, Any]], size: int) -> tuple[tuple[dict[str, Any], ...], ...]:
    if not events:
        return ((),)
    return tuple(tuple(events[index : index + size]) for index in range(0, len(events), size))


def _split_replay_fixture_filename(name: str, index: int) -> str:
    return f"{_slugify_filename(name)}-{index:04d}.json"


def _slugify_filename(value: str) -> str:
    slug_chars: list[str] = []
    previous_dash = False
    for character in value.lower():
        if character.isalnum():
            slug_chars.append(character)
            previous_dash = False
        elif not previous_dash:
            slug_chars.append("-")
            previous_dash = True
    slug = "".join(slug_chars).strip("-")
    return slug or "event-log"


def _event_log_capture_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sequences = [int(event["sequence"]) for event in events if isinstance(event.get("sequence"), int)]
    timestamps = [int(event["timestampMs"]) for event in events if isinstance(event.get("timestampMs"), int)]
    event_type_counts = _event_log_counts(event.get("eventType") for event in events)
    phase_counts = _event_log_counts(event.get("phase") for event in events)
    source_counts = _event_log_counts(event.get("source") for event in events)
    summary: dict[str, Any] = {
        "eventTypes": sorted(event_type_counts),
        "eventTypeCounts": event_type_counts,
        "phases": sorted(phase_counts),
        "phaseCounts": phase_counts,
        "sources": sorted(source_counts),
        "sourceCounts": source_counts,
    }
    tool_summary = _event_log_tool_summary(events)
    if tool_summary:
        summary["tools"] = tool_summary
    touched_summary = _event_log_touched_file_summary(events)
    if touched_summary:
        summary["touchedFiles"] = touched_summary
    if sequences:
        summary["firstSequence"] = min(sequences)
        summary["lastSequence"] = max(sequences)
    if timestamps:
        first_timestamp = min(timestamps)
        last_timestamp = max(timestamps)
        summary["firstTimestampMs"] = first_timestamp
        summary["lastTimestampMs"] = last_timestamp
        summary["durationMs"] = max(0, last_timestamp - first_timestamp)
    return summary


def _event_log_tool_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    command_count = 0
    failed_tool_result_count = 0
    for event in events:
        payload = event.get("payload")
        payload_mapping = payload if isinstance(payload, Mapping) else {}
        tool_name = payload_mapping.get("toolName")
        if isinstance(tool_name, str) and tool_name:
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        command_count += _event_command_field_count(payload_mapping)
        if event.get("eventType") in {"tool_result", "tool_execution_end"} and bool(payload_mapping.get("isError")):
            failed_tool_result_count += 1
    summary: dict[str, Any] = {}
    if tool_counts:
        summary["toolNames"] = sorted(tool_counts)
        summary["toolCounts"] = dict(sorted(tool_counts.items()))
    if command_count:
        summary["commandCount"] = command_count
    if failed_tool_result_count:
        summary["failedToolResultCount"] = failed_tool_result_count
    return summary


def _event_command_field_count(value: Any) -> int:
    if isinstance(value, Mapping):
        count = 0
        for key, child in value.items():
            if str(key) in {"cmd", "command", "shellCommand"} and isinstance(child, str):
                count += 1
            count += _event_command_field_count(child)
        return count
    if isinstance(value, list | tuple):
        return sum(_event_command_field_count(item) for item in value)
    return 0


def _event_log_touched_file_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    replay_events = tuple(_gibson_event_from_event_log_mapping(item) for item in events)
    if not replay_events:
        return {}
    touched = touched_files_context_from_events(
        replay_events,
        RendererContextConfig(max_touched_files=EVENT_SUMMARY_TOUCHED_FILE_LIMIT),
    )
    files = touched.get("files")
    file_items = [dict(item) for item in files if isinstance(item, Mapping)] if isinstance(files, list) else []
    if not file_items:
        return {}
    return {
        "schema": touched["schema"],
        "files": file_items,
        "paths": [str(item["path"]) for item in file_items if isinstance(item.get("path"), str)],
        "count": touched["count"],
        "truncated": touched["truncated"],
        "topLevelCounts": _touched_file_top_level_counts(file_items),
    }


def _gibson_event_from_event_log_mapping(event: Mapping[str, Any]) -> GibsonEvent:
    payload_value = event.get("payload")
    payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
    event_type = str(event.get("eventType") or payload.get("type") or "unknown")
    phase_value = event.get("phase")
    phase = (
        cast(EventPhase, str(phase_value))
        if phase_value in {"before", "during", "after", "lifecycle"}
        else phase_for_event(event_type)
    )
    return GibsonEvent(
        sequence=_coerce_int(event.get("sequence"), 0),
        timestamp_ms=_coerce_int(event.get("timestampMs"), 0),
        source=str(event.get("source") or "capture"),
        event_type=event_type,
        phase=phase,
        title=str(event.get("title") or event_type),
        summary=str(event.get("summary") or ""),
        payload=payload,
        recent_context=_string_tuple(event.get("recentContext", ())),
        visualization_context=_string_tuple(event.get("visualizationContext", ())),
    )


def _touched_file_top_level_counts(files: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in files:
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        top_level = path.split("/", 1)[0]
        counts[top_level] = counts.get(top_level, 0) + 1
    return dict(sorted(counts.items()))


def _event_log_counts(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


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
    _apply_replay_project_metadata(replay_state, data)
    schema = str(data.get("schema") or "harn-gibson.replay.v1")
    name = str(data.get("name") or "unnamed replay")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise ValueError("replay must contain a steps list")

    results: list[ReplayStepResult] = []
    frames: list[ReplayFrame] = []
    renderer_contexts: list[ReplayRendererContext] = []
    timestamps = _replay_step_timestamps(steps)
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
            result = _step_result_with_timing(result, timestamps, index)
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


def _apply_replay_project_metadata(state: GibsonServerState, data: Mapping[str, Any]) -> None:
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        return
    project_root = _metadata_text(metadata.get("projectRoot"))
    project_name = _metadata_text(metadata.get("projectName"))
    if project_root is None and project_name is None:
        return
    if state.project_root is not None:
        return
    resolved_project_name = project_name
    if resolved_project_name is None and project_root is not None:
        resolved_project_name = Path(project_root).expanduser().resolve().name or "workspace"
    if project_root is not None:
        state.project_root = project_root
    state.project_name = cast(str, resolved_project_name)
    config = state.pipeline.context_builder.config
    state.pipeline.context_builder.config = replace(
        config,
        project_name=state.project_name,
        project_root=state.project_root,
    )


def _metadata_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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


def _replay_step_timestamps(steps: Sequence[Any]) -> tuple[int | None, ...]:
    return tuple(_replay_step_timestamp_ms(step) for step in steps)


_SALIENT_EVENT_TYPES = frozenset({
    "tool_call",
    "tool_result",
    "tool_execution_start",
    "tool_execution_end",
    "user_bash",
    "runtime_error",
    "input",
    # a finished message is a beat: the audience gets the pause to read it
    "message_end",
    "session_start",
    "session_shutdown",
    "agent_start",
    "agent_end",
    "harn_exit",
})


def _replay_quiet_flags(steps: Sequence[Any]) -> tuple[bool, ...]:
    """Salience is an allowlist: only steps that produce a visible beat keep
    their recorded pacing. Streamed chunks AND administrative lifecycle events
    (context, provider round-trips, message boundaries) fast-forward --
    otherwise the model's first-turn latency stacks into dead air."""
    flags = []
    for step in steps:
        event = step.get("event") if isinstance(step, Mapping) else None
        event_type = str(event.get("eventType") or "") if isinstance(event, Mapping) else ""
        flags.append(bool(event_type) and event_type not in _SALIENT_EVENT_TYPES)
    return tuple(flags)


def _replay_step_delay_ms(
    index: int,
    *,
    timestamps: Sequence[int | None],
    playback_timing: ReplayPlaybackTiming,
    step_delay_ms: int,
    time_scale: float,
    max_step_delay_ms: int | None,
    quiet_step_delay_ms: int | None = None,
    min_step_delay_ms: int | None = None,
    quiet_flags: Sequence[bool] = (),
) -> float:
    if playback_timing == "fixed":
        return float(step_delay_ms)
    if index >= len(timestamps) - 1:
        return 0
    current = timestamps[index]
    following = timestamps[index + 1]
    if current is None or following is None:
        return 0
    delay_ms = max(0, following - current) / time_scale
    if max_step_delay_ms is not None:
        delay_ms = min(delay_ms, max_step_delay_ms)
    next_is_quiet = index + 1 < len(quiet_flags) and quiet_flags[index + 1]
    if quiet_step_delay_ms is not None and next_is_quiet:
        delay_ms = min(delay_ms, quiet_step_delay_ms)
    if min_step_delay_ms is not None and not next_is_quiet:
        # salient beats get breathing room: recorded bursts (several tool
        # events within milliseconds) should not machine-gun past at speed
        delay_ms = max(delay_ms, min_step_delay_ms)
    return delay_ms


def _step_result_with_timing(
    result: ReplayStepResult,
    timestamps: Sequence[int | None],
    index: int,
) -> ReplayStepResult:
    timestamp = timestamps[index] if index < len(timestamps) else None
    delay = _timestamp_delay_to_next_ms(timestamps, index)
    if timestamp is None and delay is None:
        return result
    return replace(result, timestamp_ms=timestamp, delay_ms_to_next=delay)


def _timestamp_delay_to_next_ms(timestamps: Sequence[int | None], index: int) -> int | None:
    if index >= len(timestamps) - 1:
        return None
    current = timestamps[index]
    following = timestamps[index + 1]
    if current is None or following is None:
        return None
    return max(0, following - current)


def _replay_step_timestamp_ms(step: Any) -> int | None:
    if not isinstance(step, Mapping):
        return None
    kind = step.get("type", step.get("kind"))
    if kind == "event":
        return _first_timestamp_ms(_mapping_timestamp_ms(step.get("event")), _mapping_timestamp_ms(step))
    if kind == "raw_event":
        return _first_timestamp_ms(_mapping_timestamp_ms(step), _mapping_timestamp_ms(step.get("raw")))
    if kind == "render_plan":
        return _first_timestamp_ms(_render_plan_step_timestamp_ms(step), _mapping_timestamp_ms(step))
    if kind == "mutations":
        return _first_timestamp_ms(_mapping_timestamp_ms(step.get("event")), _mapping_timestamp_ms(step))
    return _mapping_timestamp_ms(step)


def _render_plan_step_timestamp_ms(step: Mapping[str, Any]) -> int | None:
    nested_plan = step.get("plan")
    plan_payload = nested_plan if isinstance(nested_plan, Mapping) else step
    timestamp = _mapping_timestamp_ms(plan_payload)
    if timestamp is not None:
        return timestamp
    requests = plan_payload.get("requests")
    if not isinstance(requests, Sequence) or isinstance(requests, str | bytes):
        return None
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        timestamp = _first_timestamp_ms(_mapping_timestamp_ms(request.get("event")), _mapping_timestamp_ms(request))
        if timestamp is not None:
            return timestamp
    return None


def _first_timestamp_ms(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _mapping_timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    return _timestamp_ms_value(value.get("timestampMs", value.get("timestamp_ms")))


def _timestamp_ms_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp >= 0 else None


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


def _suite_review_relative_path(root: Path, path: Path) -> str:
    if root.is_dir():
        return path.relative_to(root).as_posix()
    return Path(path.name).as_posix()


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
        "timing": replay_step_timing_summary(result.steps),
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
        "timing": replay_step_timing_summary(screenshot.step for screenshot in rendered_screenshots),
        "frames": [screenshot.to_dict() for screenshot in rendered_screenshots],
        "metadata": result.metadata,
    }


def replay_step_timing_summary(steps: Iterable[ReplayStepResult]) -> dict[str, Any]:
    replay_steps = tuple(steps)
    timestamps = [
        step.timestamp_ms
        for step in replay_steps
        if step.timestamp_ms is not None
    ]
    delays = [
        step.delay_ms_to_next
        for step in replay_steps
        if step.delay_ms_to_next is not None
    ]
    summary: dict[str, Any] = {
        "stepCount": len(replay_steps),
        "timedStepCount": len(timestamps),
        "untimedStepCount": len(replay_steps) - len(timestamps),
        "delayCount": len(delays),
    }
    if timestamps:
        summary.update(
            {
                "firstTimestampMs": timestamps[0],
                "lastTimestampMs": timestamps[-1],
                "durationMs": max(0, timestamps[-1] - timestamps[0]),
                "minTimestampMs": min(timestamps),
                "maxTimestampMs": max(timestamps),
            }
        )
    if delays:
        summary.update(
            {
                "totalDelayMs": sum(delays),
                "maxDelayMs": max(delays),
            }
        )
    return summary


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
    timing_text = _replay_frame_review_timing_text(manifest.get("timing"))
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
    <div class="meta">{frame_count} frames &middot; {timing_text} &middot; schema {schema}</div>
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
      let playing = false;

      function stopPlayback() {{
        if (timer !== null) window.clearTimeout(timer);
        timer = null;
        playing = false;
        playPause.textContent = "PLAY";
      }}

      function frameDelay(index) {{
        const frame = frames[index] || {{}};
        const delay = Number(frame.delayMsToNext);
        if (Number.isFinite(delay) && delay >= 0) return Math.max(80, Math.min(delay, 30000));
        return 900;
      }}

      function scheduleNextFrame() {{
        if (!playing) return;
        timer = window.setTimeout(() => {{
          stepFrame(1);
          scheduleNextFrame();
        }}, frameDelay(current));
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
          `timestamp ${{frame.timestampText || "n/a"}}`,
          `next ${{frame.delayText || "n/a"}}`,
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
        if (playing) {{
          stopPlayback();
          return;
        }}
        playing = true;
        playPause.textContent = "PAUSE";
        scheduleNextFrame();
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
    render_summary = _replay_result_render_summary(result)
    manifest = {
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
    route_counts = render_summary["routeCounts"]
    if route_counts:
        manifest["routes"] = sorted(route_counts)
        manifest["routeCounts"] = route_counts
    renderer_counts = render_summary["rendererCounts"]
    if renderer_counts:
        manifest["renderers"] = sorted(renderer_counts)
        manifest["rendererCounts"] = renderer_counts
    visual_continuity_summary = _renderer_chunks_continuity_summary(renderer_chunks.get("chunks"))
    if visual_continuity_summary:
        manifest["visualContinuitySummary"] = visual_continuity_summary
    capture_summary = result.metadata.get("captureSummary") if isinstance(result.metadata, Mapping) else None
    if isinstance(capture_summary, Mapping):
        manifest["captureSummary"] = dict(capture_summary)
    trajectory_source: dict[str, Any] = {
        "screenshotCount": len(rendered_screenshots),
        "intentCount": int(render_intents["intentCount"]),
    }
    if isinstance(capture_summary, Mapping):
        trajectory_source["eventSummary"] = capture_summary
    if route_counts:
        trajectory_source["routes"] = sorted(route_counts)
        trajectory_source["routeCounts"] = route_counts
    if renderer_counts:
        trajectory_source["renderers"] = sorted(renderer_counts)
        trajectory_source["rendererCounts"] = renderer_counts
    if visual_continuity_summary:
        trajectory_source["visualContinuitySummary"] = visual_continuity_summary
    trajectory_coverage = _trajectory_coverage_from_summary(trajectory_source)
    if trajectory_coverage:
        manifest["trajectoryCoverage"] = trajectory_coverage
    return manifest


def replay_review_bundle_index_html(manifest: Mapping[str, Any]) -> str:
    title = str(manifest.get("replayName") or "replay review")
    schema = escape(str(manifest.get("schema", "")))
    cards = "\n".join(
        _replay_review_metric(label, value) for label, value in _replay_review_metric_items(manifest)
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


def _replay_review_metric_items(manifest: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    items: list[tuple[str, Any]] = [
        ("steps", manifest.get("stepCount", 0)),
        ("scene revision", manifest.get("sceneRevision", 0)),
        ("frames", manifest.get("frameCount", 0)),
        ("screenshots", manifest.get("screenshotCount", 0)),
        ("renderer contexts", manifest.get("contextCount", 0)),
        ("render intents", manifest.get("intentCount", 0)),
        ("renderer prompts", manifest.get("promptCount", 0)),
        ("renderer chunks", manifest.get("chunkCount", 0)),
    ]
    capture_summary = manifest.get("captureSummary")
    if isinstance(capture_summary, Mapping):
        duration_ms = capture_summary.get("durationMs")
        if isinstance(duration_ms, int | float):
            items.append(("captured duration", f"{duration_ms} ms"))
        event_types = _joined_summary_values(capture_summary.get("eventTypes"))
        if event_types:
            items.append(("captured event types", event_types))
        phases = _joined_summary_values(capture_summary.get("phases"))
        if phases:
            items.append(("captured phases", phases))
        sources = _joined_summary_values(capture_summary.get("sources"))
        if sources:
            items.append(("captured sources", sources))
    visual_summary = manifest.get("visualContinuitySummary")
    if isinstance(visual_summary, Mapping):
        visual_anchors = _int_value(visual_summary.get("maxVisualAnchorCount"))
        if visual_anchors:
            items.append(("visual anchors", visual_anchors))
        active_animations = _int_value(visual_summary.get("maxActiveAnimationCount"))
        if active_animations:
            items.append(("active animations", active_animations))
        continuity_anchors = _joined_summary_values(visual_summary.get("anchors"))
        if continuity_anchors:
            items.append(("continuity anchors", continuity_anchors))
        continuity_effects = _joined_summary_values(visual_summary.get("effects"))
        if continuity_effects:
            items.append(("continuity effects", continuity_effects))
        style_motifs = _joined_summary_values(visual_summary.get("styleMotifs"))
        if style_motifs:
            items.append(("style motifs", style_motifs))
    trajectory_coverage = manifest.get("trajectoryCoverage")
    if isinstance(trajectory_coverage, Mapping):
        signals = _joined_summary_values(trajectory_coverage.get("signals"))
        if signals:
            items.append(("trajectory signals", signals))
        gaps = _joined_summary_values(trajectory_coverage.get("gaps"))
        if gaps:
            items.append(("trajectory gaps", gaps))
        areas = _joined_summary_values(trajectory_coverage.get("topLevelAreas"))
        if areas:
            items.append(("trajectory areas", areas))
    return tuple(items)


def _joined_summary_values(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ", ".join(str(item) for item in value[:6] if item)


def _joined_count_mapping(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    items = [
        f"{key}: {count}"
        for key, count in sorted(value.items())
        if isinstance(key, str) and key and isinstance(count, int) and not isinstance(count, bool)
    ]
    return ", ".join(items[:6])


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


def write_replay_suite_review_bundle(
    path: str | Path,
    replay_path: str | Path,
    *,
    screenshot_width: int = 1280,
    screenshot_height: int = 900,
    render_chunk_size: int = 4,
    style: str | None = None,
    state_factory: Callable[[], GibsonServerState] | None = None,
) -> dict[str, Any]:
    review_path = Path(path)
    root = Path(replay_path)
    review_path.mkdir(parents=True, exist_ok=True)
    files = discover_replay_files(root)
    style_pack = style_pack_from_name(style)
    entries: list[dict[str, Any]] = []
    for replay_file in files:
        relative_path = _suite_review_relative_path(root, replay_file)
        bundle_dir = review_path / "files" / Path(relative_path).with_suffix("")
        state = state_factory() if state_factory is not None else GibsonServerState(style_pack=style_pack)
        event_summary: dict[str, Any] = {}
        try:
            replay_data = load_replay_file(replay_file)
            event_summary = _replay_data_event_summary(replay_data)
            result = run_replay_data(
                replay_data,
                state,
                capture_frames=True,
                capture_renderer_contexts=True,
            )
            screenshots = capture_replay_frame_screenshots(
                result,
                bundle_dir / "frames",
                width=screenshot_width,
                height=screenshot_height,
            )
            bundle_manifest = write_replay_review_bundle(
                bundle_dir,
                result,
                screenshots,
                render_chunk_size=render_chunk_size,
            )
            entries.append(
                _replay_suite_review_entry(
                    review_path,
                    relative_path,
                    bundle_dir,
                    bundle_manifest,
                    event_summary=event_summary,
                )
            )
        except ReplayExpectationError as error:
            entry: dict[str, Any] = {
                "path": relative_path,
                "ok": False,
                "error": str(error),
                "expectationFailures": [failure.to_dict() for failure in error.failures],
            }
            if event_summary:
                entry["eventSummary"] = event_summary
            entries.append(entry)
        except Exception as error:
            entry = {"path": relative_path, "ok": False, "error": str(error)}
            if event_summary:
                entry["eventSummary"] = event_summary
            entries.append(entry)
        finally:
            state.pipeline.stop()
    manifest = replay_suite_review_bundle_manifest(
        root,
        entries,
        render_chunk_size=render_chunk_size,
        split_manifest=_load_split_manifest(root),
    )
    (review_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (review_path / "index.html").write_text(replay_suite_review_index_html(manifest), encoding="utf-8")
    return manifest


def replay_suite_review_bundle_manifest(
    root: str | Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    render_chunk_size: int = 4,
    split_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rendered_entries = [dict(entry) for entry in entries]
    failed = sum(1 for entry in rendered_entries if not entry.get("ok"))
    manifest: dict[str, Any] = {
        "schema": "harn-gibson.replay-suite-review.v1",
        "root": Path(root).as_posix(),
        "ok": failed == 0,
        "total": len(rendered_entries),
        "failed": failed,
        "renderChunkSize": render_chunk_size,
        "artifacts": {"overview": "index.html", "manifest": "manifest.json", "files": "files/"},
        "files": rendered_entries,
    }
    manifest["summary"] = _replay_suite_review_summary(rendered_entries)
    if isinstance(split_manifest, Mapping):
        manifest["splitManifest"] = dict(split_manifest)
        capture_summary = split_manifest.get("captureSummary")
        if isinstance(capture_summary, Mapping):
            manifest["captureSummary"] = dict(capture_summary)
    return manifest


def replay_suite_review_index_html(manifest: Mapping[str, Any]) -> str:
    root = str(manifest.get("root") or "replay suite")
    schema = escape(str(manifest.get("schema", "")))
    metrics = "\n".join(
        _replay_review_metric(label, value) for label, value in _replay_suite_review_metric_items(manifest)
    )
    cards = "\n".join(_replay_suite_review_file_card(file) for file in _replay_suite_review_files(manifest))
    if not cards:
        cards = '      <section class="empty">No replay files were reviewed.</section>'
    manifest_data = _html_script_json(manifest)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(root)} replay suite review</title>
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
    .metric strong {{ display: block; color: #ffcf63; font-size: 24px; line-height: 1; overflow-wrap: anywhere; }}
    .metric span {{ display: block; margin-top: 7px; color: #b8fff3; font-size: 12px; text-transform: uppercase; }}
    .files {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }}
    article {{
      display: grid;
      gap: 10px;
      border: 1px solid rgba(35, 255, 214, 0.28);
      background: rgba(5, 16, 19, 0.88);
      padding: 14px;
    }}
    article[data-ok="false"] {{ border-color: rgba(255, 95, 157, 0.48); }}
    h2 {{ margin: 0; color: #ffcf63; font-size: 14px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .summary {{ color: #b8fff3; font-size: 12px; line-height: 1.45; }}
    a {{ color: #d9fff7; text-decoration: none; overflow-wrap: anywhere; }}
    a:hover {{ color: #ffcf63; }}
    code {{ color: #ffcf63; }}
    .error {{ color: #ff5f9d; font-size: 12px; overflow-wrap: anywhere; }}
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
    <h1>{escape(root)} replay suite review</h1>
    <div class="meta">schema {schema}</div>
  </header>
  <main>
    <section class="metrics" aria-label="Replay suite metrics">
{metrics}
    </section>
    <section class="files" aria-label="Reviewed replay files">
{cards}
    </section>
  </main>
  <script>
    window.__gibsonReplaySuiteReview = {manifest_data};
  </script>
</body>
</html>
"""


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


def _replay_suite_review_entry(
    review_path: Path,
    relative_path: str,
    bundle_dir: Path,
    bundle_manifest: Mapping[str, Any],
    *,
    event_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    bundle_index = _relative_artifact_path(review_path, bundle_dir / "index.html")
    entry: dict[str, Any] = {
        "path": relative_path,
        "ok": True,
        "review": bundle_index,
        "manifest": _relative_artifact_path(review_path, bundle_dir / "manifest.json"),
        "stepCount": bundle_manifest.get("stepCount", 0),
        "sceneRevision": bundle_manifest.get("sceneRevision", 0),
        "frameCount": bundle_manifest.get("frameCount", 0),
        "screenshotCount": bundle_manifest.get("screenshotCount", 0),
        "contextCount": bundle_manifest.get("contextCount", 0),
        "intentCount": bundle_manifest.get("intentCount", 0),
        "promptCount": bundle_manifest.get("promptCount", 0),
        "chunkCount": bundle_manifest.get("chunkCount", 0),
    }
    if event_summary:
        entry["eventSummary"] = dict(event_summary)
    route_counts = bundle_manifest.get("routeCounts")
    if isinstance(route_counts, Mapping):
        entry["routes"] = sorted(str(key) for key in route_counts if isinstance(key, str) and key)
        entry["routeCounts"] = dict(route_counts)
    renderer_counts = bundle_manifest.get("rendererCounts")
    if isinstance(renderer_counts, Mapping):
        entry["renderers"] = sorted(str(key) for key in renderer_counts if isinstance(key, str) and key)
        entry["rendererCounts"] = dict(renderer_counts)
    metadata = bundle_manifest.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("eventLogChunk"), Mapping):
        entry["eventLogChunk"] = dict(metadata["eventLogChunk"])
    capture_summary = bundle_manifest.get("captureSummary")
    if isinstance(capture_summary, Mapping):
        entry["captureSummary"] = dict(capture_summary)
    visual_continuity_summary = bundle_manifest.get("visualContinuitySummary")
    if isinstance(visual_continuity_summary, Mapping):
        entry["visualContinuitySummary"] = dict(visual_continuity_summary)
    trajectory_coverage = bundle_manifest.get("trajectoryCoverage")
    if isinstance(trajectory_coverage, Mapping):
        entry["trajectoryCoverage"] = dict(trajectory_coverage)
    return entry


def _load_split_manifest(root: Path) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "harn-gibson.event-log-split.v1":
        return None
    return payload


def _relative_artifact_path(root: Path, path: Path) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _replay_suite_review_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rendered_entries = tuple(entries)
    summary: dict[str, Any] = {
        "fileCount": len(rendered_entries),
        "okCount": sum(1 for entry in rendered_entries if entry.get("ok")),
        "failedCount": sum(1 for entry in rendered_entries if not entry.get("ok")),
        "stepCount": sum(_int_value(entry.get("stepCount")) for entry in rendered_entries),
        "frameCount": sum(_int_value(entry.get("frameCount")) for entry in rendered_entries),
        "screenshotCount": sum(_int_value(entry.get("screenshotCount")) for entry in rendered_entries),
        "contextCount": sum(_int_value(entry.get("contextCount")) for entry in rendered_entries),
        "intentCount": sum(_int_value(entry.get("intentCount")) for entry in rendered_entries),
        "promptCount": sum(_int_value(entry.get("promptCount")) for entry in rendered_entries),
        "chunkCount": sum(_int_value(entry.get("chunkCount")) for entry in rendered_entries),
    }
    event_summary = _merge_replay_event_summaries(
        entry.get("eventSummary") for entry in rendered_entries if isinstance(entry.get("eventSummary"), Mapping)
    )
    if event_summary:
        summary["eventSummary"] = event_summary
    route_counts = _merge_count_mappings(
        entry.get("routeCounts") for entry in rendered_entries if isinstance(entry.get("routeCounts"), Mapping)
    )
    if route_counts:
        summary["routes"] = sorted(route_counts)
        summary["routeCounts"] = route_counts
    renderer_counts = _merge_count_mappings(
        entry.get("rendererCounts")
        for entry in rendered_entries
        if isinstance(entry.get("rendererCounts"), Mapping)
    )
    if renderer_counts:
        summary["renderers"] = sorted(renderer_counts)
        summary["rendererCounts"] = renderer_counts
    visual_continuity_summary = _merge_visual_continuity_summaries(
        entry.get("visualContinuitySummary")
        for entry in rendered_entries
        if isinstance(entry.get("visualContinuitySummary"), Mapping)
    )
    if visual_continuity_summary:
        summary["visualContinuitySummary"] = visual_continuity_summary
    trajectory_coverage = _trajectory_coverage_from_summary(summary)
    if trajectory_coverage:
        summary["trajectoryCoverage"] = trajectory_coverage
    return summary


def _replay_suite_review_metric_items(manifest: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    items: list[tuple[str, Any]] = [
        ("files", manifest.get("total", 0)),
        ("failed", manifest.get("failed", 0)),
        ("renderer chunk size", manifest.get("renderChunkSize", 0)),
    ]
    files = _replay_suite_review_files(manifest)
    items.extend(
        [
            ("steps", sum(_int_value(file.get("stepCount")) for file in files)),
            ("frames", sum(_int_value(file.get("frameCount")) for file in files)),
            ("screenshots", sum(_int_value(file.get("screenshotCount")) for file in files)),
            ("renderer contexts", sum(_int_value(file.get("contextCount")) for file in files)),
            ("render intents", sum(_int_value(file.get("intentCount")) for file in files)),
            ("renderer prompts", sum(_int_value(file.get("promptCount")) for file in files)),
        ]
    )
    capture_summary = manifest.get("captureSummary")
    if isinstance(capture_summary, Mapping):
        duration_ms = capture_summary.get("durationMs")
        if isinstance(duration_ms, int | float):
            items.append(("captured duration", f"{duration_ms} ms"))
        event_types = _joined_summary_values(capture_summary.get("eventTypes"))
        if event_types:
            items.append(("captured event types", event_types))
        phases = _joined_summary_values(capture_summary.get("phases"))
        if phases:
            items.append(("captured phases", phases))
        sources = _joined_summary_values(capture_summary.get("sources"))
        if sources:
            items.append(("captured sources", sources))
    summary = manifest.get("summary")
    if isinstance(summary, Mapping):
        event_summary = summary.get("eventSummary")
        if isinstance(event_summary, Mapping):
            event_types = _joined_summary_values(event_summary.get("eventTypes"))
            if event_types:
                items.append(("reviewed event types", event_types))
            phases = _joined_summary_values(event_summary.get("phases"))
            if phases:
                items.append(("reviewed phases", phases))
            tools = event_summary.get("tools")
            if isinstance(tools, Mapping):
                tool_names = _joined_summary_values(tools.get("toolNames"))
                if tool_names:
                    items.append(("reviewed tools", tool_names))
                command_count = _int_value(tools.get("commandCount"))
                if command_count:
                    items.append(("reviewed command fields", command_count))
                failed_tool_results = _int_value(tools.get("failedToolResultCount"))
                if failed_tool_results:
                    items.append(("reviewed failed tools", failed_tool_results))
            touched_files = event_summary.get("touchedFiles")
            if isinstance(touched_files, Mapping):
                touched_count = _int_value(touched_files.get("count"))
                if touched_count:
                    items.append(("reviewed touched files", touched_count))
                top_levels = _joined_count_mapping(touched_files.get("topLevelCounts"))
                if top_levels:
                    items.append(("reviewed touched areas", top_levels))
                touched_paths = _joined_summary_values(touched_files.get("paths"))
                if touched_paths:
                    items.append(("reviewed touched paths", touched_paths))
        routes = _joined_summary_values(summary.get("routes"))
        if routes:
            items.append(("reviewed routes", routes))
        renderers = _joined_summary_values(summary.get("renderers"))
        if renderers:
            items.append(("reviewed renderers", renderers))
        visual_summary = summary.get("visualContinuitySummary")
        if isinstance(visual_summary, Mapping):
            visual_anchors = _int_value(visual_summary.get("maxVisualAnchorCount"))
            if visual_anchors:
                items.append(("reviewed visual anchors", visual_anchors))
            active_animations = _int_value(visual_summary.get("maxActiveAnimationCount"))
            if active_animations:
                items.append(("reviewed active animations", active_animations))
            continuity_anchors = _joined_summary_values(visual_summary.get("anchors"))
            if continuity_anchors:
                items.append(("reviewed continuity anchors", continuity_anchors))
            continuity_effects = _joined_summary_values(visual_summary.get("effects"))
            if continuity_effects:
                items.append(("reviewed continuity effects", continuity_effects))
        trajectory_coverage = summary.get("trajectoryCoverage")
        if isinstance(trajectory_coverage, Mapping):
            signals = _joined_summary_values(trajectory_coverage.get("signals"))
            if signals:
                items.append(("trajectory signals", signals))
            gaps = _joined_summary_values(trajectory_coverage.get("gaps"))
            if gaps:
                items.append(("trajectory gaps", gaps))
            areas = _joined_summary_values(trajectory_coverage.get("topLevelAreas"))
            if areas:
                items.append(("trajectory areas", areas))
    return tuple(items)


def _replay_suite_review_files(manifest: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    files = manifest.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(file for file in files if isinstance(file, Mapping))


def _replay_suite_review_file_card(file: Mapping[str, Any]) -> str:
    path = str(file.get("path") or "unknown replay")
    ok = bool(file.get("ok"))
    href = file.get("review")
    link = (
        f'<a href="{escape(str(href))}">Open Review<br><code>{escape(str(href))}</code></a>'
        if isinstance(href, str) and href
        else ""
    )
    error = str(file.get("error") or "")
    error_html = f'      <div class="error">{escape(error)}</div>' if error else ""
    chunk = file.get("eventLogChunk")
    chunk_text = ""
    if isinstance(chunk, Mapping):
        chunk_text = (
            f"chunk {escape(str(chunk.get('chunkIndex', '?')))} / {escape(str(chunk.get('chunkCount', '?')))} "
            f"&middot; offsets {escape(str(chunk.get('startEventOffset', '?')))}-"
            f"{escape(str(chunk.get('endEventOffset', '?')))}"
        )
    summary_parts = [
        f"steps {escape(str(file.get('stepCount', 0)))}",
        f"frames {escape(str(file.get('frameCount', 0)))}",
        f"contexts {escape(str(file.get('contextCount', 0)))}",
        f"prompts {escape(str(file.get('promptCount', 0)))}",
    ]
    event_summary = file.get("eventSummary")
    if isinstance(event_summary, Mapping):
        event_types = _joined_summary_values(event_summary.get("eventTypes"))
        if event_types:
            summary_parts.append(f"events {escape(event_types)}")
        tools = event_summary.get("tools")
        if isinstance(tools, Mapping):
            tool_names = _joined_summary_values(tools.get("toolNames"))
            if tool_names:
                summary_parts.append(f"tools {escape(tool_names)}")
        touched_files = event_summary.get("touchedFiles")
        if isinstance(touched_files, Mapping):
            top_levels = _joined_count_mapping(touched_files.get("topLevelCounts"))
            if top_levels:
                summary_parts.append(f"touched {escape(top_levels)}")
    routes = _joined_summary_values(file.get("routes"))
    if routes:
        summary_parts.append(f"routes {escape(routes)}")
    renderers = _joined_summary_values(file.get("renderers"))
    if renderers:
        summary_parts.append(f"renderers {escape(renderers)}")
    visual_summary = file.get("visualContinuitySummary")
    if isinstance(visual_summary, Mapping):
        anchors = _joined_summary_values(visual_summary.get("anchors"))
        if anchors:
            summary_parts.append(f"continuity {escape(anchors)}")
        effects = _joined_summary_values(visual_summary.get("effects"))
        if effects:
            summary_parts.append(f"effects {escape(effects)}")
    trajectory_coverage = file.get("trajectoryCoverage")
    if isinstance(trajectory_coverage, Mapping):
        signals = _joined_summary_values(trajectory_coverage.get("signals"))
        if signals:
            summary_parts.append(f"signals {escape(signals)}")
    if chunk_text:
        summary_parts.append(chunk_text)
    summary = " / ".join(summary_parts)
    return f"""      <article data-ok="{str(ok).lower()}">
        <h2>{escape(path)}</h2>
        <div class="summary">{summary}</div>
{error_html}
        {link}
      </article>"""


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) else 0


MAX_CHUNK_SUMMARY_VALUES = 12


def _renderer_context_chunk_payload(index: int, contexts: tuple[ReplayRendererContext, ...]) -> dict[str, Any]:
    prompts = [renderer_prompt_from_context(context.context, context_index=context.index) for context in contexts]
    summary = _renderer_context_chunk_summary(prompts, contexts)
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


def _renderer_context_chunk_summary(
    prompts: list[dict[str, Any]],
    contexts: tuple[ReplayRendererContext, ...],
) -> dict[str, Any]:
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
    visual_anchor_count = 0
    world_binding_count = 0
    active_animation_count = 0
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
        visual_anchor_count = max(visual_anchor_count, _coerce_int(metadata.get("visualAnchorCount"), 0))
        world_binding_count = max(world_binding_count, _coerce_int(metadata.get("worldBindingCount"), 0))
        active_animation_count = max(active_animation_count, _coerce_int(metadata.get("activeAnimationCount"), 0))
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
        "visualAnchorCount": visual_anchor_count,
        "worldBindingCount": world_binding_count,
        "activeAnimationCount": active_animation_count,
        **_renderer_context_chunk_continuity_summary(contexts),
    }


def _renderer_context_chunk_continuity_summary(contexts: tuple[ReplayRendererContext, ...]) -> dict[str, Any]:
    anchors: list[str] = []
    effects: list[str] = []
    targets: list[str] = []
    renderers: list[str] = []
    motifs: list[str] = []
    for context in contexts:
        continuity = _mapping_value(context.context.get("visualContinuity"))
        for anchor in _mapping_list(continuity.get("anchors")):
            anchor_id = anchor.get("id")
            if isinstance(anchor_id, str) and anchor_id:
                _append_bounded_unique(anchors, anchor_id)
        _extend_bounded_unique(effects, continuity.get("recentEffects"))
        _extend_bounded_unique(targets, continuity.get("recentTargets"))
        _extend_bounded_unique(renderers, continuity.get("recentRenderers"))
        style = _mapping_value(continuity.get("style"))
        _extend_bounded_unique(motifs, style.get("motifs"))
    payload: dict[str, Any] = {}
    if anchors:
        payload["continuityAnchors"] = anchors
    if effects:
        payload["continuityEffects"] = effects
    if targets:
        payload["continuityTargets"] = targets
    if renderers:
        payload["continuityRenderers"] = renderers
    if motifs:
        payload["styleMotifs"] = motifs
    return payload


def _renderer_chunks_continuity_summary(chunks: Any) -> dict[str, Any]:
    chunk_items = [chunk for chunk in chunks if isinstance(chunk, Mapping)] if isinstance(chunks, list) else []
    return _merge_visual_continuity_summaries(
        {
            "maxVisualAnchorCount": _coerce_int(chunk.get("visualAnchorCount"), 0),
            "maxWorldBindingCount": _coerce_int(chunk.get("worldBindingCount"), 0),
            "maxActiveAnimationCount": _coerce_int(chunk.get("activeAnimationCount"), 0),
            "anchors": chunk.get("continuityAnchors"),
            "effects": chunk.get("continuityEffects"),
            "targets": chunk.get("continuityTargets"),
            "renderers": chunk.get("continuityRenderers"),
            "styleMotifs": chunk.get("styleMotifs"),
        }
        for chunk in chunk_items
    )


def _merge_visual_continuity_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    anchors: list[str] = []
    effects: list[str] = []
    targets: list[str] = []
    renderers: list[str] = []
    motifs: list[str] = []
    max_visual_anchor_count = 0
    max_world_binding_count = 0
    max_active_animation_count = 0
    for summary in summaries:
        max_visual_anchor_count = max(max_visual_anchor_count, _int_value(summary.get("maxVisualAnchorCount")))
        max_world_binding_count = max(max_world_binding_count, _int_value(summary.get("maxWorldBindingCount")))
        max_active_animation_count = max(max_active_animation_count, _int_value(summary.get("maxActiveAnimationCount")))
        _extend_bounded_unique(anchors, summary.get("anchors"))
        _extend_bounded_unique(effects, summary.get("effects"))
        _extend_bounded_unique(targets, summary.get("targets"))
        _extend_bounded_unique(renderers, summary.get("renderers"))
        _extend_bounded_unique(motifs, summary.get("styleMotifs"))
    payload: dict[str, Any] = {}
    if max_visual_anchor_count:
        payload["maxVisualAnchorCount"] = max_visual_anchor_count
    if max_world_binding_count:
        payload["maxWorldBindingCount"] = max_world_binding_count
    if max_active_animation_count:
        payload["maxActiveAnimationCount"] = max_active_animation_count
    if anchors:
        payload["anchors"] = anchors
    if effects:
        payload["effects"] = effects
    if targets:
        payload["targets"] = targets
    if renderers:
        payload["renderers"] = renderers
    if motifs:
        payload["styleMotifs"] = motifs
    return payload


def _trajectory_coverage_from_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    event_summary = _mapping_value(summary.get("eventSummary"))
    event_type_counts = _mapping_value(event_summary.get("eventTypeCounts"))
    tools = _mapping_value(event_summary.get("tools"))
    touched_files = _mapping_value(event_summary.get("touchedFiles"))
    top_level_counts = _mapping_value(touched_files.get("topLevelCounts"))
    route_counts = _mapping_value(summary.get("routeCounts"))
    renderer_counts = _mapping_value(summary.get("rendererCounts"))
    visual_summary = _mapping_value(summary.get("visualContinuitySummary"))

    event_count = _coerce_int(event_summary.get("eventCount"), 0)
    duration_ms = _coerce_int(event_summary.get("durationMs"), 0)
    command_count = _coerce_int(tools.get("commandCount"), 0)
    failed_tool_result_count = _coerce_int(tools.get("failedToolResultCount"), 0)
    runtime_error_count = _coerce_int(event_type_counts.get("runtime_error"), 0)
    browser_input_count = _coerce_int(event_type_counts.get("browser_input"), 0)
    touched_file_count = _coerce_int(touched_files.get("count"), 0)
    screenshot_count = _int_value(summary.get("screenshotCount"))
    intent_count = _int_value(summary.get("intentCount"))
    visual_anchor_count = _int_value(visual_summary.get("maxVisualAnchorCount"))
    active_animation_count = _int_value(visual_summary.get("maxActiveAnimationCount"))

    event_types = _positive_count_mapping_keys(event_type_counts)
    top_level_areas = _positive_count_mapping_keys(top_level_counts)
    routes = _positive_count_mapping_keys(route_counts)
    renderers = _positive_count_mapping_keys(renderer_counts)
    effects = _string_list(visual_summary.get("effects"))

    has_any_coverage = any(
        (
            event_count,
            command_count,
            touched_file_count,
            screenshot_count,
            intent_count,
            visual_anchor_count,
            active_animation_count,
            routes,
            renderers,
            effects,
        )
    )
    if not has_any_coverage:
        return {}

    signals: list[str] = []
    gaps: list[str] = []
    if event_count:
        signals.append("events")
    else:
        gaps.append("no_events")
    if command_count:
        signals.append("commands")
    elif event_count:
        gaps.append("no_commands")
    if failed_tool_result_count:
        signals.append("failed_tools")
    if runtime_error_count:
        signals.append("runtime_errors")
    if browser_input_count:
        signals.append("browser_input")
    if touched_file_count:
        signals.append("touched_files")
    elif event_count:
        gaps.append("no_touched_files")
    if len(top_level_areas) >= 2:
        signals.append("top_level_spread")
    if routes:
        signals.append("renderer_routes")
    elif event_count:
        gaps.append("no_renderer_routes")
    if intent_count:
        signals.append("renderer_intents")
    if renderers:
        signals.append("renderer_plans")
    elif event_count:
        gaps.append("no_renderer_plans")
    if visual_anchor_count:
        signals.append("visual_anchors")
    elif event_count:
        gaps.append("no_visual_anchors")
    if active_animation_count:
        signals.append("active_animations")
    if effects:
        signals.append("visual_effects")
    if screenshot_count:
        signals.append("screenshots")
    elif event_count:
        gaps.append("no_screenshots")

    payload: dict[str, Any] = {
        "schema": "harn-gibson.trajectory-coverage.v1",
        "eventCount": event_count,
        "eventTypes": event_types,
        "commandCount": command_count,
        "failedToolResultCount": failed_tool_result_count,
        "runtimeErrorCount": runtime_error_count,
        "browserInputCount": browser_input_count,
        "touchedFileCount": touched_file_count,
        "topLevelAreaCount": len(top_level_areas),
        "topLevelAreas": top_level_areas,
        "routes": routes,
        "renderers": renderers,
        "visualAnchorCount": visual_anchor_count,
        "activeAnimationCount": active_animation_count,
        "effectCount": len(effects),
        "screenshotCount": screenshot_count,
        "signals": signals,
    }
    if duration_ms:
        payload["durationMs"] = duration_ms
    if intent_count:
        payload["intentCount"] = intent_count
    if gaps:
        payload["gaps"] = gaps
    return payload


def _positive_count_mapping_keys(value: Mapping[str, Any]) -> list[str]:
    return sorted(
        key
        for key, count in value.items()
        if isinstance(key, str) and key and _coerce_int(count, 0) > 0
    )


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _extend_bounded_unique(items: list[str], values: Any) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        if isinstance(value, str | int | float | bool):
            _append_bounded_unique(items, str(value))


def _append_bounded_unique(items: list[str], item: str) -> None:
    if len(items) >= MAX_CHUNK_SUMMARY_VALUES:
        return
    _append_unique(items, item)


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
    continuity_anchors = _badge_row(chunk.get("continuityAnchors"), "target")
    continuity_effects = _badge_row(chunk.get("continuityEffects"), "effect")
    continuity_targets = _badge_row(chunk.get("continuityTargets"), "target")
    continuity_renderers = _badge_row(chunk.get("continuityRenderers"), "renderer")
    style_motifs = _badge_row(chunk.get("styleMotifs"), "style")
    prompt_preview = _renderer_chunk_prompt_preview(chunk)
    return f"""    <article>
      <h2>chunk #{index} / contexts <code>{context_range}</code></h2>
      <div class="summary">
        <span>contexts <code>{escape(str(chunk.get("contextCount", 0)))}</code></span>
        <span>prompts <code>{escape(str(chunk.get("promptCount", 0)))}</code></span>
        <span>requests <code>{escape(str(chunk.get("requestCount", 0)))}</code></span>
        <span>timeline <code>{timeline_text}</code></span>
        <span>visual anchors <code>{escape(str(chunk.get("visualAnchorCount", 0)))}</code></span>
        <span>active animations <code>{escape(str(chunk.get("activeAnimationCount", 0)))}</code></span>
        <span>prompt chars <code>{escape(str(chunk.get("messageChars", 0)))}</code></span>
        <span>context chars <code>{escape(str(chunk.get("contextChars", 0)))}</code></span>
      </div>
      <div class="badge-set"><span class="badge-label">modes</span><div class="badge-row">{modes}</div></div>
      <div class="badge-set"><span class="badge-label">styles</span><div class="badge-row">{display_styles}</div></div>
      <div class="badge-set"><span class="badge-label">events</span><div class="badge-row">{event_types}</div></div>
      <div class="badge-set"><span class="badge-label">routes</span><div class="badge-row">{routes}</div></div>
      <div class="badge-set">
        <span class="badge-label">continuity anchors</span><div class="badge-row">{continuity_anchors}</div>
      </div>
      <div class="badge-set">
        <span class="badge-label">continuity effects</span><div class="badge-row">{continuity_effects}</div>
      </div>
      <div class="badge-set">
        <span class="badge-label">continuity targets</span><div class="badge-row">{continuity_targets}</div>
      </div>
      <div class="badge-set">
        <span class="badge-label">continuity renderers</span><div class="badge-row">{continuity_renderers}</div>
      </div>
      <div class="badge-set">
        <span class="badge-label">style motifs</span><div class="badge-row">{style_motifs}</div>
      </div>
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
    timestamp = escape(str(frame_data["timestampText"]))
    delay = escape(str(frame_data["delayText"]))
    step_line = (
        f"frame <code>{escape(str(index))}</code> &middot; "
        f"step <code>{kind}</code> &middot; revision <code>{revision}</code>"
    )
    route_line = f"updates <code>{updates}</code> &middot; route <code>{route}</code>"
    timing_line = f"timestamp <code>{timestamp}</code> &middot; next <code>{delay}</code>"
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
        <span>{timing_line}</span>
        <span{nonblank_class}>canvas nonblank: <code>{escape(str(frame_data["nonblankText"]))}</code></span>
      </figcaption>
    </figure>"""


def _replay_frame_review_timing_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "timing unavailable"
    timed = value.get("timedStepCount")
    total = value.get("stepCount")
    duration = value.get("durationMs")
    delay_total = value.get("totalDelayMs")
    delay_count = value.get("delayCount")
    parts: list[str] = []
    if _is_int_like(timed) and _is_int_like(total):
        parts.append(f"timed {int(timed)} / {int(total)}")
    if _is_int_like(duration):
        parts.append(f"duration {int(duration)} ms")
    if _is_int_like(delay_total) and _is_int_like(delay_count):
        parts.append(f"delays {int(delay_count)} / {int(delay_total)} ms")
    if not parts:
        return "timing unavailable"
    return " &middot; ".join(escape(part) for part in parts)


def _is_int_like(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
    timestamp_ms = step.get("timestampMs")
    delay_ms_to_next = step.get("delayMsToNext")
    timestamp_value = timestamp_ms if isinstance(timestamp_ms, int) and not isinstance(timestamp_ms, bool) else None
    delay_value = (
        delay_ms_to_next
        if isinstance(delay_ms_to_next, int) and not isinstance(delay_ms_to_next, bool)
        else None
    )
    return {
        "index": str(index),
        "src": _replay_frame_image_src(screenshot.get("path"), output_path),
        "kind": str(step.get("kind", "")),
        "revision": str(step.get("sceneRevision", screenshot.get("sceneRevision", ""))),
        "updates": str(step.get("updates", "")),
        "route": str(step.get("route", "n/a")),
        "timestampMs": timestamp_value,
        "delayMsToNext": delay_value,
        "timestampText": f"{timestamp_value} ms" if timestamp_value is not None else "n/a",
        "delayText": f"{delay_value} ms" if delay_value is not None else "n/a",
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
    "ReplayPlaybackTiming",
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
    "play_replay_data",
    "play_replay_file",
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
    "replay_suite_review_bundle_manifest",
    "replay_suite_review_index_html",
    "replay_step_timing_summary",
    "replay_timeline_from_result",
    "run_replay_data",
    "run_replay_file",
    "run_replay_suite",
    "split_replay_data_from_event_log",
    "split_replay_fixture_filename",
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
    "write_replay_suite_review_bundle",
    "write_replay_timeline",
    "write_replay_baseline",
    "write_scene",
]
