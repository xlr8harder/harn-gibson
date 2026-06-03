from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.model_renderer import (
    MODEL_RENDERER_REQUEST_SCHEMA,
    PromptCommandModelClient,
    PromptedModelRenderer,
    model_renderer_from_env,
    model_renderer_request_payload,
    render_plan_from_model_response,
)
from harn_gibson.rendering import (
    RendererContext,
    RendererContextBuilder,
    RenderInputBatch,
    RenderPipeline,
    RenderPlan,
    RenderRequest,
)
from harn_gibson.scene import SceneEngine
from harn_gibson.sinks import EventBuffer


def event(sequence: int = 1, event_type: str = "tool_call") -> GibsonEvent:
    return GibsonEvent.from_raw(
        {"type": event_type, "toolName": "bash", "input": {"command": "uv run pytest tests/test_model_renderer.py"}},
        sequence,
        timestamp_ms=sequence * 100,
    )


def render_plan_text(*, target_id: str = "status", renderer: str = "fixture-model") -> str:
    return json.dumps(
        {
            "schema": "harn-gibson.render-plan.v1",
            "metadata": {"renderer": renderer, "intent": "model patch"},
            "steps": [
                {
                    "mutations": [
                        {
                            "op": "patch",
                            "targetId": target_id,
                            "props": {"text": "model:tool_call", "phase": "lifecycle", "tone": "cyan"},
                        }
                    ]
                }
            ],
        }
    )


def test_model_response_parser_accepts_modelish_wrappers() -> None:
    request = RenderRequest(event())
    plain = render_plan_text()
    noisy = f"thinking omitted {plain} trailing text"
    fenced = f"```json\n{plain}\n```"
    content_text = {"content": plain}
    content_object = {"content": json.loads(plain)}
    direct_object = json.loads(plain)
    nested_object = {"plan": json.loads(plain)}
    escaped = (
        'prefix {"steps":[{"mutations":[],"note":"brace } and quote \\" stay in string"}],'
        '"metadata":{"intent":"escaped"}} suffix'
    )

    assert render_plan_from_model_response(plain, (request,)).metadata["renderer"] == "fixture-model"
    assert render_plan_from_model_response(noisy, (request,), renderer_id="fallback-name").metadata["renderer"] == (
        "fixture-model"
    )
    assert render_plan_from_model_response(fenced, (request,)).steps[0].mutations[0].target_id == "status"
    assert render_plan_from_model_response(content_text, (request,)).steps[0].mutations[0].props["text"] == (
        "model:tool_call"
    )
    assert render_plan_from_model_response(content_object, (request,)).metadata["intent"] == "model patch"
    assert render_plan_from_model_response(direct_object, (request,)).steps[0].mutations
    assert render_plan_from_model_response(nested_object, (request,)).metadata["renderer"] == "fixture-model"
    assert render_plan_from_model_response(escaped, (request,)).metadata["intent"] == "escaped"

    with pytest.raises(ValueError, match="JSON string or object"):
        render_plan_from_model_response(123, (request,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="content must be"):
        render_plan_from_model_response({"content": []}, (request,))
    with pytest.raises(ValueError, match="must contain a JSON object"):
        render_plan_from_model_response("no json here", (request,))
    with pytest.raises(ValueError, match="must contain a JSON object"):
        render_plan_from_model_response("```json", (request,))
    with pytest.raises(ValueError, match="must contain a JSON object"):
        render_plan_from_model_response("```json\nnot closed", (request,))
    with pytest.raises(ValueError, match="must contain a JSON object"):
        render_plan_from_model_response("{", (request,))
    with pytest.raises(ValueError, match="steps list"):
        render_plan_from_model_response("```json\n{}", (request,))
    with pytest.raises(json.JSONDecodeError):
        render_plan_from_model_response("```json\n{bad}\n```", (request,))


def test_prompt_command_model_client_exchanges_prompt_payload(tmp_path: Path) -> None:
    success = tmp_path / "prompt_command.py"
    success.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
assert payload["schema"] == "harn-gibson.model-renderer-request.v1"
assert payload["messageCount"] == 1
assert payload["messages"][0]["role"] == "user"
assert payload["metadata"]["mode"] == "test"
json.dump({"steps": [{"mutations": []}], "metadata": {"intent": "ok"}}, sys.stdout)
""".lstrip(),
        encoding="utf-8",
    )
    failing = tmp_path / "failing_prompt_command.py"
    failing.write_text(
        "import sys\nsys.stdout.write('partial')\nsys.stderr.write('failed')\nsys.exit(9)\n",
        encoding="utf-8",
    )
    silent_failing = tmp_path / "silent_prompt_command.py"
    silent_failing.write_text("import sys\nsys.exit(8)\n", encoding="utf-8")

    clamped_client = PromptCommandModelClient((sys.executable, str(success)), timeout_seconds=0)
    client = PromptCommandModelClient((sys.executable, str(success)), timeout_seconds=2)
    payload = model_renderer_request_payload(({"role": "user", "content": "hello"},), metadata={"mode": "test"})
    response = client.complete(({"role": "user", "content": "hello"},), metadata={"mode": "test"})
    renderer = model_renderer_from_env(json.dumps([sys.executable, str(success)]), "250")

    assert clamped_client.timeout_seconds == 0.001
    assert payload["schema"] == MODEL_RENDERER_REQUEST_SCHEMA
    assert payload["messageCount"] == 1
    assert json.loads(response)["metadata"]["intent"] == "ok"
    assert renderer is not None
    assert isinstance(renderer.client, PromptCommandModelClient)
    assert renderer.client.command == (sys.executable, str(success))
    assert renderer.client.timeout_seconds == 0.25
    assert model_renderer_from_env(None) is None
    with pytest.raises(ValueError, match="cannot be empty"):
        PromptCommandModelClient(())
    with pytest.raises(RuntimeError, match="exited with code 9"):
        PromptCommandModelClient((sys.executable, str(failing)), timeout_seconds=2).complete(
            ({"role": "user", "content": "hello"},)
        )
    with pytest.raises(RuntimeError, match="exited with code 8"):
        PromptCommandModelClient((sys.executable, str(silent_failing)), timeout_seconds=2).complete(
            ({"role": "user", "content": "hello"},)
        )


def test_prompted_model_renderer_applies_valid_plan_and_records_prompt_metadata() -> None:
    calls: list[dict[str, Any]] = []

    class RecordingClient:
        def complete(self, messages: object, *, metadata: object | None = None) -> str:
            calls.append({"messages": messages, "metadata": metadata})
            return render_plan_text(renderer="recording-model")

    scene = SceneEngine()
    pipeline = RenderPipeline(scene=scene, buffer=EventBuffer(), renderer=PromptedModelRenderer(RecordingClient()))
    result = pipeline.submit(RenderRequest(event(3, "tool_call")))

    prompt_metadata = result.updates[0]["renderIntent"]["metadata"]["rendererPrompt"]
    assert scene.state.primitives["status"].props["text"] == "model:tool_call"
    assert result.updates[0]["renderIntent"]["renderer"] == "recording-model"
    assert calls[0]["messages"][0]["role"] == "system"
    assert "Renderer context JSON" in calls[0]["messages"][1]["content"]
    assert calls[0]["metadata"]["renderer"] == "model"
    assert prompt_metadata["schema"] == "harn-gibson.renderer-prompt.v1"
    assert prompt_metadata["mode"] == "compaction"
    assert prompt_metadata["messageCount"] == 2
    assert prompt_metadata["messageChars"] > 0
    assert prompt_metadata["contextChars"] > 0


def test_prompted_model_renderer_preserves_safe_warning_diagnostics() -> None:
    class WarningClient:
        def complete(self, _messages: object, *, metadata: object | None = None) -> dict[str, Any]:
            return {
                "metadata": {"intent": "invent toy"},
                "steps": [
                    {
                        "mutations": [
                            {
                                "op": "upsert",
                                "primitive": {
                                    "id": "model-hologram",
                                    "kind": "neural_mist",
                                    "region": "stage",
                                    "props": {"tone": "cyan"},
                                },
                            }
                        ]
                    }
                ],
            }

    scene = SceneEngine()
    batch = RenderInputBatch.from_requests((RenderRequest(event(4, "tool_result")),))
    context = RendererContextBuilder().build(
        batch,
        scene.state,
        RenderPipeline(scene=scene, buffer=EventBuffer()).catalog,
    )
    plan = PromptedModelRenderer(WarningClient(), renderer_id="warning-model").render_with_context(
        batch.requests,
        scene.state,
        context,
    )

    diagnostics = plan.metadata["renderPlanDiagnostics"]
    assert diagnostics["status"] == "accepted_with_warnings"
    assert diagnostics["issues"][0]["code"] == "unsupported_primitive_kind"
    assert plan.metadata["renderer"] == "warning-model"
    assert plan.steps[0].mutations[0].primitive is not None
    assert plan.steps[0].mutations[0].primitive.kind == "neural_mist"


def test_prompted_model_renderer_rejects_unsafe_or_failed_model_output() -> None:
    class UnsafeClient:
        def complete(self, _messages: object, *, metadata: object | None = None) -> str:
            return render_plan_text(target_id="missing-panel")

    class FailingClient:
        def complete(self, _messages: object, *, metadata: object | None = None) -> str:
            raise RuntimeError("provider unavailable")

    class EmptyFallback:
        def render_with_context(
            self,
            requests: tuple[RenderRequest, ...],
            _scene: object,
            _context: RendererContext,
        ) -> RenderPlan:
            return RenderPlan(requests, (), {"renderer": "empty"})

    scene = SceneEngine()
    unsafe_result = RenderPipeline(
        scene=scene,
        buffer=EventBuffer(),
        renderer=PromptedModelRenderer(UnsafeClient(), renderer_id="unsafe-model"),
    ).submit(RenderRequest(event(5, "tool_result")))
    failure_plan = PromptedModelRenderer(FailingClient(), renderer_id="failing-model").render(
        (RenderRequest(event(6, "tool_call")),),
        SceneEngine().state,
    )
    empty_plan = PromptedModelRenderer(FailingClient(), renderer_id="empty-model").render((), SceneEngine().state)
    empty_fallback_plan = PromptedModelRenderer(
        FailingClient(),
        renderer_id="empty-fallback",
        fallback=EmptyFallback(),  # type: ignore[arg-type]
    ).render((RenderRequest(event(7, "tool_call")),), SceneEngine().state)
    direct_error_plan = PromptedModelRenderer(FailingClient(), renderer_id="direct-error")._error_plan(
        (RenderRequest(event(8, "tool_call")),),
        SceneEngine().state,
        RendererContext("rolling", {}, {}, {}, {}),
        message="direct failure",
        details="x" * 5000,
    )

    metadata = unsafe_result.updates[0]["renderIntent"]["metadata"]
    trace = scene.state.primitives["trace-log"].props["text"][0]
    assert metadata["fallbackRenderer"] == "deterministic"
    assert metadata["rendererError"]["message"] == "model renderer returned unsafe render plan"
    assert metadata["renderPlanDiagnostics"]["status"] == "rejected"
    assert metadata["renderPlanDiagnostics"]["issues"][0]["code"] == "patch_target_missing"
    assert scene.state.primitives["status"].props["text"] == "renderer:error"
    assert trace["eventType"] == "renderer_error"
    assert "patch_target_missing" in trace["details"]
    assert failure_plan.metadata["rendererError"]["message"].startswith("model renderer failed")
    assert failure_plan.steps[-1].mutations[-1].target_id == "trace-log"
    assert empty_plan.steps == ()
    assert empty_plan.metadata["fallbackRenderer"] == "deterministic"
    assert empty_fallback_plan.steps[0].event_index == 0
    assert empty_fallback_plan.steps[0].mutations[-1].target_id == "trace-log"
    assert "rendererPrompt" not in direct_error_plan.metadata
    assert len(direct_error_plan.metadata["rendererError"]["details"]) == 4000
    assert direct_error_plan.metadata["rendererError"]["details"].endswith("...")
