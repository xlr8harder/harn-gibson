from __future__ import annotations

from harn_gibson.styles import (
    DEFAULT_STYLE_ID,
    default_style_pack,
    style_pack_by_id,
    style_pack_from_name,
    style_pack_ids,
)


def test_style_pack_catalog_and_serialization() -> None:
    ids = style_pack_ids()
    neon = style_pack_by_id(" NEON-NOIR ")

    assert DEFAULT_STYLE_ID == "gibson"
    assert {"gibson", "neon-noir", "mainframe"} <= set(ids)
    assert default_style_pack().id == "gibson"
    assert neon is not None
    assert neon.to_dict() == {
        "schema": "harn-gibson.style-pack.v1",
        "id": "neon-noir",
        "label": "Neon Noir",
        "description": "Hot magenta and cyan back-alley cyberspace with stronger horizon glow.",
        "tones": {
            "amber": [255, 185, 86],
            "cyan": [82, 226, 255],
            "green": [115, 255, 168],
            "magenta": [255, 70, 214],
            "red": [255, 88, 111],
            "white": [247, 242, 255],
        },
        "canvas": {
            "background": "#090713",
            "gridTone": "magenta",
            "gridAlpha": 0.17,
            "gridPerspective": 0.42,
            "horizonTone": "amber",
            "horizonAlpha": 0.28,
        },
        "cssVars": {
            "--bg": "#080611",
            "--stage-bg": "#090713",
            "--panel": "rgba(18, 10, 28, 0.86)",
            "--line": "rgba(255, 70, 214, 0.28)",
            "--green": "#73ffa8",
            "--cyan": "#52e2ff",
            "--amber": "#ffb956",
            "--magenta": "#ff46d6",
            "--text": "#fff2ff",
            "--muted": "#b99fc7",
        },
        "motifs": ["horizon-glow", "hotline-grid", "chrome-decals"],
    }


def test_style_pack_from_name_falls_back_to_default() -> None:
    assert style_pack_from_name("mainframe").id == "mainframe"
    assert style_pack_from_name(None).id == "gibson"
    assert style_pack_from_name("").id == "gibson"
    assert style_pack_from_name("unknown").id == "gibson"
    assert style_pack_by_id("unknown") is None
