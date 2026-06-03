"""External renderer process adapter."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RendererContext,
    RenderInputBatch,
    RenderPlan,
    RenderRequest,
    RenderStep,
)
from harn_gibson.scene import SceneMutation, SceneState, mutation_from_mapping

DEFAULT_RENDERER_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class ExternalRenderer:
    """Renderer adapter that exchanges JSON with a command on stdin/stdout."""

    command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_RENDERER_TIMEOUT_SECONDS
    renderer_id: str = "external"
    fallback: DeterministicSceneRenderer = field(default_factory=DeterministicSceneRenderer)

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("external renderer command cannot be empty")
        self.timeout_seconds = max(0.001, self.timeout_seconds)

    def render(self, requests: Sequence[RenderRequest], scene: SceneState) -> RenderPlan:
        batch = RenderInputBatch.from_requests(tuple(requests))
        context = RendererContext(
            mode="rolling",
            project={},
            catalog={},
            scene=scene.to_dict(),
            render_input=batch.to_dict(),
        )
        return self.render_with_context(batch.requests, scene, context)

    def render_with_context(
        self,
        requests: Sequence[RenderRequest],
        scene: SceneState,
        context: RendererContext,
    ) -> RenderPlan:
        bound_requests = tuple(requests)
        payload = external_renderer_payload(bound_requests, scene, context)
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(payload) + "\n",
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                return self._error_plan(
                    bound_requests,
                    scene,
                    context,
                    message=f"external renderer exited with code {completed.returncode}",
                    details=_renderer_process_details(completed.stdout, completed.stderr),
                )
            return render_plan_from_external_response(
                _json_from_stdout(completed.stdout),
                bound_requests,
                renderer_id=self.renderer_id,
            )
        except Exception as error:
            return self._error_plan(
                bound_requests,
                scene,
                context,
                message=f"external renderer failed: {error}",
                details=repr(error),
            )

    def _error_plan(
        self,
        requests: tuple[RenderRequest, ...],
        scene: SceneState,
        context: RendererContext,
        *,
        message: str,
        details: str,
    ) -> RenderPlan:
        metadata = {
            "renderer": self.renderer_id,
            "fallbackRenderer": "deterministic",
            "rendererError": {
                "message": _clip_text(message, 500),
                "details": _clip_text(details, 4000),
            },
        }
        if not requests:
            return RenderPlan((), (), metadata)
        base_plan = self.fallback.render_with_context(requests, scene, context)
        error_mutations = _renderer_error_mutations(requests[-1], message, details)
        if not base_plan.steps:
            return RenderPlan(
                requests,
                (RenderStep(error_mutations, event_index=len(requests) - 1),),
                metadata,
            )
        steps = list(base_plan.steps)
        final_step = steps[-1]
        steps[-1] = RenderStep(
            mutations=(*final_step.mutations, *error_mutations),
            delay_ms=final_step.delay_ms,
            start_offset_ms=final_step.start_offset_ms,
            event_index=final_step.event_index,
        )
        return RenderPlan(requests, tuple(steps), metadata)


def external_renderer_payload(
    requests: Sequence[RenderRequest],
    scene: SceneState,
    context: RendererContext,
) -> dict[str, Any]:
    return {
        "schema": "harn-gibson.external-renderer-request.v1",
        "requests": [request.to_dict() for request in requests],
        "scene": scene.to_dict(),
        "context": context.to_dict(),
    }


def render_plan_from_external_response(
    value: Any,
    requests: Sequence[RenderRequest],
    *,
    renderer_id: str = "external",
) -> RenderPlan:
    if not isinstance(value, Mapping):
        raise ValueError("renderer response must be a JSON object")
    nested_plan = value.get("plan")
    plan_payload = nested_plan if isinstance(nested_plan, Mapping) else value
    steps_value = plan_payload.get("steps")
    if not isinstance(steps_value, list):
        raise ValueError("renderer response must include a steps list")
    steps = tuple(_render_step_from_mapping(step, index) for index, step in enumerate(steps_value))
    metadata = dict(plan_payload.get("metadata") or {})
    metadata.setdefault("renderer", renderer_id)
    return RenderPlan(requests=tuple(requests), steps=steps, metadata=metadata)


def parse_renderer_command(value: str) -> tuple[str, ...]:
    text = value.strip()
    if not text:
        raise ValueError("external renderer command cannot be empty")
    if text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("external renderer command JSON must be an array of strings") from error
        if not isinstance(payload, list) or not payload:
            raise ValueError("external renderer command JSON must be a non-empty array of strings")
        if not all(isinstance(item, str) and item for item in payload):
            raise ValueError("external renderer command JSON must contain only non-empty strings")
        return tuple(payload)
    command = tuple(shlex.split(text))
    if not command or not all(command):
        raise ValueError("external renderer command cannot be empty")
    return command


def renderer_timeout_seconds_from_env(value: str | None) -> float:
    if value is None or not value.strip():
        return DEFAULT_RENDERER_TIMEOUT_SECONDS
    try:
        milliseconds = float(value)
    except ValueError as error:
        raise ValueError("HARN_GIBSON_RENDERER_TIMEOUT_MS must be a positive number") from error
    if milliseconds <= 0:
        raise ValueError("HARN_GIBSON_RENDERER_TIMEOUT_MS must be a positive number")
    return milliseconds / 1000


def external_renderer_from_env(command_value: str | None, timeout_value: str | None = None) -> ExternalRenderer | None:
    if not command_value:
        return None
    return ExternalRenderer(
        parse_renderer_command(command_value),
        timeout_seconds=renderer_timeout_seconds_from_env(timeout_value),
    )


def _json_from_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        preview = _clip_text(stdout.strip(), 1000)
        raise ValueError(f"renderer stdout must be JSON: {preview!r}") from error


def _render_step_from_mapping(value: Any, index: int) -> RenderStep:
    if not isinstance(value, Mapping):
        raise ValueError(f"renderer step {index} must be an object")
    return RenderStep(
        mutations=_mutations_from_value(value.get("mutations"), index),
        delay_ms=int(value.get("delayMs", value.get("delay_ms", 0))),
        start_offset_ms=int(value.get("startOffsetMs", value.get("start_offset_ms", 0))),
        event_index=_optional_int(value.get("eventIndex", value.get("event_index"))),
    )


def _mutations_from_value(value: Any, step_index: int) -> tuple[SceneMutation, ...]:
    if not isinstance(value, list):
        raise ValueError(f"renderer step {step_index} mutations must be a list")
    mutations = []
    for mutation in value:
        if not isinstance(mutation, Mapping):
            raise ValueError(f"renderer step {step_index} mutation must be an object")
        mutations.append(mutation_from_mapping(mutation))
    return tuple(mutations)


def _renderer_error_mutations(request: RenderRequest, message: str, details: str) -> tuple[SceneMutation, ...]:
    event = request.event
    trace_entry = {
        "sequence": event.sequence,
        "eventType": "renderer_error",
        "title": "Renderer error",
        "message": _clip_text(message, 500),
        "details": _clip_text(details, 4000),
        "traceback": _clip_text(details, 4000),
    }
    return (
        SceneMutation(
            op="patch",
            target_id="status",
            props={"text": "renderer:error", "phase": "lifecycle", "tone": "red"},
        ),
        SceneMutation(
            op="append_log",
            entry={
                "sequence": event.sequence,
                "phase": "lifecycle",
                "eventType": "renderer_error",
                "title": "Renderer error",
                "summary": _clip_text(message, 240),
            },
        ),
        SceneMutation(op="patch", target_id="trace-log", props={"text": [trace_entry]}),
    )


def _renderer_process_details(stdout: str, stderr: str) -> str:
    parts = []
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.strip()}")
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.strip()}")
    return "\n\n".join(parts)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 3)]}..."


__all__ = [
    "ExternalRenderer",
    "external_renderer_from_env",
    "external_renderer_payload",
    "parse_renderer_command",
    "renderer_timeout_seconds_from_env",
    "render_plan_from_external_response",
]
