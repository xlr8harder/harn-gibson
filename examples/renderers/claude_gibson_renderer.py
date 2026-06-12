"""Cinematic, coherent harn-gibson renderer.

A "fly through the Gibson" control-room visualizer in the spirit of the 1995
film *Hackers*: a neon wireframe filesystem city, a perspective data corridor,
a radar instrument, an intrusion route, and a live terminal bank, framed by a
slow drifting camera. Dramatic beats (ICE barriers, breach waves, camera jolts,
CRT interference) fire on failures and are fully torn down on recovery.

Design goals
------------
* **Coherent over long sessions.** The renderer owns a *fixed* set of primitive
  and animation ids. Every render plan re-upserts that same set (upsert replaces
  by id), so the scene never accumulates orphaned objects no matter how many
  events stream through. The only thing that ever appears/disappears is the
  intentional "alert" overlay, which is explicitly removed on the next calm
  event. Idle, busy, and failure states all resolve back to one stable frame.
* **Cinematic.** Persistent looping camera drift + route packets keep the scene
  alive between events; phase drives a consistent neon palette; failures get a
  full Hollywood breach sequence.
* **Driven by real context.** Repo topology becomes the city skyline, touched
  files light up districts / scope blips / route hops, and command + output
  text scrolls the terminal bank. No file contents are required.

Contract: reads one ``harn-gibson.external-renderer-request.v1`` JSON object on
stdin (``requests``, ``scene``, ``context``) and writes one
``harn-gibson.render-plan.v1`` object on stdout.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# --- Fixed scene ids -------------------------------------------------------
# The whole visual vocabulary of this renderer. Re-upserting these every plan
# is what keeps long sessions garbage-free: ids are reused, never minted.
CITY = "cg-city"
TUNNEL = "cg-tunnel"
RAIN = "cg-rain"
SCOPE = "cg-scope"
ROUTE = "cg-route"
TERMINAL = "cg-terminal"
ICE = "cg-ice"  # alert-only overlay; removed on calm events

ANIM_CAM = "cg-cam"  # persistent camera drift (loop)
ANIM_ROUTE = "cg-route-trace"  # persistent packet flow (loop)
ANIM_CUES = "cg-cues"  # coalesced-window timeline markers
ANIM_BREACH = "cg-breach"  # alert-only
ANIM_JOLT = "cg-jolt"  # alert-only
ANIM_NOISE = "cg-noise"  # alert-only CRT interference

# Pipeline the agent's work "routes" through; reused by route + route_trace.
PIPELINE = (
    ("input", "INPUT", 0.10, 0.50),
    ("harn", "HARN", 0.30, 0.34),
    ("tool", "TOOL", 0.52, 0.56),
    ("fs", "FS", 0.72, 0.34),
    ("scene", "GIBSON", 0.90, 0.52),
)


def main() -> None:
    payload = _dict(_load_stdin())
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))

    event = _latest_event(requests)
    event_type = _text(event.get("eventType"), "idle")
    phase = _text(event.get("phase"), "lifecycle")
    sequence = _int(event.get("sequence"), 0)
    timestamp_ms = _int(event.get("timestampMs"), 0)
    summary = _clip(_text(event.get("summary"), event_type), 120)
    epayload = _dict(event.get("payload"))

    timeline = _dict(_dict(context.get("renderInput")).get("timeline"))
    window_ms = _clamp(_int(timeline.get("durationMs"), 0), 3200, 9000)

    touched = _touched_files(context, epayload)
    entries = _repo_entries(context)
    project_name = _text(project.get("name"), "GIBSON")

    alert = _is_alert(event_type, epayload)
    mood = _mood(phase, event_type, alert)

    # --- the always-present control room (re-upserted every plan) ---------
    mutations: list[dict[str, Any]] = [
        {
            "op": "patch",
            "targetId": "status",
            "props": {
                "text": f"{mood['label']} :: {event_type.upper()}",
                "phase": phase,
                "tone": mood["tone"],
            },
        },
        {
            "op": "append_log",
            "entry": {
                "sequence": sequence,
                "phase": phase,
                "eventType": "claude_gibson",
                "title": mood["label"],
                "summary": f"{event_type}: {summary}",
            },
        },
        _rain(event_type, summary, mood, sequence),
        _tunnel(phase, mood, touched, sequence),
        _city(entries, touched, mood, sequence),
        _scope(event_type, phase, touched, mood, sequence),
        _route(event_type, phase, touched, mood, sequence),
        _terminal(event, summary, entries, touched, mood, sequence),
        # persistent motion so the scene breathes between events
        _camera_drift(mood, sequence, timestamp_ms),
        _route_packets(touched, mood, sequence, timestamp_ms, window_ms),
        _timeline_cues(requests, timeline, mood, timestamp_ms, window_ms),
    ]

    # --- the dramatic, transient alert layer ------------------------------
    # ICE barrier + breach + jolt + interference on failures; on any calm
    # event these are torn down so the scene returns to its clean baseline.
    if alert:
        mutations += [
            _ice(epayload, mood, sequence),
            _breach(mood, sequence, timestamp_ms),
            _jolt(mood, sequence, timestamp_ms),
            _interference(summary, mood, sequence, timestamp_ms),
        ]
    else:
        mutations += [
            {"op": "remove", "targetId": ICE},
            {"op": "stop_animation", "targetId": ANIM_BREACH},
            {"op": "stop_animation", "targetId": ANIM_JOLT},
            {"op": "stop_animation", "targetId": ANIM_NOISE},
        ]

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "claude-gibson",
            "intent": f"{mood['label'].lower()} :: {event_type} over the Gibson",
            "eventType": event_type,
            "phase": phase,
            "mood": mood["name"],
            "alert": alert,
            "touchedFileCount": len(touched),
            "projectName": project_name,
        },
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


# --- mood / palette --------------------------------------------------------
def _is_alert(event_type: str, payload: dict[str, Any]) -> bool:
    if "error" in event_type or "fail" in event_type:
        return True
    if bool(payload.get("isError")):
        return True
    return _text(payload.get("severity")) == "error"


def _mood(phase: str, event_type: str, alert: bool) -> dict[str, str]:
    if alert:
        return {"name": "alert", "label": "ICE BREACH", "tone": "red", "accent": "amber"}
    if phase == "before":
        return {"name": "arming", "label": "ARMING", "tone": "green", "accent": "cyan"}
    if phase == "during":
        return {"name": "stream", "label": "UPLINK", "tone": "cyan", "accent": "magenta"}
    if phase == "after":
        return {"name": "ack", "label": "TRACE LOCK", "tone": "magenta", "accent": "cyan"}
    return {"name": "idle", "label": "STANDBY", "tone": "amber", "accent": "cyan"}


# --- background layers -----------------------------------------------------
def _rain(event_type: str, summary: str, mood: dict[str, str], sequence: int) -> dict[str, Any]:
    name = mood["name"]
    density = {"stream": 0.34, "alert": 0.30}.get(name, 0.18)
    opacity = {"stream": 0.26, "alert": 0.24}.get(name, 0.13)
    glyphs = _clip(f"{event_type.upper()} {summary.upper()} GIBSON OVERRIDE ACCESS", 200)
    return _upsert(
        RAIN,
        "data_rain",
        {
            "glyphs": glyphs,
            "columns": 34,
            "density": density,
            "speed": 0.42 if name == "stream" else 0.28,
            "direction": "down",
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "opacity": opacity,
            "position": {"x": 0.5, "y": 0.5},
            "size": {"w": 1.0, "h": 1.0},
            "trail": 12,
            "bands": 3 if name == "alert" else 2,
            "glitch": 0.22 if name == "alert" else 0.05,
            "seed": sequence,
        },
    )


def _tunnel(phase: str, mood: dict[str, str], touched: list[dict[str, Any]], sequence: int) -> dict[str, Any]:
    speed = {"before": 0.34, "during": 0.62, "after": 0.44}.get(phase, 0.26)
    return _upsert(
        TUNNEL,
        "tunnel_grid",
        {
            "position": {"x": 0.5, "y": 0.47},
            "size": {"w": 1.02, "h": 0.96},
            "rings": 14,
            "spokes": 14,
            "lanes": 4,
            "packets": 18 + min(20, len(touched) * 4),
            "speed": speed,
            "twist": 0.18,
            "depth": 0.94,
            "direction": "in",
            "tone": mood["accent"],
            "accentTone": mood["tone"],
            "opacity": 0.36,
            "label": "",
            "seed": sequence + 7,
        },
    )


# --- hero: the filesystem city ---------------------------------------------
def _city(
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    mood: dict[str, str],
    sequence: int,
) -> dict[str, Any]:
    touched_paths = [_text(item.get("path"), "") for item in touched]
    blocks: list[dict[str, Any]] = []
    # Lay districts in a centered band; leave top for route, bottom for terminal.
    cols = 4
    for index, entry in enumerate(entries[:8]):
        path = _text(entry.get("path") or entry.get("name"), f"area-{index}")
        hits = _touch_count(path, touched_paths)
        lines = _entry_lines(entry)
        col = index % cols
        row = index // cols
        x = round(0.30 + col * 0.115, 3)
        y = round(0.44 + row * 0.090, 3)
        blocks.append(
            {
                "id": f"cg-block-{index}",
                "label": _label(path),
                "path": path,
                "x": x,
                "y": y,
                "w": 0.072,
                "d": 0.078,
                "h": round(0.10 + min(0.30, lines * 0.0042 + hits * 0.09), 3),
                "tone": "magenta" if hits else _entry_tone(_text(entry.get("kind"), "file"), mood),
                "active": hits > 0,
                "lines": lines,
                "touched": hits,
            }
        )
        # one tier of children for depth-2 districts
        for ci, child in enumerate(_list(entry.get("children"))[:2]):
            cd = _dict(child)
            cpath = _text(cd.get("path") or cd.get("name"), "")
            if not cpath:
                continue
            chits = _touch_count(cpath, touched_paths)
            blocks.append(
                {
                    "id": f"cg-block-{index}-{ci}",
                    "label": _label(cpath) if chits else "",
                    "path": cpath,
                    "x": round(x + 0.020 + ci * 0.030, 3),
                    "y": round(y + 0.040 + ci * 0.012, 3),
                    "w": 0.030,
                    "d": 0.034,
                    "h": round(0.05 + min(0.18, _entry_lines(cd) * 0.0030 + chits * 0.06), 3),
                    "tone": "magenta" if chits else _entry_tone(_text(cd.get("kind"), "file"), mood),
                    "active": chits > 0,
                    "touched": chits,
                }
            )
    if not blocks:
        blocks = [
            {
                "id": f"cg-seed-{i}",
                "label": label,
                "x": round(0.33 + i * 0.11, 3),
                "y": round(0.47 + (i % 2) * 0.07, 3),
                "w": 0.072,
                "d": 0.078,
                "h": round(0.12 + i * 0.05, 3),
                "tone": mood["tone"] if i % 2 else mood["accent"],
            }
            for i, label in enumerate(["CORE", "AUTH", "NET", "VAULT"])
        ]
    focus = next(
        (b["id"] for b in blocks if b.get("active")),
        blocks[len(blocks) // 2]["id"],
    )
    return _upsert(
        CITY,
        "city_block",
        {
            "label": "GIBSON",
            "blocks": blocks,
            "focusBlockId": focus,
            "heightScale": 1.0,
            "labels": True,
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "opacity": 0.9,
            "cameraPath": {
                "durationMs": 8200,
                "loop": True,
                "yoyo": True,
                "keyframes": [
                    {"at": 0, "x": -0.010, "y": 0.012, "scale": 0.95},
                    {
                        "at": 0.5,
                        "x": round((sequence % 7 - 3) * 0.005, 3),
                        "y": -0.006,
                        "scale": 1.02,
                        "rotation": round((sequence % 5 - 2) * 0.004, 3),
                    },
                    {"at": 1, "x": 0.008, "y": 0.010, "scale": 0.97},
                ],
            },
            "seed": sequence + len(blocks) * 13,
        },
    )


# --- instruments -----------------------------------------------------------
def _scope(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    mood: dict[str, str],
    sequence: int,
) -> dict[str, Any]:
    blips = [
        {
            "angle": round((i * 1.21 + sequence * 0.06) % 6.28, 3),
            "radius": round(0.26 + (i % 4) * 0.16, 3),
            "tone": "magenta" if touched else mood["accent"],
            "label": _label(_text(item.get("path"), event_type)) if item else phase.upper(),
            "intensity": 0.82,
        }
        for i, item in enumerate(touched[:5] or [{}])
    ]
    return _upsert(
        SCOPE,
        "signal_scope",
        {
            "label": "SCOPE",
            "position": {"x": 0.87, "y": 0.21},
            "scale": 0.12,
            "mode": "radar",
            "rings": 4,
            "spokes": 8,
            "sweep": True,
            "sweepSpeed": 0.5,
            "waveform": event_type in {"message_update", "tool_result", "runtime_error"},
            "blips": blips,
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "opacity": 0.82,
            "speed": 0.4,
            "seed": sequence + 3,
        },
    )


def _route(
    event_type: str,
    phase: str,
    touched: list[dict[str, Any]],
    mood: dict[str, str],
    sequence: int,
) -> dict[str, Any]:
    active_id = {"before": "tool", "during": "harn", "after": "scene"}.get(phase, "harn")
    hops = [
        {
            "id": hid,
            "label": label,
            "x": x,
            "y": y,
            "tone": mood["accent"] if hid == active_id else "cyan",
            "active": hid == active_id or hid in {"harn", "scene"},
        }
        for hid, label, x, y in PIPELINE
    ]
    if touched:
        hops.append(
            {
                "id": "touch",
                "label": _label(_text(touched[0].get("path"), "file")),
                "x": 0.72,
                "y": 0.74,
                "tone": "magenta",
                "active": True,
            }
        )
    return _upsert(
        ROUTE,
        "trace_route",
        {
            "label": _clip(event_type.upper().replace("_", " "), 22),
            "position": {"x": 0.5, "y": 0.16},
            "size": {"w": 0.66, "h": 0.20},
            "hops": hops,
            "focusHopId": "touch" if touched else active_id,
            "packets": 10 + min(18, len(touched) * 4),
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "opacity": 0.78,
            "speed": 0.46,
            "seed": sequence + len(hops) * 5,
        },
    )


def _terminal(
    event: dict[str, Any],
    summary: str,
    entries: list[dict[str, Any]],
    touched: list[dict[str, Any]],
    mood: dict[str, str],
    sequence: int,
) -> dict[str, Any]:
    payload = _dict(event.get("payload"))
    event_type = _text(event.get("eventType"), "event")
    phase = _text(event.get("phase"), "lifecycle")
    command = _command_lines(payload)
    output = _output_lines(payload)
    files = [_clip(_text(i.get("path"), "file"), 54) for i in touched[:5]]
    areas = [_clip(_text(e.get("path") or e.get("name"), "area"), 54) for e in entries[:5]]
    panels = [
        {
            "id": "event",
            "title": f"{phase.upper()}::{event_type.upper()[:16]}",
            "lines": [
                f"SEQ {_int(event.get('sequence'), 0):04d}",
                _clip(summary, 64),
                f"{len(touched)} TOUCHED / {len(entries)} AREAS",
            ],
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "active": True,
        },
        {
            "id": "command",
            "title": "COMMAND",
            "lines": command or [f"> harn {event_type}", "> no command on wire"],
            "tone": "cyan",
            "accentTone": mood["accent"],
            "streaming": event_type in {"tool_call", "tool_result"},
        },
        {
            "id": "files",
            "title": "FILES",
            "lines": files or areas or ["no file signal"],
            "tone": "magenta" if touched else mood["tone"],
            "accentTone": "white",
            "active": bool(touched),
        },
        {
            "id": "output",
            "title": "OUTPUT",
            "lines": output or [_clip(summary, 60)],
            "tone": "red" if mood["name"] == "alert" else "amber",
            "accentTone": mood["accent"],
            "streaming": event_type in {"message_update", "tool_result", "runtime_error"},
        },
    ]
    return _upsert(
        TERMINAL,
        "terminal_wall",
        {
            "title": f"{_text(event.get('source'), 'HARN').upper()} :: GIBSON LINK",
            "position": {"x": 0.5, "y": 0.83},
            "size": {"w": 0.82, "h": 0.20},
            "columns": 2,
            "rows": 2,
            "panels": panels,
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "opacity": 0.85,
            "scan": True,
            "cursor": event_type in {"message_update", "tool_result"},
            "speed": 0.44,
            "seed": sequence + len(touched) * 11,
        },
    )


# --- alert overlay ---------------------------------------------------------
def _ice(payload: dict[str, Any], mood: dict[str, str], sequence: int) -> dict[str, Any]:
    return _upsert(
        ICE,
        "black_ice",
        {
            "label": _clip(_text(payload.get("message"), "ICE WALL"), 24).upper(),
            "position": {"x": 0.5, "y": 0.46},
            "size": {"w": 0.5, "h": 0.5},
            "columns": 6,
            "rows": 5,
            "depth": 0.7,
            "breach": 0.62,
            "breachPosition": {"x": 0.5, "y": 0.46},
            "fractures": 7,
            "sentries": 4,
            "sweep": True,
            "sweepSpeed": 0.7,
            "tone": "red",
            "accentTone": "amber",
            "opacity": 0.82,
            "seed": sequence + 17,
        },
    )


def _breach(mood: dict[str, str], sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_BREACH,
        CITY,
        "breach_wave",
        ts,
        1500,
        {
            "label": "ICE BREACH",
            "tone": "red",
            "accentTone": "amber",
            "intensity": 0.95,
            "rings": 4,
            "shards": 16,
            "position": {"x": 0.5, "y": 0.46},
            "seed": sequence,
        },
    )


def _jolt(mood: dict[str, str], sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_JOLT,
        CITY,
        "camera_jolt",
        ts,
        720,
        {"intensity": 0.7, "zoom": 1.05, "roll": 0.02, "position": {"x": 0.5, "y": 0.46}, "seed": sequence},
    )


def _interference(summary: str, mood: dict[str, str], sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_NOISE,
        "stage",
        "signal_interference",
        ts,
        1800,
        {
            "label": _clip(summary.upper(), 36),
            "tone": "red",
            "accentTone": "amber",
            "intensity": 0.6,
            "bands": 5,
            "blocks": 6,
            "noise": 8,
            "speed": 0.8,
            "seed": sequence,
        },
    )


# --- persistent motion -----------------------------------------------------
def _camera_drift(mood: dict[str, str], sequence: int, ts: int) -> dict[str, Any]:
    drift = (sequence % 6 - 3) * 0.004
    return _anim(
        ANIM_CAM,
        "stage",
        "camera_path",
        ts,
        9000,
        {
            "loop": True,
            "yoyo": True,
            "keyframes": [
                {"at": 0, "x": -0.012, "y": 0.006, "scale": 0.99, "rotation": 0.0},
                {"at": 0.5, "x": round(drift, 3), "y": -0.008, "scale": 1.015, "rotation": round(drift * 0.4, 4)},
                {"at": 1, "x": 0.010, "y": 0.004, "scale": 0.995, "rotation": 0.0},
            ],
            "seed": sequence,
        },
        loop=True,
    )


def _route_packets(
    touched: list[dict[str, Any]],
    mood: dict[str, str],
    sequence: int,
    ts: int,
    window_ms: int,
) -> dict[str, Any]:
    points = [{"id": hid, "label": label, "x": x, "y": y} for hid, label, x, y in PIPELINE]
    if touched:
        points.append({"id": "touch", "label": _label(_text(touched[0].get("path"), "file")), "x": 0.72, "y": 0.74})
    return _anim(
        ANIM_ROUTE,
        ROUTE,
        "route_trace",
        ts,
        max(2600, window_ms),
        {
            "points": points,
            "tone": mood["tone"],
            "accentTone": mood["accent"],
            "packets": 5 + min(8, len(touched) * 2),
            "tail": 5,
            "label": "ROUTE",
            "seed": sequence,
        },
        loop=True,
    )


def _timeline_cues(
    requests: list[Any],
    timeline: dict[str, Any],
    mood: dict[str, str],
    ts: int,
    window_ms: int,
) -> dict[str, Any]:
    duration = max(1, window_ms)
    cues: list[dict[str, Any]] = []
    for req in requests[-8:]:
        ev = _dict(_dict(req).get("event"))
        off = _int(_dict(req).get("timelineOffsetMs"), _int(ev.get("timelineOffsetMs"), 0))
        at = _clampf(off / duration, 0.0, 1.0)
        et = _text(ev.get("eventType"), "evt")
        cue_alert = _is_alert(et, _dict(ev.get("payload")))
        cues.append(
            {
                "at": round(at, 3),
                "label": _clip(et.upper().replace("_", " "), 16),
                "tone": "red" if cue_alert else mood["tone"],
            }
        )
    if not cues:
        cues = [{"at": 0.5, "label": mood["label"], "tone": mood["tone"]}]
    return _anim(
        ANIM_CUES,
        "status",
        "timeline_cue",
        ts,
        max(2600, window_ms),
        {
            "label": "RENDER WINDOW",
            "cues": cues[:32],
            "tone": mood["tone"],
            "accentTone": mood["accent"],
        },
    )


# --- context extraction ----------------------------------------------------
def _latest_event(requests: list[Any]) -> dict[str, Any]:
    if not requests:
        return {"eventType": "idle", "phase": "lifecycle", "sequence": 0, "timestampMs": 0}
    return _dict(_dict(requests[-1]).get("event"))


def _touched_files(context: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    project = _dict(context.get("project"))
    touched = _dict(project.get("touchedFiles"))
    files = [_dict(i) for i in _list(touched.get("files"))[:8] if _dict(i).get("path")]
    if files:
        return files
    # fall back to file paths directly on the event payload
    raw = payload.get("filePath")
    paths = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    return [{"path": p} for p in paths[:8] if isinstance(p, str) and p]


def _repo_entries(context: dict[str, Any]) -> list[dict[str, Any]]:
    project = _dict(context.get("project"))
    topology = _dict(project.get("repoTopology"))
    return [_dict(i) for i in _list(topology.get("entries"))[:8]]


def _command_lines(payload: dict[str, Any]) -> list[str]:
    command = _text(_dict(payload.get("input")).get("command"), "") or _text(payload.get("command"), "")
    return [f"> {_clip(line.strip(), 72)}" for line in command.splitlines()[:4] if line.strip()]


def _output_lines(payload: dict[str, Any]) -> list[str]:
    text = _first_content_text(payload)
    if not text:
        text = _text(payload.get("output"), "") or _text(payload.get("stderr"), "") or _text(payload.get("details"), "")
    return [_clip(line.strip(), 72) for line in text.replace("\\n", "\n").splitlines()[:5] if line.strip()]


def _first_content_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if _dict(item).get("type") == "text":
                return _text(_dict(item).get("text"), "")
    return _text(payload.get("message"), "")


def _entry_lines(entry: dict[str, Any]) -> int:
    for key in ("lineCount", "visibleLineCount", "lines"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return sum(_entry_lines(_dict(c)) for c in _list(entry.get("children"))[:8])


def _entry_tone(kind: str, mood: dict[str, str]) -> str:
    if kind in {"directory", "dir", "package"}:
        return mood["accent"]
    if kind in {"test", "tests"}:
        return "green"
    if kind in {"doc", "docs", "markdown"}:
        return "amber"
    return mood["tone"]


def _touch_count(path: str, touched_paths: list[str]) -> int:
    return sum(1 for t in touched_paths if t == path or t.startswith(f"{path}/"))


def _label(path: str) -> str:
    tail = (path.rstrip("/").rsplit("/", 1)[-1] or path).split()[0]
    return _clip(tail.upper().replace("_", "-"), 12)


# --- mutation builders -----------------------------------------------------
def _upsert(pid: str, kind: str, props: dict[str, Any], region: str = "stage") -> dict[str, Any]:
    return {"op": "upsert", "primitive": {"id": pid, "kind": kind, "region": region, "props": props}}


def _anim(
    aid: str,
    target: str,
    kind: str,
    ts: int,
    duration: int,
    props: dict[str, Any],
    *,
    loop: bool = False,
) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {
            "id": aid,
            "targetId": target,
            "kind": kind,
            "startedAtMs": ts,
            "durationMs": duration,
            "loop": loop,
            "props": props,
        },
    }


# --- defensive coercion ----------------------------------------------------
def _load_stdin() -> Any:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) and value else fallback


def _int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _clampf(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clip(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


if __name__ == "__main__":
    main()
