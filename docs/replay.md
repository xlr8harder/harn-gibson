# Replay Fixtures

Replay fixtures use `harn-gibson.replay.v1`. They can replay either side of the renderer boundary and can optionally assert the final scene state.

```json
{
  "schema": "harn-gibson.replay.v1",
  "name": "fixture name",
  "metadata": {"purpose": "why this exists"},
  "expect": {
    "sceneRevision": 1,
    "checks": [
      {"path": "primitives.status.props.text", "equals": "before:tool_call"},
      {"path": "log", "contains": {"eventType": "tool_call"}},
      {"path": "animations.pulse-1", "exists": true}
    ]
  },
  "steps": []
}
```

## Step Types

- `event`: accepts a normalized `GibsonEvent` dictionary and runs it through routing and rendering.
- `raw_event`: accepts a raw harn-style event plus optional sequence, timestamp, source, recent context, visualization context, and hook decisions.
- `render_plan`: applies saved renderer requests and delayed render steps directly against scene state.
- `mutations`: applies explicit scene mutations, optionally associated with a normalized event.

## Captured Event Logs

When `HARN_GIBSON_EVENT_LOG` is set, the harn extension writes normalized event payloads as JSONL. Convert that captured log into a replay fixture with:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json \
  --name "captured dogfood session"
```

Without `--output`, the fixture JSON is printed to stdout. The generated fixture uses `event` steps so hook decisions and renderer routing are replayed through the same path as live display events.

## Expectations

`expect.sceneRevision` is shorthand for `{"path": "revision", "equals": N}`. `expect.checks` paths are dot-separated paths into the final `SceneState.to_dict()` payload. Numeric path segments index arrays.

Supported operations:

- `equals`: exact value equality at the path.
- `contains`: list contains an item, string contains a substring, or object contains the expected partial object.
- `exists`: path existence matches the boolean value.

`harn-gibson replay` exits with status `1` when expectations fail and prints each failed check to stderr. Successful expectation results are also included in `--output-result`.

## Batch Verification

Run every replay JSON under a directory with `replay-dir`:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

The command exits with status `1` if any fixture fails to load, replay, satisfy expectations, match its requested baseline, or render its requested browser screenshot. Browser screenshots also sample the `#grid` canvas and fail if it is blank. The suite result JSON uses `harn-gibson.replay-suite-result.v1` and records per-file step counts, scene revisions, expectation counts, baseline metadata, screenshot metadata, canvas metrics, and failures.

Use `--style gibson`, `--style neon-noir`, or `--style mainframe` to render replay scenes through a specific style pack. Styled runs put the style pack in scene metadata and browser screenshots, so use a matching baseline directory if the style affects expected final scene state.

## Baseline Review

Replay baselines are canonical final-scene snapshots. They compare the visual state that renderers leave behind, including primitives, animations, event logs, and render-intent history. Absolute render-intent start/end timestamps are normalized out of baselines; duration, effects, targets, routes, and metadata remain comparable.

The checked-in fixtures include `primitive-gallery.json` and `animation-gallery.json`, which are browser-review fixtures for the generic primitive/effect set rather than captured harn sessions. The primitive gallery includes structured `svg_layer` paths, gradients, traces, and curated vector symbols, so it is the quickest fixture for reviewing explicit SVG-style rendering changes.

Update baselines after intentionally changing visual output:

```bash
uv run harn-gibson replay-dir examples/replays \
  --baseline-dir examples/baselines/replays \
  --update-baselines
```

Check current output against committed baselines:

```bash
uv run harn-gibson replay-dir examples/replays \
  --baseline-dir examples/baselines/replays
```

## Screenshot Review

Replay fixtures can still write final scene JSON, full replay result JSON, and browser screenshots:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --output-scene test-artifacts/replays/scene.json \
  --output-result test-artifacts/replays/result.json \
  --output-timeline test-artifacts/replays/timeline.json \
  --screenshot test-artifacts/replays/scene.png \
  --style neon-noir
```

Screenshot result metadata includes `canvasMetrics` with canvas dimensions, sampled pixel count, luminance total, lit-pixel count, lit ratio, maximum channel total, and a `nonblank` boolean. This makes replay screenshot artifacts reviewable in CI output even before a human opens the PNG.

`--output-timeline` enables per-step frame capture and writes `harn-gibson.replay-timeline.v1`. Each frame contains the replay step result and the full scene snapshot after that step. This is intentionally separate from the final-scene baseline so a long captured session can be reviewed, chunked for a future renderer agent, or converted into screenshot/keyframe tooling without changing ordinary replay result size.
