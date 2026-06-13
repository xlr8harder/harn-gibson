"""Command-line entry points for harn-gibson."""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from harn_gibson import __version__
from harn_gibson.auth import import_codex_auth
from harn_gibson.extension import extension_path
from harn_gibson.renderers import DEFAULT_RENDERER, direct_renderer_command, normalize_renderer
from harn_gibson.styles import style_pack_from_name, style_pack_ids

DEFAULT_RENDERER_TIMEOUT_MS = "10000"
DOGFOOD_CAPTURE_TRAJECTORY_SPLIT_EVERY = 200
PROJECT_HARN_PROVIDER = "openai-codex"
PROJECT_HARN_MODEL = "gpt-5.5"
PROJECT_HARN_THINKING = "high"
REPLAY_STATE_ENV_PASSTHROUGH = (
    "HARN_GIBSON_STYLE",
    "HARN_GIBSON_PROJECT_ROOT",
    "HARN_GIBSON_PROJECT_NAME",
    "HARN_GIBSON_RENDERER_COMPACTION_EVENTS",
    "HARN_GIBSON_RENDERER_MAX_RECENT_PLANS",
    "HARN_GIBSON_RENDERER_MAX_RECENT_LOG_ENTRIES",
    "HARN_GIBSON_RENDERER_MAX_PROP_PREVIEW_CHARS",
    "HARN_GIBSON_RENDERER_MAX_VISUAL_ANCHORS",
    "HARN_GIBSON_RENDERER_MAX_VISUAL_OBJECTS_PER_ANCHOR",
    "HARN_GIBSON_RENDERER_MAX_VISUAL_RECENT_ITEMS",
    "HARN_GIBSON_RENDERER_MAX_REPO_ENTRIES",
    "HARN_GIBSON_RENDERER_MAX_REPO_CHILDREN",
    "HARN_GIBSON_RENDERER_MAX_TOUCHED_FILES",
    "HARN_GIBSON_RENDERER_MAX_TOUCHED_PATH_CHARS",
    "HARN_GIBSON_RENDERER_MAX_WORLD_ENTITIES",
    "HARN_GIBSON_PERCEPTION_DISCOVERY",
    "HARN_GIBSON_RENDERER_SEMANTIC_GRAPH",
    "HARN_GIBSON_RENDERER_MAX_SEMANTIC_FILES",
    "HARN_GIBSON_RENDERER_MAX_SEMANTIC_EDGES",
    "HARN_GIBSON_RENDERER_MAX_SEMANTIC_SYMBOLS",
)


@dataclass(frozen=True)
class DogfoodCaptureTrajectory:
    identifier: str
    prompt_filename: str
    description: str
    split_every: int = DOGFOOD_CAPTURE_TRAJECTORY_SPLIT_EVERY

    @property
    def prompt_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / "examples" / "prompts" / self.prompt_filename


DOGFOOD_CAPTURE_TRAJECTORIES: dict[str, DogfoodCaptureTrajectory] = {
    "tiny-project": DogfoodCaptureTrajectory(
        "tiny-project",
        "dogfood-tiny-project.md",
        "bootstrap a small Python CLI project with tests, commits, a failure, and a fix",
    ),
    "repo-map": DogfoodCaptureTrajectory(
        "repo-map",
        "dogfood-repo-map.md",
        "build a depth-2 repository map with touched files across multiple top-level areas",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harn-gibson")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="run the local graphical display server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")

    dogfood = subcommands.add_parser(
        "run",
        help="run the display, open a browser, and launch harn",
    )
    dogfood.add_argument("--host", default="127.0.0.1")
    dogfood.add_argument("--port", type=int, default=0)
    dogfood.add_argument("--harn-bin", default="harn", help="harn executable to launch")
    dogfood.add_argument("--cwd", default=None, help="working directory for the launched harn process")
    dogfood.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("--codex-auth-import", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("--hold-on-error", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")
    dogfood.add_argument(
        "--renderer",
        default=DEFAULT_RENDERER,
        help="visualization name or custom perception visualization spec JSON path",
    )
    dogfood.add_argument(
        "--renderer-command",
        default=None,
        help="external renderer command for run; overrides --renderer",
    )
    dogfood.add_argument(
        "--renderer-timeout-ms",
        default=DEFAULT_RENDERER_TIMEOUT_MS,
        help="external renderer timeout in milliseconds",
    )
    dogfood.add_argument("harn_args", nargs=argparse.REMAINDER, help="arguments forwarded to harn after --")

    capture = subcommands.add_parser(
        "capture",
        help="run harn-gibson with event logging and the hard-coded showcase renderer",
    )
    capture.add_argument("--host", default="127.0.0.1")
    capture.add_argument("--port", type=int, default=0)
    capture.add_argument("--harn-bin", default="harn", help="harn executable to launch")
    capture.add_argument("--cwd", default=None, help="working directory for the launched harn process")
    capture.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    capture.add_argument("--codex-auth-import", action=argparse.BooleanOptionalAction, default=True)
    capture.add_argument("--hold-on-error", action=argparse.BooleanOptionalAction, default=True)
    capture.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")
    capture.add_argument(
        "--event-log",
        default=None,
        help="JSONL capture path; defaults to an ignored test-artifacts/captures path",
    )
    capture.add_argument(
        "--renderer",
        default="stress",
        help="visualization name or custom perception visualization spec JSON path",
    )
    capture.add_argument(
        "--renderer-command",
        default=None,
        help="external renderer command; overrides --renderer",
    )
    capture.add_argument(
        "--renderer-timeout-ms",
        default=DEFAULT_RENDERER_TIMEOUT_MS,
        help="external renderer timeout in milliseconds",
    )
    capture.add_argument(
        "--split-every",
        type=int,
        default=None,
        help="print a split replay-review command with at most this many events per fixture",
    )
    capture.add_argument(
        "--list-trajectories",
        action="store_true",
        help="print built-in long capture trajectory presets and exit",
    )
    capture.add_argument(
        "--trajectory",
        choices=_dogfood_capture_trajectory_ids(),
        default=None,
        help="apply a built-in long capture trajectory preset",
    )
    capture.add_argument("harn_args", nargs=argparse.REMAINDER, help="arguments forwarded to harn after --")

    auth = subcommands.add_parser("import-codex-auth", help="copy Codex OAuth tokens into harn auth storage")
    auth.add_argument("--codex-auth", default=None, help="path to Codex auth.json")
    auth.add_argument("--harn-auth", default=None, help="path to harn auth.json")

    subcommands.add_parser("backend-contract", help="print the display backend contract JSON")
    catalog = subcommands.add_parser("catalog", help="print the visual primitive/effect catalog JSON")
    catalog.add_argument("--kind", choices=("all", "primitive", "effect"), default="all", help="catalog entry kind")
    catalog.add_argument("--tag", action="append", default=None, help="include entries with this tag; repeatable")
    catalog.add_argument(
        "--id",
        dest="entry_ids",
        action="append",
        default=None,
        help="include this entry id; repeatable",
    )
    catalog.add_argument("--compact", action="store_true", help="omit bulky entry metadata")

    replay = subcommands.add_parser("replay", help="replay harn events, render plans, or scene mutations")
    replay.add_argument("path", help="path to replay JSON")
    replay.add_argument("--output-scene", default=None, help="write final scene JSON to this path")
    replay.add_argument("--output-result", default=None, help="write full replay result JSON to this path")
    replay.add_argument("--output-timeline", default=None, help="write per-step replay frame timeline JSON")
    replay.add_argument("--output-render-contexts", default=None, help="write captured renderer context JSON")
    replay.add_argument("--output-render-prompts", default=None, help="write renderer prompt message JSON")
    replay.add_argument("--output-render-chunks", default=None, help="write chunked renderer context/prompt JSON")
    replay.add_argument("--render-chunk-size", type=int, default=4, help="renderer contexts per replay chunk")
    replay.add_argument("--render-chunk-review", default=None, help="write a renderer chunk review HTML page")
    replay.add_argument("--render-prompt-review", default=None, help="write a renderer prompt review HTML page")
    replay.add_argument("--output-render-intents", default=None, help="write recorded renderer intent JSON")
    replay.add_argument("--render-intent-review", default=None, help="write a renderer intent review HTML page")
    replay.add_argument("--review-dir", default=None, help="write a complete replay review bundle directory")
    replay.add_argument(
        "--timeline-screenshot-dir",
        default=None,
        help="write one browser screenshot per timeline frame",
    )
    replay.add_argument("--screenshot", default=None, help="write a browser screenshot of the final replay scene")
    replay.add_argument("--screenshot-width", type=int, default=1280, help="screenshot viewport width")
    replay.add_argument("--screenshot-height", type=int, default=900, help="screenshot viewport height")
    replay.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")
    _add_replay_renderer_arguments(replay)
    _add_replay_project_arguments(replay)

    watch_replay = subcommands.add_parser(
        "watch-replay",
        help="run the display, open a browser, and play a replay fixture step by step",
    )
    watch_replay.add_argument("path", help="path to replay JSON")
    watch_replay.add_argument("--host", default="127.0.0.1")
    watch_replay.add_argument("--port", type=int, default=0)
    watch_replay.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    watch_replay.add_argument(
        "--hold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep the display server running after playback completes",
    )
    watch_replay.add_argument(
        "--start-delay-ms",
        type=int,
        default=1000,
        help="delay before the first replay step so the browser can connect",
    )
    watch_replay.add_argument(
        "--step-delay-ms",
        type=int,
        default=900,
        help="delay between replay steps when --playback-timing fixed is used",
    )
    watch_replay.add_argument(
        "--playback-timing",
        choices=("fixed", "real-time"),
        default="fixed",
        help="use a fixed delay or replay source timestamp deltas between steps",
    )
    watch_replay.add_argument(
        "--wait-for-input",
        action="store_true",
        help="hold the display idle until a directive is typed into the browser "
        "composer, then start playback -- lets a recording open on the prompt "
        "being typed and the session appearing to launch from it",
    )
    watch_replay.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="speed multiplier for --playback-timing real-time",
    )
    watch_replay.add_argument(
        "--max-step-delay-ms",
        type=int,
        default=None,
        help="cap each real-time replay delay in milliseconds",
    )
    watch_replay.add_argument(
        "--quiet-step-delay-ms",
        type=int,
        default=None,
        help="tighter delay cap before low-salience steps (streamed message/tool chunks): "
        "fast-forwards reasoning-heavy stretches in recorded demos without losing visible beats",
    )
    watch_replay.add_argument(
        "--min-step-delay-ms",
        type=int,
        default=None,
        help="delay floor before salient steps so recorded bursts do not machine-gun past at speed",
    )
    watch_replay.add_argument(
        "--start-step",
        type=int,
        default=1,
        help="1-based replay step to start from",
    )
    watch_replay.add_argument(
        "--end-step",
        type=int,
        default=None,
        help="1-based inclusive replay step to stop after",
    )
    watch_replay.add_argument(
        "--check-expectations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="check final replay expectations; defaults off for partial watch ranges",
    )
    watch_replay.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")
    _add_replay_renderer_arguments(watch_replay)
    _add_replay_project_arguments(watch_replay)

    replay_dir = subcommands.add_parser("replay-dir", help="run every replay JSON fixture under a directory")
    replay_dir.add_argument("path", help="directory or replay JSON file")
    replay_dir.add_argument("--output-result", default=None, help="write replay suite result JSON to this path")
    replay_dir.add_argument("--screenshot-dir", default=None, help="write one browser screenshot per replay file")
    replay_dir.add_argument("--screenshot-width", type=int, default=1280, help="screenshot viewport width")
    replay_dir.add_argument("--screenshot-height", type=int, default=900, help="screenshot viewport height")
    replay_dir.add_argument("--baseline-dir", default=None, help="compare final scenes against baselines in this path")
    replay_dir.add_argument("--review-dir", default=None, help="write a complete replay suite review bundle directory")
    replay_dir.add_argument("--render-chunk-size", type=int, default=4, help="renderer contexts per review chunk")
    replay_dir.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack")
    _add_replay_renderer_arguments(replay_dir)
    _add_replay_project_arguments(replay_dir)
    replay_dir.add_argument(
        "--update-baselines",
        action="store_true",
        help="write replay baselines instead of checking",
    )

    event_log = subcommands.add_parser(
        "event-log-to-replay",
        help="convert a HARN_GIBSON_EVENT_LOG JSONL file into a replay fixture",
    )
    event_log.add_argument("path", help="path to a normalized harn-gibson JSONL event log")
    event_log.add_argument("--output", "-o", default=None, help="write replay fixture JSON to this path")
    event_log.add_argument("--output-dir", default=None, help="write split replay fixtures to this directory")
    event_log.add_argument(
        "--output-result",
        default=None,
        help="write replay result JSON for converted logs, or replay suite result JSON for split logs",
    )
    event_log.add_argument("--name", default=None, help="fixture name; defaults to the event log filename")
    event_log.add_argument("--review-dir", default=None, help="write a complete replay review bundle for this log")
    event_log.add_argument("--screenshot-width", type=int, default=1280, help="review screenshot viewport width")
    event_log.add_argument("--screenshot-height", type=int, default=900, help="review screenshot viewport height")
    event_log.add_argument("--style", choices=style_pack_ids(), default=None, help="display style pack for review")
    event_log.add_argument("--render-chunk-size", type=int, default=4, help="renderer contexts per review chunk")
    event_log.add_argument(
        "--split-every",
        type=int,
        default=None,
        help="split an event log into replay fixtures with at most this many events each",
    )
    event_log.add_argument(
        "--visual-fixture",
        action="store_true",
        help="include capture summary metadata and default screenshot expectations",
    )
    event_log.add_argument(
        "--redact-sensitive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="redact common token, key, password, and credential values in converted replay fixtures",
    )
    event_log.add_argument(
        "--screenshot-lit-min",
        type=float,
        default=0.02,
        help="minimum screenshot canvas litRatio for --visual-fixture",
    )
    event_log.add_argument(
        "--screenshot-max-channel-min",
        type=int,
        default=60,
        help="minimum screenshot canvas maxChannelTotal for --visual-fixture",
    )
    _add_replay_renderer_arguments(event_log)
    _add_replay_project_arguments(event_log)

    subcommands.add_parser("extension-path", help="print the harn extension file path")
    return parser


def _add_replay_renderer_arguments(parser: argparse.ArgumentParser) -> None:
    renderer = parser.add_mutually_exclusive_group()
    renderer.add_argument(
        "--renderer-command",
        default=None,
        help="external renderer command to exercise during replay",
    )
    renderer.add_argument(
        "--renderer-model-command",
        default=None,
        help="prompt-command model renderer to exercise during replay",
    )
    renderer.add_argument(
        "--renderer",
        default=None,
        help="visualization name or custom perception visualization spec JSON path to exercise during replay",
    )
    parser.add_argument(
        "--discovery",
        choices=("workspace", "stream"),
        default=None,
        help="perception tree discovery: 'workspace' knows the tree from git/fs immediately; "
        "'stream' grows it as events touch files (use for replays of recorded sessions, "
        "since replay cannot rewind the workspace to its starting state)",
    )
    parser.add_argument(
        "--renderer-timeout-ms",
        default=None,
        help="external renderer timeout in milliseconds; also used by model renderer if no model timeout is set",
    )
    parser.add_argument(
        "--renderer-model-timeout-ms",
        default=None,
        help="prompt-command model renderer timeout in milliseconds",
    )


def _add_replay_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", default=None, help="project root used for renderer repo context")
    parser.add_argument("--project-name", default=None, help="project display name used in renderer context")


def _replay_state_from_args(args: argparse.Namespace):
    from harn_gibson.server import GibsonServerState, build_state_from_env

    state_env = _explicit_replay_state_env_from_args(args)
    if state_env:
        return build_state_from_env(state_env)
    return GibsonServerState(style_pack=style_pack_from_name(args.style))


def _explicit_replay_state_env_from_args(args: argparse.Namespace) -> dict[str, str]:
    state_env = _replay_state_env_from_process()
    state_env.update(_explicit_replay_renderer_env_from_args(args))
    if getattr(args, "style", None) is not None:
        state_env["HARN_GIBSON_STYLE"] = args.style
    if getattr(args, "project_root", None) is not None:
        state_env["HARN_GIBSON_PROJECT_ROOT"] = args.project_root
    if getattr(args, "project_name", None) is not None:
        state_env["HARN_GIBSON_PROJECT_NAME"] = args.project_name
    return state_env


def _replay_state_env_from_process() -> dict[str, str]:
    return {key: value for key in REPLAY_STATE_ENV_PASSTHROUGH if (value := os.environ.get(key)) is not None}


def _explicit_replay_renderer_env_from_args(args: argparse.Namespace) -> dict[str, str]:
    renderer_env: dict[str, str] = {}
    renderer_command = getattr(args, "renderer_command", None)
    model_command = getattr(args, "renderer_model_command", None)
    renderer_name = getattr(args, "renderer", None)
    renderer_timeout_ms = getattr(args, "renderer_timeout_ms", None)
    model_timeout_ms = getattr(args, "renderer_model_timeout_ms", None)
    if renderer_command:
        renderer_env["HARN_GIBSON_RENDERER_COMMAND"] = renderer_command
        if renderer_timeout_ms is not None:
            renderer_env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
    if model_command:
        renderer_env["HARN_GIBSON_RENDERER_MODEL_COMMAND"] = model_command
        if renderer_timeout_ms is not None:
            renderer_env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
        if model_timeout_ms is not None:
            renderer_env["HARN_GIBSON_RENDERER_MODEL_TIMEOUT_MS"] = str(model_timeout_ms)
    if renderer_name and str(renderer_name).strip():
        normalized_renderer = normalize_renderer(renderer_name, default=None) or str(renderer_name).strip()
        renderer_command_value = direct_renderer_command(normalized_renderer)
        if renderer_command_value is None:
            renderer_env["HARN_GIBSON_RENDERER"] = normalized_renderer
        else:
            renderer_env["HARN_GIBSON_RENDERER_COMMAND"] = renderer_command_value
            if renderer_timeout_ms is not None:
                renderer_env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
    discovery = getattr(args, "discovery", None)
    if discovery:
        renderer_env["HARN_GIBSON_PERCEPTION_DISCOVERY"] = discovery
    return renderer_env


def _coerce_harn_cwd(cwd: str | None) -> Path | None:
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"--cwd must be an existing directory: {path}")
    return path


def _harn_args_with_project_defaults(args: Sequence[str]) -> list[str]:
    forwarded = list(args)
    defaults: list[str] = []
    if not _argv_has_option(forwarded, "--provider"):
        defaults.extend(["--provider", PROJECT_HARN_PROVIDER])
    if not _argv_has_option(forwarded, "--model"):
        defaults.extend(["--model", PROJECT_HARN_MODEL])
    if not _argv_has_option(forwarded, "--thinking"):
        defaults.extend(["--thinking", PROJECT_HARN_THINKING])
    if not _argv_has_option(forwarded, "--no-extensions", "-ne"):
        defaults.append("--no-extensions")
    extension = extension_path()
    if not _argv_has_extension(forwarded, extension):
        defaults.extend(["--extension", extension])
    return [*defaults, *forwarded]


def _argv_has_option(args: Sequence[str], *names: str) -> bool:
    long_names = tuple(name for name in names if name.startswith("--"))
    for arg in args:
        if arg in names:
            return True
        if any(arg.startswith(f"{name}=") for name in long_names):
            return True
    return False


def _argv_has_extension(args: Sequence[str], extension: str) -> bool:
    for index, arg in enumerate(args):
        if arg == extension:
            return True
        if arg == f"--extension={extension}" or arg == f"-e={extension}":
            return True
        if arg in {"--extension", "-e"} and args[index + 1 : index + 2] == [extension]:
            return True
    return False


def run_dogfood(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    harn_bin: str = "harn",
    harn_args: Sequence[str] = (),
    launch_browser: bool = True,
    codex_auth_import: bool = True,
    hold_on_error: bool = True,
    style: str | None = None,
    env_overrides: Mapping[str, str] | None = None,
    cwd: str | None = None,
    renderer: str = DEFAULT_RENDERER,
    renderer_command: str | None = None,
    renderer_timeout_ms: str = DEFAULT_RENDERER_TIMEOUT_MS,
) -> int:
    from harn_gibson.server import build_state_from_env, publish_diagnostic_event
    from harn_gibson.viewer import start_viewer

    try:
        harn_cwd = _coerce_harn_cwd(cwd)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    preserve_renderer_env = bool(
        env_overrides
        and {
            "HARN_GIBSON_RENDERER",
            "HARN_GIBSON_RENDERER_COMMAND",
            "HARN_GIBSON_RENDERER_MODEL_COMMAND",
        }.intersection(env_overrides)
    )
    renderer_env_added = _apply_run_renderer_env(
        env,
        renderer=renderer,
        renderer_command=renderer_command,
        renderer_timeout_ms=renderer_timeout_ms,
        preserve_existing=preserve_renderer_env,
    )
    if harn_cwd is not None:
        env.setdefault("HARN_GIBSON_PROJECT_ROOT", str(harn_cwd))
        env.setdefault("HARN_GIBSON_PROJECT_NAME", harn_cwd.name or "workspace")
    if style is not None:
        env["HARN_GIBSON_STYLE"] = style
    state_needs_env = style is not None or env_overrides or harn_cwd is not None or renderer_env_added
    state = build_state_from_env(env) if state_needs_env else build_state_from_env()
    viewer = start_viewer(host, port, state=state, launch_browser=launch_browser, browser_open=webbrowser.open)
    display_url = viewer.display_url
    endpoint = viewer.endpoint
    input_endpoint = viewer.input_endpoint
    forwarded_args = list(harn_args)
    if forwarded_args[:1] == ["--"]:
        forwarded_args = forwarded_args[1:]
    if harn_cwd is not None:
        forwarded_args = _harn_args_with_project_defaults(forwarded_args)

    env["HARN_GIBSON_ENDPOINT"] = endpoint
    env["HARN_GIBSON_INPUT_ENDPOINT"] = input_endpoint
    command = [harn_bin, *forwarded_args]
    diagnostic_sequence = 0

    def publish_launcher_diagnostic(
        *,
        message: str,
        event_type: str = "launcher_diagnostic",
        severity: str = "info",
        title: str | None = None,
        details: str | None = None,
    ) -> None:
        nonlocal diagnostic_sequence
        diagnostic_sequence += 1
        publish_diagnostic_event(
            state,
            diagnostic_sequence,
            message=message,
            event_type=event_type,
            severity=severity,
            title=title,
            details=details,
        )

    print(f"harn-gibson display: {display_url}", file=sys.stderr)
    try:
        if codex_auth_import:
            auth_result = import_codex_auth(environ=env)
            print(auth_result.message, file=sys.stderr)
            publish_launcher_diagnostic(
                message=auth_result.message,
                event_type="auth_import",
                severity="info" if auth_result.available else "error",
                title="Codex auth ready" if auth_result.available else "Codex auth unavailable",
            )
        if harn_cwd is None:
            exit_code = subprocess.call(command, env=env)
        else:
            exit_code = subprocess.call(command, env=env, cwd=str(harn_cwd))
        if exit_code != 0:
            message = f"harn exited with code {exit_code}"
            publish_launcher_diagnostic(message=message, event_type="harn_exit", severity="error", title="Harn exit")
            if hold_on_error and launch_browser:
                _hold_display_on_error(display_url)
        return exit_code
    except FileNotFoundError:
        message = f"harn executable not found: {harn_bin}"
        print(message, file=sys.stderr)
        publish_launcher_diagnostic(message=message, event_type="harn_exit", severity="error", title="Harn missing")
        if hold_on_error and launch_browser:
            _hold_display_on_error(display_url)
        return 127
    finally:
        viewer.close()


def run_dogfood_capture(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    harn_bin: str = "harn",
    harn_args: Sequence[str] = (),
    launch_browser: bool = True,
    codex_auth_import: bool = True,
    hold_on_error: bool = True,
    style: str | None = None,
    event_log: str | None = None,
    renderer: str = "stress",
    renderer_command: str | None = None,
    renderer_timeout_ms: str = DEFAULT_RENDERER_TIMEOUT_MS,
    split_every: int | None = None,
    trajectory: str | None = None,
    cwd: str | None = None,
) -> int:
    try:
        harn_cwd, capture_harn_args, capture_event_log, capture_split_every = _prepare_dogfood_capture_options(
            trajectory=trajectory,
            cwd=cwd,
            harn_args=harn_args,
            event_log=event_log,
            split_every=split_every,
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if capture_split_every is not None and capture_split_every <= 0:
        print("--split-every must be positive", file=sys.stderr)
        return 2
    event_log_path = Path(capture_event_log) if capture_event_log is not None else _default_capture_event_log_path()
    if harn_cwd is not None:
        event_log_path = event_log_path.expanduser().resolve()
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    env_overrides = {
        "HARN_GIBSON_EVENT_LOG": str(event_log_path),
    }
    _apply_run_renderer_env(
        env_overrides,
        renderer=renderer,
        renderer_command=renderer_command,
        renderer_timeout_ms=str(renderer_timeout_ms),
    )
    if trajectory is not None:
        print(f"harn-gibson capture trajectory: {trajectory}", file=sys.stderr)
        print(f"harn-gibson capture workspace: {harn_cwd}", file=sys.stderr)
    print(f"harn-gibson capture log: {event_log_path}", file=sys.stderr)
    print(f"harn-gibson capture renderer: {renderer_command or renderer}", file=sys.stderr)
    exit_code = run_dogfood(
        host=host,
        port=port,
        harn_bin=harn_bin,
        harn_args=capture_harn_args,
        launch_browser=launch_browser,
        codex_auth_import=codex_auth_import,
        hold_on_error=hold_on_error,
        style=style,
        env_overrides=env_overrides,
        cwd=str(harn_cwd) if harn_cwd is not None else None,
    )
    print(
        "build a replay review from this capture with:\n"
        "  "
        + _capture_replay_command(
            event_log_path,
            renderer=renderer,
            renderer_command=renderer_command,
            renderer_timeout_ms=str(renderer_timeout_ms),
            style=style,
            split_every=capture_split_every,
            project_root=harn_cwd,
        ),
        file=sys.stderr,
    )
    return exit_code


def _prepare_dogfood_capture_options(
    *,
    trajectory: str | None,
    cwd: str | None,
    harn_args: Sequence[str],
    event_log: str | None,
    split_every: int | None,
) -> tuple[Path | None, list[str], str | None, int | None]:
    if trajectory is None:
        return _coerce_harn_cwd(cwd), list(harn_args), event_log, split_every
    preset = _dogfood_capture_trajectory(trajectory)
    harn_cwd = _prepare_dogfood_trajectory_workspace(trajectory, cwd)
    capture_harn_args = list(harn_args)
    if not _has_forwarded_harn_args(capture_harn_args):
        capture_harn_args = ["--", "-p", _dogfood_trajectory_prompt(trajectory)]
    capture_event_log = event_log if event_log is not None else str(_default_capture_event_log_path(prefix=trajectory))
    capture_split_every = split_every if split_every is not None else preset.split_every
    return harn_cwd, capture_harn_args, capture_event_log, capture_split_every


def _prepare_dogfood_trajectory_workspace(trajectory: str, cwd: str | None) -> Path:
    if cwd is not None:
        workspace = Path(cwd).expanduser().resolve()
    else:
        workspace = Path("test-artifacts") / "dogfood-workspaces" / f"{trajectory}-{time.strftime('%Y%m%d-%H%M%S')}"
        workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _has_forwarded_harn_args(harn_args: Sequence[str]) -> bool:
    return bool(harn_args) and list(harn_args) != ["--"]


def _dogfood_trajectory_prompt(trajectory: str) -> str:
    return _dogfood_capture_trajectory(trajectory).prompt_path.read_text(encoding="utf-8")


def _dogfood_capture_trajectory(trajectory: str) -> DogfoodCaptureTrajectory:
    try:
        return DOGFOOD_CAPTURE_TRAJECTORIES[trajectory]
    except KeyError as error:
        raise ValueError(f"unknown dogfood capture trajectory: {trajectory}") from error


def _dogfood_capture_trajectory_ids() -> tuple[str, ...]:
    return tuple(DOGFOOD_CAPTURE_TRAJECTORIES)


def _dogfood_capture_trajectory_listing() -> str:
    lines = ["available dogfood capture trajectories:"]
    for trajectory in DOGFOOD_CAPTURE_TRAJECTORIES.values():
        lines.append(f"  {trajectory.identifier:<12} {trajectory.description}")
    return "\n".join(lines)


def _default_capture_event_log_path(*, prefix: str = "dogfood") -> Path:
    return Path("test-artifacts") / "captures" / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def _apply_run_renderer_env(
    env: dict[str, str],
    *,
    renderer: str,
    renderer_command: str | None,
    renderer_timeout_ms: str,
    preserve_existing: bool = False,
) -> bool:
    if renderer_command is not None:
        env["HARN_GIBSON_RENDERER_COMMAND"] = renderer_command
        env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
        env.pop("HARN_GIBSON_RENDERER", None)
        env.pop("HARN_GIBSON_RENDERER_MODEL_COMMAND", None)
        return True
    normalized_renderer = normalize_renderer(renderer, default=DEFAULT_RENDERER)
    if preserve_existing:
        env.setdefault("HARN_GIBSON_RENDERER_TIMEOUT_MS", str(renderer_timeout_ms))
        return True
    renderer_command_value = direct_renderer_command(str(normalized_renderer))
    if renderer_command_value is None:
        env["HARN_GIBSON_RENDERER"] = str(normalized_renderer)
        env.pop("HARN_GIBSON_RENDERER_COMMAND", None)
        env.pop("HARN_GIBSON_RENDERER_MODEL_COMMAND", None)
        env.pop("HARN_GIBSON_RENDERER_TIMEOUT_MS", None)
    else:
        env["HARN_GIBSON_RENDERER_COMMAND"] = renderer_command_value
        env["HARN_GIBSON_RENDERER_TIMEOUT_MS"] = str(renderer_timeout_ms)
        env.pop("HARN_GIBSON_RENDERER", None)
        env.pop("HARN_GIBSON_RENDERER_MODEL_COMMAND", None)
    return True


def _capture_replay_command(
    event_log_path: Path,
    *,
    renderer: str,
    renderer_command: str | None,
    renderer_timeout_ms: str,
    style: str | None,
    split_every: int | None,
    project_root: Path | None = None,
) -> str:
    fixture_output = event_log_path.with_suffix(".replay.json")
    split_output_dir = event_log_path.with_suffix(".replays")
    result_output = event_log_path.with_suffix(".result.json")
    review_dir = event_log_path.with_name(f"{event_log_path.stem}-review")
    command = [
        "uv",
        "run",
        "harn-gibson",
        "event-log-to-replay",
        str(event_log_path),
    ]
    if split_every is None:
        command.extend(["--output", str(fixture_output)])
    else:
        command.extend(["--output-dir", str(split_output_dir), "--split-every", str(split_every)])
    command.extend(["--output-result", str(result_output)])
    command.extend(
        [
            "--visual-fixture",
            "--redact-sensitive",
            "--review-dir",
            str(review_dir),
        ]
    )
    if renderer_command is None:
        command.extend(["--renderer", renderer])
    else:
        command.extend(["--renderer-command", renderer_command, "--renderer-timeout-ms", renderer_timeout_ms])
    if style is not None:
        command.extend(["--style", style])
    if project_root is not None:
        command.extend(["--project-root", str(project_root), "--project-name", project_root.name or "workspace"])
    return " ".join(shlex.quote(part) for part in command)


def _write_json_file(path: str | Path, payload: Mapping[str, object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _hold_display_on_error(display_url: str) -> None:  # pragma: no cover - manual recovery loop
    _hold_display(display_url)


def _hold_display(display_url: str) -> None:  # pragma: no cover - manual playback loop
    print(f"harn-gibson display remains available at {display_url}; press Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


def _await_replay_directive(state: object, *, poll_seconds: float = 0.4) -> str | None:
    """Hold the boot until the browser composer delivers a directive. The
    typed prompt is part of the show: a recording opens on it being entered
    and the session appears to launch from it."""
    try:
        while True:
            item = state.inputs.pop()
            if item is not None:
                return str(item.message).strip()
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return None


def _rerun_replay(
    path: str,
    state: object,
    *,
    step_delay_ms: int,
    playback_timing: str,
    speed: float,
    max_step_delay_ms: int | None,
    quiet_step_delay_ms: int | None = None,
    min_step_delay_ms: int | None = None,
    progress: object,
) -> None:
    """Runner registered behind the browser replay button: reset the session
    (fresh perception/scene under the same config) and play the file again."""
    from harn_gibson.replay import play_replay_file
    from harn_gibson.server import reset_session

    reset_session(state)
    play_replay_file(
        path,
        state,
        start_delay_ms=1500,
        step_delay_ms=step_delay_ms,
        playback_timing=playback_timing,
        time_scale=speed,
        max_step_delay_ms=max_step_delay_ms,
        quiet_step_delay_ms=quiet_step_delay_ms,
        min_step_delay_ms=min_step_delay_ms,
        check_expectations=False,
        progress=progress,
    )


def run_watch_replay(args: argparse.Namespace) -> int:
    from harn_gibson.replay import ReplayExpectationError, ReplayStepResult, play_replay_file
    from harn_gibson.scene import SceneState
    from harn_gibson.server import ReplayControl, create_server

    if args.start_delay_ms < 0:
        print("--start-delay-ms must be non-negative", file=sys.stderr)
        return 2
    if args.step_delay_ms < 0:
        print("--step-delay-ms must be non-negative", file=sys.stderr)
        return 2
    if args.speed <= 0 or not math.isfinite(args.speed):
        print("--speed must be positive", file=sys.stderr)
        return 2
    if args.max_step_delay_ms is not None and args.max_step_delay_ms < 0:
        print("--max-step-delay-ms must be non-negative", file=sys.stderr)
        return 2
    if args.start_step < 1:
        print("--start-step must be at least 1", file=sys.stderr)
        return 2
    if args.end_step is not None and args.end_step < args.start_step:
        print("--end-step must be greater than or equal to --start-step", file=sys.stderr)
        return 2

    state = _replay_state_from_args(args)
    server = create_server(args.host, args.port, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    display_url = f"http://{actual_host}:{actual_port}"
    interrupted = False

    def report_progress(step: ReplayStepResult, position: int, total: int, scene: SceneState) -> None:
        print(
            f"watch-replay {position}/{total}: {step.kind}, revision {scene.revision}, updates {step.updates}",
            file=sys.stderr,
        )

    print(f"harn-gibson replay display: {display_url}", file=sys.stderr)
    state.replay_control = ReplayControl(
        description=str(args.path),
        runner=functools.partial(
            _rerun_replay,
            args.path,
            state,
            step_delay_ms=args.step_delay_ms,
            playback_timing=args.playback_timing,
            speed=args.speed,
            max_step_delay_ms=args.max_step_delay_ms,
            quiet_step_delay_ms=args.quiet_step_delay_ms,
            min_step_delay_ms=args.min_step_delay_ms,
            progress=report_progress,
        ),
    )
    if args.browser:
        webbrowser.open(display_url)
    if args.wait_for_input:
        from harn_gibson.server import publish_diagnostic_event

        publish_diagnostic_event(
            state, 1,
            message="AWAITING DIRECTIVE :: type your command below and press SEND",
            event_type="lobby_idle", title="Gibson lobby",
        )
        directive = _await_replay_directive(state)
        if directive is None:
            print("watch-replay closed without a directive", file=sys.stderr)
            state.pipeline.stop()
            server.shutdown()
            server.server_close()
            return 130
        print(f"directive received: {directive!r}", file=sys.stderr)
        publish_diagnostic_event(
            state, 2,
            message=f"DIRECTIVE RECEIVED :: {directive[:80]}",
            event_type="lobby_directive", title="Directive",
        )
    start_index = args.start_step - 1
    end_index = args.end_step
    partial_playback = args.start_step != 1 or args.end_step is not None
    check_expectations = (not partial_playback) if args.check_expectations is None else args.check_expectations
    try:
        try:
            result = play_replay_file(
                args.path,
                state,
                start_delay_ms=args.start_delay_ms,
                step_delay_ms=args.step_delay_ms,
                playback_timing=args.playback_timing,
                time_scale=args.speed,
                max_step_delay_ms=args.max_step_delay_ms,
                quiet_step_delay_ms=args.quiet_step_delay_ms,
                min_step_delay_ms=args.min_step_delay_ms,
                start_index=start_index,
                end_index=end_index,
                check_expectations=check_expectations,
                progress=report_progress,
            )
        except ReplayExpectationError as error:
            for failure in error.failures:
                print(f"replay expectation failed: {failure.message}", file=sys.stderr)
            return_code = 1
        else:
            print(
                f"watched {len(result.steps)} replay steps; scene revision {result.scene.revision}",
                file=sys.stderr,
            )
            return_code = 0
        if args.hold:
            _hold_display(display_url)
        return return_code
    except KeyboardInterrupt:
        interrupted = True
        return 130
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()
        if interrupted:
            print("watch-replay interrupted", file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extension-path":
        print(extension_path())
        return 0
    if args.command == "import-codex-auth":
        result = import_codex_auth(args.codex_auth, args.harn_auth)
        print(result.message)
        return 0 if result.available else 1
    if args.command == "backend-contract":
        from harn_gibson.server import GibsonServerState, backend_contract_payload

        state = GibsonServerState()
        try:
            print(json.dumps(backend_contract_payload(state), indent=2, sort_keys=True))
        finally:
            state.pipeline.stop()
        return 0
    if args.command == "catalog":
        from harn_gibson.catalog import default_visual_catalog, visual_catalog_payload

        print(
            json.dumps(
                visual_catalog_payload(
                    default_visual_catalog(),
                    kind=args.kind,
                    tags=args.tag or (),
                    entry_ids=args.entry_ids or (),
                    compact=args.compact,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "watch-replay":
        return run_watch_replay(args)
    if args.command == "replay":
        from harn_gibson.replay import (
            ReplayExpectationError,
            capture_replay_frame_screenshots,
            replay_frame_screenshot_manifest,
            replay_render_intents_from_result,
            replay_renderer_chunks_from_result,
            replay_renderer_prompts_from_result,
            run_replay_file,
            write_replay_frame_review_html,
            write_replay_frame_screenshot_manifest,
            write_replay_render_intents,
            write_replay_render_intents_review_html,
            write_replay_renderer_chunks,
            write_replay_renderer_chunks_review_html,
            write_replay_renderer_contexts,
            write_replay_renderer_prompts,
            write_replay_renderer_prompts_review_html,
            write_replay_result,
            write_replay_review_bundle,
            write_replay_timeline,
            write_scene,
        )

        replay_state = _replay_state_from_args(args)
        try:
            try:
                result = run_replay_file(
                    args.path,
                    replay_state,
                    capture_frames=bool(args.output_timeline or args.timeline_screenshot_dir or args.review_dir),
                    capture_renderer_contexts=bool(
                        args.output_render_contexts
                        or args.output_render_prompts
                        or args.output_render_chunks
                        or args.render_chunk_review
                        or args.render_prompt_review
                        or args.review_dir
                    ),
                )
            except ReplayExpectationError as error:
                for failure in error.failures:
                    print(f"replay expectation failed: {failure.message}", file=sys.stderr)
                return 1
            if args.output_scene:
                write_scene(args.output_scene, result.scene)
            if args.output_result:
                write_replay_result(args.output_result, result)
            if args.output_timeline:
                write_replay_timeline(args.output_timeline, result)
            if args.output_render_contexts:
                write_replay_renderer_contexts(args.output_render_contexts, result)
            if args.output_render_prompts:
                write_replay_renderer_prompts(args.output_render_prompts, result)
            if args.output_render_chunks:
                write_replay_renderer_chunks(args.output_render_chunks, result, chunk_size=args.render_chunk_size)
            if args.render_chunk_review:
                write_replay_renderer_chunks_review_html(
                    args.render_chunk_review,
                    replay_renderer_chunks_from_result(result, chunk_size=args.render_chunk_size),
                )
            if args.render_prompt_review:
                write_replay_renderer_prompts_review_html(
                    args.render_prompt_review,
                    replay_renderer_prompts_from_result(result),
                )
            if args.output_render_intents:
                write_replay_render_intents(args.output_render_intents, result)
            if args.render_intent_review:
                write_replay_render_intents_review_html(
                    args.render_intent_review,
                    replay_render_intents_from_result(result),
                )
            if args.timeline_screenshot_dir:
                screenshots = capture_replay_frame_screenshots(
                    result,
                    args.timeline_screenshot_dir,
                    width=args.screenshot_width,
                    height=args.screenshot_height,
                )
                screenshot_manifest = replay_frame_screenshot_manifest(result, screenshots)
                write_replay_frame_screenshot_manifest(
                    Path(args.timeline_screenshot_dir) / "manifest.json",
                    result,
                    screenshots,
                )
                write_replay_frame_review_html(
                    Path(args.timeline_screenshot_dir) / "index.html",
                    screenshot_manifest,
                )
                print(
                    f"captured replay timeline screenshots: {args.timeline_screenshot_dir} "
                    f"({len(screenshots)} frames)"
                )
            if args.review_dir:
                review_screenshots = capture_replay_frame_screenshots(
                    result,
                    Path(args.review_dir) / "frames",
                    width=args.screenshot_width,
                    height=args.screenshot_height,
                )
                write_replay_review_bundle(
                    args.review_dir,
                    result,
                    review_screenshots,
                    render_chunk_size=args.render_chunk_size,
                )
                print(f"wrote replay review bundle: {args.review_dir} ({len(review_screenshots)} frames)")
            if args.screenshot:
                from harn_gibson.browser_capture import capture_scene_screenshot

                screenshot = capture_scene_screenshot(
                    replay_state,
                    args.screenshot,
                    width=args.screenshot_width,
                    height=args.screenshot_height,
                )
                print(f"captured replay screenshot: {screenshot.path}")
            print(
                f"replayed {len(result.steps)} steps; scene revision {result.scene.revision}",
            )
            return 0
        finally:
            replay_state.pipeline.stop()
    if args.command == "replay-dir":
        from harn_gibson.replay import run_replay_suite, write_replay_suite_review_bundle

        if args.update_baselines and args.baseline_dir is None:
            print("--update-baselines requires --baseline-dir", file=sys.stderr)
            return 2
        state_factory = (lambda: _replay_state_from_args(args)) if _explicit_replay_state_env_from_args(args) else None
        result = run_replay_suite(
            args.path,
            screenshot_dir=args.screenshot_dir,
            screenshot_width=args.screenshot_width,
            screenshot_height=args.screenshot_height,
            baseline_dir=args.baseline_dir,
            update_baselines=args.update_baselines,
            style=args.style,
            state_factory=state_factory,
        )
        if args.output_result:
            Path(args.output_result).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output_result).write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        if args.review_dir:
            review_manifest = write_replay_suite_review_bundle(
                args.review_dir,
                args.path,
                screenshot_width=args.screenshot_width,
                screenshot_height=args.screenshot_height,
                render_chunk_size=args.render_chunk_size,
                style=args.style,
                state_factory=state_factory,
            )
            print(
                f"wrote replay suite review bundle: {args.review_dir} "
                f"({review_manifest['total']} files, {review_manifest['failed']} failed)"
            )
        for file_result in result.files:
            if file_result.ok:
                line = f"ok {file_result.path}: {file_result.steps} steps, revision {file_result.scene_revision}"
                if file_result.screenshot is not None:
                    line = f"{line}, screenshot {file_result.screenshot['path']}"
                if file_result.baseline is not None:
                    action = "updated" if file_result.baseline.updated else "checked"
                    line = f"{line}, baseline {action} {file_result.baseline.path}"
                print(line)
            else:
                print(f"failed {file_result.path}: {file_result.error}", file=sys.stderr)
        print(f"replayed {result.total} replay files; {result.failed} failed")
        return 0 if result.ok else 1
    if args.command == "event-log-to-replay":
        from harn_gibson.replay import (
            capture_replay_frame_screenshots,
            replay_data_from_event_log,
            run_replay_data,
            run_replay_suite,
            split_replay_data_from_event_log,
            split_replay_fixture_filename,
            write_replay_result,
            write_replay_review_bundle,
            write_replay_suite_review_bundle,
        )

        if args.split_every is not None:
            if args.split_every <= 0:
                print("--split-every must be positive", file=sys.stderr)
                return 2
            if args.output_dir is None:
                print("--split-every requires --output-dir", file=sys.stderr)
                return 2
            if args.output is not None:
                print("--split-every cannot be used with --output", file=sys.stderr)
                return 2
            fixtures, manifest = split_replay_data_from_event_log(
                args.path,
                events_per_fixture=args.split_every,
                name=args.name,
                visual_fixture=args.visual_fixture,
                screenshot_lit_min=args.screenshot_lit_min,
                screenshot_max_channel_min=args.screenshot_max_channel_min,
                redact_sensitive=args.redact_sensitive,
            )
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            base_name = str(manifest["name"])
            for index, fixture in enumerate(fixtures, start=1):
                fixture_path = output_dir / split_replay_fixture_filename(base_name, index)
                fixture_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
                print(f"wrote replay fixture chunk: {fixture_path} ({len(fixture['steps'])} events)")
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            print(
                f"wrote event-log split manifest: {manifest_path} "
                f"({manifest['chunkCount']} chunks, {manifest['eventCount']} events)"
            )
            if args.review_dir:
                state_factory = (
                    (lambda: _replay_state_from_args(args)) if _explicit_replay_state_env_from_args(args) else None
                )
                review_manifest = write_replay_suite_review_bundle(
                    args.review_dir,
                    output_dir,
                    screenshot_width=args.screenshot_width,
                    screenshot_height=args.screenshot_height,
                    render_chunk_size=args.render_chunk_size,
                    style=args.style,
                    state_factory=state_factory,
                )
                print(
                    f"wrote event-log split review bundle: {args.review_dir} "
                    f"({review_manifest['total']} chunks, {review_manifest['failed']} failed)"
                )
                if args.output_result:
                    result = run_replay_suite(output_dir, style=args.style, state_factory=state_factory)
                    _write_json_file(args.output_result, result.to_dict())
                    print(f"wrote event-log split replay result: {args.output_result}")
                return 0 if review_manifest["ok"] else 1
            if args.output_result:
                state_factory = (
                    (lambda: _replay_state_from_args(args)) if _explicit_replay_state_env_from_args(args) else None
                )
                result = run_replay_suite(output_dir, style=args.style, state_factory=state_factory)
                _write_json_file(args.output_result, result.to_dict())
                print(f"wrote event-log split replay result: {args.output_result}")
                return 0 if result.ok else 1
            return 0
        if args.output_dir is not None:
            print("--output-dir requires --split-every", file=sys.stderr)
            return 2

        fixture = replay_data_from_event_log(
            args.path,
            name=args.name,
            visual_fixture=args.visual_fixture,
            screenshot_lit_min=args.screenshot_lit_min,
            screenshot_max_channel_min=args.screenshot_max_channel_min,
            redact_sensitive=args.redact_sensitive,
        )
        text = json.dumps(fixture, indent=2) + "\n"
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote replay fixture: {args.output} ({len(fixture['steps'])} events)")
        else:
            print(text, end="")
        replay_result = None
        if args.review_dir:
            replay_state = _replay_state_from_args(args)
            try:
                replay_result = run_replay_data(
                    fixture,
                    replay_state,
                    capture_frames=True,
                    capture_renderer_contexts=True,
                )
                screenshots = capture_replay_frame_screenshots(
                    replay_result,
                    Path(args.review_dir) / "frames",
                    width=args.screenshot_width,
                    height=args.screenshot_height,
                )
                write_replay_review_bundle(
                    args.review_dir,
                    replay_result,
                    screenshots,
                    render_chunk_size=args.render_chunk_size,
                )
                print(f"wrote event-log review bundle: {args.review_dir} ({len(screenshots)} frames)")
            finally:
                replay_state.pipeline.stop()
        if args.output_result:
            if replay_result is None:
                replay_state = _replay_state_from_args(args)
                try:
                    replay_result = run_replay_data(fixture, replay_state)
                finally:
                    replay_state.pipeline.stop()
            write_replay_result(args.output_result, replay_result)
            print(f"wrote event-log replay result: {args.output_result}")
        return 0
    if args.command == "run":
        return run_dogfood(
            host=args.host,
            port=args.port,
            harn_bin=args.harn_bin,
            harn_args=args.harn_args,
            launch_browser=args.browser,
            codex_auth_import=args.codex_auth_import,
            hold_on_error=args.hold_on_error,
            style=args.style,
            cwd=args.cwd,
            renderer=args.renderer,
            renderer_command=args.renderer_command,
            renderer_timeout_ms=args.renderer_timeout_ms,
        )
    if args.command == "capture":
        if args.list_trajectories:
            print(_dogfood_capture_trajectory_listing())
            return 0
        return run_dogfood_capture(
            host=args.host,
            port=args.port,
            harn_bin=args.harn_bin,
            harn_args=args.harn_args,
            launch_browser=args.browser,
            codex_auth_import=args.codex_auth_import,
            hold_on_error=args.hold_on_error,
            style=args.style,
            event_log=args.event_log,
            renderer=args.renderer,
            renderer_command=args.renderer_command,
            renderer_timeout_ms=args.renderer_timeout_ms,
            split_every=args.split_every,
            trajectory=args.trajectory,
            cwd=args.cwd,
        )
    if args.command in {None, "serve"}:
        from harn_gibson.server import run_server

        run_server(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765), style=getattr(args, "style", None))
        return 0
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


def main() -> None:
    raise SystemExit(run())
