from __future__ import annotations

import json

from harn_gibson.renderers import (
    DEFAULT_RENDERER,
    RENDERERS,
    direct_renderer_command,
    normalize_renderer,
    projection_renderer_resource,
    renderer_listing,
)


def test_renderer_commands_and_listing() -> None:
    assert DEFAULT_RENDERER == "default"
    assert RENDERERS == ("default", "activity-roll", "thermal-roll", "classic", "stress")
    classic_command = json.loads(direct_renderer_command("classic") or "[]")
    stress_command = json.loads(direct_renderer_command(" stress ") or "[]")

    assert classic_command[-1].endswith("examples/renderers/gibson1_renderer.py")
    assert stress_command[-1].endswith("examples/renderers/gibson_dogfood_renderer.py")
    assert direct_renderer_command("default") is None
    assert direct_renderer_command("activity-roll") is None
    assert direct_renderer_command("thermal-roll") is None
    assert projection_renderer_resource("activity-roll") == "projections/activity-roll.json"
    assert projection_renderer_resource("thermal-roll") == "projections/thermal-roll.json"
    assert projection_renderer_resource("classic") is None
    assert projection_renderer_resource(" ") is None
    assert normalize_renderer(" ", default="default") == "default"
    assert normalize_renderer("examples/renderers/custom.json") == "examples/renderers/custom.json"
    assert "classic" in renderer_listing()
    assert "stress" in renderer_listing()
    assert "default" in renderer_listing()
    assert "activity-roll" in renderer_listing()
    assert "thermal-roll" in renderer_listing()
