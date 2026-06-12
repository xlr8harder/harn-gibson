"""The million-dollar demo lobby.

Boots the gibson display to an idle scene and waits. Whatever you type into
the composer becomes THE DIRECTIVE: a real harn agent is launched against a
fresh, empty git workspace with theatrical TDD house rules, your words at the
center, and the whole session captured to an event log (auto-converted to a
replay fixture afterward).

Usage:
    uv run python scripts/million_dollar_demo.py [--port 8765]
        [--workspace ~/git/gibson-venture] [--fresh] [--no-browser]
        [--model gpt-5.5] [--thinking high]

Then type something into the composer. "make me a million dollars. make no
mistakes." is traditional.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PREAMBLE = """\
You are the star of a live cinematic demo. Your work is being projected on a \
wall as a neon Gibson-style visualization while an audience watches every \
file you touch, every test you run, and every word you think. Be theatrical. \
Narrate your scheming out loud, constantly, in character as an ambitious and \
slightly unhinged AI startup founder. Have fun. Go over the top (tastefully).

THE DIRECTIVE FROM YOUR PATRON:
"{directive}"

HOUSE RULES (non-negotiable):
1. Interpret the directive as a software venture: invent a concrete, creative \
project that plausibly pursues it. Give it a memorable name.
2. Strict test-driven development. For every feature: write failing tests \
FIRST, run `uv run pytest` and watch them fail, then implement until green. \
Never skip the failing step. (Set the project up as a uv package with pytest \
in dev dependencies so `uv run pytest` works.)
3. Build a real package: pyproject.toml, src/<package>/ with at least six \
modules, a tests/ tree mirroring them, a CLI entry point, and a README with \
usage examples.
4. Commit at every green milestone with dramatic commit messages \
(use: git -c user.name='venture agent' -c user.email='agent@venture.example' \
commit ...). git is already initialized.
5. Finish by running the full suite one final time and delivering a closing \
soliloquy on the empire you have built.

Work autonomously. Do not ask questions. Begin.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="million-dollar demo lobby")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workspace", default=str(Path.home() / "git" / "gibson-venture"))
    parser.add_argument("--fresh", action="store_true", help="wipe the workspace first")
    parser.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--thinking", default="high")
    parser.add_argument(
        "--projection",
        default=str(REPO_ROOT / "examples" / "projections" / "gibson-organic.json"),
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if args.fresh and workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)

    capture_dir = REPO_ROOT / "captures"
    capture_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    event_log = capture_dir / f"venture-{stamp}.jsonl"

    env = os.environ.copy()
    env.update({
        "HARN_GIBSON_PROJECTION": args.projection,
        "HARN_GIBSON_PROJECT_ROOT": str(workspace),
        "HARN_GIBSON_PROJECT_NAME": workspace.name,
        "HARN_GIBSON_EVENT_LOG": str(event_log),
    })

    from harn_gibson.auth import import_codex_auth
    from harn_gibson.cli import _harn_args_with_project_defaults
    from harn_gibson.server import build_state_from_env, create_server, publish_diagnostic_event

    state = build_state_from_env(env)
    server = create_server(args.host, args.port, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    display_url = f"http://{args.host}:{args.port}"
    print(f"lobby open: {display_url}", file=sys.stderr)
    print("type your directive into the composer and hit SEND", file=sys.stderr)

    auth = import_codex_auth(environ=env)
    print(auth.message, file=sys.stderr)

    publish_diagnostic_event(
        state, 1,
        message="AWAITING DIRECTIVE :: type your command below and press SEND",
        event_type="lobby_idle", title="Gibson lobby",
    )
    if args.browser:
        webbrowser.open(display_url)

    directive = None
    try:
        while directive is None:
            item = state.inputs.pop()
            if item is not None:
                directive = item.message.strip()
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("lobby closed without a directive", file=sys.stderr)
        return 130

    print(f"directive received: {directive!r}", file=sys.stderr)
    publish_diagnostic_event(
        state, 2,
        message=f"DIRECTIVE RECEIVED :: {directive[:80]}",
        event_type="lobby_directive", title="Directive",
    )

    task = PREAMBLE.format(directive=directive.replace('"', "'"))
    env["HARN_GIBSON_ENDPOINT"] = f"{display_url}/events"
    env["HARN_GIBSON_INPUT_ENDPOINT"] = f"{display_url}/input/next"
    harn_args = _harn_args_with_project_defaults([
        "--model", args.model, "--thinking", args.thinking, task,
    ])
    command = ["harn", *harn_args]
    print("launching the agent...", file=sys.stderr)
    exit_code = subprocess.call(
        command, env=env, cwd=str(workspace), stdin=subprocess.DEVNULL,
    )
    print(f"agent finished (exit {exit_code})", file=sys.stderr)
    publish_diagnostic_event(
        state, 3,
        message=f"SESSION COMPLETE :: agent exit {exit_code}",
        event_type="lobby_complete", title="Session complete",
    )

    fixture = REPO_ROOT / "examples" / "claude-gibson-replays" / f"venture-{stamp}.json"
    if event_log.exists():
        convert = subprocess.run(
            ["uv", "run", "harn-gibson", "event-log-to-replay", str(event_log),
             "--output", str(fixture), "--name", f"venture-{stamp}"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        print(convert.stderr.strip() or convert.stdout.strip(), file=sys.stderr)
        print(f"replay fixture: {fixture}", file=sys.stderr)

    print("display stays up; Ctrl-C to exit", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return exit_code
    finally:
        state.pipeline.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
