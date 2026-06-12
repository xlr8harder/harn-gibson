"""Sector-map renderer for harn-gibson (spatial_map checkpoint).

The fourth renderer in the series, and the first built after the perception-model
spec (`docs/perception-model-spec.md`). It applies the spec's two projection rules
using only the tools on `work/1.1-spatial-bindings`:

1. **Position always comes from a real relation.** The stage is a single
   `spatial_map` laid out as a radial tree of the repo's *directory structure*
   (the `contains` relation): the repo root at the center, directory hubs on an
   inner ring in angular wedges sized by their file count, files on an outer arc
   inside their directory's wedge. The layout is a pure function of the sorted
   path set -- no hashes, no activity ranks, no arbitrary coordinates -- so it is
   stable across the whole session and *legible*: nearby nodes really are siblings.

2. **Effects target entities, not coordinates.** The camera frames the focused
   file via `targetRef {path}` (resolved by the framework against spatial_map
   objects); breach/unlock beats and the black-ice overlay are positioned by
   replicating the map's own coordinate math, so they erupt *from the implicated
   node* wherever the layout placed it.

On top of the structural skeleton, every dynamic property is bound to a
world-model or attention fact: node size = `activityCount`, node lift =
change magnitude, tone = `lastOutcome`/health status (via spatial_map's own
status->tone mapping), opacity = the new per-entity `lifecycle.recency`,
confidence rings = provenance. A distinguished AGENT node darts to
`agentAttention.focus.primaryPath` each step (Gource-style cursor) with
active/flow edges to the files the latest command actually touched.

Deliberately consumes no `semanticGraph` *semantics* -- only its file list, as a
stand-in for the repo tree (`git ls-files` in the future model). Structure from
the tree, activity from the stream, nothing parsed.

Contract: reads `harn-gibson.external-renderer-request.v1` on stdin, writes
`harn-gibson.render-plan.v1` on stdout.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

MAP = "cgm-map"
RAIN = "cgm-rain"
HUD = "cgm-hud"
ICE = "cgm-ice"

ANIM_CAM = "cgm-cam"
ANIM_BREACH = "cgm-breach"
ANIM_JOLT = "cgm-jolt"
ANIM_NOISE = "cgm-noise"
ANIM_UNLOCK = "cgm-unlock"

# Map geometry (shared by the primitive props and the effect-anchoring math).
MAP_POS = {"x": 0.5, "y": 0.40}
MAP_SIZE = {"w": 0.94, "h": 0.46}
DIR_RADIUS = 0.24
FILE_RADIUS = 0.45
Y_SQUASH = 0.82  # gentle ellipse so the tree reads as a tilted plane
MAX_FILES = 64


def main() -> None:
    payload = _dict(_load_stdin())
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))
    world = _dict(project.get("worldModel"))
    graph = _dict(project.get("semanticGraph"))
    attention = _dict(project.get("agentAttention"))
    project_name = _text(project.get("name"), "GIBSON")

    event = _latest_event(requests)
    sequence = _int(event.get("sequence"), 0)
    ts = _int(event.get("timestampMs"), 0)

    entities = _dict(world.get("entities"))
    wm_files = {
        _text(_dict(f).get("path")): _dict(f)
        for f in _list(entities.get("files"))
        if _text(_dict(f).get("path"))
    }
    commands = _list(entities.get("commands"))
    health = _list(entities.get("health"))
    changes = _list(entities.get("changes"))
    outcomes = _list(world.get("recentOutcomes"))
    counts = _dict(world.get("counts"))
    lifecycle_summary = _dict(world.get("lifecycle"))

    test_health = _latest_cat(health, "test")
    build_health = _latest_cat(health, "build")
    stakes = _stakes(test_health, build_health, outcomes, health)
    tone = stakes["tone"]

    fail_paths = _failing_paths(test_health, build_health, wm_files)
    change_mag = _change_mag(changes)

    # The `contains` relation: every known file, from the tree (graph file list)
    # plus anything the agent touched that the scan missed.
    layout = _radial_tree_layout(graph, wm_files)
    focus_path = _text(_dict(attention.get("focus")).get("primaryPath"))
    if focus_path not in layout:
        focus_path = _latest_touched(wm_files)
    action = _dict(attention.get("action"))
    touched_now = _latest_command_paths(commands, layout)

    mutations: list[dict[str, Any]] = [
        {
            "op": "patch", "targetId": "status",
            "props": {"text": stakes["status"], "phase": _text(event.get("phase"), "lifecycle"), "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {"sequence": sequence, "phase": _text(event.get("phase"), "lifecycle"),
                      "eventType": "claude_gibson_map", "title": stakes["label"], "summary": stakes["status"]},
        },
        _rain(stakes, sequence),
        _sector_map(layout, wm_files, change_mag, fail_paths, focus_path, touched_now,
                    project_name, stakes, sequence),
        _hud(world, counts, lifecycle_summary, test_health, build_health, attention,
             commands, outcomes, stakes, sequence),
        _attention_camera(focus_path, layout, action, stakes, sequence, ts),
    ]

    if stakes["alert"]:
        anchor = _effect_anchor(fail_paths, focus_path, layout)
        mutations += [
            _ice(fail_paths, anchor, sequence),
            _breach(anchor, stakes, sequence, ts),
            _jolt(fail_paths, focus_path, layout, sequence, ts),
            _interference(stakes, sequence, ts),
        ]
    else:
        mutations.append({"op": "remove", "targetId": ICE})
        if stakes["recovered"]:
            mutations.append(_unlock(focus_path, layout, sequence, ts))

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "claude-gibson-map",
            "intent": stakes["intent"],
            "mood": stakes["name"],
            "alert": stakes["alert"],
            "layout": "radial-contains-tree",
            "nodes": len(layout),
            "attentionAction": _text(action.get("kind")),
            "focusPath": focus_path,
            "worldRevision": _int(world.get("revision"), 0),
        },
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


# --- layout: a radial tree of the `contains` relation --------------------------
def _radial_tree_layout(
    graph: dict[str, Any], wm_files: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Return {path: {x, y, dir, dirX, dirY}} -- position as a pure function of
    the directory tree. Directories get angular wedges proportional to their file
    count; files sit on an outer arc inside their wedge. Stable for the session
    because it depends only on the sorted path set, and *meaningful* because
    proximity == siblinghood (the failure of every previous layout)."""
    paths: set[str] = set()
    for node in _list(graph.get("files")):
        p = _text(_dict(node).get("path"))
        if p:
            paths.add(p)
    for p in wm_files:
        if _plausible(p):
            paths.add(p)
    ordered = sorted(paths)[:MAX_FILES]
    if not ordered:
        return {}

    by_dir: dict[str, list[str]] = {}
    for p in ordered:
        d = p.rsplit("/", 1)[0] if "/" in p else "/"
        by_dir.setdefault(d, []).append(p)

    dirs = sorted(by_dir)
    total_weight = sum(len(by_dir[d]) + 1.5 for d in dirs)
    layout: dict[str, dict[str, Any]] = {}
    angle = -math.pi / 2  # first wedge opens at 12 o'clock
    for d in dirs:
        files = by_dir[d]
        wedge = (len(files) + 1.5) / total_weight * math.tau
        mid = angle + wedge / 2
        dir_x, dir_y = _polar(mid, DIR_RADIUS)
        spread = wedge * 0.72
        for i, p in enumerate(files):
            t = 0.5 if len(files) == 1 else i / (len(files) - 1)
            fx, fy = _polar(mid + (t - 0.5) * spread, FILE_RADIUS)
            layout[p] = {"x": fx, "y": fy, "dir": d, "dirX": dir_x, "dirY": dir_y}
        angle += wedge
    return layout


def _polar(theta: float, radius: float) -> tuple[float, float]:
    return (round(0.5 + math.cos(theta) * radius, 4),
            round(0.5 + math.sin(theta) * radius * Y_SQUASH, 4))


def _sector_map(
    layout: dict[str, dict[str, Any]],
    wm_files: dict[str, dict[str, Any]],
    change_mag: dict[str, int],
    fail_paths: set[str],
    focus_path: str,
    touched_now: list[str],
    project_name: str,
    stakes: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    tone, accent = stakes["tone"], stakes["accent"]
    objects: list[dict[str, Any]] = [{
        "id": "repo", "entityId": f"repo:{project_name}", "entityKind": "repo",
        "label": project_name.upper(), "x": 0.5, "y": 0.5, "z": 0.04,
        "mass": 0.62, "tone": tone, "active": True, "confidence": 1.0,
    }]
    edges: list[dict[str, Any]] = []

    seen_dirs: set[str] = set()
    for _path, cell in sorted(layout.items()):
        d = cell["dir"]
        if d != "/" and d not in seen_dirs:
            seen_dirs.add(d)
            objects.append({
                "id": f"dir:{d}", "entityKind": "dir", "path": d,
                "label": d.rsplit("/", 1)[-1].upper(),
                "x": cell["dirX"], "y": cell["dirY"], "z": 0.02,
                "mass": 0.40, "tone": "cyan", "opacity": 0.72, "confidence": 1.0,
            })
            # Constant-tone skeleton: the tree is the stable ground truth and
            # shouldn't re-tint with mood; thick enough to read over the grid.
            # "active" here only buys edge alpha -- inactive edges are too faint
            # to carry the structural skeleton over the backdrop grid.
            edges.append({"source": "repo", "target": f"dir:{d}", "tone": "cyan",
                          "width": 2.2, "active": True, "curve": 0.02})

    for path, cell in sorted(layout.items()):
        wm = wm_files.get(path, {})
        activity = _int(wm.get("activityCount"), 0)
        dormant = activity == 0  # known from the tree, untouched this session
        recency = _text(_dict(wm.get("lifecycle")).get("recency"), "stale" if dormant else "recent")
        status = "error" if path in fail_paths else _text(_dict(wm.get("lastOutcome")).get("status"))
        objects.append({
            "id": f"file:{path}", "entityId": f"file:{path}", "entityKind": "file",
            "path": path, "label": _label(path),
            "x": cell["x"], "y": cell["y"],
            "z": round(min(0.5, activity * 0.07 + change_mag.get(path, 0) * 0.004), 3),
            "mass": round(0.22 + min(0.55, activity * 0.11), 3),
            "status": status,
            "tone": "white" if dormant else tone,
            "opacity": 0.34 if dormant else (0.66 if recency == "stale" else 1.0),
            "active": path in touched_now or path == focus_path or path in fail_paths,
            "confidence": 1.0 if not dormant else 0.7,
            "activityCount": activity,
            "recency": recency,
        })
        hub = f"dir:{cell['dir']}" if cell["dir"] != "/" else "repo"
        edges.append({"source": hub, "target": f"file:{path}",
                      "tone": "red" if path in fail_paths else "cyan",
                      "width": 1.5, "active": True, "curve": 0.0})

    # The agent cursor: parked beside its focus, darting as attention moves.
    focus_cell = layout.get(focus_path)
    if focus_cell:
        ax = round(focus_cell["x"] + (0.5 - focus_cell["x"]) * 0.30, 4)
        ay = round(focus_cell["y"] + (0.5 - focus_cell["y"]) * 0.30, 4)
    else:
        ax, ay = 0.5, 0.34
    objects.append({
        "id": "agent", "entityKind": "agent", "label": "AGENT",
        "x": ax, "y": ay, "z": 0.30, "mass": 0.5,
        "tone": "magenta", "active": True, "confidence": 1.0,
    })
    for path in touched_now[:6]:
        edges.append({"source": "agent", "target": f"file:{path}",
                      "tone": accent, "active": True, "flow": True,
                      "width": 1.8, "curve": 0.10})
    if focus_path in layout and focus_path not in touched_now:
        edges.append({"source": "agent", "target": f"file:{focus_path}",
                      "tone": "magenta", "active": True, "curve": 0.08, "label": "focus"})

    return _upsert(MAP, "spatial_map", {
        "label": f"SECTOR MAP :: {project_name.upper()}",
        "position": dict(MAP_POS), "size": dict(MAP_SIZE),
        "layout": "radial-contains-tree", "projection": "isometric",
        "focusObjectId": f"file:{focus_path}" if focus_path in layout else "repo",
        "objects": objects, "edges": edges,
        "tone": tone, "accentTone": accent, "opacity": 0.92, "labels": True,
        "worldBindings": [
            _binding("entities.files[].activityCount", "objects[].mass", "scales",
                     "node size follows file activity"),
            _binding("entities.files[].lastOutcome.status", "objects[].status", "encodes",
                     "node tone follows file health"),
            _binding("entities.files[].lifecycle.recency", "objects[].opacity", "encodes",
                     "stale facts fade"),
            _binding("agentAttention.focus.primaryPath", "objects[id=agent].x", "tracks",
                     "agent cursor darts to the attention focus"),
        ],
        "seed": 11,
    })


# --- effect anchoring: replicate spatial_map's point math -----------------------
def _screen_pos(cell: dict[str, Any], z: float = 0.0) -> tuple[float, float]:
    """Stage-normalized position of a map object -- the same arithmetic the
    browser's spatialMapPointInRect applies, so effects land ON the node."""
    rect_x = MAP_POS["x"] - MAP_SIZE["w"] / 2
    rect_y = MAP_POS["y"] - MAP_SIZE["h"] / 2
    pad_x = MAP_SIZE["w"] * 0.08
    pad_y = MAP_SIZE["h"] * 0.12
    sx = rect_x + pad_x + cell["x"] * (MAP_SIZE["w"] - pad_x * 2)
    sy = rect_y + pad_y + cell["y"] * (MAP_SIZE["h"] - pad_y * 2) - z * MAP_SIZE["h"] * 0.18
    return round(sx, 3), round(sy, 3)


def _effect_anchor(
    fail_paths: set[str], focus_path: str, layout: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    target = next((p for p in sorted(fail_paths) if p in layout), focus_path)
    cell = layout.get(target)
    if not cell:
        return {"x": 0.5, "y": 0.40, "label": "FAULT"}
    x, y = _screen_pos(cell)
    return {"x": x, "y": y, "label": _label(target)}


# --- attention-driven object camera ---------------------------------------------
def _attention_camera(focus_path, layout, action, stakes, sequence, ts):
    target_ref = {"path": focus_path} if focus_path in layout else {"id": "repo"}
    return _anim(ANIM_CAM, MAP, "camera_path", ts, 4200, 6000, {
        "targetRef": target_ref,
        "tone": stakes["tone"], "accentTone": stakes["accent"], "yoyo": True,
        "keyframes": [
            {"at": 0, "x": 0.0, "y": 0.0, "scale": 1.0},
            {"at": 0.5, "x": 0.0, "y": -0.004, "scale": 1.05},
            {"at": 1, "x": 0.0, "y": 0.0, "scale": 1.0},
        ],
        "attention": _text(action.get("kind")),
        "seed": sequence,
    })


# --- HUD -------------------------------------------------------------------------
def _hud(world, counts, lifecycle_summary, test_health, build_health, attention,
         commands, outcomes, stakes, sequence):
    action = _dict(attention.get("action"))
    objective = _text(attention.get("objective")) or _text(action.get("label"), "—")
    focus = _dict(attention.get("focus"))
    cmd = _dict(commands[0]) if commands else {}
    cmd_status = _text(cmd.get("status"), "—")
    glyphs = " ".join("OK" if _text(_dict(o).get("status")) == "ok" else "XX" for o in outcomes[-10:])
    recency_counts = _dict(_dict(lifecycle_summary.get("renderedEntityCounts")).get("byRecency"))
    recency_line = "  ".join(f"{k}:{v}" for k, v in recency_counts.items()) or "—"
    panels = [
        {"id": "objective", "title": f"OBJECTIVE :: {_text(action.get('kind'), '—').upper()}",
         "lines": [_clip(objective, 64),
                   f"FOCUS -> {_label(_text(focus.get('primaryPath'), '—'))}",
                   f"conf {(_dict(attention.get('provenance')).get('confidence', '?'))} (inferred)"],
         "tone": stakes["tone"], "accentTone": stakes["accent"], "active": True},
        {"id": "system", "title": "WORLD STATE",
         "lines": [f"rev {_int(world.get('revision'), 0)}  files {_int(counts.get('files'), 0)}  "
                   f"cmds {_int(counts.get('commands'), 0)}",
                   f"lifecycle {recency_line}",
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
        "title": "WORLD MODEL // ATTENTION // LIFECYCLE",
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
        lines.append(f"{_text(h.get('category'), '?').upper()} {_hstat(h)} "
                     f"(conf {conf}) {_clip(_text(h.get('commandPreview'), ''), 30)}")
    return lines or ["no health checkpoints"]


# --- background + overlays -------------------------------------------------------
def _rain(stakes, sequence):
    alert = stakes["name"] == "alert"
    return _upsert(RAIN, "data_rain", {
        "glyphs": _clip(f"{stakes['status']} SECTOR MAP GIBSON".upper(), 160),
        "columns": 32, "density": 0.22 if alert else 0.13, "speed": 0.3, "direction": "down",
        "tone": stakes["tone"], "accentTone": stakes["accent"], "opacity": 0.16 if alert else 0.09,
        "position": {"x": 0.5, "y": 0.5}, "size": {"w": 1.0, "h": 1.0},
        "trail": 12, "bands": 3 if alert else 2, "glitch": 0.18 if alert else 0.04, "seed": 5})


def _ice(fail_paths, anchor, sequence):
    label = ", ".join(_label(p) for p in sorted(fail_paths)[:2]) or anchor["label"]
    return _upsert(ICE, "black_ice", {
        "label": _clip(label.upper(), 24),
        "position": {"x": anchor["x"], "y": anchor["y"]}, "size": {"w": 0.20, "h": 0.24},
        "columns": 4, "rows": 3, "depth": 0.7, "breach": 0.6, "breachPosition": {"x": 0.5, "y": 0.5},
        "fractures": 6, "sentries": 3, "sweep": True, "sweepSpeed": 0.7,
        "tone": "red", "accentTone": "amber", "opacity": 0.66, "seed": sequence + 17})


def _breach(anchor, stakes, sequence, ts):
    return _anim(ANIM_BREACH, MAP, "breach_wave", ts, 1500, 2400,
                 {"label": stakes["status"], "tone": "red", "accentTone": "amber", "intensity": 0.95,
                  "rings": 4, "shards": 16,
                  "position": {"x": anchor["x"], "y": anchor["y"]}, "seed": sequence})


def _jolt(fail_paths, focus_path, layout, sequence, ts):
    target = next((p for p in sorted(fail_paths) if p in layout), focus_path)
    ref = {"path": target} if target in layout else {"id": "repo"}
    return _anim(ANIM_JOLT, MAP, "camera_jolt", ts, 800, 1600,
                 {"targetRef": ref, "intensity": 0.7, "zoom": 1.05, "roll": 0.02, "seed": sequence})


def _interference(stakes, sequence, ts):
    return _anim(ANIM_NOISE, "stage", "signal_interference", ts, 1800, 2400,
                 {"label": _clip(stakes["status"], 36), "tone": "red", "accentTone": "amber",
                  "intensity": 0.6, "bands": 5, "blocks": 6, "noise": 8, "speed": 0.8, "seed": sequence})


def _unlock(focus_path, layout, sequence, ts):
    cell = layout.get(focus_path)
    x, y = _screen_pos(cell) if cell else (0.5, 0.40)
    ref = {"path": focus_path} if focus_path in layout else {"id": "repo"}
    return _anim(ANIM_UNLOCK, MAP, "breach_wave", ts, 1400, 2000,
                 {"targetRef": ref, "label": "LOCK RELEASED", "tone": "green", "accentTone": "cyan",
                  "intensity": 0.8, "rings": 5, "shards": 10,
                  "position": {"x": x, "y": y}, "seed": sequence})


# --- stakes (health-driven, shared logic with the earlier renderers) -------------
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


# --- world-model helpers -----------------------------------------------------------
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


def _latest_touched(wm_files: dict[str, dict[str, Any]]) -> str:
    best, best_seq = "", -1
    for path, f in wm_files.items():
        seq = _int(f.get("lastSequence"), 0)
        if seq > best_seq and _plausible(path):
            best, best_seq = path, seq
    return best


def _latest_command_paths(commands, layout):
    if not commands:
        return []
    latest = _dict(commands[0])
    return [p for p in _list(latest.get("touchedPaths"))
            if isinstance(p, str) and p in layout][:6]


def _plausible(path: str) -> bool:
    if not path or " " in path:
        return False
    head = path.split("/", 1)[0]
    if "/" in path and len(head) <= 2 and "." not in path:
        return False
    return "/" in path or "." in path


# --- builders / helpers --------------------------------------------------------------
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


def _label(path: str) -> str:
    tail = (path.rstrip("/").rsplit("/", 1)[-1] or path).split()[0]
    return _clip(tail.upper().replace("_", "-"), 14)


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
