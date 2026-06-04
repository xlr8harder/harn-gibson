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
    tone = _phase_tone(phase, event_type)
    accent = "magenta" if touched else "cyan"
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
        _upsert_repo_city(entries, touched, event_type, tone, accent, sequence),
        _upsert_signal_scope(event_type, phase, touched, tone, accent, sequence),
        _upsert_trace_route(event_type, phase, touched, tone, accent, sequence),
        _upsert_data_rain(event_type, summary, tone, accent, sequence),
        _timeline_cue(event_type, phase, sequence, timestamp_ms, duration_ms, tone, accent),
        _route_trace_animation(event_type, phase, touched, sequence, timestamp_ms, duration_ms, tone, accent),
    ]

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "gibson1",
            "intent": f"show {event_type} as a coherent live operations display",
            "eventType": event_type,
            "phase": phase,
            "visualizer": "gibson1",
            "mode": "usable-default",
            "touchedFileCount": len(touched),
            "projectName": project_name,
        },
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
                "position": {"x": 0.50, "y": 0.61},
                "size": {"w": 0.78, "h": 0.26},
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
    event_type: str,
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    touched_paths = [_text(item.get("path"), "") for item in touched]
    blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(entries[:7]):
        path = _text(entry.get("path") or entry.get("name"), f"entry-{index}")
        touched_count = sum(
            1 for touched_path in touched_paths if touched_path == path or touched_path.startswith(f"{path}/")
        )
        lines = _entry_line_count(entry)
        blocks.append(
            {
                "id": f"gibson1-block-{index}",
                "label": _path_label(path),
                "path": path,
                "x": round(0.12 + (index % 4) * 0.22, 3),
                "y": round(0.18 + (index // 4) * 0.28, 3),
                "w": 0.14,
                "d": 0.15,
                "h": round(0.18 + min(0.68, lines * 0.008 + touched_count * 0.18), 3),
                "tone": "magenta" if touched_count else _entry_tone(_text(entry.get("kind"), "file"), tone, accent),
                "active": touched_count > 0,
                "lines": lines,
                "touched": touched_count,
            }
        )
    if not blocks:
        blocks = [
            {
                "id": f"gibson1-fallback-{index}",
                "label": label,
                "x": round(0.18 + index * 0.18, 3),
                "y": round(0.24 + (index % 2) * 0.24, 3),
                "w": 0.14,
                "d": 0.16,
                "h": round(0.22 + index * 0.08, 3),
                "tone": tone if index % 2 else accent,
            }
            for index, label in enumerate(["HOOKS", _clip(event_type.upper(), 10), "RENDER", "SCENE"])
        ]
    focus = next((block["id"] for block in blocks if block.get("active")), blocks[0]["id"])
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
                "camera": {"tilt": 0.48, "yaw": round((sequence % 7 - 3) * 0.018, 3), "scale": 0.96},
                "seed": sequence + len(blocks) * 17,
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
    return {
        "op": "start_animation",
        "animation": {
            "id": "gibson1-route-trace",
            "targetId": "gibson1-route",
            "kind": "route_trace",
            "startedAtMs": timestamp_ms,
            "durationMs": max(1800, duration_ms),
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


def _phase_tone(phase: str, event_type: str) -> str:
    if "error" in event_type or "fail" in event_type:
        return "red"
    return {"before": "green", "during": "cyan", "after": "magenta"}.get(phase, "amber")


def _entry_tone(kind: str, tone: str, accent: str) -> str:
    if kind in {"directory", "dir", "package"}:
        return accent
    if kind in {"test", "tests"}:
        return "green"
    if kind in {"doc", "docs", "markdown"}:
        return "amber"
    return tone


def _entry_line_count(entry: dict[str, Any]) -> int:
    for key in ("lineCount", "lines"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    children = _list(entry.get("children"))
    return sum(_entry_line_count(_dict(child)) for child in children[:8])


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


if __name__ == "__main__":
    main()
