from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

from harn_gibson import cli
from harn_gibson.server import (
    BrowserInputQueue,
    GibsonServerState,
    HarnBridgeState,
    apply_event_to_scene,
    browser_input_event_payload,
    build_state_from_env,
    create_server,
    enqueue_browser_input,
    event_from_payload,
    format_sse,
    health_payload,
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
        assert request_text(f"{base}/assets/app.css")[1] == "text/css; charset=utf-8"
        assert request_text(f"{base}/assets/app.js")[1] == "application/javascript; charset=utf-8"
        health = json.loads(request_text(f"{base}/healthz")[2])
        assert health["ok"] is True
        assert health["events"] == 0
        assert health["sceneRevision"] == 0
        assert health["renderMode"] == "blocking"
        assert health["pendingRenderJobs"] == 0
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


def test_format_sse() -> None:
    assert format_sse({"a": 1}) == 'data: {"a":1}\n\n'


def test_cli_parser_and_run(monkeypatch: Any, capsys: Any) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["extension-path"]).command == "extension-path"
    parsed_dogfood = parser.parse_args(["dogfood", "--no-browser", "--", "-p", "hello"])
    assert parsed_dogfood.command == "dogfood"
    assert parsed_dogfood.browser is False
    assert parsed_dogfood.harn_args == ["--", "-p", "hello"]
    assert cli.run(["extension-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("extension.py")

    calls: list[tuple[str, int]] = []

    def fake_run_server(host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr("harn_gibson.server.run_server", fake_run_server)
    assert cli.run(["serve", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert cli.run([]) == 0
    assert calls == [("0.0.0.0", 9999), ("127.0.0.1", 8765)]


def test_cli_dogfood_launches_display_browser_and_harn(monkeypatch: Any, capsys: Any) -> None:
    server_calls: list[str] = []
    browser_urls: list[str] = []
    harn_calls: list[tuple[list[str], dict[str, str]]] = []

    class FakePipeline:
        def stop(self) -> None:
            server_calls.append("pipeline.stop")

    class FakeState:
        pipeline = FakePipeline()

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

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: FakeState())
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: browser_urls.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert cli.run(["dogfood", "--port", "0", "--harn-bin", "harn-dev", "--", "-p", "hello"]) == 23
    assert browser_urls == ["http://127.0.0.1:9876"]
    assert harn_calls[0][0] == ["harn-dev", "-p", "hello"]
    assert harn_calls[0][1]["HARN_GIBSON_ENDPOINT"] == "http://127.0.0.1:9876/events"
    assert harn_calls[0][1]["HARN_GIBSON_INPUT_ENDPOINT"] == "http://127.0.0.1:9876/input/next"
    assert "pipeline.stop" in server_calls
    assert "shutdown" in server_calls
    assert "server_close" in server_calls
    assert "harn-gibson display: http://127.0.0.1:9876" in capsys.readouterr().err


def test_cli_dogfood_reports_missing_harn(monkeypatch: Any, capsys: Any) -> None:
    class FakePipeline:
        def stop(self) -> None:
            return None

    class FakeState:
        pipeline = FakePipeline()

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
    monkeypatch.setattr("harn_gibson.server.build_state_from_env", lambda: FakeState())
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    assert cli.run_dogfood(harn_bin="missing-harn", launch_browser=False) == 127
    assert opened == []
    assert "harn executable not found: missing-harn" in capsys.readouterr().err
