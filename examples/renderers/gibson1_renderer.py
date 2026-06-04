"""Coherent hard-coded renderer for everyday harn-gibson dogfood runs."""

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
    summary = _clip(_text(event.get("summary"), event_type), 110)
    timeline = _dict(_dict(context.get("renderInput")).get("timeline"))
    duration_ms = _clamp(_int(timeline.get("durationMs"), 0), 2600, 7200)
    touched = _touched_files(context)
    entries = _repo_entries(context)
    semantic = _semantic_graph(context)
    tone = _phase_tone(phase, event_type, display_style)
    accent = _accent_tone(phase, event_type, display_style, touched=bool(touched), tone=tone)
    project_name = _text(project.get("name"), "project")

    mutations = [
        {
            "op": "patch",
            "targetId": "status",
            "props": {"text": f"gibson1::{event_type}", "phase": phase, "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {
                "sequence": sequence,
                "phase": phase,
                "eventType": "gibson1_renderer",
                "title": "Gibson1 renderer",
                "summary": f"{event_type}: {summary}",
            },
        },
        _upsert_terminal_wall(event, summary, entries, touched, tone, accent, sequence),
        _upsert_repo_terrain(entries, touched, semantic, event_type, tone, accent, sequence),
        _upsert_repo_city(entries, touched, semantic, event_type, tone, accent, sequence),
        _upsert_signal_scope(event_type, phase, touched, tone, accent, sequence),
        _upsert_trace_route(event_type, phase, touched, semantic, tone, accent, sequence),
        _upsert_data_rain(event_type, summary, tone, accent, sequence),
        _timeline_cue(event_type, phase, sequence, timestamp_ms, duration_ms, tone, accent),
        _route_trace_animation(event_type, phase, touched, semantic, sequence, timestamp_ms, duration_ms, tone, accent),
        _camera_path_animation(touched, sequence, timestamp_ms, duration_ms, tone, accent),
        *_camera_jolt_animations(event_type, phase, touched, sequence, timestamp_ms, tone, accent),
    ]

    metadata: dict[str, Any] = {
        "renderer": "gibson1",
        "intent": f"show {event_type} as a coherent live operations display",
        "eventType": event_type,
        "phase": phase,
        "visualizer": "gibson1",
        "mode": "usable-default",
        "touchedFileCount": len(touched),
        "repoTerrain": bool(entries),
        "semanticGraph": semantic["available"],
        "semanticNodeCount": len(semantic["nodes"]),
        "semanticEdgeCount": len(semantic["edges"]),
        "projectName": project_name,
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
    return [_dict(item) for item in _list(touched.get("files"))[:6] if _dict(item).get("path")]


def _repo_entries(context: dict[str, Any]) -> list[dict[str, Any]]:
    project = _dict(context.get("project"))
    topology = _dict(project.get("repoTopology"))
    return [_dict(item) for item in _list(topology.get("entries"))[:7]]


def _semantic_graph(context: dict[str, Any]) -> dict[str, Any]:
    project = _dict(context.get("project"))
    graph = _dict(project.get("semanticGraph"))
    nodes = [_dict(item) for item in _list(graph.get("nodes"))]
    edges = [_dict(item) for item in _list(graph.get("edges"))]
    file_nodes = {
        _text(node.get("path"), ""): node
        for node in nodes
        if _text(node.get("kind"), "") == "file" and _text(node.get("path"), "")
    }
    package_nodes = {
        _text(node.get("label") or node.get("module"), ""): node
        for node in nodes
        if _text(node.get("kind"), "") == "package" and _text(node.get("label") or node.get("module"), "")
    }
    relation_edges = [
        edge
        for edge in edges
        if _text(edge.get("relationship"), "") in {"imports", "tests"}
        and _text(edge.get("source"), "").startswith("file:")
        and _text(edge.get("target"), "").startswith("file:")
    ][:10]
    file_degrees: dict[str, int] = {}
    test_targets: dict[str, list[str]] = {}
    import_targets: dict[str, list[str]] = {}
    for edge in relation_edges:
        source_path = _text(edge.get("source"), "").removeprefix("file:")
        target_path = _text(edge.get("target"), "").removeprefix("file:")
        if not source_path or not target_path:
            continue
        file_degrees[source_path] = file_degrees.get(source_path, 0) + 1
        file_degrees[target_path] = file_degrees.get(target_path, 0) + 1
        if edge.get("relationship") == "tests":
            test_targets.setdefault(source_path, [])
            _append_unique(test_targets[source_path], target_path)
        else:
            import_targets.setdefault(source_path, [])
            _append_unique(import_targets[source_path], target_path)
    return {
        "available": bool(graph.get("available")) and bool(nodes),
        "nodes": nodes,
        "edges": relation_edges,
        "files": file_nodes,
        "packages": package_nodes,
        "degrees": file_degrees,
        "testTargets": test_targets,
        "importTargets": import_targets,
    }


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


def _upsert_terminal_wall(
    event: dict[str, Any],
    summary: str,
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    payload = _dict(event.get("payload"))
    event_type = _text(event.get("eventType"), "event")
    phase = _text(event.get("phase"), "lifecycle")
    command_lines = _event_command_lines(payload)
    output_lines = _event_output_lines(payload)
    file_lines = [_clip(_text(item.get("path"), "file"), 56) for item in touched]
    repo_lines = [_clip(_text(entry.get("path") or entry.get("name"), "entry"), 56) for entry in entries[:5]]
    panels = [
        {
            "id": "event",
            "title": f"{phase.upper()}::{event_type.upper()[:18]}",
            "lines": [
                f"SEQ {_int(event.get('sequence'), 0)}",
                _clip(summary, 70),
                f"{len(touched)} TOUCHED / {len(entries)} AREAS",
            ],
            "tone": tone,
            "accentTone": accent,
            "active": True,
        },
        {
            "id": "command",
            "title": "COMMAND",
            "lines": command_lines or [f"harn event {event_type}", "no command payload"],
            "tone": "cyan",
            "accentTone": accent,
            "streaming": event_type in {"tool_call", "tool_result"},
        },
        {
            "id": "files",
            "title": "FILES",
            "lines": file_lines or repo_lines or ["no file signal"],
            "tone": "magenta" if touched else tone,
            "accentTone": "white",
            "active": bool(touched),
        },
        {
            "id": "output",
            "title": "OUTPUT",
            "lines": output_lines or [_clip(summary, 64)],
            "tone": "amber",
            "accentTone": accent,
            "streaming": event_type in {"message_update", "tool_result", "runtime_error"},
        },
    ]
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-terminal",
            "kind": "terminal_wall",
            "region": "stage",
            "props": {
                "title": "GIBSON1 EVENT BOARD",
                "position": {"x": 0.50, "y": 0.65},
                "size": {"w": 0.78, "h": 0.18},
                "columns": 2,
                "rows": 2,
                "panels": panels,
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.82,
                "scan": True,
                "cursor": event_type in {"message_update", "tool_result"},
                "speed": 0.42,
                "seed": sequence + len(touched) * 11,
            },
        },
    }


def _upsert_repo_city(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    semantic: dict[str, Any],
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    touched_paths = [_text(item.get("path"), "") for item in touched]
    blocks: list[dict[str, Any]] = []
    child_blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(entries[:7]):
        path = _text(entry.get("path") or entry.get("name"), f"entry-{index}")
        touched_count = _touch_count(path, touched_paths)
        lines = _entry_line_count(entry)
        semantic_summary = _semantic_path_summary(path, semantic)
        x = round(0.18 + (index % 4) * 0.15 + semantic_summary["xBias"], 3)
        y = round(0.46 + (index // 4) * 0.10 + semantic_summary["yBias"], 3)
        blocks.append(
            {
                "id": f"gibson1-block-{index}",
                "label": _path_label(path),
                "path": path,
                "x": x,
                "y": y,
                "w": 0.082,
                "d": 0.088,
                "h": round(
                    0.10
                    + min(0.26, lines * 0.004 + touched_count * 0.08 + semantic_summary["degree"] * 0.018),
                    3,
                ),
                "tone": "magenta" if touched_count else _entry_tone(_text(entry.get("kind"), "file"), tone, accent),
                "active": touched_count > 0,
                "lines": lines,
                "touched": touched_count,
                **_semantic_block_props(semantic_summary),
            }
        )
        for child_index, child in enumerate(_list(entry.get("children"))[:2]):
            child_entry = _dict(child)
            child_path = _text(child_entry.get("path") or child_entry.get("name"), "")
            if not child_path:
                continue
            child_touched_count = _touch_count(child_path, touched_paths)
            child_lines = _entry_line_count(child_entry)
            child_semantic = _semantic_path_summary(child_path, semantic)
            child_blocks.append(
                {
                    "id": f"gibson1-block-{index}-child-{child_index}",
                    "parentId": f"gibson1-block-{index}",
                    "label": _path_label(child_path) if child_touched_count else "",
                    "path": child_path,
                    "x": round(x + 0.018 + child_index * 0.038 + child_semantic["xBias"] * 0.55, 3),
                    "y": round(y + 0.042 + child_index * 0.014 + child_semantic["yBias"] * 0.55, 3),
                    "w": 0.032,
                    "d": 0.038,
                    "h": round(
                        0.055
                        + min(
                            0.16,
                            child_lines * 0.0028 + child_touched_count * 0.055 + child_semantic["degree"] * 0.014,
                        ),
                        3,
                    ),
                    "tone": "magenta"
                    if child_touched_count
                    else _entry_tone(_text(child_entry.get("kind"), "file"), tone, accent),
                    "active": child_touched_count > 0,
                    "kind": _text(child_entry.get("kind"), "entry"),
                    "lines": child_lines,
                    "touched": child_touched_count,
                    **_semantic_block_props(child_semantic),
                }
            )
    blocks.extend(child_blocks)
    blocks.extend(_semantic_file_blocks(blocks, touched_paths, semantic, tone, accent))
    if not blocks:
        blocks = [
            {
                "id": f"gibson1-fallback-{index}",
                "label": label,
                "x": round(0.24 + index * 0.12, 3),
                "y": round(0.47 + (index % 2) * 0.08, 3),
                "w": 0.084,
                "d": 0.090,
                "h": round(0.12 + index * 0.045, 3),
                "tone": tone if index % 2 else accent,
            }
            for index, label in enumerate(["HOOKS", _clip(event_type.upper(), 10), "RENDER", "SCENE"])
        ]
    focus = next(
        (block["id"] for block in blocks if block.get("active") and block.get("parentId")),
        next((block["id"] for block in blocks if block.get("active")), blocks[0]["id"]),
    )
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-repo-city",
            "kind": "city_block",
            "region": "stage",
            "props": {
                "label": "REPO MAP",
                "position": {"x": 0.48, "y": 0.42},
                "size": {"w": 0.70, "h": 0.38},
                "blocks": blocks,
                "focusBlockId": focus,
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.72,
                "labels": True,
                "heightScale": 0.92,
                "layout": "semantic-repo-city" if semantic.get("available") else "repo-topology",
                "semanticEdgeCount": len(_list(semantic.get("edges"))),
                "cameraPath": {
                    "durationMs": 7400,
                    "loop": True,
                    "yoyo": True,
                    "keyframes": [
                        {"at": 0, "x": -0.004, "y": 0.012, "scale": 0.90},
                        {
                            "at": 0.48,
                            "x": round((sequence % 7 - 3) * 0.004, 3),
                            "y": 0.002,
                            "scale": 0.925,
                            "rotation": round((sequence % 5 - 2) * 0.004, 3),
                        },
                        {"at": 1, "x": 0.003, "y": 0.010, "scale": 0.905},
                    ],
                },
                "seed": sequence + len(blocks) * 17,
            },
        },
    }


def _upsert_repo_terrain(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    semantic: dict[str, Any],
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    touched_paths = [_text(item.get("path"), "") for item in touched]
    peaks: list[dict[str, Any]] = []
    for index, entry in enumerate(entries[:7]):
        path = _text(entry.get("path") or entry.get("name"), f"entry-{index}")
        children = _list(entry.get("children"))
        touched_count = _touch_count(path, touched_paths)
        line_count = _entry_line_count(entry)
        semantic_summary = _semantic_path_summary(path, semantic)
        x = round(0.12 + (index % 4) * 0.24 + semantic_summary["xBias"], 3)
        z = round(0.28 + (index // 4) * 0.32 + semantic_summary["zBias"], 3)
        peaks.append(
            {
                "id": f"gibson1-terrain-{index}",
                "label": _path_label(path),
                "path": path,
                "x": x,
                "z": z,
                "height": round(
                    0.16
                    + min(
                        0.38,
                        len(children) * 0.032
                        + line_count * 0.004
                        + touched_count * 0.11
                        + semantic_summary["degree"] * 0.022,
                    ),
                    3,
                ),
                "radius": round(
                    0.15
                    + min(0.10, len(children) * 0.014 + touched_count * 0.025 + semantic_summary["fileCount"] * 0.008),
                    3,
                ),
                "tone": "magenta" if touched_count else _entry_tone(_text(entry.get("kind"), "file"), tone, accent),
                "active": touched_count > 0,
                "lines": line_count,
                "touched": touched_count,
                **_semantic_block_props(semantic_summary),
            }
        )
        for child_index, child in enumerate(children[:2]):
            child_entry = _dict(child)
            child_path = _text(child_entry.get("path") or child_entry.get("name"), "")
            if not child_path:
                continue
            child_touched_count = _touch_count(child_path, touched_paths)
            child_lines = _entry_line_count(child_entry)
            child_semantic = _semantic_path_summary(child_path, semantic)
            peaks.append(
                {
                    "id": f"gibson1-terrain-{index}-child-{child_index}",
                    "parentId": f"gibson1-terrain-{index}",
                    "label": _path_label(child_path) if child_touched_count else "",
                    "path": child_path,
                    "x": round(min(0.94, x + 0.034 + child_index * 0.046 + child_semantic["xBias"] * 0.50), 3),
                    "z": round(min(0.88, z + 0.054 + child_index * 0.044 + child_semantic["zBias"] * 0.50), 3),
                    "height": round(
                        0.09
                        + min(
                            0.25,
                            child_lines * 0.003 + child_touched_count * 0.09 + child_semantic["degree"] * 0.018,
                        ),
                        3,
                    ),
                    "radius": round(0.11 + min(0.07, child_lines * 0.001 + child_touched_count * 0.018), 3),
                    "tone": "magenta"
                    if child_touched_count
                    else _entry_tone(_text(child_entry.get("kind"), "file"), tone, accent),
                    "active": child_touched_count > 0,
                    "kind": _text(child_entry.get("kind"), "entry"),
                    "lines": child_lines,
                    "touched": child_touched_count,
                    **_semantic_block_props(child_semantic),
                }
            )
    if not peaks:
        peaks = [
            {
                "id": f"gibson1-terrain-fallback-{index}",
                "label": label,
                "x": round(0.18 + index * 0.20, 3),
                "z": round(0.30 + (index % 2) * 0.26, 3),
                "height": round(0.18 + index * 0.06, 3),
                "radius": 0.14,
                "tone": tone if index % 2 else accent,
            }
            for index, label in enumerate(["HOOK", _clip(event_type.upper(), 8), "RENDER", "SCENE"])
        ]
    focus = next(
        (peak["id"] for peak in peaks if peak.get("active") and peak.get("parentId")),
        next((peak["id"] for peak in peaks if peak.get("active")), peaks[0]["id"]),
    )
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-repo-terrain",
            "kind": "wire_landscape",
            "region": "stage",
            "props": {
                "label": "REPO TERRAIN",
                "position": {"x": 0.50, "y": 0.45},
                "size": {"w": 0.74, "h": 0.34},
                "rows": 9 + min(5, len(entries)),
                "columns": 14 + min(10, len(entries) * 2),
                "depth": 0.72,
                "height": round(0.20 + min(0.10, len(touched) * 0.014), 3),
                "peaks": peaks,
                "focusPeakId": focus,
                "packets": 12 + min(28, len(touched) * 4 + len(entries) * 2),
                "speed": 0.32,
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.30,
                "layout": "semantic-repo-terrain" if semantic.get("available") else "repo-topology",
                "semanticEdgeCount": len(_list(semantic.get("edges"))),
                "seed": sequence + len(peaks) * 7,
            },
        },
    }


def _upsert_signal_scope(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    blips = [
        {
            "angle": round((index * 1.08 + sequence * 0.05) % 6.28, 3),
            "radius": round(0.24 + (index % 4) * 0.14, 3),
            "tone": "magenta" if touched else accent,
            "label": _path_label(_text(item.get("path"), event_type)) if item else phase.upper(),
            "intensity": 0.78,
        }
        for index, item in enumerate(touched[:4] or [{}])
    ]
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-scope",
            "kind": "signal_scope",
            "region": "side",
            "props": {
                "label": "EVENT SCOPE",
                "position": {"x": 0.82, "y": 0.26},
                "scale": 0.13,
                "mode": "radar",
                "rings": 4,
                "spokes": 8,
                "sweep": True,
                "waveform": event_type in {"message_update", "tool_result"},
                "blips": blips,
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.78,
                "speed": 0.36,
                "seed": sequence + 3,
            },
        },
    }


def _upsert_trace_route(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    semantic: dict[str, Any],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    hops = [
        {"id": "input", "label": "INPUT", "x": 0.12, "y": 0.18, "tone": "green", "active": phase == "before"},
        {"id": "harn", "label": "HARN", "x": 0.33, "y": 0.32, "tone": tone, "active": True},
        {"id": "render", "label": "RENDER", "x": 0.56, "y": 0.24, "tone": accent, "active": True},
        {"id": "scene", "label": "SCENE", "x": 0.78, "y": 0.38, "tone": "cyan", "active": True},
    ]
    for index, item in enumerate(touched[:3]):
        hops.append(
            {
                "id": f"file-{index}",
                "label": _path_label(_text(item.get("path"), f"file-{index}")),
                "x": round(0.28 + index * 0.20, 3),
                "y": round(0.62 + (index % 2) * 0.10, 3),
                "tone": "magenta",
                "active": index == 0,
            }
        )
    semantic_hops, semantic_links = _semantic_route_hops_and_links(semantic, touched, tone, accent)
    hops.extend(semantic_hops)
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-route",
            "kind": "trace_route",
            "region": "stage",
            "props": {
                "label": _clip(event_type.upper().replace("_", " "), 20),
                "position": {"x": 0.50, "y": 0.23},
                "size": {"w": 0.58, "h": 0.22},
                "hops": hops,
                "links": semantic_links,
                "focusHopId": "file-0" if touched else "scene",
                "packets": 10 + min(18, len(touched) * 4),
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.74,
                "speed": 0.44,
                "seed": sequence + len(hops) * 5,
            },
        },
    }


def _upsert_data_rain(event_type: str, summary: str, tone: str, accent: str, sequence: int) -> dict[str, Any]:
    return {
        "op": "upsert",
        "primitive": {
            "id": "gibson1-rain",
            "kind": "data_rain",
            "region": "stage",
            "props": {
                "glyphs": _clip(f"{event_type.upper()} {summary.upper()} HARN GIBSON1", 180),
                "columns": 28,
                "density": 0.28,
                "speed": 0.30,
                "direction": "down",
                "tone": tone,
                "accentTone": accent,
                "opacity": 0.20,
                "position": {"x": 0.50, "y": 0.50},
                "size": {"w": 0.92, "h": 0.82},
                "trail": 10,
                "bands": 2,
                "glitch": 0.04,
                "seed": sequence,
            },
        },
    }


def _timeline_cue(
    event_type: str,
    phase: str,
    sequence: int,
    timestamp_ms: int,
    duration_ms: int,
    tone: str,
    accent: str,
) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": "gibson1-cues",
            "targetId": "status",
            "kind": "timeline_cue",
            "startedAtMs": timestamp_ms,
            "durationMs": duration_ms,
            "ttlMs": duration_ms + 1200,
            "props": {
                "label": _clip(event_type.upper().replace("_", " "), 18),
                "phase": phase,
                "tone": tone,
                "accentTone": accent,
                "sequence": sequence,
            },
        },
    }


def _route_trace_animation(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    semantic: dict[str, Any],
    sequence: int,
    timestamp_ms: int,
    duration_ms: int,
    tone: str,
    accent: str,
) -> dict[str, Any]:
    points = [
        {"x": 0.18, "y": 0.18, "label": "INPUT"},
        {"x": 0.38, "y": 0.32, "label": "HARN"},
        {"x": 0.58, "y": 0.24, "label": "RENDER"},
        {"x": 0.76, "y": 0.38, "label": "SCENE"},
    ]
    if touched:
        points.append({"x": 0.56, "y": 0.70, "label": _path_label(_text(touched[0].get("path"), event_type))})
    semantic_points = _semantic_route_points(semantic, touched)
    if semantic_points:
        points.extend(semantic_points)
    return {
        "op": "start_animation",
        "animation": {
            "id": "gibson1-route-trace",
            "targetId": "gibson1-route",
            "kind": "route_trace",
            "startedAtMs": timestamp_ms,
            "durationMs": max(1800, duration_ms),
            "ttlMs": max(1800, duration_ms) + 1200,
            "props": {
                "points": points,
                "phase": phase,
                "tone": tone,
                "accentTone": accent,
                "packets": 4 + min(8, len(touched) * 2),
                "sequence": sequence,
            },
        },
    }


def _camera_target_ref(touched: list[dict[str, Any]]) -> dict[str, Any]:
    path = _text(_dict(touched[0]).get("path"), "") if touched else ""
    if path:
        path = path.split()[0].strip(",:;")
    return {"path": path} if path else {"index": 0}


def _camera_path_animation(
    touched: list[dict[str, Any]],
    sequence: int,
    timestamp_ms: int,
    duration_ms: int,
    tone: str,
    accent: str,
) -> dict[str, Any]:
    target_ref = _camera_target_ref(touched)
    duration = max(3600, duration_ms)
    return {
        "op": "start_animation",
        "animation": {
            "id": "gibson1-camera-drift",
            "targetId": "gibson1-repo-city",
            "kind": "camera_path",
            "startedAtMs": timestamp_ms,
            "durationMs": duration,
            "ttlMs": duration + 1800,
            "props": {
                "targetRef": target_ref,
                "phase": "focus",
                "tone": tone,
                "accentTone": accent,
                "yoyo": True,
                "keyframes": [
                    {"at": 0, "x": -0.006, "y": 0.004, "scale": 1.0, "rotation": -0.002},
                    {
                        "at": 0.52,
                        "x": round(0.010 + (sequence % 3) * 0.002, 3),
                        "y": -0.008,
                        "scale": 1.026,
                        "rotation": round(0.004 + (sequence % 4) * 0.001, 3),
                    },
                    {"at": 1, "x": 0.004, "y": 0.006, "scale": 1.006, "rotation": 0.001},
                ],
                "sequence": sequence,
            },
        },
    }


def _camera_jolt_animations(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    sequence: int,
    timestamp_ms: int,
    tone: str,
    accent: str,
) -> list[dict[str, Any]]:
    if not touched and event_type != "runtime_error":
        return []
    intensity = 0.24 if event_type != "runtime_error" else 0.42
    return [
        {
            "op": "start_animation",
            "animation": {
                "id": "gibson1-camera-focus",
                "targetId": "gibson1-repo-city",
                "kind": "camera_jolt",
                "startedAtMs": timestamp_ms,
                "durationMs": 1250,
                "ttlMs": 2300,
                "props": {
                    "targetRef": _camera_target_ref(touched),
                    "phase": phase,
                    "tone": tone,
                    "accentTone": accent,
                    "intensity": intensity,
                    "zoom": 0.010,
                    "roll": 0.005,
                    "seed": sequence + len(touched) * 13,
                },
            },
        }
    ]


def _event_command_lines(payload: dict[str, Any]) -> list[str]:
    command = _text(_dict(payload.get("input")).get("command"), "")
    if not command:
        command = _text(payload.get("command"), "")
    return [_clip(line.strip(), 76) for line in command.splitlines()[:4] if line.strip()]


def _event_output_lines(payload: dict[str, Any]) -> list[str]:
    output = _text(payload.get("output"), "")
    if not output:
        output = _text(payload.get("stderr"), "") or _text(payload.get("stdout"), "")
    return [_clip(line.strip(), 76) for line in output.splitlines()[:5] if line.strip()]


def _semantic_path_summary(path: str, semantic: dict[str, Any]) -> dict[str, Any]:
    files = _dict(semantic.get("files"))
    degrees = _dict(semantic.get("degrees"))
    matched = [
        _dict(node)
        for file_path, node in files.items()
        if isinstance(file_path, str) and (file_path == path or file_path.startswith(f"{path}/"))
    ]
    exact = _dict(files.get(path))
    package = _semantic_package(path, matched or ([exact] if exact else [])) if matched or exact else ""
    role = "test" if path.startswith("tests/") or path == "tests" else "source" if matched or exact else ""
    degree = sum(_int(degrees.get(_text(node.get("path"), "")), 0) for node in matched)
    if exact:
        degree = max(degree, _int(degrees.get(path), 0))
    package_index = _package_index(package, semantic)
    role_y = 0.018 if role == "test" else -0.014 if role == "source" else 0.0
    return {
        "package": package,
        "role": role,
        "degree": degree,
        "fileCount": len(matched) if matched else (1 if exact else 0),
        "xBias": round((package_index % 3 - 1) * 0.012, 3) if package else 0.0,
        "yBias": round(role_y + min(0.024, degree * 0.004), 3),
        "zBias": round(role_y + min(0.030, degree * 0.005), 3),
    }


def _semantic_block_props(summary: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if summary["package"]:
        props["semanticPackage"] = summary["package"]
    if summary["role"]:
        props["semanticRole"] = summary["role"]
    if summary["degree"]:
        props["semanticDegree"] = summary["degree"]
    if summary["fileCount"]:
        props["semanticFileCount"] = summary["fileCount"]
    return props


def _semantic_file_blocks(
    blocks: list[dict[str, Any]],
    touched_paths: list[str],
    semantic: dict[str, Any],
    tone: str,
    accent: str,
) -> list[dict[str, Any]]:
    existing_paths = {_text(block.get("path"), "") for block in blocks}
    files = _dict(semantic.get("files"))
    degrees = _dict(semantic.get("degrees"))
    additions: list[dict[str, Any]] = []
    semantic_paths = (
        file_path
        for file_path, node in files.items()
        if isinstance(file_path, str)
        and file_path
        and file_path not in existing_paths
        and _dict(node).get("syntaxOk") is not False
    )
    for index, path in enumerate(sorted(semantic_paths, key=lambda item: (_semantic_role_sort(item), item))[:6]):
        touched_count = _touch_count(path, touched_paths)
        node = _dict(files.get(path))
        package = _semantic_package(path, [node])
        degree = _int(degrees.get(path), 0)
        package_index = _package_index(package, semantic)
        additions.append(
            {
                "id": f"gibson1-semantic-file-{index}",
                "label": _path_label(path),
                "path": path,
                "x": round(0.22 + (package_index % 4) * 0.125 + (index % 2) * 0.034, 3),
                "y": round(0.35 + min(0.28, (package_index // 4) * 0.08 + index * 0.018), 3),
                "w": 0.030,
                "d": 0.036,
                "h": round(0.060 + min(0.18, _int(node.get("lineCount"), 0) * 0.0026 + degree * 0.018), 3),
                "tone": "magenta" if touched_count else _entry_tone(_semantic_role(path), tone, accent),
                "active": touched_count > 0,
                "kind": "semantic-file",
                "lines": _int(node.get("lineCount"), 0),
                "touched": touched_count,
                "semanticPackage": package,
                "semanticRole": _semantic_role(path),
                "semanticDegree": degree,
            }
        )
    return additions


def _semantic_route_hops_and_links(
    semantic: dict[str, Any],
    touched: list[dict[str, Any]],
    tone: str,
    accent: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    route_edges = _semantic_route_edges(semantic, touched)
    paths: list[str] = []
    for edge in route_edges:
        _append_unique(paths, _text(edge.get("source"), "").removeprefix("file:"))
        _append_unique(paths, _text(edge.get("target"), "").removeprefix("file:"))
    hops = []
    for index, path in enumerate(paths[:5]):
        role = _semantic_role(path)
        hops.append(
            {
                "id": f"semantic-{index}",
                "label": _path_label(path),
                "x": round(0.18 + (index % 3) * 0.28, 3),
                "y": round(0.80 + (index // 3) * 0.10, 3),
                "tone": "green" if role == "test" else accent if role == "source" else tone,
                "active": index == 0,
                "path": path,
                "semanticRole": role,
            }
        )
    path_to_hop = {_text(hop.get("path"), ""): _text(hop.get("id"), "") for hop in hops}
    links = []
    for edge in route_edges[:6]:
        source_path = _text(edge.get("source"), "").removeprefix("file:")
        target_path = _text(edge.get("target"), "").removeprefix("file:")
        source_id = path_to_hop.get(source_path)
        target_id = path_to_hop.get(target_path)
        if not source_id or not target_id:
            continue
        relationship = _text(edge.get("relationship"), "imports")
        links.append(
            {
                "source": source_id,
                "target": target_id,
                "label": relationship.upper(),
                "tone": "green" if relationship == "tests" else accent,
                "curved": True,
                "relationship": relationship,
            }
        )
    return hops, links


def _semantic_route_points(semantic: dict[str, Any], touched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    route_edges = _semantic_route_edges(semantic, touched)
    if not route_edges:
        return []
    points = []
    paths: list[str] = []
    for edge in route_edges[:3]:
        _append_unique(paths, _text(edge.get("source"), "").removeprefix("file:"))
        _append_unique(paths, _text(edge.get("target"), "").removeprefix("file:"))
    for index, path in enumerate(paths[:4]):
        points.append(
            {
                "x": round(0.22 + (index % 2) * 0.30, 3),
                "y": round(0.78 + (index // 2) * 0.10, 3),
                "label": _path_label(path),
                "id": f"semantic-point-{index}",
            }
        )
    return points


def _semantic_route_edges(semantic: dict[str, Any], touched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges = [_dict(edge) for edge in _list(semantic.get("edges"))]
    touched_paths = [_text(item.get("path"), "") for item in touched if _text(item.get("path"), "")]
    if touched_paths:
        focused = [
            edge
            for edge in edges
            if any(_edge_mentions_path(edge, path) or _edge_touches_parent(edge, path) for path in touched_paths)
        ]
        if focused:
            return _unique_semantic_edges(sorted(focused, key=_semantic_edge_sort_key))[:8]
    return _unique_semantic_edges(sorted(edges, key=_semantic_edge_sort_key))[:8]


def _unique_semantic_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for edge in edges:
        key = (
            _text(edge.get("source"), ""),
            _text(edge.get("target"), ""),
            _text(edge.get("relationship"), ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique


def _edge_mentions_path(edge: dict[str, Any], path: str) -> bool:
    source = _text(edge.get("source"), "").removeprefix("file:")
    target = _text(edge.get("target"), "").removeprefix("file:")
    return source == path or target == path


def _edge_touches_parent(edge: dict[str, Any], path: str) -> bool:
    source = _text(edge.get("source"), "").removeprefix("file:")
    target = _text(edge.get("target"), "").removeprefix("file:")
    return bool(path and (source.startswith(f"{path}/") or target.startswith(f"{path}/")))


def _semantic_edge_sort_key(edge: dict[str, Any]) -> tuple[int, str, str]:
    relationship = _text(edge.get("relationship"), "")
    return (0 if relationship == "tests" else 1, _text(edge.get("source"), ""), _text(edge.get("target"), ""))


def _semantic_package(path: str, nodes: list[dict[str, Any]]) -> str:
    for node in nodes:
        module = _text(node.get("module"), "")
        if module:
            return module.split(".", 1)[0]
    if path.startswith("src/"):
        parts = path.split("/")
        if len(parts) > 2:
            return parts[1]
    return path.split("/", 1)[0] if "/" in path else path.rsplit(".", 1)[0]


def _semantic_role(path: str) -> str:
    return "test" if path.startswith("tests/") or "/test_" in path else "source"


def _semantic_role_sort(path: str) -> int:
    return 0 if _semantic_role(path) == "source" else 1


def _package_index(package: str, semantic: dict[str, Any]) -> int:
    if not package:
        return 0
    packages = sorted(_dict(semantic.get("packages")))
    try:
        return packages.index(package)
    except ValueError:
        return len(packages)


def _phase_tone(phase: str, event_type: str, display_style: str) -> str:
    if "error" in event_type or "fail" in event_type:
        return "red"
    if display_style == "mainframe":
        return {"before": "green", "during": "cyan", "after": "amber", "lifecycle": "green"}.get(phase, "green")
    if display_style == "neon-noir":
        return {"before": "cyan", "during": "magenta", "after": "magenta", "lifecycle": "amber"}.get(
            phase,
            "magenta",
        )
    if display_style == "satellite-uplink":
        return {"before": "green", "during": "cyan", "after": "amber", "lifecycle": "cyan"}.get(phase, "cyan")
    return {"before": "green", "during": "cyan", "after": "magenta"}.get(phase, "amber")


def _accent_tone(phase: str, event_type: str, display_style: str, *, touched: bool, tone: str) -> str:
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
    if display_style == "satellite-uplink":
        if "error" in event_type or "fail" in event_type:
            return "amber"
        if phase == "after" or "result" in event_type:
            return "red"
        if phase == "before":
            return "amber"
        return "green"
    return "magenta" if touched and tone != "magenta" else "cyan"


def _entry_tone(kind: str, tone: str, accent: str) -> str:
    if kind in {"directory", "dir", "package"}:
        return accent
    if kind in {"test", "tests"}:
        return "green"
    if kind in {"doc", "docs", "markdown"}:
        return "amber"
    return tone


def _entry_line_count(entry: dict[str, Any]) -> int:
    for key in ("lineCount", "visibleLineCount", "lines"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    children = _list(entry.get("children"))
    return sum(_entry_line_count(_dict(child)) for child in children[:8])


def _touch_count(path: str, touched_paths: list[str]) -> int:
    return sum(1 for touched_path in touched_paths if touched_path == path or touched_path.startswith(f"{path}/"))


def _path_label(path: str) -> str:
    tail = (path.rstrip("/").rsplit("/", 1)[-1] or path).split()[0]
    return _clip(tail.upper().replace("_", "-"), 12)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) and value else fallback


def _int(value: Any, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _clip(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


if __name__ == "__main__":
    main()
