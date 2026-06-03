"""Command-line entry points for harn-gibson."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from harn_gibson.extension import extension_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harn-gibson")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="run the local graphical display server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    subcommands.add_parser("extension-path", help="print the harn extension file path")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extension-path":
        print(extension_path())
        return 0
    if args.command in {None, "serve"}:
        from harn_gibson.server import run_server

        run_server(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765))
        return 0
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


def main() -> None:
    raise SystemExit(run())
