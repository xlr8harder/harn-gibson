# Browser Integration Tests

Browser tests use Playwright to render the local display in headless Chromium and capture screenshots.

Install browser binaries once per environment:

```bash
uv run playwright install chromium
```

Run all tests:

```bash
uv run pytest
```

Run the full local release gate, including replay browser screenshots and hygiene scans:

```bash
bash scripts/acceptance.sh
```

Screenshots from the browser integration test are written under `test-artifacts/screenshots/`. Replay screenshot capture also records canvas metrics, rejects blank canvas output by default, and can apply fixture-level `screenshotExpect` checks against those metrics. The replay screenshot path loads the display with `?capture=1`, renders only when scene state changes, and waits for `window.__gibsonCaptureReady` before reading canvas metrics so review bundles do not depend on an endlessly animated browser loop.

Replay fixtures can also render their final scene through the browser display. The browser integration suite renders the checked-in replay fixtures, verifies nonblank canvas metrics, checks non-default style packs, inspects style-showcase backdrop motif state, and inspects the `hologram` projection state, `signal_scope` radar/waveform state, `tunnel_grid` corridor state, `wire_landscape` terrain state, `terminal_wall` panel-bank state, `access_matrix` lock-grid state, `orbital_map` uplink-globe state, `data_vault` wireframe state, `black_ice` barrier state, `trace_route` packet state, `city_block` camera state, `svg_layer` structured vector render state including path morph counts, sampled vector keyframe state, vector filter/clip effect state, `timeline_cue`, `route_trace`, `signal_interference`, `breach_wave`, `camera_jolt`, and `camera_path` animation state, plus `data_rain` glyph-curtain state used by the primitive gallery:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --output-scene test-artifacts/replays/scene.json \
  --output-result test-artifacts/replays/result.json \
  --screenshot test-artifacts/replays/scene.png

uv run harn-gibson replay examples/replays/renderer-plan.json \
  --output-scene test-artifacts/replays/renderer-scene.json \
  --output-result test-artifacts/replays/renderer-result.json \
  --screenshot test-artifacts/replays/renderer-scene.png
```

For historical-session review, `harn-gibson replay --review-dir ...` writes a complete review bundle with scene/result/timeline JSON, renderer contexts, chunked renderer context/prompt batches, a renderer chunk review page, renderer prompts, render intents, frame screenshots, a frame player, prompt/intent review pages, and a top-level overview. `harn-gibson replay-dir --review-dir ...` writes the same per-fixture bundles under `files/` plus a suite overview for split long captures. The lower-level `--timeline-screenshot-dir` flag renders each captured replay keyframe through the same browser path and writes a manifest with canvas metrics per frame plus an interactive `index.html` frame player. For live visual inspection, `harn-gibson watch-replay PATH --renderer-command ...` starts the display server, opens the browser, and publishes full replays or selected step ranges over SSE with fixed or timestamp-paced replay timing.

The integration test verifies:

- the display shell renders in desktop and mobile viewports;
- posting a harn-style event mutates the scene;
- the debug drawer toggles;
- the browser input composer enqueues a message and `/input/next` drains it.
- a non-default style pack reaches scene metadata, CSS variables, browser runtime state, and backdrop motif overlays.
- the checked-in agent-side and renderer-side replay fixtures render through the browser screenshot path with nonblank canvas metrics and fixture-level screenshot metric expectations.
- the generated replay frame review page can switch active frames in headless Chromium.
