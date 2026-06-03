from __future__ import annotations

from harn_gibson.catalog import CatalogEntry, VisualCatalog, default_visual_catalog


def test_catalog_entry_and_visual_catalog_to_dict() -> None:
    primitive = CatalogEntry("sprite", "primitive", "bitmap sprite", ("image",), ("generic",), {"alpha": True})
    effect = CatalogEntry("spin", "effect", "rotate target", ("targetId", "durationMs"), ("timed",))
    catalog = VisualCatalog((primitive,), (effect,))

    assert primitive.to_dict() == {
        "id": "sprite",
        "kind": "primitive",
        "description": "bitmap sprite",
        "props": ["image"],
        "tags": ["generic"],
        "metadata": {"alpha": True},
    }
    assert effect.to_dict() == {
        "id": "spin",
        "kind": "effect",
        "description": "rotate target",
        "props": ["targetId", "durationMs"],
        "tags": ["timed"],
    }
    assert catalog.entry("sprite") == primitive
    assert catalog.entry("spin") == effect
    assert catalog.entry("missing") is None
    assert catalog.to_dict()["schema"] == "harn-gibson.visual-catalog.v1"


def test_default_visual_catalog_has_generic_and_cinematic_building_blocks() -> None:
    catalog = default_visual_catalog()
    primitive_ids = {entry.id for entry in catalog.primitives}
    effect_ids = {entry.id for entry in catalog.effects}

    assert {
        "text_stream",
        "mesh",
        "hologram",
        "svg_layer",
        "data_rain",
        "particle_field",
        "city_block",
        "trace_route",
    } <= primitive_ids
    assert {"glitch", "flythrough", "packet_burst", "vector_trace", "vector_keyframes", "hold"} <= effect_ids
    assert catalog.entry("city_block") is not None
    assert "gibson" in catalog.entry("city_block").tags  # type: ignore[union-attr]
    city_block = catalog.entry("city_block")
    assert city_block is not None
    assert {"cameraPath", "cameraDurationMs", "cameraLoop"} <= set(city_block.props)
    svg_layer = catalog.entry("svg_layer")
    assert svg_layer is not None
    assert {
        "rects",
        "lines",
        "polylines",
        "polygons",
        "groups",
        "gradients",
        "traces",
        "symbols",
        "filters",
        "clip",
        "keyframes",
        "durationMs",
        "yoyo",
    } <= set(svg_layer.props)
    assert svg_layer.metadata["curatedSymbols"] == (
        "globe",
        "filesystem_gate",
        "reticle",
        "data_tunnel",
        "ice_wall",
        "mainframe_core",
    )
    assert "path_trace_particles" in svg_layer.metadata["animation"]
    assert "symbol_orbit" in svg_layer.metadata["animation"]
    assert "group_transform" in svg_layer.metadata["animation"]
    assert "clip_reveal" in svg_layer.metadata["animation"]
    assert "chromatic_split" in svg_layer.metadata["animation"]
    assert "keyframe_transform" in svg_layer.metadata["animation"]
    assert svg_layer.metadata["filters"] == (
        "glow",
        "bloom",
        "haze",
        "chromatic_split",
        "ghost",
        "scanline",
    )
    assert svg_layer.metadata["clips"] == ("rect", "circle", "iris", "wipe", "scan")
    data_rain = catalog.entry("data_rain")
    assert data_rain is not None
    assert {"glyphs", "columns", "density", "speed", "direction", "bands", "glitch"} <= set(data_rain.props)
    assert {"cinematic", "motion", "text"} <= set(data_rain.tags)
    hologram = catalog.entry("hologram")
    assert hologram is not None
    assert {"rings", "beams", "panels", "motes", "scan", "spin", "label"} <= set(hologram.props)
    assert {"gibson", "cinematic", "projection", "motion"} <= set(hologram.tags)
    trace_route = catalog.entry("trace_route")
    assert trace_route is not None
    assert {"hops", "links", "focusHopId", "packets", "speed", "label"} <= set(trace_route.props)
    assert {"gibson", "network", "motion", "map"} <= set(trace_route.tags)
