"""Small CLI used as a stable repo-map capture target."""

from __future__ import annotations

import argparse
from pathlib import Path


def summarize_lines(text: str) -> list[str]:
    rows = []
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        priority = "normal"
        if line.startswith("!"):
            priority = "high"
            line = line[1:].strip()
        if "::" in line:
            owner, title = [part.strip() for part in line.split("::", 1)]
        else:
            owner, title = "unassigned", line
        rows.append(f"{index:02d} [{priority}] {owner}: {title}")
    return rows


def format_summary(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(path)
    rows = summarize_lines(path.read_text(encoding="utf-8"))
    return "\n".join(rows) + ("\n" if rows else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize repo-map fixture tasks")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    print(format_summary(args.path), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
