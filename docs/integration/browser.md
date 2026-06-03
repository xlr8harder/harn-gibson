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

Screenshots from the browser integration test are written under `test-artifacts/screenshots/`.

Replay fixtures can also render their final scene through the browser display:

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

The integration test verifies:

- the display shell renders in desktop and mobile viewports;
- posting a harn-style event mutates the scene;
- the debug drawer toggles;
- the browser input composer enqueues a message and `/input/next` drains it.
- the checked-in agent-side and renderer-side replay fixtures render through the browser screenshot path.
