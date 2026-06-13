"""Built-in visualization selectors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

DIRECT_RENDERERS = ("classic", "stress")
RENDERERS = ("default", "classic", "stress")
DEFAULT_RENDERER = "default"
RENDERER_DESCRIPTIONS = {
    "default": "built-in organic force-layout visualization driven by the perception model",
    "classic": "older coherent hard-coded visualization for everyday sessions",
    "stress": "showcase/stress visualization with the full cinematic primitive set",
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
    if normalized == "classic":
        return _example_renderer_command("gibson1_renderer.py")
    if normalized == "stress":
        return _example_renderer_command("gibson_dogfood_renderer.py")
    return None


def renderer_listing() -> str:
    lines = ["available Gibson visualizations:"]
    for renderer in RENDERERS:
        marker = " (default)" if renderer == DEFAULT_RENDERER else ""
        lines.append(f"  {renderer:<10} {RENDERER_DESCRIPTIONS[renderer]}{marker}")
    lines.append("  <path.json> custom perception visualization spec file")
    return "\n".join(lines)


def _example_renderer_command(filename: str) -> str:
    return json.dumps([sys.executable, str(_example_renderer_path(filename))])


def _example_renderer_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "renderers" / filename
