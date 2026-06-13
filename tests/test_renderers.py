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
    assert DEFAULT_RENDERER == "default"
    assert RENDERERS == ("default", "classic", "stress")
    classic_command = json.loads(direct_renderer_command("classic") or "[]")
    stress_command = json.loads(direct_renderer_command(" stress ") or "[]")

    assert classic_command[-1].endswith("examples/renderers/gibson1_renderer.py")
    assert stress_command[-1].endswith("examples/renderers/gibson_dogfood_renderer.py")
    assert direct_renderer_command("default") is None
    assert normalize_renderer(" ", default="default") == "default"
    assert normalize_renderer("examples/renderers/custom.json") == "examples/renderers/custom.json"
    assert "classic" in renderer_listing()
    assert "stress" in renderer_listing()
    assert "default" in renderer_listing()
