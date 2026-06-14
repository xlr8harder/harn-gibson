from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from harn_gibson.browser_capture import capture_scene_screenshot
from harn_gibson.replay import replay_frame_review_html, run_replay_file
from harn_gibson.scene import SceneMutation, ScenePrimitive
from harn_gibson.server import GibsonServerState, create_server
from harn_gibson.styles import style_pack_from_name

playwright = pytest.importorskip("playwright.sync_api")
Error = playwright.Error
expect = playwright.expect
sync_playwright = playwright.sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_REPLAYS = ROOT / "examples" / "replays"
SCREENSHOT_DIR = Path("test-artifacts/screenshots")


def start_display_server(state: GibsonServerState | None = None) -> tuple[ThreadingHTTPServer, GibsonServerState, str]:
    state = state or GibsonServerState()
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
          const width = canvas.width;
          const height = canvas.height;
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
                assert page.evaluate("wrapNarration('GIBSON_BROWSER_STREAM_OK')[0]") == "GIBSON_BROWSER_STREAM_OK"
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
                page.wait_for_function("window.__gibsonCityState?.['gibson-city']?.cameraKeyframeCount === 3")
                page.wait_for_function("window.__gibsonCityState?.['repo-city']?.cameraKeyframeCount === 3")
                browser_scene = page.evaluate(
                    """() => ({
                      cityKind: window.__gibsonScene.primitives["gibson-city"].kind,
                      cityBlocks: window.__gibsonScene.primitives["gibson-city"].props.blocks.length,
                      cityCameraKeys: window.__gibsonCityState["gibson-city"].cameraKeyframeCount,
                      cityCameraScale: window.__gibsonCityState["gibson-city"].cameraScale,
                      graphKind: window.__gibsonScene.primitives["signal-graph"].kind,
                      packetKind: window.__gibsonScene.primitives["packet-field"].kind,
                      repoKind: window.__gibsonScene.primitives["repo-map"].kind,
                      repoCityKind: window.__gibsonScene.primitives["repo-city"].kind,
                      repoCameraKeys: window.__gibsonCityState["repo-city"].cameraKeyframeCount,
                      pulseKind: window.__gibsonScene.animations["pulse-7"].kind,
                      animationIds: window.__gibsonAnimationState.ids,
                      animationKinds: window.__gibsonAnimationState.kinds,
                    })"""
                )
                assert browser_scene == {
                    "cityKind": "city_block",
                    "cityBlocks": 7,
                    "cityCameraKeys": 3,
                    "cityCameraScale": browser_scene["cityCameraScale"],
                    "graphKind": "node_graph",
                    "packetKind": "particle_field",
                    "repoKind": "node_graph",
                    "repoCityKind": "city_block",
                    "repoCameraKeys": 3,
                    "pulseKind": "phase-pulse",
                    "animationIds": ["pulse-7"],
                    "animationKinds": ["phase-pulse"],
                }
                assert browser_scene["cityCameraScale"] > 0
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


def test_browser_display_applies_scene_style_pack() -> None:
    server, state, base = start_display_server(GibsonServerState(style_pack=style_pack_from_name("satellite-uplink")))
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 900, "height": 640})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonStylePack?.id === 'satellite-uplink'")
                page.wait_for_function("window.__gibsonBackdropState?.styleId === 'satellite-uplink'")
                style_state = page.evaluate(
                    """() => ({
                      id: window.__gibsonStylePack.id,
                      bodyStyle: document.body.dataset.style,
                      gridTone: window.__gibsonStylePack.canvas.gridTone,
                      cssCyan: getComputedStyle(document.documentElement).getPropertyValue("--cyan").trim(),
                      sceneStyle: window.__gibsonScene.metadata.displayStyle,
                      backdropStyle: window.__gibsonBackdropState.styleId,
                      backdropMotifs: window.__gibsonBackdropState.motifs,
                      motifEffectCount: window.__gibsonBackdropState.motifEffectCount,
                    })"""
                )
                assert style_state == {
                    "id": "satellite-uplink",
                    "bodyStyle": "satellite-uplink",
                    "gridTone": "cyan",
                    "cssCyan": "#54ebe4",
                    "sceneStyle": "satellite-uplink",
                    "backdropStyle": "satellite-uplink",
                    "backdropMotifs": ["orbital-grid", "radar-sweeps", "warning-chevrons"],
                    "motifEffectCount": 3,
                }
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_display_applies_replay_style_showcase_backdrop() -> None:
    state = GibsonServerState()
    run_replay_file(EXAMPLE_REPLAYS / "style-showcase.json", state)
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 700})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonBackdropState?.styleId === 'satellite-uplink'")
                page.wait_for_function("window.__gibsonSignalScopeState?.['style-scope']?.mode === 'radar'")
                page.wait_for_function("window.__gibsonTimelineCueState?.['style-cues']?.cueCount === 4")
                style_state = page.evaluate(
                    """() => ({
                      backdrop: window.__gibsonBackdropState,
                      bodyStyle: document.body.dataset.style,
                      scope: window.__gibsonSignalScopeState["style-scope"],
                      route: window.__gibsonTraceRouteState["style-route"],
                      cue: window.__gibsonTimelineCueState["style-cues"],
                    })"""
                )
                assert style_state["bodyStyle"] == "satellite-uplink"
                assert style_state["backdrop"] == {
                    "styleId": "satellite-uplink",
                    "motifs": ["orbital-grid", "radar-sweeps", "warning-chevrons"],
                    "gridTone": "cyan",
                    "horizonAlpha": 0.18,
                    "motifEffectCount": 3,
                }
                assert style_state["scope"]["blipCount"] == 3
                assert style_state["scope"]["tone"] == "green"
                assert style_state["route"]["focusHopId"] == "satellite"
                assert style_state["route"]["packetCount"] == 24
                assert style_state["cue"]["targetId"] == "style-route"
                assert style_state["cue"]["cueCount"] == 4
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_terminal_wall_does_not_repeat_short_panel_lines() -> None:
    state = GibsonServerState()
    state.scene.apply(
        [
            SceneMutation(
                op="upsert",
                primitive=ScenePrimitive(
                    id="short-terminal",
                    kind="terminal_wall",
                    region="stage",
                    props={
                        "position": {"x": 0.5, "y": 0.55},
                        "size": {"w": 0.62, "h": 0.22},
                        "columns": 2,
                        "rows": 1,
                        "scan": False,
                        "cursor": False,
                        "panels": [
                            {
                                "id": "command",
                                "title": "COMMAND",
                                "lines": ["uv run pytest"],
                                "active": True,
                                "tone": "cyan",
                            },
                            {
                                "id": "output",
                                "title": "OUTPUT",
                                "lines": ["tests passed"],
                                "tone": "amber",
                            },
                        ],
                    },
                ),
            )
        ]
    )
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 720, "height": 480})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function(
                    "window.__gibsonTerminalWallState?.['short-terminal']?.renderedLineCount === 2"
                )
                terminal_wall_state = page.evaluate(
                    """() => window.__gibsonTerminalWallState["short-terminal"]"""
                )
                assert terminal_wall_state == {
                    "panelCount": 2,
                    "lineCount": 2,
                    "renderedLineCount": 2,
                    "columnCount": 2,
                    "rowCount": 1,
                    "activePanelId": "command",
                    "streamingCount": 1,
                    "tone": "green",
                    "accentTone": "cyan",
                    "hasScan": False,
                    "hasCursor": False,
                    "panelLineCounts": [1, 1],
                    "panelRenderedLineCounts": [1, 1],
                }
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_activity_roll_uses_file_tracks_over_time() -> None:
    state = GibsonServerState()
    state.scene.apply(
        [
            SceneMutation(
                op="upsert",
                primitive=ScenePrimitive(
                    id="projection-scene",
                    kind="projection_scene",
                    region="stage",
                    props={
                        "theme": "gibson",
                        "title": "ACTIVITY ROLL",
                        "revision": 1,
                        "mood": {"name": "work", "label": "AGENT ACTIVE", "tone": "base"},
                        "hud": {"focus": "src/app.py", "workspace": "main @ abc123d", "ticker": []},
                        "nodes": [],
                        "edges": [],
                        "effects": [],
                        "camera": {},
                        "physics": {"layers": []},
                        "grid": {
                            "kind": "epoch-grid",
                            "presentation": {"stage": "primary", "narration": False, "spatial": False},
                            "columns": [
                                {"id": "file:src/app.py", "label": "src/app.py", "group": "src"},
                                {"id": "file:tests/test_app.py", "label": "tests/test_app.py", "group": "tests"},
                            ],
                            "epochs": [
                                {"id": "epoch:1", "label": "cmd ok", "tone": "good"},
                                {"id": "epoch:2", "label": "cmd error", "tone": "alarm"},
                            ],
                            "cells": [
                                {"epoch": "epoch:1", "entity": "file:src/app.py", "height": 0.8,
                                 "tone": "good", "commands": 1, "churn": 0},
                                {"epoch": "epoch:2", "entity": "file:tests/test_app.py", "height": 0.5,
                                 "tone": "alarm", "commands": 1, "churn": 0.2},
                            ],
                            "pending": [
                                {"epoch": "pending", "entity": "file:src/app.py", "height": 0.35,
                                 "tone": "accent", "pending": True, "edits": 1, "churn": 0.3},
                            ],
                            "summary": {
                                "columnCount": 2,
                                "epochCount": 2,
                                "cellCount": 2,
                                "pendingCellCount": 1,
                                "activeColumnCount": 2,
                            },
                        },
                    },
                ),
            )
        ]
    )
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 700})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonProjectionState?.gridLayout === 'activity-roll-tracks'")
                projection_state = page.evaluate("window.__gibsonProjectionState")
                assert projection_state["nodeCount"] == 0
                assert projection_state["effectCount"] == 0
                assert projection_state["gridAxis"] == {"x": "time", "y": "files"}
                assert projection_state["gridColumnCount"] == 2
                assert projection_state["gridEpochCount"] == 2
                assert projection_state["gridCellCount"] == 2
                assert projection_state["gridPendingCount"] == 1
                assert projection_state["gridWindow"] == 0
                assert projection_state["gridSummary"]["activeColumnCount"] == 2
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_thermal_roll_renders_heat_quench_and_focus() -> None:
    state = GibsonServerState()
    state.scene.apply(
        [
            SceneMutation(
                op="upsert",
                primitive=ScenePrimitive(
                    id="projection-scene",
                    kind="projection_scene",
                    region="stage",
                    props={
                        "schema": "harn-gibson.projection-scene.v1",
                        "theme": "gibson",
                        "title": "THERMAL ROLL",
                        "seq": 4,
                        "revision": 1,
                        "mood": {"name": "work", "label": "AGENT ACTIVE", "tone": "base"},
                        "nodes": [],
                        "edges": [],
                        "effects": [],
                        "camera": {},
                        "hud": {},
                        "physics": {"layers": []},
                        "grid": {
                            "kind": "thermal-roll",
                            "seq": 4,
                            "nowMs": 4000,
                            "windowMs": 60000,
                            "presentation": {"stage": "primary", "narration": False, "spatial": False},
                            "columns": [
                                {"id": "file:src/app.py", "label": "src/app.py", "group": "src", "focus": True},
                                {"id": "file:tests/test_app.py", "label": "tests/test_app.py",
                                 "group": "tests", "focus": False},
                            ],
                            "samples": [
                                {"id": "thermal:1:1", "seq": 1, "ts": 1000, "kind": "file_seen",
                                 "status": "", "focus": "file:src/app.py", "quench": False,
                                 "shock": False, "energy": 0.0, "targets": ["file:src/app.py"]},
                                {"id": "thermal:2:2", "seq": 2, "ts": 2000, "kind": "file_changed",
                                 "status": "", "focus": "file:src/app.py", "quench": False,
                                 "shock": False, "energy": 0.9, "targets": ["file:src/app.py"]},
                                {"id": "thermal:3:3", "seq": 3, "ts": 3000, "kind": "check_completed",
                                 "status": "ok", "focus": "file:src/app.py", "quench": True,
                                 "shock": False, "energy": 0.9, "targets": ["file:src/app.py"]},
                            ],
                            "cells": [
                                {"sample": "thermal:1:1", "entity": "file:src/app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": True, "edited": False, "target": True,
                                 "quench": False, "shock": False},
                                {"sample": "thermal:1:1", "entity": "file:tests/test_app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": False, "edited": False, "target": False,
                                 "quench": False, "shock": False},
                                {"sample": "thermal:2:2", "entity": "file:src/app.py", "heat": 0.59,
                                 "rawHeat": 0.9, "focus": True, "edited": True, "target": True,
                                 "quench": False, "shock": False},
                                {"sample": "thermal:2:2", "entity": "file:tests/test_app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": False, "edited": False, "target": False,
                                 "quench": False, "shock": False},
                                {"sample": "thermal:3:3", "entity": "file:src/app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": True, "edited": False, "target": True,
                                 "quench": True, "shock": False},
                                {"sample": "thermal:3:3", "entity": "file:tests/test_app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": False, "edited": False, "target": False,
                                 "quench": True, "shock": False},
                            ],
                            "heat": [
                                {"entity": "file:src/app.py", "heat": 0.0, "rawHeat": 0.0, "focus": True},
                                {"entity": "file:tests/test_app.py", "heat": 0.0,
                                 "rawHeat": 0.0, "focus": False},
                            ],
                            "summary": {
                                "sampleCount": 3,
                                "hotFileCount": 0,
                                "maxHeat": 0.0,
                                "rawHeat": 0.0,
                                "quenchCount": 1,
                                "shockCount": 0,
                                "historyStartMs": 1000,
                                "historyEndMs": 3000,
                            },
                        },
                    },
                ),
            )
        ]
    )
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 700})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonProjectionState?.gridLayout === 'thermal-roll'")
                projection_state = page.evaluate("window.__gibsonProjectionState")
                assert projection_state["gridKind"] == "thermal-roll"
                assert projection_state["gridAxis"] == {"x": "time", "y": "files"}
                assert projection_state["gridColumnCount"] == 2
                assert projection_state["gridCellCount"] == 6
                assert projection_state["gridSampleCount"] == 3
                assert projection_state["gridHeatCount"] == 2
                assert projection_state["gridWindowMs"] == 60000
                assert projection_state["gridSummary"]["quenchCount"] == 1
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_display_renders_vector_symbols_and_data_rain() -> None:
    state = GibsonServerState()
    run_replay_file(EXAMPLE_REPLAYS / "primitive-gallery.json", state)
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 700})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function(
                    """() => [
                      window.__gibsonVectorState?.["gallery-vector"]?.symbolCount === 6,
                      window.__gibsonVectorState?.["gallery-vector"]?.morphFrameCount === 3,
                      window.__gibsonVectorAnimationState?.["gallery-vector"]?.keyframeCount === 6,
                      window.__gibsonVectorEffectState?.["gallery-vector"]?.filterCount === 2,
                      window.__gibsonHologramState?.["gallery-hologram"]?.ringCount === 6,
                      window.__gibsonDataVaultState?.["gallery-vault"]?.lockCount === 5,
                      window.__gibsonBlackIceState?.["gallery-black-ice"]?.columnCount === 13,
                      window.__gibsonSignalScopeState?.["gallery-scope"]?.blipCount === 3,
                      window.__gibsonTunnelState?.["gallery-tunnel"]?.packetCount === 44,
                      window.__gibsonWireLandscapeState?.["gallery-landscape"]?.peakCount === 4,
                      window.__gibsonCityState?.["gallery-city"]?.cameraKeyframeCount === 3,
                      window.__gibsonSpatialMapState?.["gallery-spatial"]?.objectCount === 4,
                      window.__gibsonTerminalWallState?.["gallery-terminal"]?.panelCount === 4,
                      window.__gibsonAccessMatrixState?.["gallery-access"]?.cellCount === 8,
                      window.__gibsonOrbitalMapState?.["gallery-orbital"]?.nodeCount === 5,
                      window.__gibsonDataRainState?.["gallery-rain"]?.visibleColumns > 0,
                      window.__gibsonTraceRouteState?.["gallery-trace"]?.packetCount === 18,
                    ].every(Boolean)"""
                )
                gallery_state = page.evaluate(
                    """() => ({
                      vector: window.__gibsonVectorState["gallery-vector"],
                      vectorAnimation: window.__gibsonVectorAnimationState["gallery-vector"],
                      vectorEffect: window.__gibsonVectorEffectState["gallery-vector"],
                      hologram: window.__gibsonHologramState["gallery-hologram"],
                      dataVault: window.__gibsonDataVaultState["gallery-vault"],
                      blackIce: window.__gibsonBlackIceState["gallery-black-ice"],
                      signalScope: window.__gibsonSignalScopeState["gallery-scope"],
                      tunnel: window.__gibsonTunnelState["gallery-tunnel"],
                      wireLandscape: window.__gibsonWireLandscapeState["gallery-landscape"],
                      city: window.__gibsonCityState["gallery-city"],
                      spatialMap: window.__gibsonSpatialMapState["gallery-spatial"],
                      terminalWall: window.__gibsonTerminalWallState["gallery-terminal"],
                      accessMatrix: window.__gibsonAccessMatrixState["gallery-access"],
                      orbitalMap: window.__gibsonOrbitalMapState["gallery-orbital"],
                      dataRain: window.__gibsonDataRainState["gallery-rain"],
                      traceRoute: window.__gibsonTraceRouteState["gallery-trace"],
                    })"""
                )
                vector_state = gallery_state["vector"]
                vector_animation_state = gallery_state["vectorAnimation"]
                vector_effect_state = gallery_state["vectorEffect"]
                hologram_state = gallery_state["hologram"]
                data_vault_state = gallery_state["dataVault"]
                black_ice_state = gallery_state["blackIce"]
                signal_scope_state = gallery_state["signalScope"]
                tunnel_state = gallery_state["tunnel"]
                wire_landscape_state = gallery_state["wireLandscape"]
                city_state = gallery_state["city"]
                spatial_map_state = gallery_state["spatialMap"]
                terminal_wall_state = gallery_state["terminalWall"]
                access_matrix_state = gallery_state["accessMatrix"]
                orbital_map_state = gallery_state["orbitalMap"]
                data_rain_state = gallery_state["dataRain"]
                trace_route_state = gallery_state["traceRoute"]
                assert vector_state == {
                    "pathCount": 3,
                    "morphPathCount": 1,
                    "morphFrameCount": 3,
                    "circleCount": 3,
                    "traceCount": 1,
                    "symbolCount": 6,
                    "symbolKinds": [
                        "globe",
                        "filesystem_gate",
                        "reticle",
                        "data_tunnel",
                        "ice_wall",
                        "mainframe_core",
                    ],
                    "labelCount": 2,
                    "rectCount": 2,
                    "lineCount": 2,
                    "polylineCount": 1,
                    "polygonCount": 1,
                    "groupCount": 1,
                    "keyframeCount": 6,
                    "ignoredMarkup": True,
                }
                assert vector_animation_state["keyframeCount"] == 6
                assert 0 <= vector_animation_state["progress"] <= 1
                assert vector_animation_state["scale"] > 0
                assert 0 <= vector_animation_state["opacity"] <= 1
                assert vector_effect_state["filterCount"] == 2
                assert vector_effect_state["filterKinds"] == ["chromatic_split", "scanline"]
                assert vector_effect_state["clipKind"] == "iris"
                assert vector_effect_state["clipActive"] is True
                assert 0 <= vector_effect_state["clipProgress"] <= 1
                assert hologram_state == {
                    "ringCount": 6,
                    "beamCount": 7,
                    "panelCount": 4,
                    "moteCount": 26,
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasScan": True,
                }
                assert data_vault_state == {
                    "layerCount": 4,
                    "ringCount": 5,
                    "panelCount": 6,
                    "lockCount": 5,
                    "packetCount": 36,
                    "tone": "amber",
                    "accentTone": "cyan",
                    "hasLabel": True,
                    "phase": data_vault_state["phase"],
                }
                assert 0 <= data_vault_state["phase"] <= 1
                assert black_ice_state == {
                    "columnCount": 13,
                    "rowCount": 6,
                    "fractureCount": 26,
                    "sentryCount": 7,
                    "breach": 0.58,
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasSweep": True,
                    "hasLabel": True,
                }
                assert signal_scope_state == {
                    "mode": "hybrid",
                    "ringCount": 5,
                    "spokeCount": 10,
                    "blipCount": 3,
                    "waveformCount": 2,
                    "hasSweep": True,
                    "tone": "green",
                    "accentTone": "magenta",
                    "hasLabels": True,
                }
                assert tunnel_state == {
                    "ringCount": 14,
                    "spokeCount": 18,
                    "laneCount": 9,
                    "packetCount": 44,
                    "direction": "inward",
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasLabels": True,
                    "phase": tunnel_state["phase"],
                }
                assert 0 <= tunnel_state["phase"] <= 1
                assert wire_landscape_state == {
                    "rowCount": 13,
                    "columnCount": 20,
                    "peakCount": 4,
                    "packetCount": 32,
                    "focusPeakId": "gibson",
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasLabels": True,
                }
                assert city_state["blockCount"] == 4
                assert city_state["focusBlockId"] == "core-2"
                assert city_state["cameraKeyframeCount"] == 3
                assert 0 <= city_state["cameraProgress"] <= 1
                assert city_state["cameraScale"] > 0
                assert spatial_map_state == {
                    "objectCount": 4,
                    "edgeCount": 3,
                    "focusObjectId": "file:src/harn_gibson/rendering.py",
                    "focusedEntityId": "file:src/harn_gibson/rendering.py",
                    "objectKinds": ["file", "symbol", "health"],
                    "worldBindingCount": 2,
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasLabels": True,
                }
                assert terminal_wall_state == {
                    "panelCount": 4,
                    "lineCount": 11,
                    "renderedLineCount": 11,
                    "columnCount": 2,
                    "rowCount": 2,
                    "activePanelId": "evt",
                    "streamingCount": 3,
                    "tone": "green",
                    "accentTone": "cyan",
                    "hasScan": True,
                    "hasCursor": True,
                    "panelLineCounts": [3, 2, 3, 3],
                    "panelRenderedLineCounts": [3, 2, 3, 3],
                }
                assert access_matrix_state == {
                    "rowCount": 3,
                    "columnCount": 4,
                    "cellCount": 8,
                    "activeCount": 3,
                    "lockedCount": 3,
                    "breachedCount": 1,
                    "focusCellId": "core",
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasSweep": True,
                    "hasLabels": True,
                }
                assert orbital_map_state == {
                    "nodeCount": 5,
                    "arcCount": 5,
                    "ringCount": 4,
                    "packetCount": 34,
                    "focusNodeId": "uplink",
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasScan": True,
                    "hasLabel": True,
                }
                assert data_rain_state == {
                    "columns": 42,
                    "direction": "down",
                    "density": 0.74,
                    "glyphCount": 20,
                    "bandCount": 3,
                    "hasGlitch": True,
                    "tone": "green",
                    "accentTone": "white",
                    "visibleColumns": data_rain_state["visibleColumns"],
                }
                assert 1 <= data_rain_state["visibleColumns"] <= 42
                assert trace_route_state == {
                    "hopCount": 4,
                    "linkCount": 3,
                    "packetCount": 18,
                    "focusHopId": "gibson",
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasLabels": True,
                }
                assert page.locator("svg").count() == 0
                assert page.locator("script", has_text="ignored").count() == 0
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_browser_display_renders_timeline_cue_animation() -> None:
    state = GibsonServerState()
    run_replay_file(EXAMPLE_REPLAYS / "animation-gallery.json", state)
    server, state, base = start_display_server(state)
    try:
        with sync_playwright() as driver:
            try:
                browser = driver.chromium.launch()
            except Error as exc:
                pytest.skip(f"Chromium is not installed for Playwright: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 700})
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("window.__gibsonTimelineCueState?.['gallery-cues']?.cueCount === 4")
                page.wait_for_function("window.__gibsonRouteTraceState?.['gallery-route']?.pointCount === 4")
                page.wait_for_function("window.__gibsonBreachWaveState?.['gallery-breach']?.ringCount === 5")
                page.wait_for_function(
                    "window.__gibsonSignalInterferenceState?.['gallery-interference']?.bandCount === 14"
                )
                page.wait_for_function("window.__gibsonCameraState?.animationIds?.includes('gallery-camera')")
                page.wait_for_function("window.__gibsonCameraState?.animationIds?.includes('gallery-camera-path')")
                state_payload = page.evaluate(
                    """() => ({
                      animationKinds: window.__gibsonAnimationState.kinds,
                      cueState: window.__gibsonTimelineCueState["gallery-cues"],
                      routeState: window.__gibsonRouteTraceState["gallery-route"],
                      breachState: window.__gibsonBreachWaveState["gallery-breach"],
                      interferenceState: window.__gibsonSignalInterferenceState["gallery-interference"],
                      cameraState: window.__gibsonCameraState,
                    })"""
                )
                assert "timeline_cue" in state_payload["animationKinds"]
                assert "route_trace" in state_payload["animationKinds"]
                assert "breach_wave" in state_payload["animationKinds"]
                assert "signal_interference" in state_payload["animationKinds"]
                assert "camera_jolt" in state_payload["animationKinds"]
                assert "camera_path" in state_payload["animationKinds"]
                assert state_payload["cueState"] == {
                    "targetId": "animation-vector",
                    "cueCount": 4,
                    "activeCueIndex": state_payload["cueState"]["activeCueIndex"],
                    "activeLabel": state_payload["cueState"]["activeLabel"],
                    "progress": state_payload["cueState"]["progress"],
                    "hasLabels": True,
                }
                assert 0 <= state_payload["cueState"]["activeCueIndex"] <= 3
                assert state_payload["cueState"]["activeLabel"] in {"QUEUE", "ROUTE", "BREACH", "HOLD"}
                assert 0 <= state_payload["cueState"]["progress"] <= 1
                assert state_payload["routeState"] == {
                    "targetId": "animation-ribbon",
                    "pointCount": 4,
                    "packetCount": 24,
                    "activePointId": state_payload["routeState"]["activePointId"],
                    "hasLabel": True,
                    "progress": state_payload["routeState"]["progress"],
                }
                assert state_payload["routeState"]["activePointId"] in {"queue", "route", "breach", "hold"}
                assert 0 <= state_payload["routeState"]["progress"] <= 1
                assert state_payload["breachState"] == {
                    "targetId": "animation-vector",
                    "ringCount": 5,
                    "shardCount": 34,
                    "tone": "magenta",
                    "accentTone": "white",
                    "hasLabel": True,
                    "progress": state_payload["breachState"]["progress"],
                }
                assert 0 <= state_payload["breachState"]["progress"] <= 1
                assert state_payload["interferenceState"] == {
                    "targetId": "scan-grid",
                    "bandCount": 14,
                    "blockCount": 32,
                    "noiseCount": 88,
                    "tone": "cyan",
                    "accentTone": "magenta",
                    "hasLabel": True,
                    "progress": state_payload["interferenceState"]["progress"],
                }
                assert 0 <= state_payload["interferenceState"]["progress"] <= 1
                assert state_payload["cameraState"] == {
                    "activeCount": 2,
                    "animationIds": ["gallery-camera", "gallery-camera-path"],
                    "targetIds": ["animation-city", "animation-ribbon"],
                    "anchorRefs": [
                        {
                            "source": "targetRef",
                            "primitiveId": "animation-city",
                            "kind": "block",
                            "objectId": "fx-city-2",
                            "label": "scan",
                        },
                        {
                            "source": "targetRef",
                            "primitiveId": "animation-ribbon",
                            "kind": "point",
                            "index": 3,
                        },
                    ],
                    "kinds": ["camera_jolt", "camera_path"],
                    "pathKeyframeCount": 3,
                    "anchorX": state_payload["cameraState"]["anchorX"],
                    "anchorY": state_payload["cameraState"]["anchorY"],
                    "x": state_payload["cameraState"]["x"],
                    "y": state_payload["cameraState"]["y"],
                    "scale": state_payload["cameraState"]["scale"],
                    "rotation": state_payload["cameraState"]["rotation"],
                }
                assert state_payload["cameraState"]["anchorX"] > 0
                assert state_payload["cameraState"]["anchorY"] > 0
                assert state_payload["cameraState"]["scale"] > 0
                assert_canvas_nonblank(page)
            finally:
                browser.close()
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def test_replay_frame_review_html_player_switches_frames() -> None:
    html = replay_frame_review_html(
        {
            "schema": "harn-gibson.replay-frame-screenshots.v1",
            "replayName": "browser review",
            "screenshotCount": 2,
            "frames": [
                {
                    "index": 0,
                    "step": {"kind": "event", "sceneRevision": 1, "updates": 1},
                    "screenshot": {"path": "frame-a.png", "canvasMetrics": {"nonblank": True}},
                },
                {
                    "index": 1,
                    "step": {"kind": "mutations", "sceneRevision": 2, "updates": 3, "route": "direct_scene"},
                    "screenshot": {"path": "frame-b.png", "canvasMetrics": {"nonblank": False}},
                },
            ],
        }
    )
    with sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except Error as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        try:
            page = browser.new_page(viewport={"width": 900, "height": 700})
            page.set_content(html, wait_until="domcontentloaded")
            expect(page.locator("#timelineCounter")).to_have_text("1 / 2")
            expect(page.locator("#frameMeta")).to_contain_text("frame 0")
            page.locator('[data-frame-select="1"]').click()
            expect(page.locator("#timelineCounter")).to_have_text("2 / 2")
            expect(page.locator("#frameMeta")).to_contain_text("route direct_scene")
            expect(page.locator("#frameHealth")).to_have_attribute("data-ok", "false")
            assert page.locator("#activeFrame").get_attribute("src").endswith("frame-b.png")
            assert page.evaluate("window.__gibsonReplayFrames.length") == 2
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("fixture_name", "screenshot_name"),
    [
        ("animation-gallery.json", "replay-animation-gallery.png"),
        ("stream-and-diagnostic.json", "replay-stream-and-diagnostic.png"),
        ("renderer-plan.json", "replay-renderer-plan.png"),
        ("primitive-gallery.json", "replay-primitive-gallery.png"),
        ("style-showcase.json", "replay-style-showcase.png"),
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
