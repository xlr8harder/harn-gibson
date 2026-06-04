"""Browser screenshot helpers for replay and integration fixtures."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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
    canvas_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "path": str(self.path),
            "url": self.url,
            "sceneRevision": self.scene_revision,
            "width": self.width,
            "height": self.height,
        }
        if self.canvas_metrics:
            payload["canvasMetrics"] = self.canvas_metrics
        return payload


CANVAS_METRICS_SCRIPT = """canvas => {
  const context = canvas.getContext("2d");
  const width = Math.min(canvas.width, 160);
  const height = Math.min(canvas.height, 120);
  const data = context.getImageData(0, 0, width, height).data;
  let luminanceTotal = 0;
  let litPixels = 0;
  let maxChannelTotal = 0;
  for (let index = 0; index < data.length; index += 4) {
    const channelTotal = data[index] + data[index + 1] + data[index + 2];
    luminanceTotal += channelTotal;
    maxChannelTotal = Math.max(maxChannelTotal, channelTotal);
    if (channelTotal > 24) litPixels += 1;
  }
  const sampledPixels = Math.max(1, width * height);
  return {
    canvasWidth: canvas.width,
    canvasHeight: canvas.height,
    sampleWidth: width,
    sampleHeight: height,
    sampledPixels,
    luminanceTotal,
    litPixels,
    litRatio: Math.round((litPixels / sampledPixels) * 10000) / 10000,
    maxChannelTotal,
    nonblank: luminanceTotal > 0 && litPixels > 0,
  };
}"""

CAPTURE_READY_TIMEOUT_MS = 15_000


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
    require_nonblank: bool = True,
    playwright_factory: PlaywrightFactory | None = None,
) -> BrowserScreenshotResult:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    factory = playwright_factory or resolve_playwright_factory()
    server = create_server(host, port, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}?capture=1"
    try:
        with factory() as driver:
            browser = driver.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonCaptureReady === true", timeout=CAPTURE_READY_TIMEOUT_MS)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                canvas_metrics = capture_canvas_metrics(page)
                if require_nonblank and not canvas_metrics["nonblank"]:
                    raise RuntimeError("browser canvas rendered blank")
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
        canvas_metrics=canvas_metrics,
    )


def capture_canvas_metrics(page: Any) -> dict[str, Any]:
    metrics = page.locator("#grid").evaluate(CANVAS_METRICS_SCRIPT)
    if not isinstance(metrics, dict):
        raise RuntimeError("browser canvas metrics unavailable")
    return metrics


__all__ = [
    "BrowserScreenshotResult",
    "CANVAS_METRICS_SCRIPT",
    "PlaywrightFactory",
    "capture_canvas_metrics",
    "capture_scene_screenshot",
    "resolve_playwright_factory",
]
