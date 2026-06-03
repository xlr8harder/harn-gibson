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

Screenshots from the browser integration test are written under `test-artifacts/screenshots/`. Replay screenshot capture also records canvas metrics and rejects blank canvas output by default.

Replay fixtures can also render their final scene through the browser display. The browser integration suite renders the checked-in replay fixtures, verifies nonblank canvas metrics, checks non-default style packs, and inspects the `svg_layer` structured vector render state used by the primitive gallery:

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

For historical-session review, `harn-gibson replay --review-dir ...` writes a complete review bundle with scene/result/timeline JSON, renderer contexts, renderer prompts, render intents, frame screenshots, a frame player, prompt/intent review pages, and a top-level overview. The lower-level `--timeline-screenshot-dir` flag renders each captured replay keyframe through the same browser path and writes a manifest with canvas metrics per frame plus an interactive `index.html` frame player.

The integration test verifies:

- the display shell renders in desktop and mobile viewports;
- posting a harn-style event mutates the scene;
- the debug drawer toggles;
- the browser input composer enqueues a message and `/input/next` drains it.
- a non-default style pack reaches scene metadata, CSS variables, and browser runtime state.
- the checked-in agent-side and renderer-side replay fixtures render through the browser screenshot path with nonblank canvas metrics.
- the generated replay frame review page can switch active frames in headless Chromium.
