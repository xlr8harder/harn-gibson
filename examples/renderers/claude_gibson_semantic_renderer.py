"""Semantic-graph + attention driven renderer for harn-gibson (1.1 branch).

Builds on `claude_gibson_world_renderer.py`, now consuming the three contracts
that landed on `work/1.1-spatial-bindings`:

* **`context.project.semanticGraph`** (R4) — the city is laid out by *real repo
  structure*: districts are Python packages, towers are files, and import /
  test-to-code **edges are drawn between the towers**. Layout is no longer an FNV
  hash; it means something.
* **`context.project.agentAttention`** (R2) — the camera frames the file the agent
  is *focused on* (`focus.primaryPath`) via an object-addressable `targetRef`, and
  the HUD shows the inferred `action` / `objective` as a live objective line.
* **Animation `ttlMs` + object camera `targetRef`** (R9) — transient beats carry a
  TTL and self-expire (the framework prunes them), so the alert/unlock overlays no
  longer need manual `stop_animation` bookkeeping.

World-model facts (R1/R3/R5) still drive height (`activityCount` + change
magnitude), color (health/outcome), and stakes (test/build health), and the city
still persists/accretes. Declarative `props.worldBindings` (R8 metadata) record
which fact each visual property follows.

Deliberately does NOT use the in-progress `spatial_map` primitive — that R8
primitive isn't on this checkpoint yet. This renderer expresses the semantic
layout through the existing `city_block` + `trace_route` vocabulary.

Contract: reads `harn-gibson.external-renderer-request.v1` on stdin, writes
`harn-gibson.render-plan.v1` on stdout.
"""

from __future__ import annotations

import json
import sys
from typing import Any

CITY = "cgs-city"
RAIN = "cgs-rain"
TUNNEL = "cgs-tunnel"
DEPS = "cgs-deps"  # dependency-edge route through the towers
SCOPE = "cgs-scope"
HUD = "cgs-hud"
ICE = "cgs-ice"

ANIM_CAM = "cgs-cam"  # attention-targeted camera (persistent, retargeted each plan)
ANIM_BREACH = "cgs-breach"
ANIM_JOLT = "cgs-jolt"
ANIM_NOISE = "cgs-noise"
ANIM_UNLOCK = "cgs-unlock"

CITY_X0, CITY_X1 = 0.16, 0.80
CITY_Y0, CITY_Y1 = 0.30, 0.60
GRID_COLS = 5
GRID_ROWS = 4
MAX_FILES = GRID_COLS * GRID_ROWS


def main() -> None:
    payload = _dict(_load_stdin())
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))
    world = _dict(project.get("worldModel"))
    graph = _dict(project.get("semanticGraph"))
    attention = _dict(project.get("agentAttention"))

    event = _latest_event(requests)
    sequence = _int(event.get("sequence"), 0)
    ts = _int(event.get("timestampMs"), 0)

    wm_files = {
        _text(f.get("path")): _dict(f)
        for f in _list(_dict(world.get("entities")).get("files"))
        if _text(_dict(f).get("path"))
    }
    commands = _list(_dict(world.get("entities")).get("commands"))
    health = _list(_dict(world.get("entities")).get("health"))
    changes = _list(_dict(world.get("entities")).get("changes"))
    outcomes = _list(world.get("recentOutcomes"))
    counts = _dict(world.get("counts"))

    test_health = _latest_cat(health, "test")
    build_health = _latest_cat(health, "build")
    stakes = _stakes(test_health, build_health, outcomes, health)
    tone, accent = stakes["tone"], stakes["accent"]

    fail_paths = _failing_paths(test_health, build_health, wm_files)
    change_mag = _change_mag(changes)

    # Semantic layout: districts = packages, towers = files. World facts overlay.
    layout = _semantic_layout(graph, wm_files)
    focus_path = _text(_dict(attention.get("focus")).get("primaryPath"))
    action = _dict(attention.get("action"))
    latest_seq = max((_int(f.get("lastSequence"), 0) for f in wm_files.values()), default=0)

    mutations: list[dict[str, Any]] = [
        {
            "op": "patch", "targetId": "status",
            "props": {"text": stakes["status"], "phase": _text(event.get("phase"), "lifecycle"), "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {"sequence": sequence, "phase": _text(event.get("phase"), "lifecycle"),
                      "eventType": "claude_gibson_semantic", "title": stakes["label"], "summary": stakes["status"]},
        },
        _rain(stakes, sequence),
        _tunnel(stakes, len(layout), sequence),
        _city(layout, wm_files, change_mag, fail_paths, focus_path, latest_seq, tone, accent, sequence),
        _scope(layout, wm_files, fail_paths, latest_seq, tone, accent, sequence),
        _hud(world, counts, test_health, build_health, attention, commands, outcomes, stakes, sequence),
        _attention_camera(focus_path, layout, action, stakes, sequence, ts),
    ]

    if stakes["alert"]:
        mutations += [
            _ice(fail_paths, layout, stakes, sequence),
            _breach(stakes, sequence, ts),
            _jolt(focus_path, fail_paths, layout, sequence, ts),
            _interference(stakes, sequence, ts),
        ]
    else:
        mutations.append({"op": "remove", "targetId": ICE})
        if stakes["recovered"]:
            mutations.append(_unlock(focus_path, layout, sequence, ts))

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "claude-gibson-semantic",
            "intent": stakes["intent"],
            "mood": stakes["name"],
            "alert": stakes["alert"],
            "semanticAvailable": bool(graph.get("available")),
            "packages": sorted({d["package"] for d in layout.values()}),
            "attentionAction": _text(action.get("kind")),
            "focusPath": focus_path,
            "worldRevision": _int(world.get("revision"), 0),
        },
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


# --- semantic layout: districts = packages, towers = files --------------------
def _semantic_layout(graph: dict[str, Any], wm_files: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return {path: {x, y, package, lineCount}} clustered by package."""
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _list(graph.get("files")):
        nd = _dict(node)
        path = _text(nd.get("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        files.append({"path": path, "package": _package_of(path, _text(nd.get("module"))),
                      "lineCount": _int(nd.get("lineCount"), 0)})
    # include world-model files the graph didn't surface (non-python touches)
    for path in wm_files:
        if path not in seen and _plausible(path):
            seen.add(path)
            files.append({"path": path, "package": _package_of(path, ""), "lineCount": 0})
    if not files:
        return {}

    # Assign files to distinct grid cells in path-sorted order. Sorting by path
    # naturally groups a directory's files together (src/* adjacent, tests/*
    # adjacent), so districts emerge without hashing -- and every file gets its
    # own cell, so nothing overlaps. The set only changes when a new file is
    # discovered, which is the acceptable "city still forming" reflow (and would
    # ideally be an animated transition once a layout-owning primitive exists).
    layout: dict[str, dict[str, Any]] = {}
    for i, f in enumerate(sorted(files, key=lambda f: f["path"])[:MAX_FILES]):
        col, row = i % GRID_COLS, i // GRID_COLS
        x = CITY_X0 + (col + 0.5) / GRID_COLS * (CITY_X1 - CITY_X0)
        y = CITY_Y0 + (row + 0.5) / GRID_ROWS * (CITY_Y1 - CITY_Y0)
        layout[f["path"]] = {"x": round(x, 4), "y": round(y, 4),
                             "package": f["package"], "lineCount": f["lineCount"]}
    return layout


def _package_of(path: str, module: str) -> str:
    if module and "." in module:
        return module.split(".", 1)[0]
    if module:
        return module
    head = path.strip("/").split("/", 1)[0]
    return head or "root"


def _city(
    layout: dict[str, dict[str, Any]],
    wm_files: dict[str, dict[str, Any]],
    change_mag: dict[str, int],
    fail_paths: set[str],
    focus_path: str,
    latest_seq: int,
    tone: str,
    accent: str,
    sequence: int,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    focus_id = ""
    for path, cell in layout.items():
        wm = wm_files.get(path, {})
        activity = _int(wm.get("activityCount"), 0)
        last_seq = _int(wm.get("lastSequence"), 0)
        recent = last_seq >= latest_seq and latest_seq > 0
        stale = activity > 0 and latest_seq - last_seq >= 4
        dormant = activity == 0  # in the repo graph but untouched this session
        base = 0.04 + min(0.05, cell["lineCount"] * 0.0009)
        height = round(base + min(0.32, activity * 0.05 + change_mag.get(path, 0) * 0.003), 3)
        bid = f"cgs-b-{_fnv(path):08x}"  # stable per file identity, not positional
        blocks.append({
            "id": bid, "path": path,
            "label": _label(path) if (recent or path in fail_paths or activity >= 2) else "",
            "x": cell["x"], "y": cell["y"], "w": 0.06, "d": 0.066, "h": height,
            "tone": _file_tone(wm, path, fail_paths, recent, stale, dormant, tone),
            "active": recent or path in fail_paths,
            "package": cell["package"],
        })
        if path == focus_path:
            focus_id = bid
    if not focus_id and blocks:
        focus_id = blocks[0]["id"]
    if not blocks:
        blocks = [{"id": "cgs-seed", "label": "GIBSON", "x": 0.47, "y": 0.46,
                   "w": 0.08, "d": 0.085, "h": 0.16, "tone": tone}]
        focus_id = "cgs-seed"
    return _upsert(CITY, "city_block", {
        "label": "SEMANTIC CITY",
        "blocks": blocks,
        "focusBlockId": focus_id,
        "heightScale": 1.0, "labels": True,
        "tone": tone, "accentTone": accent, "opacity": 0.92,
        "cameraPath": {  # gentle ambient drift; the sharp framing is the attention camera
            "durationMs": 11000, "loop": True, "yoyo": True,
            "keyframes": [
                {"at": 0, "x": -0.005, "y": 0.005, "scale": 0.985},
                {"at": 0.5, "x": 0.005, "y": -0.004, "scale": 1.0},
                {"at": 1, "x": 0.004, "y": 0.005, "scale": 0.99},
            ],
        },
        "worldBindings": [
            _binding("entities.files[].activityCount", "blocks[].h", "scales", "tower height follows file activity"),
            _binding("entities.files[].lastOutcome.status", "blocks[].tone", "encodes",
                     "tower color follows file health"),
        ],
        "seed": sequence + len(blocks) * 7,
    })


def _file_tone(
    wm: dict[str, Any], path: str, fail_paths: set[str], recent: bool, stale: bool, dormant: bool, base: str
) -> str:
    if path in fail_paths:
        return "red"
    status = _text(_dict(wm.get("lastOutcome")).get("status"))
    if status == "error":
        return "red"
    if dormant:
        return "white"  # known from the graph but untouched -> ghosted district
    if status == "ok":
        return "green" if recent else "cyan"
    if stale:
        return "white"
    return "cyan"



# --- attention-driven object camera (R2 + R9) ---------------------------------
def _attention_camera(
    focus_path: str,
    layout: dict[str, dict[str, Any]],
    action: dict[str, Any],
    stakes: dict[str, Any],
    sequence: int,
    ts: int,
) -> dict[str, Any]:
    # Frame the file the agent appears focused on; fall back to scene center.
    target_ref = {"path": focus_path} if focus_path in layout else {"index": 0}
    return {
        "op": "start_animation",
        "animation": {
            "id": ANIM_CAM, "targetId": CITY, "kind": "camera_path",
            "startedAtMs": ts, "durationMs": 4200, "ttlMs": 6000, "loop": False,
            "props": {
                "targetRef": target_ref,
                "tone": stakes["tone"], "accentTone": stakes["accent"], "yoyo": True,
                "keyframes": [
                    {"at": 0, "x": 0.0, "y": 0.0, "scale": 1.0},
                    {"at": 0.5, "x": 0.0, "y": -0.004, "scale": 1.06},
                    {"at": 1, "x": 0.0, "y": 0.0, "scale": 1.01},
                ],
                "attention": _text(action.get("kind")),
                "seed": sequence,
            },
        },
    }


# --- instruments --------------------------------------------------------------
def _scope(layout, wm_files, fail_paths, latest_seq, tone, accent, sequence):
    ranked = sorted(layout.items(), key=lambda kv: -_int(wm_files.get(kv[0], {}).get("activityCount"), 0))
    blips = []
    for i, (path, _cell) in enumerate(ranked[:6]):
        wm = wm_files.get(path, {})
        last_seq = _int(wm.get("lastSequence"), 0)
        recency = 1.0 if latest_seq <= 0 else max(0.12, min(1.0, 1.0 - (latest_seq - last_seq) * 0.18))
        blips.append({"angle": round((i * 1.05 + sequence * 0.04) % 6.28, 3),
                      "radius": round(0.92 - recency * 0.6, 3),
                      "tone": "red" if path in fail_paths else (accent if recency > 0.8 else tone),
                      "label": _label(path), "intensity": round(0.4 + recency * 0.5, 3)})
    return _upsert(SCOPE, "signal_scope", {
        "label": "HOT FILES", "position": {"x": 0.87, "y": 0.22}, "scale": 0.12, "mode": "radar",
        "rings": 4, "spokes": 8, "sweep": True, "sweepSpeed": 0.5,
        "blips": blips or [{"angle": 0.0, "radius": 0.5, "label": "IDLE", "tone": tone}],
        "tone": tone, "accentTone": accent, "opacity": 0.82, "speed": 0.38, "seed": sequence + 3,
    })


def _hud(world, counts, test_health, build_health, attention, commands, outcomes, stakes, sequence):
    action = _dict(attention.get("action"))
    objective = _text(attention.get("objective")) or _text(action.get("label"), "—")
    focus = _dict(attention.get("focus"))
    cmd = _dict(commands[0]) if commands else {}
    cmd_status = _text(cmd.get("status"), "—")
    glyphs = " ".join("OK" if _text(_dict(o).get("status")) == "ok" else "XX" for o in outcomes[-10:])
    panels = [
        {"id": "objective", "title": f"OBJECTIVE :: {_text(action.get('kind'), '—').upper()}",
         "lines": [_clip(objective, 64),
                   f"FOCUS -> {_label(_text(focus.get('primaryPath'), '—'))}",
                   f"conf {(_dict(attention.get('provenance')).get('confidence', '?'))} (inferred)"],
         "tone": stakes["tone"], "accentTone": stakes["accent"], "active": True},
        {"id": "system", "title": "WORLD STATE",
         "lines": [f"rev {_int(world.get('revision'), 0)}  files {_int(counts.get('files'), 0)}  "
                   f"edges {_int(_dict(world.get('semanticGraph')).get('edgeCount'), _int(counts.get('changes'), 0))}",
                   f"TEST: {_hstat(test_health)}   BUILD: {_hstat(build_health)}",
                   f"cmd: {_clip(_text(cmd.get('commandPreview'), '—'), 48)}"],
         "tone": "cyan", "accentTone": stakes["accent"], "streaming": cmd_status == "running"},
        {"id": "health", "title": "HEALTH (INFERRED)",
         "lines": _health_lines(test_health, build_health),
         "tone": stakes["tone"], "accentTone": "white", "active": bool(test_health or build_health)},
        {"id": "outcomes", "title": "OUTCOME TRACE",
         "lines": [glyphs or "no outcomes yet", stakes["status"]],
         "tone": "amber", "accentTone": stakes["accent"], "streaming": True},
    ]
    return _upsert(HUD, "terminal_wall", {
        "title": "WORLD + SEMANTIC GRAPH + ATTENTION",
        "position": {"x": 0.5, "y": 0.71}, "size": {"w": 0.86, "h": 0.16},
        "columns": 2, "rows": 2, "panels": panels,
        "tone": stakes["tone"], "accentTone": stakes["accent"], "opacity": 0.86,
        "scan": True, "cursor": cmd_status == "running", "speed": 0.42, "seed": sequence + 11,
    })


def _hstat(health: dict[str, Any]) -> str:
    if not health:
        return "—"
    status = _text(health.get("status"))
    return {"ok": "GREEN", "error": "RED", "running": "…"}.get(status, status.upper() or "?")


def _health_lines(test_health: dict[str, Any], build_health: dict[str, Any]) -> list[str]:
    lines = []
    for h in (test_health, build_health):
        if not h:
            continue
        conf = _dict(h.get("provenance")).get("confidence", 0.85)
        lines.append(f"{_text(h.get('category'), '?').upper()} {_text(h.get('status'), '?').upper()} "
                     f"(conf {conf}) {_clip(_text(h.get('commandPreview'), ''), 30)}")
    return lines or ["no health checkpoints"]


# --- background + overlays ----------------------------------------------------
def _rain(stakes, sequence):
    alert = stakes["name"] == "alert"
    return _upsert(RAIN, "data_rain", {
        "glyphs": _clip(f"{stakes['status']} SEMANTIC GRAPH GIBSON".upper(), 160),
        "columns": 32, "density": 0.24 if alert else 0.15, "speed": 0.3, "direction": "down",
        "tone": stakes["tone"], "accentTone": stakes["accent"], "opacity": 0.2 if alert else 0.11,
        "position": {"x": 0.5, "y": 0.5}, "size": {"w": 1.0, "h": 1.0},
        "trail": 12, "bands": 3 if alert else 2, "glitch": 0.2 if alert else 0.04, "seed": sequence})


def _tunnel(stakes, n, sequence):
    return _upsert(TUNNEL, "tunnel_grid", {
        "position": {"x": 0.5, "y": 0.46}, "size": {"w": 1.02, "h": 0.96},
        "rings": 14, "spokes": 14, "lanes": 4, "packets": 14 + min(20, n * 2),
        "speed": 0.46 if stakes["name"] in ("work", "verify") else 0.3, "twist": 0.16, "depth": 0.94,
        "direction": "in", "tone": stakes["accent"], "accentTone": stakes["tone"],
        "opacity": 0.32, "seed": sequence + 7})


def _ice(fail_paths, layout, stakes, sequence):
    xy = [(_dict(layout.get(p)).get("x"), _dict(layout.get(p)).get("y")) for p in fail_paths if p in layout]
    xy = [(x, y) for x, y in xy if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    cx = round(sum(x for x, _ in xy) / len(xy), 3) if xy else 0.47
    cy = round(sum(y for _, y in xy) / len(xy), 3) if xy else 0.46
    label = ", ".join(_label(p) for p in list(fail_paths)[:2]) or "FAULT"
    return _upsert(ICE, "black_ice", {
        "label": _clip(label.upper(), 24), "position": {"x": cx, "y": cy}, "size": {"w": 0.4, "h": 0.42},
        "columns": 6, "rows": 5, "depth": 0.7, "breach": 0.6, "breachPosition": {"x": 0.5, "y": 0.46},
        "fractures": 7, "sentries": 4, "sweep": True, "sweepSpeed": 0.7,
        "tone": "red", "accentTone": "amber", "opacity": 0.8, "seed": sequence + 17})


def _breach(stakes, sequence, ts):
    return _anim(ANIM_BREACH, CITY, "breach_wave", ts, 1500, 2400,
                 {"label": stakes["status"], "tone": "red", "accentTone": "amber", "intensity": 0.95,
                  "rings": 4, "shards": 16, "position": {"x": 0.5, "y": 0.46}, "seed": sequence})


def _jolt(focus_path, fail_paths, layout, sequence, ts):
    target = next((p for p in fail_paths if p in layout), focus_path)
    ref = {"path": target} if target in layout else {"index": 0}
    return _anim(ANIM_JOLT, CITY, "camera_jolt", ts, 800, 1600,
                 {"targetRef": ref, "intensity": 0.7, "zoom": 1.05, "roll": 0.02, "seed": sequence})


def _interference(stakes, sequence, ts):
    return _anim(ANIM_NOISE, "stage", "signal_interference", ts, 1800, 2400,
                 {"label": _clip(stakes["status"], 36), "tone": "red", "accentTone": "amber",
                  "intensity": 0.6, "bands": 5, "blocks": 6, "noise": 8, "speed": 0.8, "seed": sequence})


def _unlock(focus_path, layout, sequence, ts):
    ref = {"path": focus_path} if focus_path in layout else {"index": 0}
    return _anim(ANIM_UNLOCK, CITY, "breach_wave", ts, 1400, 2000,
                 {"targetRef": ref, "label": "LOCK RELEASED", "tone": "green", "accentTone": "cyan",
                  "intensity": 0.8, "rings": 5, "shards": 10, "position": {"x": 0.5, "y": 0.46}, "seed": sequence})


# --- stakes (same health-driven logic as the world renderer) ------------------
def _stakes(test_health, build_health, outcomes, health):
    ts_ = _text(test_health.get("status")) if test_health else ""
    bs = _text(build_health.get("status")) if build_health else ""
    last_error = _text(_dict(outcomes[-1]).get("status")) == "error" if outcomes else False
    alert = ts_ == "error" or bs == "error" or last_error
    test_runs = [h for h in (_dict(x) for x in health) if _text(h.get("category")) == "test"]
    recovered = ts_ == "ok" and not alert and any(_text(r.get("status")) == "error" for r in test_runs[1:])
    if alert:
        what = "TESTS RED" if ts_ == "error" else ("BUILD RED" if bs == "error" else "FAULT")
        return {"name": "alert", "label": "ICE", "status": f"BREACH :: {what}", "tone": "red", "accent": "amber",
                "alert": True, "recovered": False, "intent": f"stakes high :: {what.lower()}"}
    if recovered:
        return {"name": "recovery", "label": "UNLOCK", "status": "TESTS GREEN :: LOCK RELEASED",
                "tone": "green", "accent": "cyan", "alert": False, "recovered": True,
                "intent": "tests recovered to green"}
    if ts_ == "running" or bs == "running":
        return {"name": "verify", "label": "VERIFY", "status": "RUNNING CHECKS :: AWAIT OUTCOME",
                "tone": "amber", "accent": "cyan", "alert": False, "recovered": False,
                "intent": "verification in flight"}
    if outcomes:
        return {"name": "work", "label": "TRACE", "status": f"WORKING :: {len(outcomes)} OUTCOMES",
                "tone": "cyan", "accent": "magenta", "alert": False, "recovered": False, "intent": "agent active"}
    return {"name": "idle", "label": "STANDBY", "status": "GIBSON LINK :: STANDBY",
            "tone": "amber", "accent": "cyan", "alert": False, "recovered": False, "intent": "awaiting activity"}


# --- world-model helpers ------------------------------------------------------
def _failing_paths(test_health, build_health, wm_files):
    paths: set[str] = set()
    for h in (test_health, build_health):
        if h and _text(h.get("status")) == "error":
            paths.update(p for p in _list(h.get("touchedPaths")) if isinstance(p, str))
    for path, f in wm_files.items():
        if _text(_dict(f.get("lastOutcome")).get("status")) == "error":
            paths.add(path)
    return {p for p in paths if _plausible(p)}


def _change_mag(changes):
    out: dict[str, int] = {}
    for c in changes:
        cd = _dict(c)
        p = _text(cd.get("path"))
        if p:
            out[p] = out.get(p, 0) + _int(cd.get("magnitudeLines"), 0)
    return out


def _latest_cat(entities, category):
    for e in entities:
        ed = _dict(e)
        if _text(ed.get("category")) == category:
            return ed
    return {}


def _plausible(path: str) -> bool:
    if not path or " " in path:
        return False
    head = path.split("/", 1)[0]
    if "/" in path and len(head) <= 2 and "." not in path:
        return False
    return "/" in path or "." in path


# --- builders / helpers -------------------------------------------------------
def _binding(field_path: str, target_prop: str, relationship: str, intent: str) -> dict[str, Any]:
    return {"schema": "harn-gibson.world-binding.v1", "source": "worldModel",
            "fieldPath": field_path, "targetProp": target_prop, "relationship": relationship, "intent": intent}


def _upsert(pid, kind, props, region="stage"):
    return {"op": "upsert", "primitive": {"id": pid, "kind": kind, "region": region, "props": props}}


def _anim(aid, target, kind, ts, duration, ttl, props, *, loop=False):
    return {"op": "start_animation",
            "animation": {"id": aid, "targetId": target, "kind": kind, "startedAtMs": ts,
                          "durationMs": duration, "ttlMs": ttl, "loop": loop, "props": props}}


def _latest_event(requests):
    if not requests:
        return {"eventType": "idle", "phase": "lifecycle", "sequence": 0, "timestampMs": 0}
    return _dict(_dict(requests[-1]).get("event"))


def _fnv(text: str) -> int:
    # Deterministic across per-event subprocess calls (unlike hash()).
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return h


def _label(path: str) -> str:
    tail = (path.rstrip("/").rsplit("/", 1)[-1] or path).split()[0]
    return _clip(tail.upper().replace("_", "-"), 12)


def _load_stdin():
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value, fallback=""):
    return value if isinstance(value, str) and value else fallback


def _int(value, fallback):
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


def _clip(value, limit):
    text = str(value)
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


if __name__ == "__main__":
    main()
