from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.browser_capture import (
    BrowserScreenshotResult,
    capture_scene_screenshot,
    resolve_playwright_factory,
)
from harn_gibson.scene import SceneMutation
from harn_gibson.server import GibsonServerState


class FakePage:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def goto(self, url: str, wait_until: str) -> None:
        self.calls.append(("goto", (url, wait_until)))

    def wait_for_timeout(self, wait_ms: int) -> None:
        self.calls.append(("wait_for_timeout", wait_ms))

    def screenshot(self, *, path: Path, full_page: bool) -> None:
        self.calls.append(("screenshot", (path, full_page)))
        path.write_bytes(b"fake replay screenshot")


class FakeBrowser:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def new_page(self, *, viewport: dict[str, int]) -> FakePage:
        self.calls.append(("new_page", viewport))
        return FakePage(self.calls)

    def close(self) -> None:
        self.calls.append(("close", None))


class FakeChromium:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def launch(self) -> FakeBrowser:
        self.calls.append(("launch", None))
        return FakeBrowser(self.calls)


class FakeDriver:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.chromium = FakeChromium(calls)


class FakePlaywrightContext:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def __enter__(self) -> FakeDriver:
        self.calls.append(("enter", None))
        return FakeDriver(self.calls)

    def __exit__(self, *_exc: object) -> None:
        self.calls.append(("exit", None))


def fake_playwright_factory(calls: list[tuple[str, object]]) -> Any:
    return lambda: FakePlaywrightContext(calls)


def test_capture_scene_screenshot_serves_scene_with_fake_browser(tmp_path: Path) -> None:
    state = GibsonServerState()
    state.scene.apply([SceneMutation("append_log", entry={"eventType": "fixture"})])
    calls: list[tuple[str, object]] = []
    output = tmp_path / "captures" / "replay.png"

    result = capture_scene_screenshot(
        state,
        output,
        width=640,
        height=480,
        full_page=False,
        wait_ms=0,
        playwright_factory=fake_playwright_factory(calls),
    )

    assert isinstance(result, BrowserScreenshotResult)
    assert result.to_dict() == {
        "path": str(output),
        "url": result.url,
        "sceneRevision": 1,
        "width": 640,
        "height": 480,
    }
    assert output.read_bytes() == b"fake replay screenshot"
    assert ("new_page", {"width": 640, "height": 480}) in calls
    assert ("screenshot", (output, False)) in calls
    assert not any(call[0] == "wait_for_timeout" for call in calls)
    assert calls[-2:] == [("close", None), ("exit", None)]


def test_capture_scene_screenshot_resolves_factory_and_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "harn_gibson.browser_capture.resolve_playwright_factory",
        lambda: fake_playwright_factory(calls),
    )

    result = capture_scene_screenshot(GibsonServerState(), tmp_path / "replay.png", wait_ms=3)

    assert result.scene_revision == 0
    assert ("wait_for_timeout", 3) in calls
    assert calls[-3][0] == "screenshot"


def test_resolve_playwright_factory_import_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_sync() -> None:
        return None

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = fake_sync  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    assert resolve_playwright_factory() is fake_sync

    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(RuntimeError, match="Playwright is required"):
        resolve_playwright_factory()
