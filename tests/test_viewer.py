from __future__ import annotations

from typing import Any

from harn_gibson.viewer import start_viewer


class FakePipeline:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def stop(self) -> None:
        self.calls.append("pipeline.stop")


class FakeState:
    def __init__(self, calls: list[str]) -> None:
        self.pipeline = FakePipeline(calls)


class FakeServer:
    def __init__(self, calls: list[str], port: int = 9876) -> None:
        self.calls = calls
        self.server_address = ("127.0.0.1", port)

    def serve_forever(self) -> None:
        self.calls.append("serve_forever")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def server_close(self) -> None:
        self.calls.append("server_close")


def test_start_viewer_starts_server_opens_browser_and_closes(monkeypatch: Any) -> None:
    calls: list[str] = []
    opened: list[str] = []
    state = FakeState(calls)

    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer(calls))

    viewer = start_viewer("127.0.0.1", 0, state=state, browser_open=opened.append)
    viewer.thread.join(timeout=1)
    viewer.open_browser()
    viewer.close()
    viewer.close()

    assert viewer.display_url == "http://127.0.0.1:9876"
    assert viewer.endpoint == "http://127.0.0.1:9876/events"
    assert viewer.input_endpoint == "http://127.0.0.1:9876/input/next"
    assert opened == ["http://127.0.0.1:9876", "http://127.0.0.1:9876"]
    assert calls == ["serve_forever", "pipeline.stop", "shutdown", "server_close"]


def test_start_viewer_builds_state_from_env_without_opening_browser(monkeypatch: Any) -> None:
    calls: list[str] = []
    envs: list[dict[str, str] | None] = []
    opened: list[str] = []
    state = FakeState(calls)

    def fake_build_state_from_env(env: dict[str, str] | None = None) -> FakeState:
        envs.append(env)
        return state

    monkeypatch.setattr("harn_gibson.server.build_state_from_env", fake_build_state_from_env)
    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer(calls, 7777))

    viewer = start_viewer(env={"HARN_GIBSON_STYLE": "mainframe"}, launch_browser=False, browser_open=opened.append)
    viewer.thread.join(timeout=1)
    viewer.close()

    assert envs == [{"HARN_GIBSON_STYLE": "mainframe"}]
    assert opened == []
    assert viewer.display_url == "http://127.0.0.1:7777"


def test_start_viewer_closes_without_pipeline_stop(monkeypatch: Any) -> None:
    calls: list[str] = []
    state = object()

    monkeypatch.setattr("harn_gibson.server.create_server", lambda _host, _port, _state: FakeServer(calls))

    viewer = start_viewer(state=state, launch_browser=False)
    viewer.thread.join(timeout=1)
    viewer.close()

    assert calls == ["serve_forever", "shutdown", "server_close"]
