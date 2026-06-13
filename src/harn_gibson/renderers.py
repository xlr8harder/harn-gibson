"""Built-in renderer selectors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

DIRECT_RENDERERS = ("gibson1", "dogfood")
RENDERERS = ("gibson1", "dogfood", "perception", "none")
DEFAULT_RENDERER = "gibson1"
RENDERER_DESCRIPTIONS = {
    "gibson1": "default coherent renderer for everyday interactive sessions",
    "dogfood": "showcase/stress renderer with the full cinematic primitive set",
    "perception": "declarative renderer driven by the perception model",
    "none": "use the built-in deterministic renderer without an external renderer process",
}


def normalize_renderer(value: str | None, *, default: str | None = DEFAULT_RENDERER) -> str | None:
    if value is None:
        return default
    stripped = value.strip()
    normalized = stripped.lower()
    if normalized == "":
        return default
    if normalized in RENDERERS:
        return normalized
    return stripped


def direct_renderer_command(renderer: str) -> str | None:
    normalized = normalize_renderer(renderer, default=None)
    if normalized == "gibson1":
        return _example_renderer_command("gibson1_renderer.py")
    if normalized == "dogfood":
        return _example_renderer_command("gibson_dogfood_renderer.py")
    return None


def renderer_listing() -> str:
    lines = ["available Gibson renderers:"]
    for renderer in RENDERERS:
        marker = " (default)" if renderer == DEFAULT_RENDERER else ""
        lines.append(f"  {renderer:<10} {RENDERER_DESCRIPTIONS[renderer]}{marker}")
    lines.append("  <path.json> perception renderer spec file")
    return "\n".join(lines)


def _example_renderer_command(filename: str) -> str:
    return json.dumps([sys.executable, str(_example_renderer_path(filename))])


def _example_renderer_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "renderers" / filename
