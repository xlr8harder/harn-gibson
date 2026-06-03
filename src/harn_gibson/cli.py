"""Command-line entry points for harn-gibson."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence

from harn_gibson.auth import import_codex_auth
from harn_gibson.extension import extension_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harn-gibson")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="run the local graphical display server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    dogfood = subcommands.add_parser("dogfood", help="run the display, open a browser, and launch harn")
    dogfood.add_argument("--host", default="127.0.0.1")
    dogfood.add_argument("--port", type=int, default=0)
    dogfood.add_argument("--harn-bin", default="harn", help="harn executable to launch")
    dogfood.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("--codex-auth-import", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("--hold-on-error", action=argparse.BooleanOptionalAction, default=True)
    dogfood.add_argument("harn_args", nargs=argparse.REMAINDER, help="arguments forwarded to harn after --")

    auth = subcommands.add_parser("import-codex-auth", help="copy Codex OAuth tokens into harn auth storage")
    auth.add_argument("--codex-auth", default=None, help="path to Codex auth.json")
    auth.add_argument("--harn-auth", default=None, help="path to harn auth.json")

    replay = subcommands.add_parser("replay", help="replay harn events, render plans, or scene mutations")
    replay.add_argument("path", help="path to replay JSON")
    replay.add_argument("--output-scene", default=None, help="write final scene JSON to this path")
    replay.add_argument("--output-result", default=None, help="write full replay result JSON to this path")
    replay.add_argument("--screenshot", default=None, help="write a browser screenshot of the final replay scene")
    replay.add_argument("--screenshot-width", type=int, default=1280, help="screenshot viewport width")
    replay.add_argument("--screenshot-height", type=int, default=900, help="screenshot viewport height")

    replay_dir = subcommands.add_parser("replay-dir", help="run every replay JSON fixture under a directory")
    replay_dir.add_argument("path", help="directory or replay JSON file")
    replay_dir.add_argument("--output-result", default=None, help="write replay suite result JSON to this path")
    replay_dir.add_argument("--screenshot-dir", default=None, help="write one browser screenshot per replay file")
    replay_dir.add_argument("--screenshot-width", type=int, default=1280, help="screenshot viewport width")
    replay_dir.add_argument("--screenshot-height", type=int, default=900, help="screenshot viewport height")

    subcommands.add_parser("extension-path", help="print the harn extension file path")
    return parser


def run_dogfood(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    harn_bin: str = "harn",
    harn_args: Sequence[str] = (),
    launch_browser: bool = True,
    codex_auth_import: bool = True,
    hold_on_error: bool = True,
) -> int:
    from harn_gibson.server import build_state_from_env, create_server, publish_diagnostic_event

    state = build_state_from_env()
    server = create_server(host, port, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    display_url = f"http://{actual_host}:{actual_port}"
    endpoint = f"{display_url}/events"
    input_endpoint = f"{display_url}/input/next"
    forwarded_args = list(harn_args)
    if forwarded_args[:1] == ["--"]:
        forwarded_args = forwarded_args[1:]

    env = os.environ.copy()
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
    if launch_browser:
        webbrowser.open(display_url)
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
        exit_code = subprocess.call(command, env=env)
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
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def _hold_display_on_error(display_url: str) -> None:  # pragma: no cover - manual recovery loop
    print(f"harn-gibson display remains available at {display_url}; press Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


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
    if args.command == "replay":
        from harn_gibson.replay import ReplayExpectationError, run_replay_file, write_replay_result, write_scene
        from harn_gibson.server import GibsonServerState

        replay_state = GibsonServerState()
        try:
            result = run_replay_file(args.path, replay_state)
        except ReplayExpectationError as error:
            for failure in error.failures:
                print(f"replay expectation failed: {failure.message}", file=sys.stderr)
            return 1
        if args.output_scene:
            write_scene(args.output_scene, result.scene)
        if args.output_result:
            write_replay_result(args.output_result, result)
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
    if args.command == "replay-dir":
        from pathlib import Path

        from harn_gibson.replay import run_replay_suite

        result = run_replay_suite(
            args.path,
            screenshot_dir=args.screenshot_dir,
            screenshot_width=args.screenshot_width,
            screenshot_height=args.screenshot_height,
        )
        if args.output_result:
            Path(args.output_result).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output_result).write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        for file_result in result.files:
            if file_result.ok:
                line = f"ok {file_result.path}: {file_result.steps} steps, revision {file_result.scene_revision}"
                if file_result.screenshot is not None:
                    line = f"{line}, screenshot {file_result.screenshot['path']}"
                print(line)
            else:
                print(f"failed {file_result.path}: {file_result.error}", file=sys.stderr)
        print(f"replayed {result.total} replay files; {result.failed} failed")
        return 0 if result.ok else 1
    if args.command == "dogfood":
        return run_dogfood(
            host=args.host,
            port=args.port,
            harn_bin=args.harn_bin,
            harn_args=args.harn_args,
            launch_browser=args.browser,
            codex_auth_import=args.codex_auth_import,
            hold_on_error=args.hold_on_error,
        )
    if args.command in {None, "serve"}:
        from harn_gibson.server import run_server

        run_server(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765))
        return 0
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


def main() -> None:
    raise SystemExit(run())
