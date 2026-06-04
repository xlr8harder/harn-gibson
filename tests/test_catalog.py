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
        "signal_scope",
        "tunnel_grid",
        "wire_landscape",
        "terminal_wall",
        "data_vault",
        "black_ice",
        "svg_layer",
        "data_rain",
        "particle_field",
        "city_block",
        "trace_route",
    } <= primitive_ids
    assert {
        "glitch",
        "signal_interference",
        "breach_wave",
        "camera_jolt",
        "camera_path",
        "flythrough",
        "packet_burst",
        "timeline_cue",
        "route_trace",
        "vector_trace",
        "vector_keyframes",
        "hold",
    } <= effect_ids
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
    assert "path_morph" in svg_layer.metadata["animation"]
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
    particle_field = catalog.entry("particle_field")
    assert particle_field is not None
    assert {"emitter", "emitters", "count", "velocity", "label", "seed"} <= set(particle_field.props)
    hologram = catalog.entry("hologram")
    assert hologram is not None
    assert {"rings", "beams", "panels", "motes", "scan", "spin", "label"} <= set(hologram.props)
    assert {"gibson", "cinematic", "projection", "motion"} <= set(hologram.tags)
    signal_scope = catalog.entry("signal_scope")
    assert signal_scope is not None
    assert {"mode", "rings", "spokes", "sweep", "blips", "waveforms", "label"} <= set(signal_scope.props)
    assert {"gibson", "cinematic", "motion", "telemetry"} <= set(signal_scope.tags)
    tunnel_grid = catalog.entry("tunnel_grid")
    assert tunnel_grid is not None
    assert {"rings", "spokes", "lanes", "packets", "speed", "twist", "direction", "label"} <= set(
        tunnel_grid.props
    )
    assert {"gibson", "cinematic", "motion", "3d"} <= set(tunnel_grid.tags)
    wire_landscape = catalog.entry("wire_landscape")
    assert wire_landscape is not None
    assert {"rows", "columns", "peaks", "focusPeakId", "packets", "speed", "label"} <= set(wire_landscape.props)
    assert {"gibson", "cinematic", "motion", "3d", "map"} <= set(wire_landscape.tags)
    terminal_wall = catalog.entry("terminal_wall")
    assert terminal_wall is not None
    assert {"panels", "columns", "rows", "scan", "cursor", "speed", "seed"} <= set(terminal_wall.props)
    assert {"gibson", "cinematic", "motion", "text", "terminal"} <= set(terminal_wall.tags)
    data_vault = catalog.entry("data_vault")
    assert data_vault is not None
    assert {"layers", "rings", "panels", "locks", "packets", "spin", "label", "seed"} <= set(data_vault.props)
    assert {"gibson", "cinematic", "motion", "3d", "security"} <= set(data_vault.tags)
    black_ice = catalog.entry("black_ice")
    assert black_ice is not None
    assert {"columns", "rows", "breach", "fractures", "sentries", "sweep", "label", "seed"} <= set(
        black_ice.props
    )
    assert {"gibson", "cinematic", "security", "barrier", "motion"} <= set(black_ice.tags)
    trace_route = catalog.entry("trace_route")
    assert trace_route is not None
    assert {"hops", "links", "focusHopId", "packets", "speed", "label"} <= set(trace_route.props)
    assert {"gibson", "network", "motion", "map"} <= set(trace_route.tags)
    breach_wave = catalog.entry("breach_wave")
    assert breach_wave is not None
    assert {"targetId", "intensity", "rings", "shards", "label", "position"} <= set(breach_wave.props)
    assert {"gibson", "cinematic", "motion", "timed"} <= set(breach_wave.tags)
    signal_interference = catalog.entry("signal_interference")
    assert signal_interference is not None
    assert {"targetId", "intensity", "bands", "blocks", "noise", "speed", "label", "seed"} <= set(
        signal_interference.props
    )
    assert {"gibson", "cinematic", "motion", "timed", "overlay"} <= set(signal_interference.tags)
    camera_jolt = catalog.entry("camera_jolt")
    assert camera_jolt is not None
    assert {"targetId", "intensity", "zoom", "roll", "position", "seed"} <= set(camera_jolt.props)
    assert {"gibson", "cinematic", "camera", "timed"} <= set(camera_jolt.tags)
    camera_path = catalog.entry("camera_path")
    assert camera_path is not None
    assert {"targetId", "keyframes", "durationMs", "position", "loop", "yoyo", "seed"} <= set(camera_path.props)
    assert {"gibson", "cinematic", "camera", "timed"} <= set(camera_path.tags)
    timeline_cue = catalog.entry("timeline_cue")
    assert timeline_cue is not None
    assert {"targetId", "cues", "durationMs", "label"} <= set(timeline_cue.props)
    assert {"cinematic", "motion", "timed", "sequence"} <= set(timeline_cue.tags)
    route_trace = catalog.entry("route_trace")
    assert route_trace is not None
    assert {"targetId", "points", "durationMs", "packets", "tail", "label"} <= set(route_trace.props)
    assert {"gibson", "cinematic", "motion", "timed", "sequence"} <= set(route_trace.tags)
