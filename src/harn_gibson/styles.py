"""Declarative display style packs for browser and renderer context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StylePack:
    id: str
    label: str
    description: str
    tones: dict[str, tuple[int, int, int]]
    canvas: dict[str, Any] = field(default_factory=dict)
    css_vars: dict[str, str] = field(default_factory=dict)
    motifs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.style-pack.v1",
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tones": {key: list(value) for key, value in self.tones.items()},
            "canvas": self.canvas,
            "cssVars": self.css_vars,
            "motifs": list(self.motifs),
        }


STYLE_PACKS: tuple[StylePack, ...] = (
    StylePack(
        id="gibson",
        label="Gibson Link",
        description="Cold neon city-grid display with cyan, green, amber, and magenta signal layers.",
        tones={
            "amber": (255, 204, 102),
            "cyan": (88, 215, 255),
            "green": (105, 255, 184),
            "magenta": (255, 91, 200),
            "red": (255, 89, 89),
            "white": (230, 255, 248),
        },
        canvas={
            "background": "#05070b",
            "gridTone": "cyan",
            "gridAlpha": 0.12,
            "gridPerspective": 0.32,
            "horizonTone": "cyan",
            "horizonAlpha": 0.0,
        },
        css_vars={
            "--bg": "#05060a",
            "--stage-bg": "#070b0f",
            "--panel": "rgba(13, 18, 25, 0.86)",
            "--line": "rgba(110, 255, 207, 0.22)",
            "--green": "#69ffb8",
            "--cyan": "#58d7ff",
            "--amber": "#ffcc66",
            "--magenta": "#ff5bc8",
            "--text": "#e8fff8",
            "--muted": "#8aa69f",
        },
        motifs=("city-grid", "packet-routes", "vector-ice"),
    ),
    StylePack(
        id="neon-noir",
        label="Neon Noir",
        description="Hot magenta and cyan back-alley cyberspace with stronger horizon glow.",
        tones={
            "amber": (255, 185, 86),
            "cyan": (82, 226, 255),
            "green": (115, 255, 168),
            "magenta": (255, 70, 214),
            "red": (255, 88, 111),
            "white": (247, 242, 255),
        },
        canvas={
            "background": "#090713",
            "gridTone": "magenta",
            "gridAlpha": 0.17,
            "gridPerspective": 0.42,
            "horizonTone": "amber",
            "horizonAlpha": 0.28,
        },
        css_vars={
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
        motifs=("horizon-glow", "hotline-grid", "chrome-decals"),
    ),
    StylePack(
        id="mainframe",
        label="Mainframe Breach",
        description="Green phosphor command architecture with amber warning traces and restrained cyan edges.",
        tones={
            "amber": (255, 207, 92),
            "cyan": (94, 220, 210),
            "green": (117, 255, 127),
            "magenta": (218, 107, 255),
            "red": (255, 94, 94),
            "white": (230, 255, 229),
        },
        canvas={
            "background": "#040904",
            "gridTone": "green",
            "gridAlpha": 0.15,
            "gridPerspective": 0.22,
            "horizonTone": "amber",
            "horizonAlpha": 0.12,
        },
        css_vars={
            "--bg": "#030703",
            "--stage-bg": "#050b05",
            "--panel": "rgba(7, 18, 8, 0.88)",
            "--line": "rgba(117, 255, 127, 0.24)",
            "--green": "#75ff7f",
            "--cyan": "#5edcd2",
            "--amber": "#ffcf5c",
            "--magenta": "#da6bff",
            "--text": "#e6ffe5",
            "--muted": "#91ae89",
        },
        motifs=("phosphor-grid", "audit-frames", "amber-alerts"),
    ),
)

DEFAULT_STYLE_ID = "gibson"


def style_pack_ids() -> tuple[str, ...]:
    return tuple(style.id for style in STYLE_PACKS)


def style_pack_by_id(style_id: str) -> StylePack | None:
    normalized = style_id.strip().lower()
    for style in STYLE_PACKS:
        if style.id == normalized:
            return style
    return None


def default_style_pack() -> StylePack:
    style = style_pack_by_id(DEFAULT_STYLE_ID)
    if style is None:  # pragma: no cover - module invariant
        raise RuntimeError("default style pack is missing")
    return style


def style_pack_from_name(value: str | None) -> StylePack:
    if not value:
        return default_style_pack()
    return style_pack_by_id(value) or default_style_pack()


__all__ = [
    "DEFAULT_STYLE_ID",
    "STYLE_PACKS",
    "StylePack",
    "default_style_pack",
    "style_pack_by_id",
    "style_pack_from_name",
    "style_pack_ids",
]
