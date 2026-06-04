"""Visual primitive and effect catalog for renderer prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CatalogKind = Literal["primitive", "effect"]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    kind: CatalogKind
    description: str
    props: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "props": list(self.props),
            "tags": list(self.tags),
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True, slots=True)
class VisualCatalog:
    primitives: tuple[CatalogEntry, ...]
    effects: tuple[CatalogEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.visual-catalog.v1",
            "primitives": [entry.to_dict() for entry in self.primitives],
            "effects": [entry.to_dict() for entry in self.effects],
        }

    def entry(self, entry_id: str) -> CatalogEntry | None:
        for entry in (*self.primitives, *self.effects):
            if entry.id == entry_id:
                return entry
        return None


def default_visual_catalog() -> VisualCatalog:
    return VisualCatalog(
        primitives=(
            CatalogEntry(
                "viewport",
                "primitive",
                "Top-level camera or coordinate space for a display surface.",
                ("title", "theme", "camera", "perspective"),
                ("generic", "layout"),
            ),
            CatalogEntry(
                "text_stream",
                "primitive",
                "Mutable text buffer suitable for assistant output, logs, or command output.",
                ("text", "title", "streamId", "isStreaming", "maxChars"),
                ("generic", "stream"),
            ),
            CatalogEntry(
                "glyph_layer",
                "primitive",
                "A positioned layer of text glyphs, symbols, numbers, or code fragments.",
                ("text", "font", "density", "motion", "palette"),
                ("generic", "cinematic"),
            ),
            CatalogEntry(
                "data_rain",
                "primitive",
                "Animated glyph columns for code rain, telemetry waterfalls, packet curtains, or signal noise.",
                (
                    "glyphs",
                    "columns",
                    "density",
                    "speed",
                    "direction",
                    "tone",
                    "accentTone",
                    "opacity",
                    "position",
                    "size",
                    "trail",
                    "bands",
                    "glitch",
                    "seed",
                ),
                ("generic", "cinematic", "motion", "text"),
            ),
            CatalogEntry(
                "mesh",
                "primitive",
                "Abstract polygon or wireframe geometry that can become buildings, files, portals, or terrain.",
                ("vertices", "edges", "material", "position", "scale"),
                ("generic", "3d"),
            ),
            CatalogEntry(
                "hologram",
                "primitive",
                "Layered projection with rings, scan planes, beams, panels, orbiting motes, and short labels.",
                (
                    "position",
                    "scale",
                    "tone",
                    "accentTone",
                    "opacity",
                    "rings",
                    "beams",
                    "panels",
                    "motes",
                    "scan",
                    "spin",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "projection", "motion"),
            ),
            CatalogEntry(
                "signal_scope",
                "primitive",
                "Radar or oscilloscope display for telemetry, intrusions, command activity, and signal traces.",
                (
                    "mode",
                    "position",
                    "scale",
                    "tone",
                    "accentTone",
                    "opacity",
                    "rings",
                    "spokes",
                    "sweep",
                    "sweepSpeed",
                    "blips",
                    "waveforms",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "motion", "telemetry"),
            ),
            CatalogEntry(
                "tunnel_grid",
                "primitive",
                "Perspective vector tunnel for flythroughs, data corridors, and mainframe traversal scenes.",
                (
                    "position",
                    "size",
                    "rings",
                    "spokes",
                    "lanes",
                    "packets",
                    "speed",
                    "twist",
                    "depth",
                    "direction",
                    "tone",
                    "accentTone",
                    "opacity",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "motion", "3d"),
            ),
            CatalogEntry(
                "wire_landscape",
                "primitive",
                "Animated wireframe terrain or filesystem plane with peaks, rails, packets, and focus state.",
                (
                    "position",
                    "size",
                    "rows",
                    "columns",
                    "depth",
                    "height",
                    "peaks",
                    "focusPeakId",
                    "packets",
                    "speed",
                    "tone",
                    "accentTone",
                    "opacity",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "motion", "3d", "map"),
            ),
            CatalogEntry(
                "data_vault",
                "primitive",
                "Rotating wireframe vault/core with nested layers, orbiting locks, panels, and packet motes.",
                (
                    "position",
                    "scale",
                    "tone",
                    "accentTone",
                    "opacity",
                    "layers",
                    "rings",
                    "panels",
                    "locks",
                    "packets",
                    "spin",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "motion", "3d", "security"),
            ),
            CatalogEntry(
                "black_ice",
                "primitive",
                "Faceted ICE barrier with scanner shutters, breach glow, fracture lines, and sentry locks.",
                (
                    "position",
                    "size",
                    "columns",
                    "rows",
                    "depth",
                    "breach",
                    "breachPosition",
                    "fractures",
                    "sentries",
                    "sweep",
                    "sweepSpeed",
                    "tone",
                    "accentTone",
                    "opacity",
                    "label",
                    "seed",
                ),
                ("gibson", "cinematic", "security", "barrier", "motion"),
            ),
            CatalogEntry(
                "svg_layer",
                "primitive",
                "Constrained SVG-style vector layer for symbols, schematics, decals, and animated traces.",
                (
                    "viewBox",
                    "paths",
                    "rects",
                    "lines",
                    "polylines",
                    "polygons",
                    "circles",
                    "labels",
                    "groups",
                    "gradients",
                    "traces",
                    "symbols",
                    "filters",
                    "clip",
                    "keyframes",
                    "durationMs",
                    "delayMs",
                    "loop",
                    "yoyo",
                    "position",
                    "scale",
                    "tone",
                ),
                ("generic", "vector", "cinematic"),
                {
                    "safety": (
                        "structured vector data only; no raw markup, scripts, event handlers, foreignObject, "
                        "or external refs"
                    ),
                    "curatedSymbols": (
                        "globe",
                        "filesystem_gate",
                        "reticle",
                        "data_tunnel",
                        "ice_wall",
                        "mainframe_core",
                    ),
                    "animation": (
                        "stroke_reveal",
                        "dash_motion",
                        "pulse",
                        "spin",
                        "clip_reveal",
                        "group_transform",
                        "gradient_paint",
                        "path_morph",
                        "path_trace_particles",
                        "symbol_orbit",
                        "symbol_scan",
                        "chromatic_split",
                        "scanline_overlay",
                        "ghost_echo",
                        "keyframe_transform",
                    ),
                    "filters": (
                        "glow",
                        "bloom",
                        "haze",
                        "chromatic_split",
                        "ghost",
                        "scanline",
                    ),
                    "clips": ("rect", "circle", "iris", "wipe", "scan"),
                },
            ),
            CatalogEntry(
                "node_graph",
                "primitive",
                "Nodes and edges for agents, tools, files, hosts, or arbitrary entities.",
                ("nodes", "edges", "layout", "focusNodeId"),
                ("generic", "map"),
            ),
            CatalogEntry(
                "trace_route",
                "primitive",
                "Animated hop-by-hop route with packet pulses, curved links, labels, and focus state.",
                (
                    "hops",
                    "links",
                    "focusHopId",
                    "packets",
                    "speed",
                    "tone",
                    "accentTone",
                    "label",
                    "seed",
                ),
                ("gibson", "network", "motion", "map"),
            ),
            CatalogEntry(
                "particle_field",
                "primitive",
                "Low-level particles for packets, sparks, rain, snow, stars, or data motes.",
                ("count", "velocity", "emitter", "emitters", "color", "blend", "label", "seed"),
                ("generic", "motion"),
            ),
            CatalogEntry(
                "city_block",
                "primitive",
                "Extruded blocks suitable for Gibson-style city grids or 3D filesystem districts.",
                ("blocks", "heightScale", "streets", "labels", "cameraPath", "cameraDurationMs", "cameraLoop"),
                ("gibson", "3d", "map"),
            ),
            CatalogEntry(
                "ribbon",
                "primitive",
                "A flexible path for data flows, timelines, traversal routes, or command pipelines.",
                ("points", "width", "material", "direction", "labels"),
                ("generic", "motion"),
            ),
        ),
        effects=(
            CatalogEntry(
                "pulse",
                "effect",
                "Radial or target-bound emphasis pulse.",
                ("targetId", "tone", "durationMs", "intensity"),
                ("generic", "timed"),
            ),
            CatalogEntry(
                "glitch",
                "effect",
                "Temporary displacement, chromatic split, dropped frames, or noisy text corruption.",
                ("targetId", "durationMs", "amount", "seed"),
                ("cinematic", "timed"),
            ),
            CatalogEntry(
                "breach_wave",
                "effect",
                "Expanding cinematic breach overlay with rings, shards, scan flashes, and optional label.",
                ("targetId", "durationMs", "tone", "accentTone", "intensity", "rings", "shards", "label", "position"),
                ("gibson", "cinematic", "motion", "timed"),
            ),
            CatalogEntry(
                "camera_jolt",
                "effect",
                "Scene-level shake, zoom, and roll for impact beats or sudden traversal changes.",
                ("targetId", "durationMs", "intensity", "zoom", "roll", "position", "seed"),
                ("gibson", "cinematic", "camera", "timed"),
            ),
            CatalogEntry(
                "camera_path",
                "effect",
                "Scene-level pan, zoom, and roll keyframes for staged motion across a coalesced render window.",
                ("targetId", "durationMs", "keyframes", "position", "loop", "yoyo", "seed"),
                ("gibson", "cinematic", "camera", "timed"),
            ),
            CatalogEntry(
                "scan",
                "effect",
                "Sweep a beam, line, frustum, or grid over a target.",
                ("targetId", "durationMs", "direction", "color"),
                ("generic", "timed"),
            ),
            CatalogEntry(
                "flythrough",
                "effect",
                "Move the camera through a coordinate space over time.",
                ("cameraPath", "durationMs", "easing", "lookAt"),
                ("generic", "3d", "timed"),
            ),
            CatalogEntry(
                "extrude",
                "effect",
                "Grow flat or abstract data into 3D forms.",
                ("targetId", "from", "to", "durationMs"),
                ("gibson", "3d", "timed"),
            ),
            CatalogEntry(
                "packet_burst",
                "effect",
                "Emit particles or glyphs along a route.",
                ("sourceId", "targetId", "count", "durationMs"),
                ("cinematic", "motion", "timed"),
            ),
            CatalogEntry(
                "timeline_cue",
                "effect",
                "Play a bounded sequence of labeled cue markers over one persistent animation window.",
                ("targetId", "cues", "durationMs", "label", "width", "tone", "accentTone"),
                ("cinematic", "motion", "timed", "sequence"),
            ),
            CatalogEntry(
                "route_trace",
                "effect",
                "Animate packets through labeled route waypoints over one persistent render window.",
                ("targetId", "points", "durationMs", "tone", "accentTone", "packets", "tail", "label", "seed"),
                ("gibson", "cinematic", "motion", "timed", "sequence"),
            ),
            CatalogEntry(
                "vector_trace",
                "effect",
                "Move glowing particles along declared vector-space points on an svg_layer.",
                ("targetId", "points", "count", "speed", "tail", "tone"),
                ("generic", "vector", "motion"),
            ),
            CatalogEntry(
                "vector_keyframes",
                "effect",
                "Loop numeric transform keyframes on an svg_layer or nested vector group.",
                ("targetId", "keyframes", "durationMs", "delayMs", "loop", "yoyo"),
                ("generic", "vector", "motion", "timed"),
            ),
            CatalogEntry(
                "typewriter",
                "effect",
                "Reveal text over a specified duration.",
                ("targetId", "buffer", "durationMs", "cursor"),
                ("generic", "stream", "timed"),
            ),
            CatalogEntry(
                "hold",
                "effect",
                "Keep an object active until a later mutation removes or replaces it.",
                ("targetId", "until", "reason"),
                ("generic", "state"),
            ),
        ),
    )


__all__ = [
    "CatalogEntry",
    "CatalogKind",
    "VisualCatalog",
    "default_visual_catalog",
]
