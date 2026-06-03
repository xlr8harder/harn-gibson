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
  "screenshotExpect": {
    "nonblank": true,
    "checks": [
      {"path": "canvasMetrics.litRatio", "min": 0.02},
      {"path": "canvasMetrics.maxChannelTotal", "min": 60}
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

For live trajectory capture, use the dogfood capture wrapper:

```bash
uv run harn-gibson dogfood-capture -- -p "bootstrap a tiny project here"
```

It launches the display with `examples/renderers/gibson_dogfood_renderer.py`, writes normalized event payloads as JSONL under ignored `test-artifacts/captures/` by default, and prints the exact conversion command for that capture. Pass `--event-log path/to/session.jsonl` for a stable path. Captures can contain prompts, tool output, file paths, diagnostics, and tracebacks, so keep them out of committed fixtures until they have been reviewed and scrubbed.

When `HARN_GIBSON_EVENT_LOG` is set directly, the harn extension writes the same normalized event payloads as JSONL. Convert a captured log into a replay fixture with:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json \
  --name "captured dogfood session" \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-review \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py'
```

Without `--output`, the fixture JSON is printed to stdout. The generated fixture uses `event` steps so hook decisions and renderer routing are replayed through the same path as live display events. `--visual-fixture` adds capture-summary metadata plus `screenshotExpect` checks for nonblank browser output, `canvasMetrics.litRatio >= 0.02`, and `canvasMetrics.maxChannelTotal >= 60`; use `--screenshot-lit-min` and `--screenshot-max-channel-min` to tune those thresholds for a specific long capture. `--review-dir` immediately replays the converted log, captures frame screenshots, renderer contexts, provider-neutral prompts, renderer chunks, render intents, and writes the same HTML review bundle as `harn-gibson replay --review-dir`. The bundle manifest and overview page promote capture duration, event types, phases, and sources when capture-summary metadata is present, which makes longer dogfood trajectories easier to compare at a glance.

## Expectations

`expect.sceneRevision` is shorthand for `{"path": "revision", "equals": N}`. `expect.checks` paths are dot-separated paths into the final `SceneState.to_dict()` payload. Numeric path segments index arrays.

Supported operations:

- `equals`: exact value equality at the path.
- `contains`: list contains an item, string contains a substring, or object contains the expected partial object.
- `exists`: path existence matches the boolean value.
- `min`: numeric value at the path is greater than or equal to the expected number.
- `max`: numeric value at the path is less than or equal to the expected number.

`harn-gibson replay` exits with status `1` when expectations fail and prints each failed check to stderr. Successful expectation results are also included in `--output-result`.

When browser screenshots are captured, `screenshotExpect` applies the same check format to screenshot metadata instead of scene state. It is evaluated only by screenshot-producing paths such as `replay-dir --screenshot-dir`; ordinary replay runs still work without launching a browser. The `nonblank` shorthand checks `canvasMetrics.nonblank`.

## Batch Verification

Run every replay JSON under a directory with `replay-dir`:

```bash
uv run harn-gibson replay-dir examples/replays \
  --output-result test-artifacts/replays/suite.json \
  --baseline-dir examples/baselines/replays \
  --screenshot-dir test-artifacts/replays/screenshots
```

The command exits with status `1` if any fixture fails to load, replay, satisfy expectations, match its requested baseline, render its requested browser screenshot, or satisfy screenshot expectations. Browser screenshots also sample the `#grid` canvas and fail if it is blank. The suite result JSON uses `harn-gibson.replay-suite-result.v1` and records per-file step counts, scene revisions, expectation counts, screenshot expectation counts, baseline metadata, screenshot metadata, canvas metrics, and failures.

Use `--style gibson`, `--style neon-noir`, or `--style mainframe` to render replay scenes through a specific style pack. Styled runs put the style pack in scene metadata and browser screenshots, so use a matching baseline directory if the style affects expected final scene state.

Replay does not use ambient `HARN_GIBSON_RENDERER_COMMAND` or `HARN_GIBSON_RENDERER_MODEL_COMMAND` values by default. That keeps baseline verification deterministic even when a dogfood shell has renderer environment configured. To intentionally exercise renderer adapters offline, pass explicit flags to `replay` or `replay-dir`:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --renderer-model-command 'uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
  --renderer-model-timeout-ms 10000 \
  --output-render-prompts test-artifacts/replays/prompts.json \
  --output-scene test-artifacts/replays/model-scene.json

uv run harn-gibson replay-dir examples/replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000
```

The model command receives `harn-gibson.model-renderer-request.v1`; the external command receives `harn-gibson.external-renderer-request.v1`. Returned plans still go through the same validation, diagnostics, fail-open fallback, and final-scene expectation checks as live dogfood rendering.

The hard-coded `gibson_dogfood_renderer.py` is meant for live harn use before the renderer-agent backend is good enough. A useful future fixture workflow is to run `uv run harn-gibson dogfood-capture`, ask harn to spend 15-20 minutes bootstrapping a tiny project in a bare directory, then convert that event trajectory into replay fixtures and browser screenshots. Several such trajectories should become regression inputs for event coalescing, renderer timing, touched-file visualization, and visual continuity.

## Baseline Review

Replay baselines are canonical final-scene snapshots. They compare the visual state that renderers leave behind, including primitives, animations, event logs, and render-intent history. Absolute render-intent start/end timestamps are normalized out of baselines; duration, effects, targets, routes, and metadata remain comparable.

The checked-in fixtures include `primitive-gallery.json` and `animation-gallery.json`, which are browser-review fixtures for the generic primitive/effect set rather than captured harn sessions. The primitive gallery includes a `hologram` projection, animated `signal_scope` radar/oscilloscope instrument, animated `tunnel_grid` data corridor, animated `trace_route`, camera-drifting `city_block`, structured `svg_layer` paths, path morph frames, rects, lines, polylines, polygons, grouped transforms, numeric transform keyframes, gradients, filter/clip presets, traces, curated vector symbols, and a `data_rain` glyph curtain, so it is the quickest fixture for reviewing explicit vector or cinematic primitive rendering changes. The animation gallery covers persistent effects, including `timeline_cue` beat markers, `breach_wave` overlays, `camera_jolt` impacts, and `camera_path` scene transforms for coalesced render windows.

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
  --output-render-contexts test-artifacts/replays/renderer-contexts.json \
  --output-render-prompts test-artifacts/replays/renderer-prompts.json \
  --output-render-chunks test-artifacts/replays/renderer-chunks.json \
  --render-chunk-size 4 \
  --render-chunk-review test-artifacts/replays/renderer-chunks.html \
  --render-prompt-review test-artifacts/replays/renderer-prompts.html \
  --output-render-intents test-artifacts/replays/render-intents.json \
  --render-intent-review test-artifacts/replays/render-intents.html \
  --review-dir test-artifacts/replays/review \
  --output-timeline test-artifacts/replays/timeline.json \
  --timeline-screenshot-dir test-artifacts/replays/timeline-frames \
  --screenshot test-artifacts/replays/scene.png \
  --style neon-noir
```

Screenshot result metadata includes `canvasMetrics` with canvas dimensions, sampled pixel count, luminance total, lit-pixel count, lit ratio, maximum channel total, and a `nonblank` boolean. This makes replay screenshot artifacts reviewable in CI output even before a human opens the PNG. Checked-in fixtures use conservative `screenshotExpect` thresholds so browser rendering can fail fast if a fixture becomes blank or severely underlit.

`--review-dir` is the fastest historical-session review path. It captures renderer contexts and per-step frames automatically, renders timeline screenshots under `frames/`, and writes `scene.json`, `result.json`, `timeline.json`, `renderer-contexts.json`, `renderer-prompts.json`, `renderer-chunks.json`, `renderer-chunks.html`, `renderer-prompts.html`, `render-intents.json`, `render-intents.html`, `frames/index.html`, `frames/manifest.json`, `manifest.json`, and a top-level `index.html` overview. Use the lower-level flags below when CI only needs one artifact family.

`--output-render-contexts` records each `harn-gibson.renderer-context.v1` payload that replay sent to a renderer. Stream-buffer, debug-only, direct-scene, and saved-render-plan steps do not invent renderer contexts; the artifact is an exact review aid for model-renderer prompt inputs, compaction cadence, repo topology, touched-file extraction, and render-input batching.

`--output-render-prompts` writes `harn-gibson.replay-renderer-prompts.v1`, a provider-neutral system/user message artifact built from the captured renderer contexts. `--render-prompt-review` writes a standalone HTML page that shows those messages, event types, routes, timing, and prompt sizes before any live model adapter exists. This lets prompt shape, context size, and safety instructions be reviewed offline from the same replay fixture used for scene screenshots.

`--output-render-chunks` writes `harn-gibson.replay-renderer-chunks.v1`, grouping captured renderer contexts and their exact prompt artifacts into feedable batches. Use `--render-chunk-size N` to control how many renderer contexts are included per chunk. Each chunk records context indexes, modes, display styles, event types, routes, request counts, a covered timeline, estimated prompt/context characters, the original contexts including `visualContinuity` anchors, and the provider-neutral prompts. `--render-chunk-review` writes a standalone HTML page over the same payload so historical-session batches can be inspected without opening the full JSON. This is meant for renderer experiments where a full session should be replayed to a model in pieces.

`--output-render-intents` writes `harn-gibson.replay-render-intents.v1`, a compact artifact extracted from `scene.metadata.renderIntents`. It preserves exact render-intent timelines, renderer names, requested intents, event types, routes, effects, targets, mutation counts, and plan metadata. `--render-intent-review` writes a standalone HTML page over the same payload, which is useful when reviewing whether a renderer planned a coherent sequence before looking at screenshots or final scene JSON.

`--output-timeline` enables per-step frame capture and writes `harn-gibson.replay-timeline.v1`. Each frame contains the replay step result and the full scene snapshot after that step. `--timeline-screenshot-dir` renders those captured frames as `frame-0000.png`, `frame-0001.png`, and so on, plus a `manifest.json` with canvas metrics for each screenshot and an `index.html` review page. The review page includes a large active frame, previous/next controls, autoplay, a scrubber, and a clickable filmstrip. This is intentionally separate from the final-scene baseline so a long captured session can be reviewed, chunked for a future renderer agent, or converted into screenshot/keyframe tooling without changing ordinary replay result size.
