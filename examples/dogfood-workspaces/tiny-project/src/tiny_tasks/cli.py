"""Command-line task formatter for dogfood replay fixtures."""

from __future__ import annotations


def format_tasks(items: list[str]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.strip()}")
    return "\n".join(lines)


def main() -> None:
    print(format_tasks(["write fixture", "run tests", "commit changes"]))
