"""World-model-driven cinematic renderer for harn-gibson.

Where ``claude_gibson_renderer.py`` encoded *event phase* (a shallow, oscillating
signal) into a fixed control room, this renderer encodes the framework-owned
**world model** (`context.project.worldModel`, schema `harn-gibson.world-model.v1`).
Every visual property is bound to a durable, observed fact about the work:

* **The city is a persistent, accreting map of the agent's activity.** Each
  building is a *file entity* the agent has touched. It is placed at a stable
  coordinate derived deterministically from its path (the filesystem as scaffold:
  a given file is always in the same spot), so the skyline *accumulates* over a
  long session instead of being rebuilt per event.
* **Height = cumulative `activityCount` (+ change magnitude).** Buildings grow as
  the agent keeps working in them.
* **Color = health/outcome, not phase.** Red where the last outcome errored or the
  file is implicated in a failing test; amber for planned-but-unconfirmed; green
  for confirmed-ok; cyan otherwise. Because health changes rarely, the palette
  stops flipping every step.
* **Focus = recency.** The most-recently-touched file is the lit "you are here".
* **Causality is spatial.** The latest command draws packets from a HARN core to
  the exact buildings it `touchedPaths`, so flow runs *through the world*.
* **Stakes = real health.** A failing `test` health checkpoint raises ICE over the
  implicated files; tests recovering to green fires a one-shot "unlock" beat.
* **Provenance is honored (R5).** Health facts are `inferred` (confidence 0.85) and
  render with an INFERRED tag and dashed treatment; observed facts render crisp.

Coherence over long sessions is preserved exactly as before: a fixed set of
primitive/animation ids is re-upserted each plan (the *world model itself* is
bounded by the framework), and the alert overlay is torn down on recovery.

Contract: reads `harn-gibson.external-renderer-request.v1` on stdin, writes
`harn-gibson.render-plan.v1` on stdout.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Fixed scene ids (re-upserted every plan -> no accumulation).
CITY = "cgw-city"
RAIN = "cgw-rain"
TUNNEL = "cgw-tunnel"
SCOPE = "cgw-scope"
ROUTE = "cgw-route"
HUD = "cgw-hud"
ICE = "cgw-ice"  # alert-only

ANIM_CAM = "cgw-cam"
ANIM_FLOW = "cgw-flow"  # causality packets
ANIM_BREACH = "cgw-breach"  # alert-only
ANIM_JOLT = "cgw-jolt"  # alert-only
ANIM_NOISE = "cgw-noise"  # alert-only
ANIM_UNLOCK = "cgw-unlock"  # recovery one-shot

# City layout box (normalized viewport). Path-hash maps each file into this grid.
CITY_X0, CITY_X1 = 0.20, 0.74
CITY_Y0, CITY_Y1 = 0.34, 0.64
GRID_COLS, GRID_ROWS = 6, 4
HARN_CORE = (0.115, 0.50)


def main() -> None:
    payload = _dict(_load_stdin())
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))
    world = _dict(project.get("worldModel"))

    event = _latest_event(requests)
    sequence = _int(event.get("sequence"), 0)
    timestamp_ms = _int(event.get("timestampMs"), 0)

    files = _files(world)
    commands = _list(_dict(world.get("entities")).get("commands"))
    health = _list(_dict(world.get("entities")).get("health"))
    changes = _list(_dict(world.get("entities")).get("changes"))
    outcomes = _list(world.get("recentOutcomes"))
    counts = _dict(world.get("counts"))

    test_health = _latest_of_category(health, "test")
    build_health = _latest_of_category(health, "build")
    stakes = _stakes(test_health, build_health, outcomes, health)
    tone = stakes["tone"]
    accent = stakes["accent"]

    latest_seq = max((_int(f.get("lastSequence"), 0) for f in files), default=0)
    change_by_path = _change_magnitude_by_path(changes)
    fail_paths = _failing_paths(test_health, build_health, commands, files)

    latest_command = commands[0] if commands else {}

    mutations: list[dict[str, Any]] = [
        {
            "op": "patch",
            "targetId": "status",
            "props": {"text": stakes["status"], "phase": event.get("phase", "lifecycle"), "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {
                "sequence": sequence,
                "phase": _text(event.get("phase"), "lifecycle"),
                "eventType": "claude_gibson_world",
                "title": stakes["label"],
                "summary": stakes["status"],
            },
        },
        _rain(stakes, sequence),
        _tunnel(stakes, len(files), sequence),
        _city(files, latest_seq, change_by_path, fail_paths, tone, accent, sequence),
        _scope(files, latest_seq, fail_paths, tone, accent, sequence),
        _route(latest_command, files, fail_paths, tone, accent, sequence),
        _hud(world, counts, test_health, build_health, latest_command, outcomes, stakes, sequence),
        _camera_drift(sequence, timestamp_ms),
        _flow(latest_command, files, fail_paths, tone, accent, sequence, timestamp_ms),
    ]

    if stakes["alert"]:
        mutations += [
            _ice(stakes, fail_paths, files, sequence),
            _breach(stakes, sequence, timestamp_ms),
            _jolt(sequence, timestamp_ms),
            _interference(stakes, sequence, timestamp_ms),
            {"op": "stop_animation", "targetId": ANIM_UNLOCK},
        ]
    else:
        mutations += [
            {"op": "remove", "targetId": ICE},
            {"op": "stop_animation", "targetId": ANIM_BREACH},
            {"op": "stop_animation", "targetId": ANIM_JOLT},
            {"op": "stop_animation", "targetId": ANIM_NOISE},
        ]
        if stakes["recovered"]:
            mutations.append(_unlock(sequence, timestamp_ms))
        else:
            mutations.append({"op": "stop_animation", "targetId": ANIM_UNLOCK})

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "claude-gibson-world",
            "intent": stakes["intent"],
            "mood": stakes["name"],
            "alert": stakes["alert"],
            "recovered": stakes["recovered"],
            "worldRevision": _int(world.get("revision"), 0),
            "fileCount": _int(counts.get("files"), len(files)),
            "testHealth": stakes["test_status"],
        },
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


# --- stakes: mood is derived from health + outcomes, not phase ----------------
def _stakes(
    test_health: dict[str, Any],
    build_health: dict[str, Any],
    outcomes: list[Any],
    health: list[Any],
) -> dict[str, Any]:
    test_status = _text(test_health.get("status"), "") if test_health else ""
    build_status = _text(build_health.get("status"), "") if build_health else ""
    last_outcome = _dict(outcomes[-1]) if outcomes else {}
    last_error = _text(last_outcome.get("status")) == "error"

    failing = test_status == "error" or build_status == "error"
    alert = failing or last_error

    # Recovery: most recent test health is ok, but an earlier run errored.
    test_runs = [h for h in (_dict(x) for x in health) if _text(h.get("category")) == "test"]
    recovered = (
        test_status == "ok"
        and not alert
        and any(_text(r.get("status")) == "error" for r in test_runs[1:])
    )

    if alert:
        what = "TESTS RED" if test_status == "error" else ("BUILD RED" if build_status == "error" else "FAULT")
        return {
            "name": "alert", "label": "ICE", "status": f"BREACH :: {what}",
            "tone": "red", "accent": "amber", "alert": True, "recovered": False,
            "intent": f"stakes high :: {what.lower()} — ice over implicated files",
            "test_status": test_status or "unknown",
        }
    if recovered:
        return {
            "name": "recovery", "label": "UNLOCK", "status": "TESTS GREEN :: LOCK RELEASED",
            "tone": "green", "accent": "cyan", "alert": False, "recovered": True,
            "intent": "stakes resolved :: tests recovered to green",
            "test_status": test_status,
        }
    running = test_status == "running" or build_status == "running"
    if running:
        return {
            "name": "verify", "label": "VERIFY", "status": "RUNNING CHECKS :: AWAIT OUTCOME",
            "tone": "amber", "accent": "cyan", "alert": False, "recovered": False,
            "intent": "verification in flight", "test_status": test_status,
        }
    if outcomes:
        return {
            "name": "work", "label": "TRACE", "status": f"WORKING :: {len(outcomes)} OUTCOMES TRACED",
            "tone": "cyan", "accent": "magenta", "alert": False, "recovered": False,
            "intent": "agent active across the world map", "test_status": test_status or "—",
        }
    return {
        "name": "idle", "label": "STANDBY", "status": "GIBSON LINK :: STANDBY",
        "tone": "amber", "accent": "cyan", "alert": False, "recovered": False,
        "intent": "awaiting agent activity", "test_status": "—",
    }


# --- the hero: a persistent, accreting world map -----------------------------
def _city(
    files: list[dict[str, Any]],
    latest_seq: int,
    change_by_path: dict[str, int],
    fail_paths: set[str],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    focus_id = ""
    focus_seq = -1
    occupied: dict[tuple[int, int], int] = {}
    for entity in files[:16]:
        path = _text(entity.get("path"), "")
        if not path:
            continue
        cell = _cell_for_path(path, occupied)
        x, y = _cell_xy(cell)
        activity = _int(entity.get("activityCount"), 1)
        mag = change_by_path.get(path, 0)
        last_seq = _int(entity.get("lastSequence"), 0)
        recent = last_seq >= latest_seq and latest_seq > 0
        stale = latest_seq - last_seq >= 4
        block_tone = _file_tone(entity, path, fail_paths, tone, recent, stale)
        height = round(0.07 + min(0.34, activity * 0.05 + mag * 0.003), 3)
        bid = f"cgw-b-{cell[0]}-{cell[1]}"
        blocks.append(
            {
                "id": bid,
                "label": _label(path) if (recent or path in fail_paths or activity >= 3) else "",
                "path": path,
                "x": x,
                "y": y,
                "w": 0.066,
                "d": 0.07,
                "h": height,
                "tone": block_tone,
                "active": recent or path in fail_paths,
            }
        )
        if last_seq > focus_seq:
            focus_seq, focus_id = last_seq, bid
    if not blocks:
        blocks = [
            {"id": "cgw-seed", "label": "GIBSON", "x": 0.47, "y": 0.5, "w": 0.08, "d": 0.085, "h": 0.16, "tone": tone}
        ]
        focus_id = "cgw-seed"
    return _upsert(
        CITY,
        "city_block",
        {
            "label": "WORLD MAP",
            "blocks": blocks,
            "focusBlockId": focus_id,
            "heightScale": 1.0,
            "labels": True,
            "tone": tone,
            "accentTone": accent,
            "opacity": 0.92,
            "cameraPath": {
                "durationMs": 9000,
                "loop": True,
                "yoyo": True,
                "keyframes": [
                    {"at": 0, "x": -0.006, "y": 0.006, "scale": 0.97},
                    {
                        "at": 0.5, "x": 0.006, "y": -0.004, "scale": 1.01,
                        "rotation": round((sequence % 5 - 2) * 0.003, 4),
                    },
                    {"at": 1, "x": 0.004, "y": 0.006, "scale": 0.98},
                ],
            },
            "seed": sequence + len(blocks) * 7,
        },
    )


def _file_tone(
    entity: dict[str, Any],
    path: str,
    fail_paths: set[str],
    base: str,
    recent: bool,
    stale: bool,
) -> str:
    if path in fail_paths:
        return "red"
    outcome = _dict(entity.get("lastOutcome"))
    status = _text(outcome.get("status"))
    if status == "error":
        return "red"
    # planned / unconfirmed: touched in a 'before' op with no outcome yet
    if not status and "bash:before" in _list(entity.get("operations")) and not recent:
        pass
    if status == "ok":
        return "green" if recent else "cyan"
    if stale:
        return "white"  # dim/ghosted: known but not recently confirmed
    return "cyan"


# --- causality: packets run from HARN core to the files a command touched -----
def _route(
    command: dict[str, Any],
    files: list[dict[str, Any]],
    fail_paths: set[str],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    pos = _path_positions(files)
    hops = [{"id": "harn", "label": "HARN", "x": HARN_CORE[0], "y": HARN_CORE[1], "tone": accent, "active": True}]
    touched = [p for p in _list(command.get("touchedPaths")) if isinstance(p, str)]
    focus = "harn"
    for i, path in enumerate(touched[:5]):
        xy = pos.get(path)
        if xy is None:
            continue
        hid = f"t{i}"
        hops.append(
            {
                "id": hid,
                "label": _label(path),
                "x": xy[0],
                "y": xy[1],
                "tone": "red" if path in fail_paths else tone,
                "active": True,
            }
        )
        focus = hid
    status = _text(command.get("status"), "running")
    return _upsert(
        ROUTE,
        "trace_route",
        {
            "label": f"{_text(command.get('toolName'), 'cmd').upper()} :: {status.upper()}",
            "position": {"x": 0.5, "y": 0.5},
            "size": {"w": 0.7, "h": 0.4},
            "hops": hops,
            "focusHopId": focus,
            "packets": 8 + min(16, len(touched) * 3),
            "tone": "red" if status == "error" else tone,
            "accentTone": accent,
            "opacity": 0.7,
            "speed": 0.5,
            "seed": sequence + len(hops) * 5,
        },
    )


def _flow(
    command: dict[str, Any],
    files: list[dict[str, Any]],
    fail_paths: set[str],
    tone: str,
    accent: str,
    sequence: int,
    ts: int,
) -> dict[str, Any]:
    pos = _path_positions(files)
    points = [{"id": "harn", "label": "HARN", "x": HARN_CORE[0], "y": HARN_CORE[1]}]
    for i, path in enumerate([p for p in _list(command.get("touchedPaths")) if isinstance(p, str)][:5]):
        xy = pos.get(path)
        if xy is not None:
            points.append({"id": f"t{i}", "label": _label(path), "x": xy[0], "y": xy[1]})
    status = _text(command.get("status"), "running")
    return _anim(
        ANIM_FLOW,
        ROUTE,
        "route_trace",
        ts,
        3200,
        {
            "points": points,
            "tone": "red" if status == "error" else tone,
            "accentTone": accent,
            "packets": 6,
            "tail": 5,
            "label": "FLOW",
            "seed": sequence,
        },
        loop=True,
    )


# --- instruments --------------------------------------------------------------
def _scope(
    files: list[dict[str, Any]],
    latest_seq: int,
    fail_paths: set[str],
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    blips = []
    for i, entity in enumerate(files[:6]):
        path = _text(entity.get("path"), "")
        last_seq = _int(entity.get("lastSequence"), 0)
        recency = 1.0 if latest_seq <= 0 else max(0.12, min(1.0, 1.0 - (latest_seq - last_seq) * 0.18))
        blips.append(
            {
                "angle": round((i * 1.05 + sequence * 0.04) % 6.28, 3),
                "radius": round(0.92 - recency * 0.6, 3),  # hot files near center
                "tone": "red" if path in fail_paths else (accent if recency > 0.8 else tone),
                "label": _label(path),
                "intensity": round(0.4 + recency * 0.5, 3),
            }
        )
    return _upsert(
        SCOPE,
        "signal_scope",
        {
            "label": "HOT FILES",
            "position": {"x": 0.87, "y": 0.22},
            "scale": 0.12,
            "mode": "radar",
            "rings": 4,
            "spokes": 8,
            "sweep": True,
            "sweepSpeed": 0.5,
            "blips": blips or [{"angle": 0.0, "radius": 0.5, "label": "IDLE", "tone": tone}],
            "tone": tone,
            "accentTone": accent,
            "opacity": 0.82,
            "speed": 0.38,
            "seed": sequence + 3,
        },
    )


def _hud(
    world: dict[str, Any],
    counts: dict[str, Any],
    test_health: dict[str, Any],
    build_health: dict[str, Any],
    command: dict[str, Any],
    outcomes: list[Any],
    stakes: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    th = _health_line(test_health, "TEST")
    bh = _health_line(build_health, "BUILD") if build_health else "BUILD: —"
    cmd_status = _text(command.get("status"), "—")
    cmd_line = f"{_text(command.get('toolName'), 'cmd')}: {_text(command.get('commandPreview'), '—')}"
    cmd_touched = ", ".join(_label(p) for p in _list(command.get("touchedPaths"))[:4] if isinstance(p, str))
    outcome_glyphs = " ".join("OK" if _text(_dict(o).get("status")) == "ok" else "XX" for o in outcomes[-10:])
    panels = [
        {
            "id": "system",
            "title": "SYSTEM STATE",
            "lines": [
                f"WORLD rev {_int(world.get('revision'), 0)}",
                f"FILES {_int(counts.get('files'), 0)}  CMD {_int(counts.get('commands'), 0)}  "
                f"CHG {_int(counts.get('changes'), 0)}",
                f"{th}   {bh}",
            ],
            "tone": stakes["tone"],
            "accentTone": stakes["accent"],
            "active": True,
        },
        {
            "id": "command",
            "title": f"ACTIVE COMMAND :: {cmd_status.upper()}",
            "lines": [_clip(cmd_line, 70), f"-> {cmd_touched}" if cmd_touched else "-> (no paths)"],
            "tone": "red" if cmd_status == "error" else "cyan",
            "accentTone": stakes["accent"],
            "streaming": cmd_status == "running",
        },
        {
            "id": "health",
            "title": "HEALTH (INFERRED)",
            "lines": _health_panel_lines(test_health, build_health),
            "tone": stakes["tone"],
            "accentTone": "white",
            "active": bool(test_health or build_health),
        },
        {
            "id": "outcomes",
            "title": "OUTCOME TRACE",
            "lines": [outcome_glyphs or "no outcomes yet", stakes["status"]],
            "tone": "amber",
            "accentTone": stakes["accent"],
            "streaming": True,
        },
    ]
    return _upsert(
        HUD,
        "terminal_wall",
        {
            "title": "WORLD MODEL :: harn-gibson.world-model.v1",
            "position": {"x": 0.5, "y": 0.84},
            "size": {"w": 0.84, "h": 0.2},
            "columns": 2,
            "rows": 2,
            "panels": panels,
            "tone": stakes["tone"],
            "accentTone": stakes["accent"],
            "opacity": 0.86,
            "scan": True,
            "cursor": _text(command.get("status")) == "running",
            "speed": 0.42,
            "seed": sequence + 11,
        },
    )


def _health_line(health: dict[str, Any], label: str) -> str:
    if not health:
        return f"{label}: —"
    status = _text(health.get("status"), "?").upper()
    mark = {"OK": "GREEN", "ERROR": "RED", "RUNNING": "…"}.get(status, status)
    return f"{label}: {mark}"


def _health_panel_lines(test_health: dict[str, Any], build_health: dict[str, Any]) -> list[str]:
    lines = []
    for health in (test_health, build_health):
        if not health:
            continue
        conf = health.get("provenance", {}).get("confidence", 0.85)
        lines.append(
            f"{_text(health.get('category'), '?').upper()} {_text(health.get('status'), '?').upper()} "
            f"(conf {conf}) {_clip(_text(health.get('commandPreview'), ''), 34)}"
        )
    return lines or ["no health checkpoints observed"]


# --- background ---------------------------------------------------------------
def _rain(stakes: dict[str, Any], sequence: int) -> dict[str, Any]:
    alert = stakes["name"] == "alert"
    return _upsert(
        RAIN,
        "data_rain",
        {
            "glyphs": _clip(f"{stakes['status']} WORLD MODEL GIBSON".upper(), 160),
            "columns": 32,
            "density": 0.26 if alert else 0.16,
            "speed": 0.32,
            "direction": "down",
            "tone": stakes["tone"],
            "accentTone": stakes["accent"],
            "opacity": 0.2 if alert else 0.12,
            "position": {"x": 0.5, "y": 0.5},
            "size": {"w": 1.0, "h": 1.0},
            "trail": 12,
            "bands": 3 if alert else 2,
            "glitch": 0.2 if alert else 0.04,
            "seed": sequence,
        },
    )


def _tunnel(stakes: dict[str, Any], file_count: int, sequence: int) -> dict[str, Any]:
    return _upsert(
        TUNNEL,
        "tunnel_grid",
        {
            "position": {"x": 0.5, "y": 0.47},
            "size": {"w": 1.02, "h": 0.96},
            "rings": 14,
            "spokes": 14,
            "lanes": 4,
            "packets": 14 + min(20, file_count * 2),
            "speed": 0.5 if stakes["name"] in ("work", "verify") else 0.3,
            "twist": 0.16,
            "depth": 0.94,
            "direction": "in",
            "tone": stakes["accent"],
            "accentTone": stakes["tone"],
            "opacity": 0.34,
            "seed": sequence + 7,
        },
    )


# --- alert / recovery overlays ------------------------------------------------
def _ice(stakes: dict[str, Any], fail_paths: set[str], files: list[dict[str, Any]], sequence: int) -> dict[str, Any]:
    # Center the ICE over the failing region if we can locate it.
    pos = _path_positions(files)
    fail_xy = [pos[p] for p in fail_paths if p in pos]
    cx = round(sum(x for x, _ in fail_xy) / len(fail_xy), 3) if fail_xy else 0.47
    cy = round(sum(y for _, y in fail_xy) / len(fail_xy), 3) if fail_xy else 0.48
    label = ", ".join(_label(p) for p in list(fail_paths)[:2]) or "FAULT"
    return _upsert(
        ICE,
        "black_ice",
        {
            "label": _clip(label.upper(), 24),
            "position": {"x": cx, "y": cy},
            "size": {"w": 0.42, "h": 0.44},
            "columns": 6,
            "rows": 5,
            "depth": 0.7,
            "breach": 0.6,
            "breachPosition": {"x": 0.5, "y": 0.46},
            "fractures": 7,
            "sentries": 4,
            "sweep": True,
            "sweepSpeed": 0.7,
            "tone": "red",
            "accentTone": "amber",
            "opacity": 0.8,
            "seed": sequence + 17,
        },
    )


def _breach(stakes: dict[str, Any], sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_BREACH, CITY, "breach_wave", ts, 1500,
        {"label": stakes["status"], "tone": "red", "accentTone": "amber", "intensity": 0.95,
         "rings": 4, "shards": 16, "position": {"x": 0.5, "y": 0.46}, "seed": sequence},
    )


def _jolt(sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_JOLT, CITY, "camera_jolt", ts, 700,
        {"intensity": 0.7, "zoom": 1.05, "roll": 0.02, "position": {"x": 0.5, "y": 0.46}, "seed": sequence},
    )


def _interference(stakes: dict[str, Any], sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_NOISE, "stage", "signal_interference", ts, 1800,
        {"label": _clip(stakes["status"], 36), "tone": "red", "accentTone": "amber",
         "intensity": 0.6, "bands": 5, "blocks": 6, "noise": 8, "speed": 0.8, "seed": sequence},
    )


def _unlock(sequence: int, ts: int) -> dict[str, Any]:
    return _anim(
        ANIM_UNLOCK, CITY, "breach_wave", ts, 1400,
        {"label": "LOCK RELEASED", "tone": "green", "accentTone": "cyan", "intensity": 0.8,
         "rings": 5, "shards": 10, "position": {"x": 0.5, "y": 0.46}, "seed": sequence},
    )


def _camera_drift(sequence: int, ts: int) -> dict[str, Any]:
    drift = (sequence % 6 - 3) * 0.003
    return _anim(
        ANIM_CAM, "stage", "camera_path", ts, 10000,
        {"loop": True, "yoyo": True, "keyframes": [
            {"at": 0, "x": -0.008, "y": 0.004, "scale": 0.995},
            {"at": 0.5, "x": round(drift, 3), "y": -0.006, "scale": 1.01, "rotation": round(drift * 0.3, 4)},
            {"at": 1, "x": 0.006, "y": 0.004, "scale": 1.0}], "seed": sequence},
        loop=True,
    )


# --- world-model extraction ---------------------------------------------------
def _files(world: dict[str, Any]) -> list[dict[str, Any]]:
    raw = [_dict(f) for f in _list(_dict(world.get("entities")).get("files"))]
    # Filter the perception layer's occasional command-fragment "paths"
    # (e.g. sed expressions parsed as 's/return'): keep plausible files/dirs.
    return [f for f in raw if _plausible_path(_text(f.get("path"), ""))]


def _plausible_path(path: str) -> bool:
    if not path or " " in path:
        return False
    head = path.split("/", 1)[0]
    if "/" in path and len(head) <= 2 and "." not in path:
        return False  # 's/return', '2/return'
    return "/" in path or "." in path


def _latest_of_category(entities: list[Any], category: str) -> dict[str, Any]:
    # Entities arrive sorted by -lastSequence, so the first match is most recent.
    for entity in entities:
        ed = _dict(entity)
        if _text(ed.get("category")) == category:
            return ed
    return {}


def _failing_paths(
    test_health: dict[str, Any],
    build_health: dict[str, Any],
    commands: list[Any],
    files: list[dict[str, Any]],
) -> set[str]:
    paths: set[str] = set()
    for health in (test_health, build_health):
        if health and _text(health.get("status")) == "error":
            paths.update(p for p in _list(health.get("touchedPaths")) if isinstance(p, str))
    for entity in files:
        if _text(_dict(entity.get("lastOutcome")).get("status")) == "error":
            p = _text(entity.get("path"))
            if p:
                paths.add(p)
    return {p for p in paths if _plausible_path(p)}


def _change_magnitude_by_path(changes: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for change in changes:
        cd = _dict(change)
        path = _text(cd.get("path"))
        if path:
            out[path] = out.get(path, 0) + _int(cd.get("magnitudeLines"), 0)
    return out


def _path_positions(files: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    positions: dict[str, tuple[float, float]] = {}
    occupied: dict[tuple[int, int], int] = {}
    for entity in files[:16]:
        path = _text(entity.get("path"), "")
        if not path:
            continue
        positions[path] = _cell_xy(_cell_for_path(path, occupied))
    return positions


# --- stable spatial scaffold: deterministic path -> grid cell -----------------
def _fnv1a(text: str) -> int:
    # Deterministic across the per-event subprocess calls (unlike hash()).
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return h


def _cell_for_path(path: str, occupied: dict[tuple[int, int], int]) -> tuple[int, int]:
    h = _fnv1a(path)
    base_col = h % GRID_COLS
    base_row = (h // GRID_COLS) % GRID_ROWS
    # linear-probe to a free cell so two files don't fully overlap, while a
    # given path still lands in the same place every render.
    for step in range(GRID_COLS * GRID_ROWS):
        col = (base_col + step) % GRID_COLS
        row = (base_row + (step // GRID_COLS)) % GRID_ROWS
        if (col, row) not in occupied:
            occupied[(col, row)] = 1
            return (col, row)
    return (base_col, base_row)


def _cell_xy(cell: tuple[int, int]) -> tuple[float, float]:
    col, row = cell
    x = CITY_X0 + (col + 0.5) / GRID_COLS * (CITY_X1 - CITY_X0)
    y = CITY_Y0 + (row + 0.5) / GRID_ROWS * (CITY_Y1 - CITY_Y0)
    return (round(x, 4), round(y, 4))


# --- mutation builders --------------------------------------------------------
def _upsert(pid: str, kind: str, props: dict[str, Any], region: str = "stage") -> dict[str, Any]:
    return {"op": "upsert", "primitive": {"id": pid, "kind": kind, "region": region, "props": props}}


def _anim(
    aid: str, target: str, kind: str, ts: int, duration: int, props: dict[str, Any], *, loop: bool = False
) -> dict[str, Any]:
    return {
        "op": "start_animation",
        "animation": {"id": aid, "targetId": target, "kind": kind, "startedAtMs": ts,
                      "durationMs": duration, "loop": loop, "props": props},
    }


# --- helpers ------------------------------------------------------------------
def _latest_event(requests: list[Any]) -> dict[str, Any]:
    if not requests:
        return {"eventType": "idle", "phase": "lifecycle", "sequence": 0, "timestampMs": 0}
    return _dict(_dict(requests[-1]).get("event"))


def _label(path: str) -> str:
    tail = (path.rstrip("/").rsplit("/", 1)[-1] or path).split()[0]
    return _clip(tail.upper().replace("_", "-"), 12)


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


def _clip(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


if __name__ == "__main__":
    main()
