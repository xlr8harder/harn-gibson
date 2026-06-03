"""Browser screenshot helpers for replay and integration fixtures."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harn_gibson.server import GibsonServerState, create_server

PlaywrightFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class BrowserScreenshotResult:
    path: Path
    url: str
    scene_revision: int
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "url": self.url,
            "sceneRevision": self.scene_revision,
            "width": self.width,
            "height": self.height,
        }


def resolve_playwright_factory() -> PlaywrightFactory:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        message = "Playwright is required for replay screenshots; install browser test dependencies"
        raise RuntimeError(message) from error
    return sync_playwright


def capture_scene_screenshot(
    state: GibsonServerState,
    path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    width: int = 1280,
    height: int = 900,
    full_page: bool = True,
    wait_ms: int = 160,
    playwright_factory: PlaywrightFactory | None = None,
) -> BrowserScreenshotResult:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    factory = playwright_factory or resolve_playwright_factory()
    server = create_server(host, port, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}"
    try:
        with factory() as driver:
            browser = driver.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(url, wait_until="domcontentloaded")
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                page.screenshot(path=output_path, full_page=full_page)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    return BrowserScreenshotResult(
        path=output_path,
        url=url,
        scene_revision=state.scene.state.revision,
        width=width,
        height=height,
    )


__all__ = [
    "BrowserScreenshotResult",
    "PlaywrightFactory",
    "capture_scene_screenshot",
    "resolve_playwright_factory",
]
