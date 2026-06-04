"""Hard-coded cinematic renderer for live harn-gibson dogfood sessions."""

from __future__ import annotations

import json
import sys
from typing import Any


def main() -> None:
    payload = json.load(sys.stdin)
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))
    display_style = _display_style(project, context)
    style_motifs = _style_motifs(project, context)
    event = _latest_event(requests)
    event_type = _text(event.get("eventType"), "event")
    phase = _text(event.get("phase"), "lifecycle")
    sequence = _int(event.get("sequence"), 0)
    timestamp_ms = _int(event.get("timestampMs"), 0)
    tone = _phase_tone(phase, event_type, display_style)
    accent = _accent_tone(phase, event_type, display_style)
    summary = _clip(_text(event.get("summary"), event_type), 110)
    timeline = _dict(_dict(context.get("renderInput")).get("timeline"))
    duration_ms = _clamp(_int(timeline.get("durationMs"), 0), 4200, 9800)
    project_name = _text(project.get("name"), "project")
    touched = _touched_files(context)
    entries = _repo_entries(context)

    mutations = [
        {
            "op": "patch",
            "targetId": "status",
            "props": {"text": f"dogfood::{event_type}", "phase": phase, "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {
                "sequence": sequence,
                "phase": phase,
                "eventType": "dogfood_renderer",
                "title": "Dogfood renderer",
                "summary": f"{event_type}: {summary}",
            },
        },
        _upsert_data_rain(event_type, summary, tone, accent, sequence),
        _upsert_opcode_glyphs(event_type, summary, tone, accent, sequence, touched),
        _upsert_tunnel(event_type, tone, accent, sequence, touched),
        _upsert_data_vault(project_name, event_type, tone, accent, sequence, touched, entries),
        _upsert_ice_mesh(event_type, phase, tone, accent, sequence, touched),
        _upsert_scope(event_type, phase, tone, accent, sequence, touched),
        _upsert_control_graph(event_type, phase, tone, accent, sequence, touched),
        _upsert_route(event_type, phase, tone, accent, sequence, touched),
        _upsert_city(entries, touched, event_type, tone, accent, sequence),
        _upsert_file_particles(entries, touched, tone, accent, sequence),
        _upsert_hologram(project_name, event_type, tone, accent, sequence, touched, entries),
        _upsert_command_ribbon(event_type, phase, tone, accent, sequence, touched),
        _upsert_sigil(event_type, summary, tone, accent, sequence, touched),
        _timeline_cue_animation(event_type, phase, sequence, timestamp_ms, duration_ms, tone, accent, touched),
        _camera_path_animation(sequence, timestamp_ms, duration_ms),
        _camera_jolt_animation(sequence, timestamp_ms, phase),
        _packet_burst_animation(sequence, timestamp_ms, phase, tone),
        _scan_animation(sequence, timestamp_ms, phase),
        _extrude_animation(sequence, timestamp_ms, phase),
    ]
    if phase == "after" or "error" in event_type or "fail" in summary.lower():
        mutations.append(_breach_wave_animation(sequence, timestamp_ms, phase, tone, accent))

    metadata = {
        "renderer": "gibson-dogfood-showcase",
        "intent": f"stage {event_type} as a Hollywood terminal intrusion",
        "eventType": event_type,
        "phase": phase,
        "touchedFileCount": len(touched),
    }
    if display_style != "gibson":
        metadata["displayStyle"] = display_style
        if style_motifs:
            metadata["styleMotifs"] = style_motifs
    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": metadata,
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


def _latest_event(requests: list[Any]) -> dict[str, Any]:
    if not requests:
        return {"eventType": "idle", "phase": "lifecycle", "sequence": 0, "timestampMs": 0}
    request = _dict(requests[-1])
    return _dict(request.get("event"))


def _touched_files(context: dict[str, Any]) -> list[dict[str, Any]]:
    project = _dict(context.get("project"))
    touched = _dict(project.get("touchedFiles"))
    return [_dict(item) for item in _list(touched.get("files"))[:8] if _dict(item).get("path")]


def _repo_entries(context: dict[str, Any]) -> list[dict[str, Any]]:
    project = _dict(context.get("project"))
    topology = _dict(project.get("repoTopology"))
    return [_dict(item) for item in _list(topology.get("entries"))[:9]]


def _display_style(project: dict[str, Any], context: dict[str, Any]) -> str:
    display_style = _text(project.get("displayStyle"), "")
    if display_style:
        return display_style
    project_style = _dict(project.get("stylePack"))
    style_id = _text(project_style.get("id"), "")
    if style_id:
        return style_id
    continuity_style = _dict(_dict(context.get("visualContinuity")).get("style"))
    return _text(continuity_style.get("id"), "gibson")


def _style_motifs(project: dict[str, Any], context: dict[str, Any]) -> list[str]:
    project_style = _dict(project.get("stylePack"))
    motifs = [_text(item, "") for item in _list(project_style.get("motifs"))]
    motifs = [item for item in motifs if item]
    if motifs:
        return motifs
    continuity_style = _dict(_dict(context.get("visualContinuity")).get("style"))
    return [item for item in (_text(value, "") for value in _list(continuity_style.get("motifs"))) if item]


def _upsert_data_rain(event_type: str, summary: str, tone: str, accent: str, sequence: int) -> dict[str, Any]:
    glyphs = " ".join([event_type.upper(), summary.upper(), "ACCESS GRANTED 0xC0DE 2600 GIBSON"])
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-rain",
            "kind": "data_rain",
            "region": "stage",
            "props": {
                "glyphs": _clip(glyphs, 220),
                "columns": 52,
                "density": 0.78,
                "speed": 0.82,
                "direction": "down",
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.42,
                "position": {"x": 0.50, "y": 0.50},
                "size": {"w": 1.0, "h": 0.92},
                "trail": 18,
                "bands": 3,
                "glitch": 0.18,
                "seed": sequence,
            },
        },
    }


def _upsert_opcode_glyphs(
    event_type: str,
    summary: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    touched_labels = " ".join(_path_label(_text(item.get("path"), "")) for item in touched[:4])
    text = " ".join(
        [
            "GIBSON",
            event_type.upper().replace("_", "-"),
            summary.upper(),
            touched_labels.upper(),
            "TRACE ROUTE ICE MESH NODE GRAPH",
        ]
    )
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-opcodes",
            "kind": "glyph_layer",
            "region": "stage",
            "props": {
                "text": _clip(text, 260),
                "font": "terminal",
                "density": round(0.22 + min(0.20, len(touched) * 0.025), 3),
                "motion": "drift",
                "palette": tone,
                "tone": tone,
                "accentTone": accent,
                "seed": sequence + 7,
            },
        },
    }


def _upsert_tunnel(
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-tunnel",
            "kind": "tunnel_grid",
            "region": "stage",
            "props": {
                "position": {"x": 0.52, "y": 0.46},
                "size": {"w": 0.72, "h": 0.54},
                "rings": 18,
                "spokes": 18,
                "lanes": 9,
                "packets": 46 + min(40, len(touched) * 7),
                "speed": 0.86,
                "twist": 0.52 if sequence % 2 else -0.44,
                "depth": 1.25,
                "direction": "inward",
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.74,
                "label": _clip(event_type.upper().replace("_", " "), 22),
                "seed": sequence + 13,
            },
        },
    }


def _upsert_scope(
    event_type: str,
    phase: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    blips = [
        {
            "angle": round((index * 0.84 + sequence * 0.09) % 6.28, 3),
            "radius": round(0.22 + (index % 5) * 0.13, 3),
            "tone": "magenta" if item else accent,
            "label": _path_label(_text(item.get("path"), event_type)) if item else phase.upper(),
            "intensity": 0.9,
        }
        for index, item in enumerate(touched[:6] or [{}])
    ]
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-scope",
            "kind": "signal_scope",
            "region": "stage",
            "props": {
                "mode": "radar",
                "position": {"x": 0.78, "y": 0.30},
                "scale": 0.17,
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.9,
                "rings": 5,
                "spokes": 10,
                "sweep": True,
                "sweepSpeed": 1.15,
                "blips": blips,
                "waveforms": [
                    {"label": "IO", "amplitude": 0.22, "frequency": 3.1, "speed": 0.0022, "tone": tone},
                    {"label": "CPU", "amplitude": 0.15, "frequency": 6.2, "speed": 0.0016, "tone": accent},
                ],
                "label": _clip(f"{phase}:{event_type}", 20),
                "seed": sequence + 29,
            },
        },
    }


def _upsert_data_vault(
    project_name: str,
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-vault",
            "kind": "data_vault",
            "region": "stage",
            "props": {
                "label": _clip(project_name.upper().replace("_", "-"), 18),
                "position": {"x": 0.36, "y": 0.42},
                "scale": round(0.13 + min(0.045, len(entries) * 0.004 + len(touched) * 0.003), 3),
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.86,
                "layers": 3 + min(3, len(entries) // 3),
                "rings": 4 + min(6, len(touched) + sequence % 3),
                "panels": 4 + min(8, len(entries) + len(touched)),
                "locks": 3 + min(9, len(touched) * 2 + sequence % 4),
                "packets": 32 + min(70, len(entries) * 4 + len(touched) * 8),
                "spin": 0.62 if sequence % 2 else -0.52,
                "seed": sequence + len(touched) * 19 + len(entries) * 5,
            },
        },
    }


def _upsert_ice_mesh(
    event_type: str,
    phase: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    height = 0.72 + min(0.22, len(touched) * 0.035)
    vertices = [
        [-0.52, -0.42, -0.52],
        [0.52, -0.42, -0.52],
        [0.52, 0.42, -0.52],
        [-0.52, 0.42, -0.52],
        [-0.38, -height, 0.34],
        [0.38, -height, 0.34],
        [0.48, 0.36, 0.48],
        [-0.48, 0.36, 0.48],
        [0.0, -0.08, 0.92],
        [0.0, 0.60, -0.06],
    ]
    edges = [
        [0, 1],
        [1, 2],
        [2, 3],
        [3, 0],
        [4, 5],
        [5, 6],
        [6, 7],
        [7, 4],
        [0, 4],
        [1, 5],
        [2, 6],
        [3, 7],
        [0, 8],
        [2, 8],
        [4, 8],
        [6, 8],
        [1, 9],
        [3, 9],
        [5, 9],
        [7, 9],
    ]
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-ice-mesh",
            "kind": "mesh",
            "region": "stage",
            "props": {
                "vertices": vertices,
                "edges": edges,
                "faces": [[0, 1, 5, 4], [2, 3, 7, 6], [0, 3, 9], [1, 2, 9], [4, 5, 8], [6, 7, 8]],
                "material": tone,
                "tone": tone,
                "accentTone": accent,
                "position": {"x": 0.51, "y": 0.46},
                "scale": round(0.18 + min(0.04, len(touched) * 0.007), 3),
                "rotation": {"x": 0.58 + (sequence % 3) * 0.06, "y": 0.70 + (sequence % 5) * 0.04},
                "spin": 0.52 if phase == "after" else 0.32,
                "label": _clip(f"ICE {event_type.upper().replace('_', ' ')}", 18),
                "seed": sequence + 37,
            },
        },
    }


def _upsert_control_graph(
    event_type: str,
    phase: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    focus = "file-0" if touched else "event"
    nodes = [
        {"id": "agent", "label": "AGENT", "x": 0.18, "y": 0.24, "tone": "green"},
        {"id": "router", "label": "ROUTER", "x": 0.31, "y": 0.18, "tone": "cyan"},
        {"id": "event", "label": event_type.upper()[:12], "x": 0.45, "y": 0.22, "tone": tone},
        {"id": "scene", "label": "SCENE", "x": 0.58, "y": 0.18, "tone": accent},
        {"id": "browser", "label": "BROWSER", "x": 0.72, "y": 0.24, "tone": "amber"},
    ]
    edges = [
        {"source": "agent", "target": "router", "label": "hook"},
        {"source": "router", "target": "event", "label": phase[:8]},
        {"source": "event", "target": "scene", "label": "plan"},
        {"source": "scene", "target": "browser", "label": "sse"},
    ]
    for index, item in enumerate(touched[:3]):
        node_id = f"file-{index}"
        nodes.append(
            {
                "id": node_id,
                "label": _path_label(_text(item.get("path"), "file")),
                "x": round(0.62 + index * 0.08, 3),
                "y": round(0.34 + index * 0.055, 3),
                "tone": "magenta",
            }
        )
        edges.append({"source": "scene", "target": node_id, "label": "touch"})
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-control-graph",
            "kind": "node_graph",
            "region": "stage",
            "props": {
                "nodes": nodes,
                "edges": edges,
                "layout": "fixed",
                "focusNodeId": focus,
                "tone": tone,
                "accentTone": accent,
                "label": "CONTROL GRAPH",
                "seed": sequence + 31,
            },
        },
    }


def _upsert_route(
    event_type: str,
    phase: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    focus = "target-0" if touched else "gibson-core"
    hops = [
        {"id": "agent", "label": "AGENT", "x": 0.10, "y": 0.84, "tone": "green"},
        {"id": "router", "label": phase.upper()[:10], "x": 0.29, "y": 0.72, "tone": tone},
        {"id": "event", "label": event_type.upper()[:12], "x": 0.48, "y": 0.62, "tone": accent},
        {"id": "gibson-core", "label": "GIBSON", "x": 0.69, "y": 0.48, "tone": "cyan"},
    ]
    links = [
        {"source": "agent", "target": "router", "label": "hook"},
        {"source": "router", "target": "event", "label": "route"},
        {"source": "event", "target": "gibson-core", "label": "render"},
    ]
    for index, item in enumerate(touched[:3]):
        hop_id = f"target-{index}"
        hops.append(
            {
                "id": hop_id,
                "label": _path_label(_text(item.get("path"), "file")),
                "x": round(0.78 + index * 0.06, 3),
                "y": round(0.35 + index * 0.12, 3),
                "tone": "magenta",
            }
        )
        links.append({"source": "gibson-core", "target": hop_id, "label": _text(item.get("operation"), "touch")})
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-route",
            "kind": "trace_route",
            "region": "stage",
            "props": {
                "hops": hops,
                "links": links,
                "focusHopId": focus,
                "packets": 28 + len(touched) * 4,
                "speed": 0.92,
                "tone": tone,
                "accentTone": accent,
                "label": "LIVE ROUTE",
                "seed": sequence + 41,
            },
        },
    }


def _upsert_command_ribbon(
    event_type: str,
    phase: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    lift = min(0.10, len(touched) * 0.018)
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-command-ribbon",
            "kind": "ribbon",
            "region": "stage",
            "props": {
                "points": [
                    {"x": 0.08, "y": 0.78},
                    {"x": 0.24, "y": round(0.70 - lift, 3)},
                    {"x": 0.43, "y": round(0.64 - lift * 0.5, 3)},
                    {"x": 0.60, "y": round(0.58 - lift, 3)},
                    {"x": 0.80, "y": 0.50},
                ],
                "width": 2.2 + min(1.4, len(touched) * 0.25),
                "material": accent,
                "direction": "east" if sequence % 2 else "west",
                "labels": [phase.upper()[:8], event_type.upper()[:14]],
                "seed": sequence + 53,
            },
        },
    }


def _upsert_city(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    touched_paths = [_text(item.get("path"), "") for item in touched]
    blocks = [
        {
            "id": "dogfood-city-core",
            "path": ".",
            "x": 0.10,
            "y": 0.64,
            "w": 0.07,
            "d": 0.08,
            "h": 0.28,
            "tone": "amber",
            "label": "ROOT",
        }
    ]
    for index, entry in enumerate(entries[:8]):
        path = _text(entry.get("path") or entry.get("name"), f"entry-{index}")
        children = _list(entry.get("children"))
        line_count = _entry_line_count(entry)
        touched_count = sum(
            1 for touched_path in touched_paths if touched_path == path or touched_path.startswith(f"{path}/")
        )
        height = round(
            0.16 + min(0.34, len(children) * 0.026 + min(0.18, line_count * 0.005) + touched_count * 0.08),
            3,
        )
        blocks.append(
            {
                "id": f"dogfood-city-{index}",
                "path": path,
                "x": round(0.17 + (index % 4) * 0.085, 3),
                "y": round(0.69 - (index // 4) * 0.12, 3),
                "w": 0.058,
                "d": 0.070,
                "h": height,
                "tone": "magenta" if touched_count else _entry_tone(_text(entry.get("kind"), "dir"), tone, accent),
                "label": _path_label(path),
                "touched": touched_count,
                "lines": line_count,
            }
        )
    if len(blocks) == 1:
        blocks.extend(_fallback_city_blocks(event_type, tone, accent))
    direction = -1 if sequence % 2 else 1
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-city",
            "kind": "city_block",
            "region": "stage",
            "props": {
                "focusBlockId": _city_focus(blocks),
                "heightScale": 1.18,
                "blocks": blocks,
                "labels": ["DOGFOOD CITY", f"{len(touched)} touched"],
                "tone": tone,
                "accentTone": accent,
                "cameraPath": {
                    "durationMs": 8200,
                    "loop": True,
                    "yoyo": True,
                    "keyframes": [
                        {"at": 0, "x": round(-0.012 * direction, 3), "y": 0.004, "scale": 0.995},
                        {"at": 0.48, "x": round(0.024 * direction, 3), "y": -0.018, "scale": 1.045},
                        {"at": 1, "x": 0.006, "y": 0.008, "scale": 1.01},
                    ],
                },
            },
        },
    }


def _upsert_hologram(
    project_name: str,
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-hologram",
            "kind": "hologram",
            "region": "stage",
            "props": {
                "label": _clip(project_name.upper().replace("_", "-"), 18),
                "position": {"x": 0.24, "y": 0.34},
                "scale": round(0.15 + min(0.05, len(entries) * 0.005), 3),
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.84,
                "rings": 4 + min(5, len(entries)),
                "beams": 5 + min(10, len(touched) * 2),
                "panels": 3 + min(7, len(touched) + sequence % 3),
                "motes": 22 + min(42, len(entries) * 3 + len(touched) * 5),
                "scan": True,
                "spin": 0.48 if sequence % 2 else -0.42,
                "seed": sequence + len(touched) * 17 + len(entries) * 3,
            },
        },
    }


def _upsert_file_particles(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    emitters = _file_particle_emitters(entries, touched, tone, accent, sequence)
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-file-sparks",
            "kind": "particle_field",
            "region": "stage",
            "props": {
                "count": sum(_int(item.get("count"), 0) for item in emitters),
                "velocity": 0.74,
                "emitter": emitters[0],
                "emitters": emitters,
                "color": accent,
                "blend": "screen",
                "label": f"{len(touched)} TOUCHED FILES" if touched else "EVENT SPARKS",
                "seed": sequence + 109,
            },
        },
    }


def _file_particle_emitters(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    tone: str,
    accent: str,
    sequence: int,
) -> list[dict[str, Any]]:
    if not touched:
        return [
            {
                "x": 0.28,
                "y": 0.62,
                "count": 28,
                "color": tone,
                "label": "EVENT",
                "seed": sequence + 11,
                "spread": 0.41,
            }
        ]
    emitters = []
    for index, item in enumerate(touched[:8]):
        path = _text(item.get("path"), f"event-{index}")
        entry_index = _entry_index_for_path(entries, path)
        if entry_index is None:
            x = round(0.20 + (index % 4) * 0.075, 3)
            y = round(0.64 - (index // 4) * 0.10, 3)
        else:
            x = round(0.17 + (entry_index % 4) * 0.085, 3)
            y = round(0.625 - (entry_index // 4) * 0.12, 3)
        signal_count = len(_list(item.get("phases"))) * 2 + len(_list(item.get("sources"))) + index
        emitters.append(
            {
                "x": x,
                "y": y,
                "count": 18 + min(10, signal_count),
                "color": "magenta" if index % 2 == 0 else accent,
                "label": _path_label(path),
                "seed": sequence + index * 17,
                "angle": round(-0.95 + index * 0.12, 3),
                "spread": round(0.30 + (index % 3) * 0.04, 3),
            }
        )
    return emitters


def _upsert_sigil(
    event_type: str,
    summary: str,
    tone: str,
    accent: str,
    sequence: int,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    label = _clip(event_type.upper().replace("_", " "), 14)
    return {
        "op": "upsert",
        "primitive": {
            "id": "dogfood-sigil",
            "kind": "svg_layer",
            "region": "stage",
            "props": {
                "viewBox": [0, 0, 100, 100],
                "position": {"x": 0.48, "y": 0.34},
                "scale": 0.20,
                "tone": tone,
                "accentTone": accent,
                "blend": "screen",
                "durationMs": 7200,
                "loop": True,
                "yoyo": True,
                "keyframes": [
                    {"at": 0, "scale": 1.0, "rotation": -0.03, "opacity": 0.84},
                    {"at": 0.42, "scale": 1.08, "rotation": 0.05, "opacity": 1.0},
                    {"at": 1, "scale": 1.02, "rotation": -0.01, "opacity": 0.9},
                ],
                "gradients": [
                    {
                        "id": "dogfood-core",
                        "type": "radial",
                        "center": {"x": 50, "y": 52},
                        "innerRadius": 4,
                        "outerRadius": 58,
                        "stops": [
                            {"offset": 0, "tone": "white", "alpha": 0.95},
                            {"offset": 0.42, "tone": tone, "alpha": 0.72},
                            {"offset": 1, "tone": accent, "alpha": 0.70},
                        ],
                    }
                ],
                "paths": [
                    {
                        "d": "M50 5 L91 29 L83 82 L50 96 L17 82 L9 29 Z",
                        "stroke": "gradient:dogfood-core",
                        "fill": "gradient:dogfood-core",
                        "fillAlpha": 0.12,
                        "width": 1.8,
                        "morphs": [
                            {"at": 0, "d": "M50 5 L91 29 L83 82 L50 96 L17 82 L9 29 Z"},
                            {"at": 0.5, "d": "M50 8 L87 24 L91 74 L50 92 L9 74 L13 24 Z"},
                            {"at": 1, "d": "M50 5 L91 29 L83 82 L50 96 L17 82 L9 29 Z"},
                        ],
                    },
                    {
                        "d": "M23 60 C35 25 65 25 77 60 M28 64 L50 32 L72 64",
                        "tone": accent,
                        "width": 2.2,
                        "dash": [8, 7],
                        "speed": 0.018,
                        "reveal": True,
                    },
                ],
                "symbols": [
                    {"kind": "globe", "x": 50, "y": 42, "r": 17, "tone": tone, "accentTone": accent, "packets": 7},
                    {
                        "kind": "filesystem_gate",
                        "x": 50,
                        "y": 42,
                        "w": 54,
                        "h": 36,
                        "tone": accent,
                        "accentTone": tone,
                        "open": bool(touched),
                    },
                    {"kind": "reticle", "x": 50, "y": 42, "r": 28, "tone": "white", "accentTone": accent},
                    {"kind": "ice_wall", "x": 50, "y": 43, "w": 66, "h": 42, "tone": "cyan", "accentTone": accent},
                ],
                "traces": [
                    {
                        "points": [
                            {"x": 18, "y": 78},
                            {"x": 32, "y": 28},
                            {"x": 68, "y": 28},
                            {"x": 82, "y": 78},
                            {"x": 50, "y": 91},
                            {"x": 18, "y": 78},
                        ],
                        "gradient": "gradient:dogfood-core",
                        "count": 9 + min(8, len(touched) * 2),
                        "speed": 0.00024,
                        "tail": 0.09,
                    }
                ],
                "labels": [
                    {"text": label, "x": 50, "y": 56, "tone": "white", "size": 6.4},
                    {"text": _clip(summary.upper(), 18), "x": 50, "y": 66, "tone": accent, "size": 4.4},
                ],
                "filters": [
                    {"kind": "chromatic_split", "intensity": 0.8, "offset": 1.2},
                    {"kind": "scanline", "alpha": 0.36, "spacing": 6},
                ],
                "clip": {"kind": "iris", "durationMs": 2600, "loop": True, "yoyo": True},
                "seed": sequence + 59,
            },
        },
    }


def _timeline_cue_animation(
    event_type: str,
    phase: str,
    sequence: int,
    timestamp_ms: int,
    duration_ms: int,
    tone: str,
    accent: str,
    touched: list[dict[str, Any]],
) -> dict[str, Any]:
    cues = [
        {"at": 0.04, "label": "HOOK", "tone": "green", "showLabel": True},
        {"at": 0.30, "label": phase.upper()[:8], "tone": tone},
        {"at": 0.58, "label": event_type.upper()[:10], "tone": accent},
        {"at": 0.86, "label": f"{len(touched)} FILES" if touched else "SCENE", "tone": "amber", "showLabel": True},
    ]
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-cues",
            "targetId": "dogfood-sigil",
            "kind": "timeline_cue",
            "startedAtMs": timestamp_ms,
            "durationMs": duration_ms,
            "loop": True,
            "props": {
                "phase": phase,
                "label": "LIVE HARN WINDOW",
                "tone": tone,
                "accentTone": accent,
                "offsetY": 0.19,
                "width": 0.42,
                "window": 0.12,
                "cues": cues,
                "sequence": sequence,
            },
        },
    }


def _camera_path_animation(sequence: int, timestamp_ms: int, duration_ms: int) -> dict[str, Any]:
    direction = -1 if sequence % 2 else 1
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-camera-path",
            "targetId": "dogfood-tunnel",
            "kind": "camera_path",
            "startedAtMs": timestamp_ms,
            "durationMs": max(5200, duration_ms),
            "loop": True,
            "props": {
                "phase": "lifecycle",
                "yoyo": True,
                "position": {"x": 0.50, "y": 0.50},
                "keyframes": [
                    {"at": 0, "x": round(-0.014 * direction, 3), "y": 0.010, "scale": 1.0, "rotation": -0.004},
                    {"at": 0.48, "x": round(0.026 * direction, 3), "y": -0.020, "scale": 1.038, "rotation": 0.010},
                    {"at": 1, "x": round(0.006 * direction, 3), "y": 0.006, "scale": 1.012, "rotation": -0.003},
                ],
                "seed": sequence + 71,
            },
        },
    }


def _camera_jolt_animation(sequence: int, timestamp_ms: int, phase: str) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-camera-jolt",
            "targetId": "dogfood-sigil",
            "kind": "camera_jolt",
            "startedAtMs": timestamp_ms,
            "durationMs": 1900,
            "props": {
                "phase": phase,
                "intensity": 0.58 + (sequence % 5) * 0.08,
                "zoom": 0.018,
                "roll": 0.013,
                "seed": sequence + 83,
            },
        },
    }


def _packet_burst_animation(sequence: int, timestamp_ms: int, phase: str, tone: str) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-packets",
            "targetId": "dogfood-route",
            "kind": "packet_burst",
            "startedAtMs": timestamp_ms,
            "durationMs": 2600,
            "loop": True,
            "props": {"phase": phase, "tone": tone, "sequence": sequence},
        },
    }


def _scan_animation(sequence: int, timestamp_ms: int, phase: str) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-scan",
            "targetId": "scan-grid",
            "kind": "scan",
            "startedAtMs": timestamp_ms,
            "durationMs": 3200,
            "loop": True,
            "props": {"phase": phase, "tone": "green", "direction": "down", "sequence": sequence},
        },
    }


def _extrude_animation(sequence: int, timestamp_ms: int, phase: str) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-city-extrude",
            "targetId": "dogfood-city",
            "kind": "extrude",
            "startedAtMs": timestamp_ms,
            "durationMs": 3400,
            "loop": True,
            "props": {"phase": phase, "tone": "cyan", "sequence": sequence},
        },
    }


def _breach_wave_animation(sequence: int, timestamp_ms: int, phase: str, tone: str, accent: str) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "dogfood-breach",
            "targetId": "dogfood-sigil",
            "kind": "breach_wave",
            "startedAtMs": timestamp_ms,
            "durationMs": 5200,
            "loop": True,
            "props": {
                "phase": phase,
                "tone": "magenta" if tone != "magenta" else tone,
                "accentTone": accent,
                "intensity": 0.88,
                "rings": 5,
                "shards": 32,
                "slices": 6,
                "label": "BREACH",
                "seed": sequence + 97,
            },
        },
    }


def _fallback_city_blocks(event_type: str, tone: str, accent: str) -> list[dict[str, Any]]:
    labels = ["HOOK", event_type.upper()[:8], "RENDER", "TRACE"]
    return [
        {
            "id": f"dogfood-city-fallback-{index}",
            "x": round(0.18 + index * 0.09, 3),
            "y": round(0.70 - (index % 2) * 0.10, 3),
            "w": 0.060,
            "d": 0.070,
            "h": round(0.18 + index * 0.06, 3),
            "tone": tone if index % 2 else accent,
            "label": label,
        }
        for index, label in enumerate(labels)
    ]


def _city_focus(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        if block.get("touched"):
            return _text(block.get("id"), "dogfood-city-core")
    return _text(blocks[min(1, len(blocks) - 1)].get("id"), "dogfood-city-core")


def _entry_index_for_path(entries: list[dict[str, Any]], path: str) -> int | None:
    for index, entry in enumerate(entries[:8]):
        entry_path = _text(entry.get("path") or entry.get("name"), "")
        if entry_path and (path == entry_path or path.startswith(f"{entry_path}/")):
            return index
    return None


def _phase_tone(phase: str, event_type: str, display_style: str) -> str:
    if "error" in event_type or "fail" in event_type:
        return "red" if display_style == "mainframe" else "magenta"
    if display_style == "mainframe":
        return {"before": "green", "during": "cyan", "after": "amber", "lifecycle": "green"}.get(phase, "green")
    if display_style == "neon-noir":
        return {"before": "cyan", "during": "magenta", "after": "magenta", "lifecycle": "amber"}.get(
            phase,
            "magenta",
        )
    return {"before": "green", "during": "cyan", "after": "magenta", "lifecycle": "amber"}.get(phase, "cyan")


def _accent_tone(phase: str, event_type: str, display_style: str) -> str:
    if display_style == "mainframe":
        if "error" in event_type or "fail" in event_type:
            return "amber"
        if phase == "after" or "result" in event_type:
            return "green"
        if phase == "before":
            return "amber"
        return "cyan"
    if display_style == "neon-noir":
        if "error" in event_type or "fail" in event_type:
            return "amber"
        if phase == "after" or "result" in event_type:
            return "cyan"
        if phase == "before":
            return "amber"
        return "magenta"
    if phase == "after" or "result" in event_type:
        return "white"
    if phase == "before":
        return "amber"
    return "magenta"


def _entry_tone(kind: str, tone: str, accent: str) -> str:
    if kind == "file":
        return "green"
    if kind == "symlink":
        return "amber"
    return tone if kind == "dir" else accent


def _entry_line_count(entry: dict[str, Any]) -> int:
    for key in ("lineCount", "visibleLineCount"):
        value = entry.get(key)
        if type(value) is int and value >= 0:
            return value
    return 0


def _path_label(path: str) -> str:
    if not path:
        return "ITEM"
    return path.rstrip("/").split("/")[-1][:12] or "ITEM"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clip(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


if __name__ == "__main__":
    main()
