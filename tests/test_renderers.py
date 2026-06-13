from __future__ import annotations

import json

from harn_gibson.renderers import (
    DEFAULT_RENDERER,
    RENDERERS,
    direct_renderer_command,
    normalize_renderer,
    renderer_listing,
)


def test_renderer_commands_and_listing() -> None:
    assert DEFAULT_RENDERER == "gibson1"
    assert RENDERERS == ("gibson1", "dogfood", "perception", "none")
    gibson1_command = json.loads(direct_renderer_command("gibson1") or "[]")
    dogfood_command = json.loads(direct_renderer_command(" dogfood ") or "[]")

    assert gibson1_command[-1].endswith("examples/renderers/gibson1_renderer.py")
    assert dogfood_command[-1].endswith("examples/renderers/gibson_dogfood_renderer.py")
    assert direct_renderer_command("none") is None
    assert normalize_renderer(" ", default="gibson1") == "gibson1"
    assert normalize_renderer("examples/renderers/custom.json") == "examples/renderers/custom.json"
    assert "perception" in renderer_listing()
    assert "default" in renderer_listing()
