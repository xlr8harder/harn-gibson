"""Command-line entry points for harn-gibson."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Sequence

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
    dogfood.add_argument("harn_args", nargs=argparse.REMAINDER, help="arguments forwarded to harn after --")

    subcommands.add_parser("extension-path", help="print the harn extension file path")
    return parser


def run_dogfood(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    harn_bin: str = "harn",
    harn_args: Sequence[str] = (),
    launch_browser: bool = True,
) -> int:
    from harn_gibson.server import build_state_from_env, create_server

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

    print(f"harn-gibson display: {display_url}", file=sys.stderr)
    if launch_browser:
        webbrowser.open(display_url)
    try:
        return subprocess.call(command, env=env)
    except FileNotFoundError:
        print(f"harn executable not found: {harn_bin}", file=sys.stderr)
        return 127
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extension-path":
        print(extension_path())
        return 0
    if args.command == "dogfood":
        return run_dogfood(
            host=args.host,
            port=args.port,
            harn_bin=args.harn_bin,
            harn_args=args.harn_args,
            launch_browser=args.browser,
        )
    if args.command in {None, "serve"}:
        from harn_gibson.server import run_server

        run_server(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765))
        return 0
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


def main() -> None:
    raise SystemExit(run())
