"""Perception-model renderer for harn-gibson (v5).

The first renderer that projects **only** from `context.project.perceptionModel`
(`harn-gibson.perception-model.v1`) -- no worldModel, no semanticGraph, no
touched-file batches. It exists to prove the decision-point claim: one temporal
entity-relation graph is enough to drive a full display.

Projections (each visual property names the perception fact it follows):

* **Layout** -- a radial tree of the `contains` relations: `dir:.` at the
  center, directories on an inner ring in wedges sized by subtree file count,
  files on an outer arc. Position comes from a real relation, never a hash.
* **Causality** -- the latest command's `touched` relations become flowing
  edges; when a check fails, the blast radius is `check <- produced <- command
  -> touched -> files`, traversed along real edges.
* **Attention** -- the `focused_on` relation parks the AGENT cursor beside its
  target and aims the object camera at it.
* **Facts on nodes** -- `touchCount` -> size, `dirty` -> amber, blast -> red,
  `exists: false` -> ghosted, perception `file_changed` churn -> lift pulse.
* **Milestones** -- a `commit_created` event fires a golden ring at the root.

Contract: reads `harn-gibson.external-renderer-request.v1` on stdin, writes
`harn-gibson.render-plan.v1` on stdout.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

MAP = "cgp-map"
RAIN = "cgp-rain"
HUD = "cgp-hud"
ICE = "cgp-ice"

ANIM_CAM = "cgp-cam"
ANIM_BREACH = "cgp-breach"
ANIM_JOLT = "cgp-jolt"
ANIM_MILESTONE = "cgp-milestone"
ANIM_UNLOCK = "cgp-unlock"

MAP_POS = {"x": 0.5, "y": 0.40}
MAP_SIZE = {"w": 0.94, "h": 0.46}
DIR_RADIUS = 0.24
FILE_RADIUS = 0.45
Y_SQUASH = 0.82


def main() -> None:
    payload = _dict(_load_stdin())
    requests = _list(payload.get("requests"))
    context = _dict(payload.get("context"))
    project = _dict(context.get("project"))
    perception = _dict(project.get("perceptionModel"))
    project_name = _text(project.get("name"), "GIBSON")

    event = _latest_event(requests)
    sequence = _int(event.get("sequence"), 0)
    ts = _int(event.get("timestampMs"), 0)

    entities = {_text(_dict(e).get("id")): _dict(e) for e in _list(perception.get("entities"))}
    relations = [_dict(r) for r in _list(perception.get("relations"))]
    events = [_dict(e) for e in _list(perception.get("events"))]
    workspace = _dict(perception.get("workspace"))
    latest_seq = _int(perception.get("latestSequence"), 0)

    files = {eid[5:]: e for eid, e in entities.items() if eid.startswith("file:")}
    checks = [e for e in entities.values() if _text(e.get("type")) == "check"]
    commands = [e for e in entities.values() if _text(e.get("type")) == "command"]

    stakes = _stakes(checks, commands)
    tone = stakes["tone"]
    blast = _blast_radius(stakes["failingCheckId"], relations)
    focus_path = _focus_path(relations)
    touched_now = _touched_now(relations, latest_seq)
    churn = _churn_by_path(events)
    milestone = _latest_milestone(events, latest_seq)

    layout = _radial_tree_layout(relations, files)

    mutations: list[dict[str, Any]] = [
        {
            "op": "patch", "targetId": "status",
            "props": {"text": stakes["status"], "phase": _text(event.get("phase"), "lifecycle"), "tone": tone},
        },
        {
            "op": "append_log",
            "entry": {"sequence": sequence, "phase": _text(event.get("phase"), "lifecycle"),
                      "eventType": "claude_gibson_perception", "title": stakes["label"],
                      "summary": stakes["status"]},
        },
        _rain(stakes, sequence),
        _sector_map(layout, files, churn, blast, focus_path, touched_now,
                    project_name, stakes, relations),
        _hud(perception, workspace, checks, commands, events, stakes, focus_path, sequence),
        _attention_camera(focus_path, layout, stakes, sequence, ts),
    ]

    if stakes["alert"]:
        anchor = _effect_anchor(blast, focus_path, layout)
        mutations += [
            _ice(blast, anchor, sequence),
            _breach(anchor, stakes, sequence, ts),
            _jolt(blast, focus_path, layout, sequence, ts),
        ]
    else:
        mutations.append({"op": "remove", "targetId": ICE})
        if stakes["recovered"]:
            mutations.append(_unlock(focus_path, layout, sequence, ts))
    if milestone is not None:
        mutations.append(_milestone_beat(milestone, sequence, ts))

    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "claude-gibson-perception",
            "intent": stakes["intent"],
            "mood": stakes["name"],
            "alert": stakes["alert"],
            "layout": "radial-contains-tree",
            "source": "perceptionModel",
            "nodes": len(layout),
            "blastPaths": sorted(blast),
            "focusPath": focus_path,
            "perceptionRevision": _int(perception.get("revision"), 0),
        },
        "steps": [{"eventIndex": max(0, len(requests) - 1), "mutations": mutations}],
    }
    json.dump(plan, sys.stdout, separators=(",", ":"))


# --- projections over relations -------------------------------------------------
def _focus_path(relations: list[dict[str, Any]]) -> str:
    for relation in relations:
        if _text(relation.get("type")) == "focused_on" and _text(relation.get("from")) == "agent":
            target = _text(relation.get("to"))
            if target.startswith("file:"):
                return target[5:]
    return ""


def _touched_now(relations: list[dict[str, Any]], latest_seq: int) -> list[str]:
    """Files touched by the most recent command, along real `touched` edges."""
    best_seq = 0
    for relation in relations:
        if _text(relation.get("type")) == "touched":
            best_seq = max(best_seq, _int(relation.get("lastSeq"), 0))
    if best_seq <= 0 or (latest_seq and best_seq < latest_seq - 2):
        return []
    return [
        _text(r.get("to"))[5:]
        for r in relations
        if _text(r.get("type")) == "touched"
        and _int(r.get("lastSeq"), 0) == best_seq
        and _text(r.get("to")).startswith("file:")
    ][:6]


def _blast_radius(failing_check_id: str, relations: list[dict[str, Any]]) -> set[str]:
    """check <- produced <- command -> touched -> files: the real causal chain."""
    if not failing_check_id:
        return set()
    command_id = ""
    for relation in relations:
        if _text(relation.get("type")) == "produced" and _text(relation.get("to")) == failing_check_id:
            command_id = _text(relation.get("from"))
            break
    if not command_id:
        return set()
    return {
        _text(r.get("to"))[5:]
        for r in relations
        if _text(r.get("type")) == "touched"
        and _text(r.get("from")) == command_id
        and _text(r.get("to")).startswith("file:")
    }


def _churn_by_path(events: list[dict[str, Any]]) -> dict[str, float]:
    churn: dict[str, float] = {}
    for event in events:
        if _text(event.get("kind")) != "file_changed":
            continue
        entity = _text(event.get("entity"))
        if entity.startswith("file:"):
            path = entity[5:]
            fraction = event.get("churnFraction")
            value = float(fraction) if isinstance(fraction, (int, float)) else 1.0
            churn[path] = max(churn.get(path, 0.0), min(1.0, value))
    return churn


def _latest_milestone(events: list[dict[str, Any]], latest_seq: int) -> dict[str, Any] | None:
    for event in reversed(events):
        if _text(event.get("kind")) == "commit_created" and _int(event.get("seq"), 0) >= latest_seq:
            return event
    return None


def _stakes(checks: list[dict[str, Any]], commands: list[dict[str, Any]]) -> dict[str, Any]:
    latest_by_category: dict[str, dict[str, Any]] = {}
    saw_error: dict[str, bool] = {}
    for check in sorted(checks, key=lambda c: _int(_dict(c.get("attrs")).get("seq"), 0)):
        attrs = _dict(check.get("attrs"))
        category = _text(attrs.get("category"), "check")
        if _text(attrs.get("status")) == "error":
            saw_error[category] = True
        latest_by_category[category] = check
    failing = next(
        (check for check in latest_by_category.values()
         if _text(_dict(check.get("attrs")).get("status")) == "error"),
        None,
    )
    if failing is not None:
        category = _text(_dict(failing.get("attrs")).get("category"), "check")
        return {"name": "alert", "label": "ICE", "status": f"BREACH :: {category.upper()} RED",
                "tone": "red", "accent": "amber", "alert": True, "recovered": False,
                "failingCheckId": _text(failing.get("id")),
                "intent": f"stakes high :: {category} red"}
    recovered_category = next(
        (category for category, check in latest_by_category.items()
         if saw_error.get(category) and _text(_dict(check.get("attrs")).get("status")) == "ok"),
        "",
    )
    if recovered_category:
        return {"name": "recovery", "label": "UNLOCK",
                "status": f"{recovered_category.upper()} GREEN :: LOCK RELEASED",
                "tone": "green", "accent": "cyan", "alert": False, "recovered": True,
                "failingCheckId": "", "intent": "checks recovered to green"}
    running = any(_text(_dict(c.get("attrs")).get("status")) == "running" for c in commands)
    if running:
        return {"name": "verify", "label": "VERIFY", "status": "COMMAND RUNNING :: AWAIT OUTCOME",
                "tone": "amber", "accent": "cyan", "alert": False, "recovered": False,
                "failingCheckId": "", "intent": "verification in flight"}
    if commands:
        return {"name": "work", "label": "TRACE", "status": f"WORKING :: {len(commands)} COMMANDS",
                "tone": "cyan", "accent": "magenta", "alert": False, "recovered": False,
                "failingCheckId": "", "intent": "agent active"}
    return {"name": "idle", "label": "STANDBY", "status": "GIBSON LINK :: STANDBY",
            "tone": "amber", "accent": "cyan", "alert": False, "recovered": False,
            "failingCheckId": "", "intent": "awaiting activity"}


# --- layout: radial tree over the `contains` relations ---------------------------
def _radial_tree_layout(
    relations: list[dict[str, Any]], files: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """{path: {x, y, dir, dirX, dirY}} from `contains` edges -- the perception
    model serves the tree; the renderer only assigns angles."""
    parent_of: dict[str, str] = {}
    for relation in relations:
        if _text(relation.get("type")) == "contains":
            parent_of[_text(relation.get("to"))] = _text(relation.get("from"))

    by_dir: dict[str, list[str]] = {}
    for path in files:
        parent = parent_of.get(f"file:{path}", "dir:.")
        by_dir.setdefault(parent[4:] if parent.startswith("dir:") else ".", []).append(path)

    dirs = sorted(by_dir)
    if not dirs:
        return {}
    total_weight = sum(len(by_dir[d]) + 1.5 for d in dirs)
    layout: dict[str, dict[str, Any]] = {}
    angle = -math.pi / 2
    for d in dirs:
        members = sorted(by_dir[d])
        wedge = (len(members) + 1.5) / total_weight * math.tau
        mid = angle + wedge / 2
        dir_x, dir_y = _polar(mid, DIR_RADIUS)
        spread = wedge * 0.72
        for index, path in enumerate(members):
            t = 0.5 if len(members) == 1 else index / (len(members) - 1)
            fx, fy = _polar(mid + (t - 0.5) * spread, FILE_RADIUS)
            layout[path] = {"x": fx, "y": fy, "dir": d, "dirX": dir_x, "dirY": dir_y}
        angle += wedge
    return layout


def _polar(theta: float, radius: float) -> tuple[float, float]:
    return (round(0.5 + math.cos(theta) * radius, 4),
            round(0.5 + math.sin(theta) * radius * Y_SQUASH, 4))


def _sector_map(
    layout: dict[str, dict[str, Any]],
    files: dict[str, dict[str, Any]],
    churn: dict[str, float],
    blast: set[str],
    focus_path: str,
    touched_now: list[str],
    project_name: str,
    stakes: dict[str, Any],
    relations: list[dict[str, Any]],
) -> dict[str, Any]:
    tone, accent = stakes["tone"], stakes["accent"]
    objects: list[dict[str, Any]] = [{
        "id": "repo", "entityId": "dir:.", "entityKind": "dir",
        "label": project_name.upper(), "x": 0.5, "y": 0.5, "z": 0.04,
        "mass": 0.62, "tone": tone, "active": True, "confidence": 1.0,
    }]
    edges: list[dict[str, Any]] = []

    seen_dirs: set[str] = set()
    for cell in (layout[path] for path in sorted(layout)):
        d = cell["dir"]
        if d != "." and d not in seen_dirs:
            seen_dirs.add(d)
            objects.append({
                "id": f"dir:{d}", "entityKind": "dir", "path": d,
                "label": d.rsplit("/", 1)[-1].upper(),
                "x": cell["dirX"], "y": cell["dirY"], "z": 0.02,
                "mass": 0.40, "tone": "cyan", "opacity": 0.72, "confidence": 1.0,
            })
            edges.append({"source": "repo", "target": f"dir:{d}", "tone": "cyan",
                          "width": 2.2, "active": True, "curve": 0.02})

    for path, cell in sorted(layout.items()):
        attrs = _dict(files.get(path, {}).get("attrs"))
        touch_count = _int(attrs.get("touchCount"), 0)
        dormant = touch_count == 0
        exists = attrs.get("exists") is not False
        dirty = attrs.get("dirty") is True
        status = "error" if path in blast else ""
        node_tone = "white" if dormant else ("amber" if dirty and not status else tone)
        objects.append({
            "id": f"file:{path}", "entityId": f"file:{path}", "entityKind": "file",
            "path": path, "label": _label(path),
            "x": cell["x"], "y": cell["y"],
            "z": round(min(0.5, touch_count * 0.07 + churn.get(path, 0.0) * 0.25), 3),
            "mass": round(0.22 + min(0.55, touch_count * 0.11), 3),
            "status": status,
            "tone": node_tone,
            "opacity": 0.34 if dormant else (0.5 if not exists else 1.0),
            "active": path in touched_now or path == focus_path or path in blast,
            "confidence": 1.0 if exists else 0.6,
            "touchCount": touch_count,
        })
        hub = f"dir:{cell['dir']}" if cell["dir"] != "." else "repo"
        edges.append({"source": hub, "target": f"file:{path}",
                      "tone": "red" if path in blast else "cyan",
                      "width": 1.5, "active": True, "curve": 0.0})

    focus_cell = layout.get(focus_path)
    if focus_cell:
        ax = round(focus_cell["x"] + (0.5 - focus_cell["x"]) * 0.30, 4)
        ay = round(focus_cell["y"] + (0.5 - focus_cell["y"]) * 0.30, 4)
    else:
        ax, ay = 0.5, 0.34
    objects.append({
        "id": "agent", "entityId": "agent", "entityKind": "agent", "label": "AGENT",
        "x": ax, "y": ay, "z": 0.30, "mass": 0.5,
        "tone": "magenta", "active": True, "confidence": 1.0,
    })
    for path in touched_now:
        edges.append({"source": "agent", "target": f"file:{path}",
                      "tone": accent, "active": True, "flow": True,
                      "width": 1.8, "curve": 0.10})
    if focus_path in layout and focus_path not in touched_now:
        edges.append({"source": "agent", "target": f"file:{focus_path}",
                      "tone": "magenta", "active": True, "curve": 0.08, "label": "focus"})

    return _upsert(MAP, "spatial_map", {
        "label": f"PERCEPTION MAP :: {project_name.upper()}",
        "position": dict(MAP_POS), "size": dict(MAP_SIZE),
        "layout": "radial-contains-tree", "projection": "isometric",
        "focusObjectId": f"file:{focus_path}" if focus_path in layout else "repo",
        "objects": objects, "edges": edges,
        "tone": tone, "accentTone": accent, "opacity": 0.92, "labels": True,
        "worldBindings": [
            _binding("perceptionModel.relations[contains]", "objects[].x", "derives",
                     "position is a projection of the contains tree"),
            _binding("perceptionModel.entities[file].attrs.touchCount", "objects[].mass", "scales",
                     "node size follows touch count"),
            _binding("perceptionModel.events[file_changed].churnFraction", "objects[].z", "scales",
                     "recent churn lifts the node"),
            _binding("perceptionModel.relations[focused_on]", "objects[id=agent]", "tracks",
                     "agent cursor follows the focused_on relation"),
        ],
        "seed": 11,
    })


# --- effect anchoring (same arithmetic as spatialMapPointInRect) ------------------
def _screen_pos(cell: dict[str, Any], z: float = 0.0) -> tuple[float, float]:
    rect_x = MAP_POS["x"] - MAP_SIZE["w"] / 2
    rect_y = MAP_POS["y"] - MAP_SIZE["h"] / 2
    pad_x = MAP_SIZE["w"] * 0.08
    pad_y = MAP_SIZE["h"] * 0.12
    sx = rect_x + pad_x + cell["x"] * (MAP_SIZE["w"] - pad_x * 2)
    sy = rect_y + pad_y + cell["y"] * (MAP_SIZE["h"] - pad_y * 2) - z * MAP_SIZE["h"] * 0.18
    return round(sx, 3), round(sy, 3)


def _effect_anchor(blast: set[str], focus_path: str, layout: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target = next((p for p in sorted(blast) if p in layout), focus_path)
    cell = layout.get(target)
    if not cell:
        return {"x": 0.5, "y": 0.40, "label": "FAULT"}
    x, y = _screen_pos(cell)
    return {"x": x, "y": y, "label": _label(target)}


# --- camera / overlays / HUD ------------------------------------------------------
def _attention_camera(focus_path, layout, stakes, sequence, ts):
    target_ref = {"path": focus_path} if focus_path in layout else {"id": "repo"}
    return _anim(ANIM_CAM, MAP, "camera_path", ts, 4200, 6000, {
        "targetRef": target_ref,
        "tone": stakes["tone"], "accentTone": stakes["accent"], "yoyo": True,
        "keyframes": [
            {"at": 0, "x": 0.0, "y": 0.0, "scale": 1.0},
            {"at": 0.5, "x": 0.0, "y": -0.004, "scale": 1.05},
            {"at": 1, "x": 0.0, "y": 0.0, "scale": 1.0},
        ],
        "seed": sequence,
    })


def _hud(perception, workspace, checks, commands, events, stakes, focus_path, sequence):
    git = _dict(workspace.get("git"))
    counts = _dict(perception.get("counts"))
    entity_counts = _dict(counts.get("entitiesByType"))
    latest_command = max(commands, key=lambda c: _int(_dict(c.get("attrs")).get("startSeq"), 0), default={})
    command_attrs = _dict(_dict(latest_command).get("attrs"))
    check_line = "  ".join(
        f"{_text(_dict(c.get('attrs')).get('category'), '?').upper()}:"
        f"{_text(_dict(c.get('attrs')).get('status'), '?').upper()}"
        for c in sorted(checks, key=lambda c: -_int(_dict(c.get("attrs")).get("seq"), 0))[:3]
    ) or "no checks yet"
    event_line = " ".join(
        {"file_changed": "Δ", "command_completed": "$", "check_completed": "✓",
         "commit_created": "◆"}.get(_text(_dict(e).get("kind")), "·")
        for e in events[-14:]
    ) or "no perception events"
    panels = [
        {"id": "focus", "title": "FOCUS (focused_on)",
         "lines": [f"-> {_label(focus_path) if focus_path else '—'}",
                   f"cmd: {_clip(_text(command_attrs.get('preview'), '—'), 46)}",
                   f"status: {_text(command_attrs.get('status'), '—')}"],
         "tone": stakes["tone"], "accentTone": stakes["accent"], "active": True},
        {"id": "workspace", "title": "WORKSPACE (git)",
         "lines": [f"branch {_text(git.get('branch'), '?')}  head {_text(git.get('headSha'), '?')}",
                   f"files {_int(workspace.get('fileCount'), 0)}  "
                   f"dirty {_int(git.get('dirtyPathCount'), 0)}  "
                   f"untracked {_int(git.get('untrackedPathCount'), 0)}",
                   f"basis: {_text(workspace.get('basis'), '?')}"],
         "tone": "cyan", "accentTone": stakes["accent"]},
        {"id": "checks", "title": "CHECKS (produced)",
         "lines": [check_line, stakes["status"]],
         "tone": stakes["tone"], "accentTone": "white", "active": bool(checks)},
        {"id": "timeline", "title": "EVENT LOG",
         "lines": [event_line,
                   f"entities {sum(_int(v, 0) for v in entity_counts.values())}  "
                   f"events {_int(counts.get('events'), 0)}  rev {_int(perception.get('revision'), 0)}"],
         "tone": "amber", "accentTone": stakes["accent"], "streaming": True},
    ]
    return _upsert(HUD, "terminal_wall", {
        "title": "PERCEPTION MODEL v1",
        "position": {"x": 0.5, "y": 0.71}, "size": {"w": 0.86, "h": 0.16},
        "columns": 2, "rows": 2, "panels": panels,
        "tone": stakes["tone"], "accentTone": stakes["accent"], "opacity": 0.86,
        "scan": True, "speed": 0.42, "seed": sequence + 11,
    })


def _rain(stakes, sequence):
    alert = stakes["name"] == "alert"
    return _upsert(RAIN, "data_rain", {
        "glyphs": _clip(f"{stakes['status']} PERCEPTION GIBSON".upper(), 160),
        "columns": 32, "density": 0.22 if alert else 0.13, "speed": 0.3, "direction": "down",
        "tone": stakes["tone"], "accentTone": stakes["accent"], "opacity": 0.16 if alert else 0.09,
        "position": {"x": 0.5, "y": 0.5}, "size": {"w": 1.0, "h": 1.0},
        "trail": 12, "bands": 3 if alert else 2, "glitch": 0.18 if alert else 0.04, "seed": 5})


def _ice(blast, anchor, sequence):
    label = ", ".join(_label(p) for p in sorted(blast)[:2]) or anchor["label"]
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


def _jolt(blast, focus_path, layout, sequence, ts):
    target = next((p for p in sorted(blast) if p in layout), focus_path)
    ref = {"path": target} if target in layout else {"id": "repo"}
    return _anim(ANIM_JOLT, MAP, "camera_jolt", ts, 800, 1600,
                 {"targetRef": ref, "intensity": 0.7, "zoom": 1.05, "roll": 0.02, "seed": sequence})


def _unlock(focus_path, layout, sequence, ts):
    cell = layout.get(focus_path)
    x, y = _screen_pos(cell) if cell else (0.5, 0.40)
    ref = {"path": focus_path} if focus_path in layout else {"id": "repo"}
    return _anim(ANIM_UNLOCK, MAP, "breach_wave", ts, 1400, 2000,
                 {"targetRef": ref, "label": "LOCK RELEASED", "tone": "green", "accentTone": "cyan",
                  "intensity": 0.8, "rings": 5, "shards": 10,
                  "position": {"x": x, "y": y}, "seed": sequence})


def _milestone_beat(milestone, sequence, ts):
    subject = _clip(_text(milestone.get("subject"), "COMMIT"), 28).upper()
    return _anim(ANIM_MILESTONE, MAP, "breach_wave", ts, 1800, 2600,
                 {"targetRef": {"id": "repo"}, "label": f"COMMIT :: {subject}",
                  "tone": "amber", "accentTone": "white", "intensity": 0.85,
                  "rings": 6, "shards": 12, "position": {"x": 0.5, "y": 0.40}, "seed": sequence})


# --- builders / helpers -------------------------------------------------------------
def _binding(field_path: str, target_prop: str, relationship: str, intent: str) -> dict[str, Any]:
    return {"schema": "harn-gibson.world-binding.v1", "source": "perceptionModel",
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
