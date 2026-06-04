from __future__ import annotations

from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            relative = path.relative_to(root)
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            print(f"{line_count:03d} {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
