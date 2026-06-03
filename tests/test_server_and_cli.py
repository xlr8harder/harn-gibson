from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

from harn_gibson import cli
from harn_gibson.server import GibsonServerState, apply_event_to_scene, create_server, event_from_payload, format_sse


def request_text(url: str, data: bytes | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status, response.headers["Content-Type"], response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read().decode("utf-8")


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
        assert json.loads(request_text(f"{base}/healthz")[2]) == {"ok": True, "events": 0, "sceneRevision": 0}
        assert json.loads(request_text(f"{base}/scene")[2])["schema"] == "harn-gibson.scene.v1"
        assert json.loads(request_text(f"{base}/missing")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/bad", b"{}")[2]) == {"error": "not found"}
        assert json.loads(request_text(f"{base}/events", b"{")[2]) == {"error": "invalid json"}
        assert json.loads(request_text(f"{base}/events", b"[]")[2]) == {"error": "event payload must be an object"}
        assert json.loads(request_text(f"{base}/events", b'{"sequence":1}')[2]) == {
            "error": "event payload missing eventType"
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
        assert json.loads(body) == {"ok": True, "sceneRevision": 1}
        assert json.loads(request_text(f"{base}/healthz")[2]) == {"ok": True, "events": 1, "sceneRevision": 1}
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

    assert event.event_type == "tool_call"
    assert event.recent_context == ("ctx",)
    assert update["schema"] == "harn-gibson.scene-update.v1"
    assert update["decisions"] == [{"block": True, "reason": "no"}]
    assert update["scene"]["revision"] == 1


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


def test_format_sse() -> None:
    assert format_sse({"a": 1}) == 'data: {"a":1}\n\n'


def test_cli_parser_and_run(monkeypatch: Any, capsys: Any) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["extension-path"]).command == "extension-path"
    assert cli.run(["extension-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("extension.py")

    calls: list[tuple[str, int]] = []

    def fake_run_server(host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr("harn_gibson.server.run_server", fake_run_server)
    assert cli.run(["serve", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert cli.run([]) == 0
    assert calls == [("0.0.0.0", 9999), ("127.0.0.1", 8765)]
