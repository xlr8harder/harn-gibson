"""Prompt-driven renderer adapter for model-backed renderers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from harn_gibson.catalog import VisualCatalog, default_visual_catalog
from harn_gibson.external_renderer import (
    parse_renderer_command,
    render_plan_from_external_response,
    renderer_timeout_seconds_from_env,
)
from harn_gibson.renderer_prompt import RENDERER_PROMPT_SCHEMA, renderer_prompt_from_context
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RendererContext,
    RenderInputBatch,
    RenderPlan,
    RenderRequest,
    RenderStep,
    render_plan_diagnostics_payload,
    render_plan_has_validation_errors,
    validate_render_plan,
)
from harn_gibson.scene import SceneMutation, SceneState

MODEL_RENDERER_REQUEST_SCHEMA = "harn-gibson.model-renderer-request.v1"
DEFAULT_MODEL_RENDERER_TIMEOUT_SECONDS = 30.0


class RendererModelClient(Protocol):
    """Completion client used by `PromptedModelRenderer`."""

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | Mapping[str, Any]: ...


@dataclass(slots=True)
class PromptCommandModelClient:
    """Runs a local command that receives renderer prompt messages and returns model text."""

    command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_MODEL_RENDERER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("model renderer command cannot be empty")
        self.timeout_seconds = max(0.001, self.timeout_seconds)

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        completed = subprocess.run(
            self.command,
            input=json.dumps(model_renderer_request_payload(messages, metadata=metadata)) + "\n",
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"model renderer command exited with code {completed.returncode}: "
                f"{_process_details(completed.stdout, completed.stderr)}"
            )
        return completed.stdout


@dataclass(slots=True)
class PromptedModelRenderer:
    """Renderer that asks a model-like client for a JSON render plan."""

    client: RendererModelClient
    renderer_id: str = "model"
    fallback: DeterministicSceneRenderer = field(default_factory=DeterministicSceneRenderer)
    catalog: VisualCatalog = field(default_factory=default_visual_catalog)

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
        prompt = renderer_prompt_from_context(context.to_dict())
        metadata = _prompt_metadata(prompt)
        try:
            response = self.client.complete(
                prompt["messages"],
                metadata={
                    "renderer": self.renderer_id,
                    "prompt": metadata,
                },
            )
            plan = render_plan_from_model_response(response, bound_requests, renderer_id=self.renderer_id)
            plan = _merge_plan_metadata(plan, metadata)
            issues = validate_render_plan(plan, scene, self.catalog)
            if render_plan_has_validation_errors(issues):
                return self._error_plan(
                    bound_requests,
                    scene,
                    context,
                    message="model renderer returned unsafe render plan",
                    details=_validation_details(issues),
                    metadata={
                        "renderPlanDiagnostics": render_plan_diagnostics_payload(issues, status="rejected"),
                        "rendererPrompt": metadata,
                    },
                )
            if issues:
                return RenderPlan(
                    plan.requests,
                    plan.steps,
                    {
                        **plan.metadata,
                        "renderPlanDiagnostics": render_plan_diagnostics_payload(issues),
                    },
                )
            return plan
        except Exception as error:
            return self._error_plan(
                bound_requests,
                scene,
                context,
                message=f"model renderer failed: {error}",
                details=repr(error),
                metadata={"rendererPrompt": metadata},
            )

    def _error_plan(
        self,
        requests: tuple[RenderRequest, ...],
        scene: SceneState,
        context: RendererContext,
        *,
        message: str,
        details: str,
        metadata: dict[str, Any] | None = None,
    ) -> RenderPlan:
        plan_metadata = {
            "renderer": self.renderer_id,
            "fallbackRenderer": "deterministic",
            "rendererError": {
                "message": _clip_text(message, 500),
                "details": _clip_text(details, 4000),
            },
        }
        if metadata:
            plan_metadata.update(metadata)
        if not requests:
            return RenderPlan((), (), plan_metadata)
        base_plan = self.fallback.render_with_context(requests, scene, context)
        error_mutations = _renderer_error_mutations(requests[-1], message, details)
        if not base_plan.steps:
            return RenderPlan(
                requests,
                (RenderStep(error_mutations, event_index=len(requests) - 1),),
                plan_metadata,
            )
        steps = list(base_plan.steps)
        final_step = steps[-1]
        steps[-1] = RenderStep(
            mutations=(*final_step.mutations, *error_mutations),
            delay_ms=final_step.delay_ms,
            start_offset_ms=final_step.start_offset_ms,
            event_index=final_step.event_index,
        )
        return RenderPlan(requests, tuple(steps), plan_metadata)


def model_renderer_request_payload(
    messages: Sequence[Mapping[str, str]],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": MODEL_RENDERER_REQUEST_SCHEMA,
        "messageCount": len(messages),
        "messages": [dict(message) for message in messages],
        "metadata": dict(metadata or {}),
    }


def model_renderer_from_env(
    command_value: str | None,
    timeout_value: str | None = None,
) -> PromptedModelRenderer | None:
    if not command_value:
        return None
    return PromptedModelRenderer(
        PromptCommandModelClient(
            parse_renderer_command(command_value),
            timeout_seconds=renderer_timeout_seconds_from_env(timeout_value),
        ),
        renderer_id="model-command",
    )


def render_plan_from_model_response(
    value: str | Mapping[str, Any],
    requests: Sequence[RenderRequest],
    *,
    renderer_id: str = "model",
) -> RenderPlan:
    if isinstance(value, str):
        payload = _json_from_model_text(value)
    elif isinstance(value, Mapping):
        content = value.get("content")
        if "steps" not in value and "plan" not in value and content is not None:
            if isinstance(content, str):
                payload = _json_from_model_text(content)
            elif isinstance(content, Mapping):
                payload = content
            else:
                raise ValueError("model response content must be a JSON string or object")
        else:
            payload = value
    else:
        raise ValueError("model response must be a JSON string or object")
    return render_plan_from_external_response(payload, requests, renderer_id=renderer_id)


def _json_from_model_text(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fenced = _first_fenced_json(stripped)
    if fenced is not None:
        return json.loads(fenced)
    extracted = _extract_first_json_object(stripped)
    if extracted is None:
        raise ValueError(f"model response must contain a JSON object: {_clip_text(stripped, 1000)!r}")
    return json.loads(extracted)


def _first_fenced_json(text: str) -> str | None:
    fence_start = text.find("```")
    if fence_start < 0:
        return None
    content_start = text.find("\n", fence_start)
    if content_start < 0:
        return None
    fence_end = text.find("```", content_start + 1)
    if fence_end < 0:
        return None
    return text[content_start + 1 : fence_end].strip()


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _merge_plan_metadata(plan: RenderPlan, prompt_metadata: dict[str, Any]) -> RenderPlan:
    metadata = dict(plan.metadata)
    metadata.setdefault("renderer", "model")
    metadata["rendererPrompt"] = prompt_metadata
    return RenderPlan(plan.requests, plan.steps, metadata)


def _prompt_metadata(prompt: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(prompt.get("metadata") or {})
    return {
        "schema": RENDERER_PROMPT_SCHEMA,
        "mode": str(prompt.get("mode") or "rolling"),
        "messageCount": int(metadata.get("messageCount", 0)),
        "messageChars": int(metadata.get("messageChars", 0)),
        "contextChars": int(metadata.get("contextChars", 0)),
    }


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


def _validation_details(issues: Sequence[Any]) -> str:
    return json.dumps(render_plan_diagnostics_payload(issues, status="rejected"), indent=2, sort_keys=True)


def _process_details(stdout: str, stderr: str) -> str:
    parts = []
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.strip()}")
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.strip()}")
    return _clip_text("\n\n".join(parts), 4000)


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 3)]}..."


__all__ = [
    "DEFAULT_MODEL_RENDERER_TIMEOUT_SECONDS",
    "MODEL_RENDERER_REQUEST_SCHEMA",
    "PromptCommandModelClient",
    "PromptedModelRenderer",
    "RendererModelClient",
    "model_renderer_from_env",
    "model_renderer_request_payload",
    "render_plan_from_model_response",
]
