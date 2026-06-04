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
uv run harn-gibson dogfood-capture --list-trajectories
uv run harn-gibson dogfood-capture --trajectory tiny-project
uv run harn-gibson dogfood-capture --trajectory repo-map
```

Built-in trajectories create ignored bare workspaces under `test-artifacts/dogfood-workspaces/`, inject long capture prompts from `examples/prompts/`, write ignored capture logs under `test-artifacts/captures/`, default to split fixture conversion, and print the exact review command when harn exits. `tiny-project` is the general bootstrap path; `repo-map` creates a broader depth-2 layout so repo-city visuals can be checked against varied directories, line counts, and touched-file batches. For a custom workspace or edited prompt, use the manual form:

```bash
mkdir -p test-artifacts/dogfood-workspaces/custom-tiny-project
uv run harn-gibson dogfood-capture \
  --cwd test-artifacts/dogfood-workspaces/custom-tiny-project \
  --split-every 200 \
  -- -p "$(cat examples/prompts/dogfood-tiny-project.md)"
```

It launches the display with `examples/renderers/gibson_dogfood_renderer.py`, writes normalized event payloads as JSONL under ignored `test-artifacts/captures/` by default, and prints the exact conversion command for that capture. `--cwd` runs harn in a separate project directory while harn-gibson injects this repo's extension plus the Codex provider/model defaults explicitly, so the target directory does not need its own `.harn/settings.json`; renderer context and repo-city visuals use that target directory as `HARN_GIBSON_PROJECT_ROOT`. The built-in prompt templates are intentionally designed to produce long trajectories with git initialization, file creation, edits, tests, command failures, fixes, commits, final status, and in the `repo-map` case a file listing plus line-count summary for depth-2 city mapping. Pass `--event-log path/to/session.jsonl` if you want a stable capture path. With `--cwd`, relative event-log paths are resolved before launching harn so the log still lands under the launcher directory, not the target project, and the printed follow-up command includes the matching `--project-root` and `--project-name` for replay review. For longer custom sessions, pass `--split-every N` to make the printed follow-up command use split fixture conversion and suite review. Captures can contain prompts, tool output, file paths, diagnostics, and tracebacks, so keep the raw JSONL out of committed fixtures. `event-log-to-replay` redacts common token, key, password, and credential values by default before writing replay JSON; use `--no-redact-sensitive` only for private local debugging.

When `HARN_GIBSON_EVENT_LOG` is set directly, the harn extension writes the same normalized event payloads as JSONL. Convert a captured log into a replay fixture with:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output examples/replays/captured-session.json \
  --output-result test-artifacts/replays/captured-session-result.json \
  --name "captured dogfood session" \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-review \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py'
```

Without `--output`, the fixture JSON is printed to stdout. The generated fixture uses `event` steps so hook decisions and renderer routing are replayed through the same path as live display events. Fixture metadata includes `redaction.enabled` and `redaction.count` so promoted captures can be audited for scrubbed values. `--visual-fixture` adds capture-summary metadata plus `screenshotExpect` checks for nonblank browser output, `canvasMetrics.litRatio >= 0.02`, and `canvasMetrics.maxChannelTotal >= 60`; use `--screenshot-lit-min` and `--screenshot-max-channel-min` to tune those thresholds for a specific long capture. `--output-result` replays the converted fixture and writes `harn-gibson.replay-result.v1` JSON, reusing the review replay when `--review-dir` is also present. `--review-dir` immediately replays the converted log, captures frame screenshots, renderer contexts, provider-neutral prompts, renderer chunks, render intents, and writes the same HTML review bundle as `harn-gibson replay --review-dir`. The bundle manifest and overview page promote capture duration, event types, phases, and sources when capture-summary metadata is present, which makes longer dogfood trajectories easier to compare at a glance.

Long captures can be split into smaller replay fixtures:

```bash
uv run harn-gibson event-log-to-replay .harn-gibson.jsonl \
  --output-dir test-artifacts/replays/captured-session-split \
  --split-every 200 \
  --output-result test-artifacts/replays/captured-session-split-result.json \
  --name "captured dogfood session" \
  --visual-fixture \
  --review-dir test-artifacts/replays/captured-session-split-review
```

Split mode writes `manifest.json` plus numbered replay fixtures such as `captured-dogfood-session-0001.json`. Each fixture has `metadata.eventLogChunk` with the chunk index, total chunk count, event offsets, and total capture size; each visual fixture also carries chunk-level capture-summary metadata and screenshot expectations. The split manifest has schema `harn-gibson.event-log-split.v1`, the full capture summary, and the relative fixture filenames. `--split-every` requires `--output-dir` and does not combine with `--output`. `--output-result` replays the generated directory and writes `harn-gibson.replay-suite-result.v1` JSON for quick CI or trajectory auditing, including the original `splitManifest` and top-level `captureSummary` when the replay directory has a split manifest. When `--review-dir` is present, conversion immediately replays the generated split directory, captures per-chunk browser frames, renderer contexts, prompts, renderer chunks, render intents, final scenes, and result JSON, then writes a suite overview. You can also run `replay-dir` on the generated directory later:

```bash
uv run harn-gibson replay-dir test-artifacts/replays/captured-session-split \
  --screenshot-dir test-artifacts/replays/captured-session-split-screenshots \
  --review-dir test-artifacts/replays/captured-session-split-review \
  --project-root test-artifacts/dogfood-workspaces/tiny-project
```

`replay-dir` skips `manifest.json` metadata files, so split fixture directories can be replayed directly. `--project-root PATH` and `--project-name NAME` are explicit replay controls for renderer context; use them when the original target workspace still exists and repo topology should match the captured session. `--review-dir` creates `harn-gibson.replay-suite-review.v1`: a top-level `manifest.json` and `index.html` with aggregate metrics, split capture summary, reviewed event/route/renderer coverage, per-fixture status, and links into one complete review bundle per replay fixture under `files/`.

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

The command exits with status `1` if any fixture fails to load, replay, satisfy expectations, match its requested baseline, render its requested browser screenshot, or satisfy screenshot expectations. Browser screenshots also sample the `#grid` canvas and fail if it is blank. The suite result JSON uses `harn-gibson.replay-suite-result.v1` and records per-file step counts, scene revisions, expectation counts, screenshot expectation counts, baseline metadata, screenshot metadata, canvas metrics, event summaries, renderer/route counts, visual-continuity summaries, and failures. Event summaries include bounded tool counts, command-field counts, failed tool-result counts, touched-file entries, touched paths, and top-level touched areas using the same path extraction as renderer context. The top-level `summary` aggregates those counts across the suite, so long dogfood captures can be compared for event mix, timing, tools, touched files, routes, renderers, screenshots, baseline coverage, visible anchors, active animations, effects, targets, and renderer continuity before opening the review bundle.

Use `--style gibson`, `--style neon-noir`, `--style mainframe`, or `--style satellite-uplink` to render replay scenes through a specific style pack. Styled runs put the style pack in scene metadata and browser screenshots, so use a matching baseline directory if the style affects expected final scene state.

Replay does not use ambient `HARN_GIBSON_RENDERER_COMMAND` or `HARN_GIBSON_RENDERER_MODEL_COMMAND` values by default. That keeps baseline verification deterministic even when a dogfood shell has renderer environment configured. To intentionally exercise renderer adapters offline, pass explicit flags to `replay` or `replay-dir`:

```bash
uv run harn-gibson replay examples/replays/stream-and-diagnostic.json \
  --renderer-model-command 'uv run python examples/renderers/gibson_prompt_echo_renderer.py' \
  --renderer-model-timeout-ms 10000 \
  --project-root test-artifacts/dogfood-workspaces/tiny-project \
  --output-render-prompts test-artifacts/replays/prompts.json \
  --output-scene test-artifacts/replays/model-scene.json

uv run harn-gibson replay-dir examples/replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root test-artifacts/dogfood-workspaces/tiny-project
```

The model command receives `harn-gibson.model-renderer-request.v1`; the external command receives `harn-gibson.external-renderer-request.v1`. Returned plans still go through the same validation, diagnostics, fail-open fallback, and final-scene expectation checks as live dogfood rendering.

The hard-coded `gibson_dogfood_renderer.py` is meant for live harn use before the renderer-agent backend is good enough. The checked-in `examples/dogfood-replays/` fixtures exercise that renderer against `examples/dogfood-workspaces/tiny-project`, giving the showcase renderer committed trajectories for project bootstrapping, runtime diagnostics, failed tests, browser steering input, repo-topology, touched-file signals, active style packs, and the project hologram/data-vault/data-tunnel/ICE-mesh/control-graph/glyph-layer/ribbon/repo-city/spark-field scene. A useful future fixture workflow is to run `uv run harn-gibson dogfood-capture --trajectory tiny-project` for a general bootstrap capture and `uv run harn-gibson dogfood-capture --trajectory repo-map` for a topology-heavy capture, then convert those event trajectories into split replay directories and browser screenshots. Several such trajectories should become regression inputs for event coalescing, renderer timing, touched-file visualization, style-pack rendering, and visual continuity.

## Baseline Review

Replay baselines are canonical final-scene snapshots. They compare the visual state that renderers leave behind, including primitives, animations, event logs, and render-intent history. Absolute render-intent start/end timestamps are normalized out of baselines; duration, effects, targets, routes, and metadata remain comparable.

The checked-in fixtures include `primitive-gallery.json`, `animation-gallery.json`, and `style-showcase.json`, which are browser-review fixtures for the generic primitive/effect/style set rather than captured harn sessions. The primitive gallery includes a `hologram` projection, animated `signal_scope` radar/oscilloscope instrument, animated `tunnel_grid` data corridor, rotating `data_vault` core, animated `trace_route`, camera-drifting `city_block`, structured `svg_layer` paths, path morph frames, rects, lines, polylines, polygons, grouped transforms, numeric transform keyframes, gradients, filter/clip presets, traces, curated vector symbols, and a `data_rain` glyph curtain, so it is the quickest fixture for reviewing explicit vector or cinematic primitive rendering changes. The animation gallery covers persistent effects, including `timeline_cue` beat markers, `breach_wave` overlays, `camera_jolt` impacts, and `camera_path` scene transforms for coalesced render windows. The style showcase applies the `satellite-uplink` style pack through scene state and pairs orbital backdrop motifs with radar, route, hologram, city, vector, data-rain, and timeline-cue primitives.

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

Check the hard-coded dogfood renderer trajectory against its committed baseline and browser screenshot expectations:

```bash
uv run harn-gibson replay-dir examples/dogfood-replays \
  --renderer-command 'uv run python examples/renderers/gibson_dogfood_renderer.py' \
  --renderer-timeout-ms 10000 \
  --project-root examples/dogfood-workspaces/tiny-project \
  --project-name tiny-project \
  --baseline-dir examples/baselines/dogfood-replays \
  --screenshot-dir test-artifacts/replays/dogfood-screenshots
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
  --style satellite-uplink
```

Screenshot result metadata includes `canvasMetrics` with canvas dimensions, sampled pixel count, luminance total, lit-pixel count, lit ratio, maximum channel total, and a `nonblank` boolean. This makes replay screenshot artifacts reviewable in CI output even before a human opens the PNG. Checked-in fixtures use conservative `screenshotExpect` thresholds so browser rendering can fail fast if a fixture becomes blank or severely underlit.

`--review-dir` is the fastest historical-session review path. It captures renderer contexts and per-step frames automatically, renders timeline screenshots under `frames/`, and writes `scene.json`, `result.json`, `timeline.json`, `renderer-contexts.json`, `renderer-prompts.json`, `renderer-chunks.json`, `renderer-chunks.html`, `renderer-prompts.html`, `render-intents.json`, `render-intents.html`, `frames/index.html`, `frames/manifest.json`, `manifest.json`, and a top-level `index.html` overview. Use the lower-level flags below when CI only needs one artifact family.

`replay-dir --review-dir` applies the same review-bundle generation to every replay file in a directory. This is the preferred review path for split long captures: the suite overview records aggregate counts, reviewed event types, renderer routes, renderer names, and links to each chunk's frame player, prompts, renderer chunks, render intents, final scene, and raw result JSON.

`--output-render-contexts` records each `harn-gibson.renderer-context.v1` payload that replay sent to a renderer. Stream-buffer, debug-only, direct-scene, and saved-render-plan steps do not invent renderer contexts; the artifact is an exact review aid for model-renderer prompt inputs, compaction cadence, repo topology, touched-file extraction, and render-input batching.

`--output-render-prompts` writes `harn-gibson.replay-renderer-prompts.v1`, a provider-neutral system/user message artifact built from the captured renderer contexts. `--render-prompt-review` writes a standalone HTML page that shows those messages, event types, routes, timing, and prompt sizes before any live model adapter exists. This lets prompt shape, context size, and safety instructions be reviewed offline from the same replay fixture used for scene screenshots.

`--output-render-chunks` writes `harn-gibson.replay-renderer-chunks.v1`, grouping captured renderer contexts and their exact prompt artifacts into feedable batches. Use `--render-chunk-size N` to control how many renderer contexts are included per chunk. Each chunk records context indexes, modes, display styles, event types, routes, request counts, a covered timeline, estimated prompt/context characters, visual-continuity summaries for anchors, active animations, recent effects, recent targets, recent renderers, and style motifs, the original contexts including `visualContinuity` anchors, and the provider-neutral prompts. `--render-chunk-review` writes a standalone HTML page over the same payload so historical-session batches can be inspected without opening the full JSON. This is meant for renderer experiments where a full session should be replayed to a model in pieces.

`--output-render-intents` writes `harn-gibson.replay-render-intents.v1`, a compact artifact extracted from `scene.metadata.renderIntents`. It preserves exact render-intent timelines, renderer names, requested intents, event types, routes, effects, targets, mutation counts, and plan metadata. `--render-intent-review` writes a standalone HTML page over the same payload, which is useful when reviewing whether a renderer planned a coherent sequence before looking at screenshots or final scene JSON.

`--output-timeline` enables per-step frame capture and writes `harn-gibson.replay-timeline.v1`. Each frame contains the replay step result and the full scene snapshot after that step. `--timeline-screenshot-dir` renders those captured frames as `frame-0000.png`, `frame-0001.png`, and so on, plus a `manifest.json` with canvas metrics for each screenshot and an `index.html` review page. The review page includes a large active frame, previous/next controls, autoplay, a scrubber, and a clickable filmstrip. This is intentionally separate from the final-scene baseline so a long captured session can be reviewed, chunked for a future renderer agent, or converted into screenshot/keyframe tooling without changing ordinary replay result size.
