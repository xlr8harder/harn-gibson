from __future__ import annotations

import json

import pytest

from harn_gibson.renderer_presets import (
    DEFAULT_RENDERER_PRESET,
    RENDERER_PRESETS,
    renderer_preset_command,
    renderer_preset_listing,
)


def test_renderer_preset_commands_and_listing() -> None:
    assert DEFAULT_RENDERER_PRESET == "gibson1"
    assert RENDERER_PRESETS == ("gibson1", "dogfood", "none")
    gibson1_command = json.loads(renderer_preset_command("gibson1") or "[]")
    dogfood_command = json.loads(renderer_preset_command(" dogfood ") or "[]")

    assert gibson1_command[-1].endswith("examples/renderers/gibson1_renderer.py")
    assert dogfood_command[-1].endswith("examples/renderers/gibson_dogfood_renderer.py")
    assert renderer_preset_command("none") is None
    assert "gibson1" in renderer_preset_listing()
    assert "default" in renderer_preset_listing()
    with pytest.raises(ValueError, match="unknown renderer preset"):
        renderer_preset_command("missing")
