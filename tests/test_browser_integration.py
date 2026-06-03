from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.browser_capture import capture_scene_screenshot
from harn_gibson.replay import run_replay_file
from harn_gibson.server import GibsonServerState, create_server

playwright = pytest.importorskip("playwright.sync_api")
Error = playwright.Error
expect = playwright.expect
sync_playwright = playwright.sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_REPLAYS = ROOT / "examples" / "replays"
SCREENSHOT_DIR = Path("test-artifacts/screenshots")


def start_display_server() -> tuple[ThreadingHTTPServer, GibsonServerState, str]:
    state = GibsonServerState()
    server = create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, state, f"http://{host}:{port}"


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def assert_canvas_nonblank(page: Any) -> None:
    sample = page.locator("#grid").evaluate(
        """canvas => {
          const context = canvas.getContext("2d");
          const width = Math.min(canvas.width, 96);
          const height = Math.min(canvas.height, 96);
          const data = context.getImageData(0, 0, width, height).data;
          let total = 0;
          for (let index = 0; index < data.length; index += 4) {
            total += data[index] + data[index + 1] + data[index + 2];
          }
          return {width: canvas.width, height: canvas.height, total};
        }"""
    )
    assert sample["width"] >= 320
    assert sample["height"] >= 240
    assert sample["total"] > 0


def assert_screenshot(path: Path) -> None:
    assert path.exists()
    assert path.stat().st_size > 10_000


def test_browser_display_renders_events_debug_and_input_queue() -> None:
    server, state, base = start_display_server()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    desktop = SCREENSHOT_DIR / "gibson-desktop.png"
    mobile = SCREENSHOT_DIR / "gibson-mobile.png"
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(base, wait_until="domcontentloaded")
                expect(page.get_by_role("heading", name="GIBSON LINK")).to_be_visible()
                expect(page.locator("#inputStatus")).to_have_text("ready")
                expect(page.locator("#bridgeStatus")).to_have_text("harn bridge idle")
                page.wait_for_timeout(120)
                assert_canvas_nonblank(page)

                accepted = post_json(
                    f"{base}/events",
                    {
                        "sequence": 7,
                        "timestampMs": 700,
                        "source": "test",
                        "eventType": "tool_call",
                        "phase": "before",
                        "title": "Tool preflight",
                        "summary": "shell command accepted",
                        "payload": {"type": "tool_call", "toolName": "bash"},
                    },
                )
                assert accepted == {"ok": True, "renderMode": "blocking", "sceneRevision": 1}
                page.wait_for_function("window.__gibsonScene?.primitives?.['gibson-city']")
                browser_scene = page.evaluate(
                    """() => ({
                      cityKind: window.__gibsonScene.primitives["gibson-city"].kind,
                      cityBlocks: window.__gibsonScene.primitives["gibson-city"].props.blocks.length,
                      graphKind: window.__gibsonScene.primitives["signal-graph"].kind,
                      packetKind: window.__gibsonScene.primitives["packet-field"].kind,
                      repoKind: window.__gibsonScene.primitives["repo-map"].kind,
                      pulseKind: window.__gibsonScene.animations["pulse-7"].kind,
                      animationIds: window.__gibsonAnimationState.ids,
                      animationKinds: window.__gibsonAnimationState.kinds,
                    })"""
                )
                assert browser_scene == {
                    "cityKind": "city_block",
                    "cityBlocks": 7,
                    "graphKind": "node_graph",
                    "packetKind": "particle_field",
                    "repoKind": "node_graph",
                    "pulseKind": "phase-pulse",
                    "animationIds": ["pulse-7"],
                    "animationKinds": ["phase-pulse"],
                }
                expect(page.locator("#phase")).to_have_text("before")
                expect(page.locator("#eventType")).to_have_text("tool_call")
                expect(page.locator("#sequence")).to_have_text("7")

                page.locator("#debugToggle").click()
                assert page.locator("body").evaluate("body => body.classList.contains('debug-open')") is True
                page.wait_for_function(
                    """Math.abs(
                      document.querySelector('#debugPanel').getBoundingClientRect().right - (innerWidth - 18)
                    ) < 2"""
                )
                expect(page.locator("#feed")).to_contain_text("Tool preflight")
                expect(page.locator("#intentLog")).to_contain_text("visualize tool_call")
                page.screenshot(path=desktop, full_page=True)
                assert_screenshot(desktop)

                page.locator("#promptInput").fill("scan perimeter")
                page.get_by_role("button", name="SEND").click()
                expect(page.locator("#inputStatus")).to_have_text("1 input waiting for harn")
                expect(page.locator("#bridgeStatus")).to_have_text("harn bridge waiting")
                assert get_json(f"{base}/input/next") == {
                    "id": "input-1",
                    "sequence": 1,
                    "message": "scan perimeter",
                    "deliverAs": "followUp",
                }
                page.evaluate("refreshHealth()")
                expect(page.locator("#inputStatus")).to_have_text("input-1 delivered to harn")
                expect(page.locator("#bridgeStatus")).to_have_text("harn bridge linked")

                page.locator("#debugClose").click()
                assert page.locator("body").evaluate("body => body.classList.contains('debug-open')") is False
                expect(page.locator("#debugToggle")).to_have_attribute("aria-expanded", "false")
                page.wait_for_function(
                    "document.querySelector('#debugPanel').getBoundingClientRect().left >= innerWidth"
                )
                page.set_viewport_size({"width": 390, "height": 760})
                expect(page.get_by_role("heading", name="GIBSON LINK")).to_be_visible()
                page.screenshot(path=mobile)
                assert_screenshot(mobile)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("fixture_name", "screenshot_name"),
    [
        ("animation-gallery.json", "replay-animation-gallery.png"),
        ("stream-and-diagnostic.json", "replay-stream-and-diagnostic.png"),
        ("renderer-plan.json", "replay-renderer-plan.png"),
        ("primitive-gallery.json", "replay-primitive-gallery.png"),
    ],
)
def test_checked_in_replay_fixtures_render_browser_screenshots(fixture_name: str, screenshot_name: str) -> None:
    state = GibsonServerState()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    output = SCREENSHOT_DIR / screenshot_name
    try:
        result = run_replay_file(EXAMPLE_REPLAYS / fixture_name, state)
        try:
            screenshot = capture_scene_screenshot(state, output, width=1280, height=900, wait_ms=120)
        except Error as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        assert screenshot.scene_revision == result.scene.revision
        assert screenshot.canvas_metrics["nonblank"] is True
        assert screenshot.canvas_metrics["luminanceTotal"] > 0
        assert_screenshot(output)
    finally:
        state.pipeline.stop()
