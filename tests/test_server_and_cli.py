from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harn_gibson import (
    BrowserScreenshotResult,
    EventRouter,
    EventRouteRule,
    RendererEventInterest,
    RenderPlan,
    RenderRequest,
    RenderStep,
    SceneMutation,
    cli,
)
from harn_gibson.server import (
    BrowserInputQueue,
    GibsonServerState,
    HarnBridgeState,
    apply_event_to_scene,
    browser_input_event_payload,
    build_state_from_env,
    create_server,
    diagnostic_event_payload,
    enqueue_browser_input,
    event_from_payload,
    format_sse,
    health_payload,
    publish_diagnostic_event,
    renderer_interest_from_env,
    route_rules_from_env,
    submit_event_to_renderer,
)


def request_text(url: str, data: bytes | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def start_server() -> tuple[ThreadingHTTPServer, str]:
    state = GibsonServerState()
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_http_server_routes() -> None:
    server, base = start_server()
    try:
        assert request_text(f"{base}/")[0:2] == (200, "text/html; charset=utf-8")
        assert "GIBSON LINK" in request_text(f"{base}/index.html")[2]
        assert "Tracebacks" in request_text(f"{base}/index.html")[2]
        assert request_text(f"{base}/assets/app.css")[1] == "text/css; charset=utf-8"
        assert request_text(f"{base}/assets/app.js")[1] == "application/javascript; charset=utf-8"
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["ok"] is True
        assert health["events"] == 0
        assert health["sceneRevision"] == 0
        assert health["renderMode"] == "blocking"
        assert health["pendingRenderJobs"] == 0
        assert health["streams"] == {}
        assert health["inputBridge"] == {
            "pendingInputs": 0,
            "inputPollerSeen": False,
            "inputPollerConnected": False,
            "lastInputPollMs": None,
            "lastInputPollAgeMs": None,
            "lastInputDeliveryMs": None,
            "pollCount": 0,
            "deliveredInputs": 0,
        }
        assert json.loads(request_text(f"{base}/scene")[2])["schema"] == "harn-gibson.scene.v1"
        catalog = json.loads(request_text(f"{base}/catalog")[2])
        assert catalog["schema"] == "harn-gibson.visual-catalog.v1"
        assert any(entry["id"] == "text_stream" for entry in catalog["primitives"])
        assert json.loads(request_text(f"{base}/missing")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/bad", b"{}")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/events", b"{")[2]) == {"error": "invalid json"}
        assert json.loads(request_text(f"{base}/events", b"[]")[2]) == {"error": "event payload must be an object"}
        assert json.loads(request_text(f"{base}/events", b'{"sequence":1}')[2]) == {
            "error": "event payload missing eventType"
        }
        assert request_text(f"{base}/input/next")[0:2] == (204, "")
        assert json.loads(request_text(f"{base}/input", b"{")[2]) == {"error": "invalid json"}
        assert json.loads(request_text(f"{base}/input", b"[]")[2]) == {"error": "input payload must be an object"}
        assert json.loads(request_text(f"{base}/input", b'{"message":1}')[2]) == {"error": "message must be a string"}
        assert json.loads(request_text(f"{base}/input", b'{"message":"hi","deliverAs":1}')[2]) == {
            "error": "deliverAs must be a string"
        }

        payload = {
            "sequence": 1,
            "timestampMs": 10,
            "source": "test",
            "eventType": "input",
            "phase": "before",
            "title": "Input intercept",
            "summary": "interactive input: hi",
            "payload": {"type": "input", "text": "hi", "source": "interactive"},
        }
        status, _content_type, body = request_text(f"{base}/events", json.dumps(payload).encode("utf-8"))
        assert status == 202
        assert json.loads(body) == {"ok": True, "renderMode": "blocking", "sceneRevision": 1}
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["ok"] is True
        assert health["events"] == 1
        assert health["sceneRevision"] == 1
        assert health["renderMode"] == "blocking"
        assert health["pendingRenderJobs"] == 0
        assert health["inputBridge"]["pendingInputs"] == 0
        assert health["inputBridge"]["inputPollerSeen"] is True
        assert health["inputBridge"]["inputPollerConnected"] is True
        assert health["inputBridge"]["pollCount"] == 1

        status, _content_type, body = request_text(
            f"{base}/input",
            json.dumps({"message": " launch sequence ", "deliverAs": "steer"}).encode("utf-8"),
        )
        accepted = json.loads(body)
        assert status == 202
        assert accepted["ok"] is True
        assert accepted["renderMode"] == "blocking"
        assert accepted["input"] == {
            "id": "input-1",
            "sequence": 1,
            "message": "launch sequence",
            "deliverAs": "steer",
        }
        assert accepted["pendingInputs"] == 1
        assert accepted["inputBridge"]["pendingInputs"] == 1
        assert accepted["inputBridge"]["inputPollerSeen"] is True
        assert accepted["inputBridge"]["deliveredInputs"] == 0
        assert json.loads(request_text(f"{base}/input/next")[2]) == accepted["input"]
        assert request_text(f"{base}/input/next")[0] == 204
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["inputBridge"]["pendingInputs"] == 0
        assert health["inputBridge"]["deliveredInputs"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_apply_event_to_scene_and_event_from_payload() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 3,
        "timestampMs": 33,
        "source": "unit",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash"},
        "recentContext": ["ctx"],
        "visualizationContext": ["scene"],
        "decisions": [{"block": True, "reason": "no"}],
    }

    event = event_from_payload(payload)
    update = apply_event_to_scene(payload, state)
    submit_result = submit_event_to_renderer(payload, state)

    assert event.event_type == "tool_call"
    assert event.recent_context == ("ctx",)
    assert update["schema"] == "harn-gibson.scene-update.v1"
    assert update["decisions"] == [{"block": True, "reason": "no"}]
    assert update["scene"]["revision"] == 1
    assert submit_result.scene_revision == 2


def test_diagnostic_event_payload_and_publish() -> None:
    state = GibsonServerState()
    payload = diagnostic_event_payload(
        10,
        event_type="runtime_error",
        severity="error",
        message="delivery failed",
        details="input=input-1",
        traceback_text="Traceback...",
    )
    result = publish_diagnostic_event(
        state,
        10,
        event_type="runtime_error",
        severity="error",
        message="delivery failed",
        details="input=input-1",
        traceback_text="Traceback...",
    )

    assert payload["eventType"] == "runtime_error"
    assert payload["payload"]["traceback"] == "Traceback..."
    assert result.scene_revision == 1
    assert state.scene.state.primitives["trace-log"].props["text"][0]["details"] == "input=input-1"


def test_stream_update_routes_to_local_scene_buffer() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 11,
        "timestampMs": 1100,
        "source": "unit",
        "eventType": "message_update",
        "phase": "during",
        "title": "Stream update",
        "summary": "assistant stream {type, delta}",
        "payload": {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "booting"}},
    }

    result = submit_event_to_renderer(payload, state)
    scene = state.scene.state

    assert result.scene_revision == 1
    assert "assistant-stream" in scene.primitives
    assert scene.primitives["assistant-stream"].props["text"] == "booting"
    assert scene.log == []
    assert state.router.stream_snapshot()["assistant-main"]["text"] == "booting"
    update = result.updates[0]
    assert update["renderPlan"]["metadata"]["route"]["route"] == "stream_buffer"
    assert update["renderRequests"][0]["route"] == "stream_buffer"


def test_empty_stream_update_routes_debug_only_without_scene_mutation() -> None:
    state = GibsonServerState()
    payload = {
        "sequence": 12,
        "timestampMs": 1200,
        "source": "unit",
        "eventType": "message_update",
        "phase": "during",
        "title": "Stream update",
        "summary": "assistant stream {type}",
        "payload": {"type": "message_update", "assistantMessageEvent": {"type": "ping"}},
    }

    result = submit_event_to_renderer(payload, state)

    assert result.scene_revision == 0
    assert state.scene.state.revision == 0
    assert state.router.stream_snapshot() == {}
    assert result.updates[0]["renderPlan"]["metadata"]["route"]["route"] == "debug_only"
    assert result.updates[0]["renderRequests"][0]["route"] == "debug_only"


def test_direct_scene_route_rule_bypasses_renderer_and_updates_scene() -> None:
    state = GibsonServerState(router=EventRouter(route_rules=(EventRouteRule("tool_result", "direct_scene", "local"),)))
    payload = {
        "sequence": 13,
        "timestampMs": 1300,
        "source": "unit",
        "eventType": "tool_result",
        "phase": "after",
        "title": "Tool result",
        "summary": "bash completed: ok",
        "payload": {"type": "tool_result", "toolName": "bash"},
        "decisions": [{"content": "ok"}],
    }

    result = submit_event_to_renderer(payload, state)
    update = result.updates[0]

    assert result.scene_revision == 1
    assert state.scene.state.primitives["status"].props["text"] == "after:tool_result"
    assert update["renderPlan"]["metadata"]["route"]["route"] == "direct_scene"
    assert update["renderRequests"][0]["route"] == "direct_scene"
    assert state.buffer.snapshot() == [update]


def test_drop_route_rule_accepts_without_scene_update() -> None:
    state = GibsonServerState(router=EventRouter(route_rules=(EventRouteRule("model_select", "drop", "sampled"),)))
    payload = {
        "sequence": 14,
        "timestampMs": 1400,
        "source": "unit",
        "eventType": "model_select",
        "phase": "lifecycle",
        "title": "Model select",
        "summary": "model selected",
        "payload": {"type": "model_select", "model": {"provider": "openai", "id": "test"}},
    }

    result = submit_event_to_renderer(payload, state)
    accepted = apply_event_to_scene(payload, state)

    assert result.updates == ()
    assert result.scene_revision is None
    assert state.scene.state.revision == 0
    assert state.buffer.snapshot() == []
    assert accepted == {"ok": True, "renderMode": "blocking", "sceneRevision": 0}


def test_renderer_advertised_interest_controls_default_routing() -> None:
    class SelectiveRenderer:
        event_interest = RendererEventInterest(event_types=("tool_call",), fallback_route="direct_scene")

        def __init__(self) -> None:
            self.requests: list[tuple[RenderRequest, ...]] = []

        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            self.requests.append(requests)
            return RenderPlan(
                requests=requests,
                steps=(
                    RenderStep(
                        (SceneMutation("append_log", entry={"eventType": "renderer", "summary": "renderer saw it"}),),
                        event_index=0,
                    ),
                ),
                metadata={"renderer": "selective"},
            )

    renderer = SelectiveRenderer()
    state = GibsonServerState(renderer=renderer)
    fallback_payload = {
        "sequence": 15,
        "timestampMs": 1500,
        "source": "unit",
        "eventType": "tool_result",
        "phase": "after",
        "title": "Tool result",
        "summary": "bash completed: ok",
        "payload": {"type": "tool_result", "toolName": "bash"},
    }
    renderer_payload = {
        "sequence": 16,
        "timestampMs": 1600,
        "source": "unit",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
    }

    fallback = submit_event_to_renderer(fallback_payload, state)
    rendered = submit_event_to_renderer(renderer_payload, state)

    assert renderer.requests[0][0].event.event_type == "tool_call"
    assert fallback.updates[0]["renderPlan"]["metadata"]["route"]["route"] == "direct_scene"
    assert fallback.updates[0]["renderPlan"]["metadata"]["route"]["metadata"]["rendererInterest"]["eventTypes"] == [
        "tool_call"
    ]
    assert rendered.updates[0]["renderPlan"]["metadata"] == {"renderer": "selective"}


def test_explicit_router_interest_is_not_overwritten_by_renderer() -> None:
    class InterestedRenderer:
        event_interest = RendererEventInterest(event_types=("tool_call",), fallback_route="direct_scene")

        def render(self, requests: tuple[RenderRequest, ...], _scene: object) -> RenderPlan:
            return RenderPlan(requests=requests, steps=(), metadata={"renderer": "unused"})

    router = EventRouter(renderer_interest=RendererEventInterest(event_types=("tool_result",), fallback_route="drop"))
    state = GibsonServerState(renderer=InterestedRenderer(), router=router)

    assert state.router.renderer_interest is router.renderer_interest
    assert state.router.renderer_interest.event_types == ("tool_result",)  # type: ignore[union-attr]


def test_event_from_payload_validation() -> None:
    state = GibsonServerState()
    for payload, message in (
        ({"eventType": "", "phase": "before", "payload": {}}, "missing eventType"),
        ({"eventType": "x", "phase": "bad", "payload": {}}, "invalid phase"),
        ({"eventType": "x", "phase": "before", "payload": []}, "missing payload object"),
    ):
        try:
            apply_event_to_scene(payload, state)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")


def test_browser_input_queue_and_payload_validation(monkeypatch: Any) -> None:
    state = GibsonServerState()
    queue = BrowserInputQueue()
    item = queue.enqueue(" hello ", "followUp")

    assert item.to_dict() == {"id": "input-1", "sequence": 1, "message": "hello", "deliverAs": "followUp"}
    assert queue.pending_count() == 1
    assert queue.pop() == item
    assert queue.pop() is None

    for message, deliver_as, error in (
        (" ", "followUp", "message cannot be empty"),
        ("hi", "later", "deliverAs must be followUp or steer"),
    ):
        try:
            queue.enqueue(message, deliver_as)
        except ValueError as exc:
            assert error in str(exc)
        else:
            raise AssertionError("expected ValueError")

    queued = enqueue_browser_input({"message": "abc"}, state)
    assert queued.deliver_as == "followUp"
    monkeypatch.setattr("harn_gibson.server.time.time", lambda: 12.3)
    payload = browser_input_event_payload(queued)
    assert payload["timestampMs"] == 12300
    assert payload["eventType"] == "browser_input"
    assert payload["summary"] == "gibson input queued: abc"
    assert "..." in browser_input_event_payload(queue.enqueue("x" * 120))["summary"]


def test_harn_bridge_state_and_health_payload() -> None:
    bridge = HarnBridgeState(connected_window_ms=100)

    assert bridge.snapshot(pending_inputs=2, timestamp_ms=1000) == {
        "pendingInputs": 2,
        "inputPollerSeen": False,
        "inputPollerConnected": False,
        "lastInputPollMs": None,
        "lastInputPollAgeMs": None,
        "lastInputDeliveryMs": None,
        "pollCount": 0,
        "deliveredInputs": 0,
    }
    bridge.record_input_poll(delivered=False, timestamp_ms=1000)
    assert bridge.snapshot(pending_inputs=1, timestamp_ms=1050)["inputPollerConnected"] is True
    stale = bridge.snapshot(pending_inputs=1, timestamp_ms=1201)
    assert stale["inputPollerConnected"] is False
    assert stale["lastInputPollAgeMs"] == 201
    bridge.record_input_poll(delivered=True, timestamp_ms=1250)
    delivered = bridge.snapshot(pending_inputs=0, timestamp_ms=1250)
    assert delivered["deliveredInputs"] == 1
    assert delivered["lastInputDeliveryMs"] == 1250

    state = GibsonServerState(input_bridge=bridge)
    assert health_payload(state)["inputBridge"]["deliveredInputs"] == 1


def test_async_state_accepts_without_immediate_scene_update() -> None:
    state = GibsonServerState(render_mode="async", render_batch_window_ms=0)
    state.pipeline.start = lambda: None  # type: ignore[method-assign]
    payload = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "input",
        "phase": "before",
        "title": "Input intercept",
        "summary": "input",
        "payload": {"type": "input", "text": "hi", "source": "test"},
    }

    accepted = apply_event_to_scene(payload, state)
    assert "schema" not in accepted
    assert accepted["ok"] is True
    assert accepted["renderMode"] == "async"
    assert accepted["sceneRevision"] == 0
    state.pipeline.stop()


def test_build_state_from_env() -> None:
    state = build_state_from_env({"HARN_GIBSON_RENDER_MODE": "async", "HARN_GIBSON_RENDER_BATCH_MS": "5"})

    assert state.pipeline.mode == "async"
    assert state.pipeline.batch_window_ms == 5
    state.pipeline.stop()


def test_renderer_interest_from_env_and_build_state() -> None:
    interest_payload = json.dumps(
        {
            "eventTypes": ["tool_call"],
            "phases": ["before"],
            "fallbackRoute": "drop",
            "reason": "dogfood renderer scope",
        }
    )
    rules_payload = json.dumps(
        [
            {
                "eventType": "runtime_error",
                "route": "debug_only",
                "reason": "local diagnostics",
            },
            {
                "eventType": "model_select",
                "route": "drop",
            },
        ]
    )
    interest = renderer_interest_from_env(interest_payload)
    route_rules = route_rules_from_env(rules_payload)
    state = build_state_from_env(
        {
            "HARN_GIBSON_RENDERER_INTEREST": interest_payload,
            "HARN_GIBSON_ROUTE_RULES": rules_payload,
        }
    )

    assert renderer_interest_from_env(None) is None
    assert renderer_interest_from_env("") is None
    assert route_rules_from_env(None) == ()
    assert route_rules_from_env("") == ()
    assert interest is not None
    assert interest.event_types == ("tool_call",)
    assert interest.fallback_route == "drop"
    assert route_rules[0].event_type == "runtime_error"
    assert route_rules[0].route == "debug_only"
    assert route_rules[1].reason == "drop route rule"
    assert state.router.renderer_interest == interest
    assert state.router.route_rules["runtime_error"] == route_rules[0]

    for value, message in (
        ("{", "JSON object"),
        ("[]", "invalid"),
        (json.dumps({"fallbackRoute": "renderer_agent"}), "fallback route"),
    ):
        try:
            renderer_interest_from_env(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")

    for value, message in (
        ("{", "JSON list"),
        (json.dumps({"eventType": "x", "route": "drop"}), "invalid"),
        (json.dumps([{"eventType": "x", "route": "stream_buffer"}]), "unsupported"),
    ):
        try:
            route_rules_from_env(value)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("expected ValueError")


def test_format_sse() -> None:
    assert format_sse({"a": 1}) == 'data: {"a":1}\n\n'


def test_cli_parser_and_run(monkeypatch: Any, capsys: Any) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["extension-path"]).command == "extension-path"
    parsed_dogfood = parser.parse_args(["dogfood", "--no-browser", "--", "-p", "hello"])
    assert parsed_dogfood.command == "dogfood"
    assert parsed_dogfood.browser is False
    assert parsed_dogfood.harn_args == ["--", "-p", "hello"]
    parsed_auth = parser.parse_args(["import-codex-auth", "--codex-auth", "codex.json", "--harn-auth", "harn.json"])
    assert parsed_auth.command == "import-codex-auth"
    parsed_replay = parser.parse_args(
        [
            "replay",
            "fixture.json",
            "--output-scene",
            "scene.json",
            "--output-result",
            "result.json",
            "--screenshot",
            "scene.png",
            "--screenshot-width",
            "800",
            "--screenshot-height",
            "600",
        ]
    )
    assert parsed_replay.command == "replay"
    assert parsed_replay.path == "fixture.json"
    assert parsed_replay.output_scene == "scene.json"
    assert parsed_replay.output_result == "result.json"
    assert parsed_replay.screenshot == "scene.png"
    assert parsed_replay.screenshot_width == 800
    assert parsed_replay.screenshot_height == 600
    assert cli.run(["extension-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("extension.py")

    calls: list[tuple[str, int]] = []

    def fake_run_server(host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr("harn_gibson.server.run_server", fake_run_server)
    assert cli.run(["serve", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert cli.run([]) == 0
    assert calls == [("0.0.0.0", 9999), ("127.0.0.1", 8765)]


def test_cli_replay_writes_outputs(tmp_path: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    scene_path = tmp_path / "scene.json"
    result_path = tmp_path / "result.json"
    event = {
        "sequence": 1,
        "timestampMs": 10,
        "source": "test",
        "eventType": "tool_call",
        "phase": "before",
        "title": "Tool preflight",
        "summary": "bash starting with {command}",
        "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
    }
    replay_path.write_text(json.dumps({"steps": [{"type": "event", "event": event}]}), encoding="utf-8")

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--output-scene",
                str(scene_path),
                "--output-result",
                str(result_path),
            ]
        )
        == 0
    )

    assert json.loads(scene_path.read_text(encoding="utf-8"))["revision"] == 1
    assert json.loads(result_path.read_text(encoding="utf-8"))["steps"][0]["updates"] == 1
    assert capsys.readouterr().out.strip() == "replayed 1 steps; scene revision 1"


def test_cli_replay_without_outputs(tmp_path: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "message_update",
                            "phase": "during",
                            "title": "Stream update",
                            "summary": "assistant stream {delta}",
                            "payload": {"type": "message_update", "assistantMessageEvent": {"delta": "ok"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert cli.run(["replay", str(replay_path)]) == 0
    assert capsys.readouterr().out.strip() == "replayed 1 steps; scene revision 1"


def test_cli_replay_captures_screenshot(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    replay_path = tmp_path / "replay.json"
    screenshot_path = tmp_path / "replay.png"
    calls: list[tuple[int, int, int]] = []
    replay_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "type": "event",
                        "event": {
                            "sequence": 1,
                            "timestampMs": 10,
                            "source": "test",
                            "eventType": "tool_call",
                            "phase": "before",
                            "title": "Tool preflight",
                            "summary": "bash starting with {command}",
                            "payload": {"type": "tool_call", "toolName": "bash", "input": {"command": "pwd"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_capture(state: Any, path: str, *, width: int, height: int) -> BrowserScreenshotResult:
        calls.append((state.scene.state.revision, width, height))
        return BrowserScreenshotResult(Path(path), "http://127.0.0.1:1", state.scene.state.revision, width, height)

    monkeypatch.setattr("harn_gibson.browser_capture.capture_scene_screenshot", fake_capture)

    assert (
        cli.run(
            [
                "replay",
                str(replay_path),
                "--screenshot",
                str(screenshot_path),
                "--screenshot-width",
                "1024",
                "--screenshot-height",
                "768",
            ]
        )
        == 0
    )

    assert calls == [(1, 1024, 768)]
    assert capsys.readouterr().out.splitlines() == [
        f"captured replay screenshot: {screenshot_path}",
        "replayed 1 steps; scene revision 1",
    ]


def test_cli_dogfood_launches_display_browser_and_harn(monkeypatch: Any, capsys: Any) -> None:
    server_calls: list[str] = []
    browser_urls: list[str] = []
    harn_calls: list[tuple[list[str], dict[str, str]]] = []
    state = GibsonServerState()
    state.pipeline.stop = lambda: server_calls.append("pipeline.stop")  # type: ignore[method-assign]

    class FakeServer:
        server_address = ("127.0.0.1", 9876)

        def serve_forever(self) -> None:
            server_calls.append("serve_forever")

        def shutdown(self) -> None:
            server_calls.append("shutdown")

        def server_close(self) -> None:
            server_calls.append("server_close")

    def fake_call(command: list[str], env: dict[str, str]) -> int:
        harn_calls.append((command, env))
        return 23

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: browser_urls.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert (
        cli.run(
            [
                "dogfood",
                "--port",
                "0",
                "--harn-bin",
                "harn-dev",
                "--no-codex-auth-import",
                "--no-hold-on-error",
                "--",
                "-p",
                "hello",
            ]
        )
        == 23
    )
    assert browser_urls == ["http://127.0.0.1:9876"]
    assert harn_calls[0][0] == ["harn-dev", "-p", "hello"]
    assert harn_calls[0][1]["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9876/events"
    assert harn_calls[0][1]["HARN_GIBSON_INPUT_ENDPOINT"] == "http://127.0.0.1:9876/input/next"
    assert "pipeline.stop" in server_calls
    assert "shutdown" in server_calls
    assert "server_close" in server_calls
    assert "harn-gibson display: http://127.0.0.1:9876" in capsys.readouterr().err


def test_cli_dogfood_reports_missing_harn(monkeypatch: Any, capsys: Any) -> None:
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9877)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def fake_call(_command: list[str], env: dict[str, str]) -> int:
        assert env["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9877/events"
        raise FileNotFoundError

    opened: list[str] = []
    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert cli.run_dogfood(harn_bin="missing-harn", launch_browser=False, codex_auth_import=False) == 127
    assert opened == []
    assert "harn executable not found: missing-harn" in capsys.readouterr().err


def test_cli_import_codex_auth_command(monkeypatch: Any, capsys: Any) -> None:
    class Result:
        available = True
        message = "imported"

    calls: list[tuple[str, str]] = []

    def fake_import(source: str, target: str) -> Result:
        calls.append((source, target))
        return Result()

    monkeypatch.setattr(cli, "import_codex_auth", fake_import)

    assert cli.run(["import-codex-auth", "--codex-auth", "codex.json", "--harn-auth", "harn.json"]) == 0
    assert calls == [("codex.json", "harn.json")]
    assert capsys.readouterr().out.strip() == "imported"


def test_cli_dogfood_imports_auth_and_publishes_diagnostics(monkeypatch: Any, capsys: Any) -> None:
    harn_calls: list[list[str]] = []
    state = GibsonServerState()

    class FakeServer:
        server_address = ("127.0.0.1", 9888)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    class Result:
        available = True
        message = "auth ready"

    def fake_call(command: list[str], env: dict[str, str]) -> int:
        harn_calls.append(command)
        assert env["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9888/events"
        return 0

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.setattr(cli, "import_codex_auth", lambda environ=None: Result())

    assert cli.run_dogfood(harn_args=["--", "-p", "hello"], hold_on_error=False) == 0
    assert harn_calls == [["harn", "-p", "hello"]]
    assert state.scene.state.log[-1]["eventType"] == "auth_import"
    assert "auth ready" in capsys.readouterr().err


def test_cli_dogfood_holds_display_on_harn_error(monkeypatch: Any) -> None:
    state = GibsonServerState()
    held: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 9890)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", lambda _command, env: 2)
    monkeypatch.setattr(cli, "_hold_display_on_error", lambda url: held.append(url))

    assert cli.run_dogfood(codex_auth_import=False) == 2
    assert held == ["http://127.0.0.1:9890"]
    assert state.scene.state.log[-1]["summary"] == "error: harn exited with code 2"


def test_cli_dogfood_holds_display_on_missing_harn(monkeypatch: Any) -> None:
    state = GibsonServerState()
    held: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 9891)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    def missing(_command: list[str], env: dict[str, str]) -> int:
        raise FileNotFoundError

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: state)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(cli.subprocess, "call", missing)
    monkeypatch.setattr(cli, "_hold_display_on_error", lambda url: held.append(url))

    assert cli.run_dogfood(harn_bin="missing", codex_auth_import=False) == 127
    assert held == ["http://127.0.0.1:9891"]
    assert state.scene.state.log[-1]["eventType"] == "harn_exit"
