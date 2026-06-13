"""Built-in renderer command presets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RENDERER_PRESETS = ("gibson1", "dogfood", "none")
DEFAULT_RENDERER_PRESET = "gibson1"
RENDERER_PRESET_DESCRIPTIONS = {
    "gibson1": "default coherent renderer for everyday interactive sessions",
    "dogfood": "showcase/stress renderer with the full cinematic primitive set",
    "none": "use the built-in deterministic renderer without an external renderer process",
}


def renderer_preset_command(preset: str) -> str | None:
    normalized = preset.strip().lower()
    if normalized == "none":
        return None
    if normalized == "gibson1":
        return _example_renderer_command("gibson1_renderer.py")
    if normalized == "dogfood":
        return _example_renderer_command("gibson_dogfood_renderer.py")
    raise ValueError(f"unknown renderer preset: {preset}")


def renderer_preset_listing() -> str:
    lines = ["available Gibson renderers:"]
    for preset in RENDERER_PRESETS:
        marker = " (default)" if preset == DEFAULT_RENDERER_PRESET else ""
        lines.append(f"  {preset:<8} {RENDERER_PRESET_DESCRIPTIONS[preset]}{marker}")
    return "\n".join(lines)


def _example_renderer_command(filename: str) -> str:
    return json.dumps([sys.executable, str(_example_renderer_path(filename))])


def _example_renderer_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "renderers" / filename
