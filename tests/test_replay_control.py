"""Tests for the replay restart control (browser replay button)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from harn_gibson.cli import _rerun_replay
from harn_gibson.server import (
    GibsonServerState,
    ReplayControl,
    build_state_from_env,
    create_server,
    replay_status_payload,
    reset_session,
    submit_event_to_renderer,
)

EVENT_PAYLOAD = {
    "schema": "harn-gibson.event.v1",
    "sequence": 1,
    "timestampMs": 100,
    "source": "harn",
    "eventType": "tool_result",
    "phase": "after",
    "title": "Tool result",
    "summary": "bash completed",
    "payload": {"type": "tool_result", "toolName": "bash", "input": {"command": "ls"}},
}


def test_replay_control_runs_once_at_a_time() -> None:
    release = threading.Event()
    started = threading.Event()

    def runner() -> None:
        started.set()
        release.wait(timeout=5)

    control = ReplayControl(description="fixture.json", runner=runner)
    assert control.running is False
    assert control.restart() is True
    assert started.wait(timeout=5)
    assert control.running is True
    assert control.restart() is False  # second click while playing is a no-op
    assert control.runs == 1
    release.set()
    control._thread.join(timeout=5)
    assert control.running is False
    assert control.restart() is True
    control._thread.join(timeout=5)
    assert control.runs == 2


def test_replay_status_payload_reports_registration() -> None:
    state = GibsonServerState()
    try:
        assert replay_status_payload(state) == {"available": False}
        state.replay_control = ReplayControl(description="arc.json", runner=lambda: None)
        payload = replay_status_payload(state)
        assert payload == {"available": True, "description": "arc.json", "runs": 0, "running": False}
    finally:
        state.pipeline.stop()


def test_reset_session_gives_a_fresh_world() -> None:
    state = build_state_from_env({"HARN_GIBSON_PROJECTION": "1"})
    try:
        submit_event_to_renderer(dict(EVENT_PAYLOAD), state)
        assert "projection-scene" in state.scene.state.primitives
        old_builder = state.pipeline.context_builder
        old_engine = state.pipeline.renderer.engine
        assert old_engine.revision > 0

        reset_session(state)
        assert state.pipeline.context_builder is not old_builder
        assert state.pipeline.renderer.engine is not old_engine
        assert state.pipeline.renderer.engine.revision == 0
        assert "projection-scene" not in state.scene.state.primitives

        # the same event replays into the fresh session instead of deduping
        submit_event_to_renderer(dict(EVENT_PAYLOAD), state)
        assert "projection-scene" in state.scene.state.primitives
        assert state.pipeline.renderer.engine.revision > 0
    finally:
        state.pipeline.stop()


def test_reset_session_with_renderer_lacking_reset() -> None:
    state = GibsonServerState()
    try:
        reset_session(state)  # DeterministicSceneRenderer has no reset(); no crash
        assert state.scene.state.revision >= 0
    finally:
        state.pipeline.stop()


def test_replay_endpoints_drive_restart(tmp_path: Path) -> None:
    state = GibsonServerState()
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"

    def _request(path: str, *, post: bool = False) -> tuple[int, dict]:
        request = urllib.request.Request(f"{base}{path}", data=b"" if post else None,
                                         method="POST" if post else "GET")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    try:
        status, payload = _request("/replay")
        assert (status, payload) == (200, {"available": False})
        status, payload = _request("/replay/restart", post=True)
        assert status == 409

        ran = threading.Event()
        state.replay_control = ReplayControl(description="arc.json", runner=ran.set)
        status, payload = _request("/replay/restart", post=True)
        assert status == 202
        assert payload["restarted"] is True
        assert payload["runs"] == 1
        assert ran.wait(timeout=5)
        status, payload = _request("/replay")
        assert status == 200
        assert payload["available"] is True
    finally:
        server.shutdown()
        server.server_close()
        state.pipeline.stop()


def test_rerun_replay_resets_and_plays_the_file(tmp_path: Path) -> None:
    replay_path = tmp_path / "mini.json"
    replay_path.write_text(json.dumps({
        "schema": "harn-gibson.replay.v1",
        "name": "mini",
        "steps": [{"type": "event", "event": EVENT_PAYLOAD}],
    }), encoding="utf-8")
    state = build_state_from_env({"HARN_GIBSON_PROJECTION": "1"})
    try:
        submit_event_to_renderer(dict(EVENT_PAYLOAD), state)
        engine_before = state.pipeline.renderer.engine
        progressed: list[int] = []
        _rerun_replay(
            str(replay_path),
            state,
            step_delay_ms=0,
            playback_timing="fixed",
            speed=1.0,
            max_step_delay_ms=0,
            progress=lambda step, position, total, scene: progressed.append(position),
        )
        assert progressed == [1]
        assert state.pipeline.renderer.engine is not engine_before
        # the replayed event landed in the fresh session
        assert "projection-scene" in state.scene.state.primitives
    finally:
        state.pipeline.stop()
