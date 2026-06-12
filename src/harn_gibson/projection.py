"""Projection engine: scene = f(projection, perception).

Implements `docs/projection-engine-vision.md`. A *projection* is a standing,
declarative spec (`harn-gibson.projection.v1`) describing how to turn the
perception model into a drawable scene: entity selection, relation-driven
layout, attribute->channel encodings, event->effect rules, camera, theme.
The engine re-resolves the projection against the perception model each step
and emits one `projection_scene` primitive; the browser tweens between
resolved scenes and draws them in the active theme.

Engine-owned guarantees (the chronic renderer failures, fixed structurally):

* position always comes from a layout over a real relation -- renderers never
  supply coordinates;
* effects target entity ids and are anchored wherever the active layout put
  the entity;
* the scene is derived, never accumulated -- long sessions cannot leak state;
* layouts warm-start from previous positions, so reflow settles instead of
  jumping (object constancy lives here and in the browser tween).

Every spec field is optional; missing pieces take the smart defaults below.
``ProjectionSceneRenderer`` adapts the engine to the existing renderer
protocol, so the pipeline, replay, and review tooling work unchanged. Enable
with ``HARN_GIBSON_PROJECTION=1`` (default projection) or
``HARN_GIBSON_PROJECTION=<path.json>``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .scene import SceneMutation, ScenePrimitive, SceneState

PROJECTION_SCHEMA = "harn-gibson.projection.v1"
PROJECTION_SCENE_SCHEMA = "harn-gibson.projection-scene.v1"
PROJECTION_SCENE_ID = "projection-scene"

_SEMANTIC_TONES = ("base", "accent", "good", "warn", "alarm", "ghost")
_EFFECT_TTLS_MS = {
    "pulse": 1800,
    "ring": 2600,
    "breach": 2800,
    "beam": 1600,
    "shake": 1200,
    "alarm": 2600,
    "banner": 2600,
}
_MAX_NODES_PER_LAYER = 150
_FORCE_ITERATIONS = 60
_TICKER_LENGTH = 16

DEFAULT_PROJECTION: dict[str, Any] = {
    "schema": PROJECTION_SCHEMA,
    "theme": "gibson",
    "layers": [
        {
            "id": "world",
            "select": {"types": ["dir", "file"]},
            "layout": {"kind": "radial-tree", "relation": "contains", "root": "dir:."},
            "encode": {
                "size": {"attr": "touchCount", "range": [0.25, 1.0]},
                "lift": {"attr": "touchCount", "range": [0.0, 0.5]},
                "opacity": {"attr": "touchCount", "zero": 0.35},
            },
            "edges": [
                {"relation": "contains", "style": "skeleton"},
                {"relation": "touched", "recent": True, "style": "flow"},
                {"relation": "focused_on", "style": "beam"},
            ],
        },
        {"id": "cursor", "select": {"ids": ["agent"]}, "place": {"near": "$focus"}},
    ],
    "camera": {"follow": "focused_on"},
    "on": [
        {"event": "file_changed", "effects": [{"kind": "pulse", "target": "$entity", "magnitude": "$churnFraction"}]},
        {
            "event": "check_completed",
            "when": {"status": "error"},
            "effects": [
                {"kind": "alarm"},
                {"kind": "breach", "target": "$blast"},
                {"kind": "shake"},
            ],
        },
        {
            "event": "check_completed",
            "when": {"status": "ok", "recovers": True},
            "effects": [{"kind": "ring", "target": "$blast", "label": "LOCK RELEASED", "tone": "good"}],
        },
        {
            "event": "commit_created",
            "effects": [{"kind": "ring", "target": "$root", "label": "$subject", "tone": "warn"}],
        },
    ],
}


def load_projection_spec(value: str) -> dict[str, Any]:
    """Resolve an env/CLI projection setting: truthy flag -> defaults, path ->
    JSON file (merged over defaults inside the engine)."""
    text = value.strip()
    if text.lower() in {"1", "true", "yes", "on", "default"}:
        return {}
    return json.loads(Path(text).expanduser().read_text(encoding="utf-8"))


class ProjectionEngine:
    """Resolves a projection spec against perception payloads, statefully:
    warm-started layouts, effect lifetimes, and check history live here."""

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.spec = _merge_spec(spec)
        self.revision = 0
        self._positions: dict[str, tuple[float, float]] = {}
        self._effects: list[dict[str, Any]] = []
        self._seen_events: set[tuple[int, str, str]] = set()
        self._check_errors_seen: set[str] = set()
        self._attr_max: dict[tuple[str, str], float] = {}

    # -- resolution -------------------------------------------------------------

    def resolve(self, perception: Mapping[str, Any], *, project_name: str = "", now_ms: int = 0) -> dict[str, Any]:
        entities = {
            str(item.get("id")): item
            for item in _list(perception.get("entities"))
            if isinstance(item, Mapping) and item.get("id")
        }
        relations = [item for item in _list(perception.get("relations")) if isinstance(item, Mapping)]
        events = [item for item in _list(perception.get("events")) if isinstance(item, Mapping)]
        latest_seq = _int(perception.get("latestSequence"), 0)

        mood = self._mood(entities)
        blast = _blast_targets(entities, relations)
        focus = _focus_target(relations)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        root_id = ""

        layers = [layer for layer in _list(self.spec.get("layers")) if isinstance(layer, Mapping)]
        for layer in layers:
            selected = _select(entities, _dict(layer.get("select")))
            placed, layer_root = self._layout(layer, selected, relations, nodes)
            if layer_root and not root_id:
                root_id = layer_root
            layer_id = str(layer.get("id") or "layer")
            encode = _dict(layer.get("encode"))
            # normalization maxima are settled across the whole layer before any
            # node is encoded, so iteration order cannot skew channel values
            self._update_attr_maxima(encode, layer_id, (entities.get(node_id, {}) for node_id in placed))
            for node_id, (x, y) in placed.items():
                entity = entities.get(node_id, {})
                nodes[node_id] = self._node(
                    node_id, entity, x, y, encode,
                    layer_id=layer_id, blast=blast, focus=focus, mood=mood,
                )
        # edges resolve after every layer has placed its nodes, so cross-layer
        # anchors (e.g. flow edges re-rooted on the agent cursor) always exist
        for layer in layers:
            edges.extend(_layer_edges(layer, relations, nodes, latest_seq))

        self._apply_event_rules(events, entities, relations, nodes, blast, root_id, focus, now_ms)
        self._effects = [
            effect for effect in self._effects
            if effect["startedAtMs"] + effect["ttlMs"] > now_ms
        ]
        self._positions.update({node_id: (node["x"], node["y"]) for node_id, node in nodes.items()})
        self.revision += 1

        return {
            "schema": PROJECTION_SCENE_SCHEMA,
            "theme": str(self.spec.get("theme") or "gibson"),
            "title": str(self.spec.get("title") or project_name or "GIBSON"),
            "seq": latest_seq,
            "revision": self.revision,
            "mood": mood,
            "nodes": [nodes[node_id] for node_id in sorted(nodes)],
            "edges": edges,
            "effects": [dict(effect) for effect in self._effects],
            "camera": self._camera(focus, root_id, nodes),
            "hud": self._hud(perception, entities, relations, events, mood, focus),
        }

    # -- layout -----------------------------------------------------------------

    def _layout(
        self,
        layer: Mapping[str, Any],
        selected: dict[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        placed_so_far: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, tuple[float, float]], str]:
        place = _dict(layer.get("place"))
        if place:
            return self._place_pinned(selected, place, placed_so_far), ""
        spec = _dict(layer.get("layout"))
        kind = str(spec.get("kind") or "radial-tree")
        ids = sorted(selected)[:_MAX_NODES_PER_LAYER]
        if not ids:
            return {}, ""
        if kind == "force":
            return self._layout_force(ids, spec, relations), ""
        if kind == "grid":
            return _layout_grid(ids, spec, selected), ""
        if kind == "ring":
            return _layout_ring(ids), ""
        return _layout_radial_tree(ids, spec, relations, selected)

    def _place_pinned(
        self,
        selected: dict[str, Mapping[str, Any]],
        place: Mapping[str, Any],
        placed_so_far: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, tuple[float, float]]:
        anchor = str(place.get("near") or "$focus")
        target: Mapping[str, Any] | None = None
        if anchor == "$focus":
            target = next((node for node in placed_so_far.values() if node.get("focus")), None)
        else:
            target = placed_so_far.get(anchor)
        if target is not None:
            ax = float(target["x"]) + (0.5 - float(target["x"])) * 0.3
            ay = float(target["y"]) + (0.5 - float(target["y"])) * 0.3
        else:
            ax, ay = 0.5, 0.3
        return {node_id: (round(ax, 4), round(ay, 4)) for node_id in sorted(selected)}

    def _layout_force(
        self, ids: list[str], spec: Mapping[str, Any], relations: list[Mapping[str, Any]]
    ) -> dict[str, tuple[float, float]]:
        """Deterministic spring/repulsion settle, warm-started from the previous
        step so growth reads as the graph reaching a new equilibrium."""
        wanted = spec.get("relations")
        wanted_types = {str(item) for item in wanted} if isinstance(wanted, list) else None
        id_set = set(ids)
        springs = [
            (str(r.get("from")), str(r.get("to")))
            for r in relations
            if (wanted_types is None or str(r.get("type")) in wanted_types)
            and str(r.get("from")) in id_set
            and str(r.get("to")) in id_set
        ]
        positions: dict[str, list[float]] = {}
        for node_id in ids:
            if node_id in self._positions:
                x, y = self._positions[node_id]
            else:
                angle = (_fnv(node_id) % 6283) / 1000.0
                radius = 0.28 + ((_fnv(node_id) >> 8) % 100) / 700.0
                x, y = 0.5 + math.cos(angle) * radius, 0.5 + math.sin(angle) * radius
            positions[node_id] = [x, y]
        for _ in range(_FORCE_ITERATIONS):
            forces = {node_id: [0.0, 0.0] for node_id in ids}
            for index, a in enumerate(ids):
                for b in ids[index + 1:]:
                    dx = positions[a][0] - positions[b][0]
                    dy = positions[a][1] - positions[b][1]
                    dist_sq = max(1e-4, dx * dx + dy * dy)
                    push = 0.0021 / dist_sq
                    forces[a][0] += dx * push
                    forces[a][1] += dy * push
                    forces[b][0] -= dx * push
                    forces[b][1] -= dy * push
            for a, b in springs:
                dx = positions[b][0] - positions[a][0]
                dy = positions[b][1] - positions[a][1]
                dist = math.sqrt(dx * dx + dy * dy) or 1e-4
                pull = (dist - 0.16) * 0.08
                forces[a][0] += dx / dist * pull
                forces[a][1] += dy / dist * pull
                forces[b][0] -= dx / dist * pull
                forces[b][1] -= dy / dist * pull
            for node_id in ids:
                forces[node_id][0] += (0.5 - positions[node_id][0]) * 0.01
                forces[node_id][1] += (0.5 - positions[node_id][1]) * 0.01
                positions[node_id][0] = min(0.96, max(0.04, positions[node_id][0] + forces[node_id][0]))
                positions[node_id][1] = min(0.96, max(0.04, positions[node_id][1] + forces[node_id][1]))
        return {node_id: (round(pos[0], 4), round(pos[1], 4)) for node_id, pos in positions.items()}

    # -- nodes ------------------------------------------------------------------

    def _node(
        self,
        node_id: str,
        entity: Mapping[str, Any],
        x: float,
        y: float,
        encode: Mapping[str, Any],
        *,
        layer_id: str,
        blast: set[str],
        focus: str,
        mood: Mapping[str, Any],
    ) -> dict[str, Any]:
        attrs = _dict(entity.get("attrs"))
        kind = str(entity.get("type") or _kind_from_id(node_id))
        node = {
            "id": node_id,
            "kind": kind,
            "layer": layer_id,
            "label": _node_label(node_id, attrs, _dict(encode.get("label"))),
            "x": x,
            "y": y,
            "size": self._channel(encode, "size", layer_id, attrs, default=_default_size(kind)),
            "lift": self._channel(encode, "lift", layer_id, attrs, default=0.0),
            "opacity": self._channel(encode, "opacity", layer_id, attrs, default=1.0),
            "tone": _node_tone(node_id, kind, attrs, _dict(encode.get("tone")), blast, mood),
            "focus": node_id == focus,
        }
        if attrs.get("exists") is False:
            node["opacity"] = min(node["opacity"], 0.4)
            node["tone"] = "ghost"
        return node

    def _update_attr_maxima(
        self, encode: Mapping[str, Any], layer_id: str, entities: Any
    ) -> None:
        rules = [
            (channel, str(_dict(encode.get(channel)).get("attr") or ""))
            for channel in ("size", "lift", "opacity", "glow")
            if _dict(encode.get(channel)).get("attr")
        ]
        if not rules:
            return
        for entity in entities:
            attrs = _dict(_dict(entity).get("attrs"))
            for channel, attr in rules:
                raw = attrs.get(attr)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    key = (layer_id, f"{channel}:{attr}")
                    # session-monotonic: normalization stays stable as activity grows
                    self._attr_max[key] = max(self._attr_max.get(key, 1.0), float(raw))

    def _channel(
        self,
        encode: Mapping[str, Any],
        channel: str,
        layer_id: str,
        attrs: Mapping[str, Any],
        *,
        default: float,
    ) -> float:
        rule = _dict(encode.get(channel))
        if not rule:
            return default
        raw = attrs.get(str(rule.get("attr") or ""))
        value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
        if value <= 0 and "zero" in rule:
            zero = rule.get("zero")
            return float(zero) if isinstance(zero, (int, float)) else default
        key = (layer_id, f"{channel}:{rule.get('attr')}")
        normalized = value / self._attr_max.get(key, 1.0)
        range_pair = rule.get("range")
        if isinstance(range_pair, list) and len(range_pair) == 2:
            low, high = float(range_pair[0]), float(range_pair[1])
        else:
            low, high = (0.0, 1.0) if channel != "opacity" else (0.55, 1.0)
        return round(low + normalized * (high - low), 4)

    # -- events -> effects --------------------------------------------------------

    def _apply_event_rules(
        self,
        events: list[Mapping[str, Any]],
        entities: Mapping[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        nodes: Mapping[str, Mapping[str, Any]],
        blast: set[str],
        root_id: str,
        focus: str,
        now_ms: int,
    ) -> None:
        latest_event_seq = max((_int(event.get("seq"), 0) for event in events), default=0)
        for event in events:
            key = (_int(event.get("seq"), 0), str(event.get("kind") or ""), str(event.get("entity") or ""))
            if key in self._seen_events:
                continue
            self._seen_events.add(key)
            for rule_index, rule in enumerate(_list(self.spec.get("on"))):
                if not isinstance(rule, Mapping):
                    continue
                if str(rule.get("event") or "") != key[1]:
                    continue
                if not self._rule_matches(rule, event):
                    continue
                for effect_index, effect_spec in enumerate(_list(rule.get("effects"))):
                    if not isinstance(effect_spec, Mapping):
                        continue
                    self._effects.append(self._effect_instance(
                        effect_spec, event, rule_index, effect_index,
                        entities, relations, nodes, blast, root_id, focus, now_ms,
                    ))
            if key[1] == "check_completed" and str(event.get("status")) == "error":
                self._check_errors_seen.add(str(event.get("category") or "check"))
        # the perception event window is bounded, so old keys can never reappear
        self._seen_events = {key for key in self._seen_events if key[0] >= latest_event_seq - 64}

    def _rule_matches(self, rule: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        when = _dict(rule.get("when"))
        for field_name, expected in when.items():
            if field_name == "recovers":
                category = str(event.get("category") or "check")
                if bool(expected) != (category in self._check_errors_seen):
                    return False
                continue
            if event.get(field_name) != expected:
                return False
        return True

    def _effect_instance(
        self,
        spec: Mapping[str, Any],
        event: Mapping[str, Any],
        rule_index: int,
        effect_index: int,
        entities: Mapping[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        nodes: Mapping[str, Mapping[str, Any]],
        blast: set[str],
        root_id: str,
        focus: str,
        now_ms: int,
    ) -> dict[str, Any]:
        kind = str(spec.get("kind") or "pulse")
        seq = _int(event.get("seq"), 0)
        target = spec.get("target")
        if target == "$entity":
            targets = [str(event.get("entity") or "")]
        elif target == "$blast":
            event_blast = _blast_for_check(str(event.get("entity") or ""), relations)
            targets = sorted(event_blast or blast)
        elif target == "$root":
            targets = [root_id]
        elif target == "$focus":
            targets = [focus]
        elif isinstance(target, str):
            targets = [target]
        else:
            targets = []
        targets = [target_id for target_id in targets if target_id in nodes] or ([] if target is None else [root_id])
        magnitude = spec.get("magnitude")
        if isinstance(magnitude, str) and magnitude.startswith("$"):
            raw = event.get(magnitude[1:])
            magnitude = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 1.0
        elif not isinstance(magnitude, (int, float)):
            magnitude = 1.0
        label = spec.get("label")
        if isinstance(label, str) and label.startswith("$"):
            label = str(event.get(label[1:]) or "")
        return {
            "id": f"fx-{rule_index}-{effect_index}-{seq}",
            "kind": kind,
            "targets": [t for t in targets if t],
            "tone": str(spec.get("tone") or ("alarm" if kind in {"breach", "alarm", "shake"} else "accent")),
            "label": str(label or "")[:48],
            "magnitude": round(min(1.0, max(0.0, float(magnitude))), 4),
            "startedAtMs": max(now_ms, _int(event.get("ts"), now_ms)),
            "ttlMs": _int(spec.get("ttlMs"), _EFFECT_TTLS_MS.get(kind, 2000)),
        }

    # -- mood / camera / hud --------------------------------------------------------

    def _mood(self, entities: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        latest_by_category: dict[str, str] = {}
        for entity in sorted(
            (e for e in entities.values() if str(e.get("type")) == "check"),
            key=lambda e: _int(_dict(e.get("attrs")).get("seq"), 0),
        ):
            attrs = _dict(entity.get("attrs"))
            category = str(attrs.get("category") or "check")
            latest_by_category[category] = str(attrs.get("status") or "")
            if latest_by_category[category] == "error":
                self._check_errors_seen.add(category)
        failing = next((c for c, status in latest_by_category.items() if status == "error"), "")
        if failing:
            return {"name": "alert", "label": f"BREACH :: {failing.upper()} RED", "tone": "alarm", "alert": True}
        recovered = next(
            (c for c, status in latest_by_category.items() if status == "ok" and c in self._check_errors_seen),
            "",
        )
        if recovered:
            return {"name": "recovery", "label": f"{recovered.upper()} GREEN :: LOCK RELEASED",
                    "tone": "good", "alert": False}
        running = any(
            str(_dict(e.get("attrs")).get("status")) == "running"
            for e in entities.values()
            if str(e.get("type")) == "command"
        )
        if running:
            return {"name": "verify", "label": "COMMAND RUNNING", "tone": "warn", "alert": False}
        if any(str(e.get("type")) == "command" for e in entities.values()):
            return {"name": "work", "label": "AGENT ACTIVE", "tone": "base", "alert": False}
        return {"name": "idle", "label": "STANDBY", "tone": "warn", "alert": False}

    def _camera(self, focus: str, root_id: str, nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        follow = str(_dict(self.spec.get("camera")).get("follow") or "")
        pin = str(_dict(self.spec.get("camera")).get("target") or "")
        if pin and pin in nodes:
            return {"target": pin, "zoom": 1.0}
        if follow == "focused_on" and focus in nodes:
            return {"target": focus, "zoom": 1.12}
        return {"target": root_id, "zoom": 1.0}

    def _hud(
        self,
        perception: Mapping[str, Any],
        entities: Mapping[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        events: list[Mapping[str, Any]],
        mood: Mapping[str, Any],
        focus: str,
    ) -> dict[str, Any]:
        workspace = _dict(perception.get("workspace"))
        git = _dict(workspace.get("git"))
        commands = sorted(
            (e for e in entities.values() if str(e.get("type")) == "command"),
            key=lambda e: -_int(_dict(e.get("attrs")).get("startSeq"), 0),
        )
        command_attrs = _dict(commands[0].get("attrs")) if commands else {}
        checks = sorted(
            (e for e in entities.values() if str(e.get("type")) == "check"),
            key=lambda e: -_int(_dict(e.get("attrs")).get("seq"), 0),
        )
        check_line = "  ".join(
            f"{str(_dict(c.get('attrs')).get('category') or '?').upper()}:"
            f"{str(_dict(c.get('attrs')).get('status') or '?').upper()}"
            for c in checks[:3]
        )
        return {
            "mood": str(mood.get("label") or ""),
            "focus": focus[5:] if focus.startswith("file:") else focus,
            "command": str(command_attrs.get("preview") or "")[:64],
            "commandStatus": str(command_attrs.get("status") or ""),
            "checks": check_line,
            "workspace": (
                f"{str(git.get('branch') or '?')} @ {str(git.get('headSha') or '?')[:7]}  "
                f"files {_int(workspace.get('fileCount'), 0)}  dirty {_int(git.get('dirtyPathCount'), 0)}"
            ),
            "ticker": [
                {"kind": str(event.get("kind") or ""), "seq": _int(event.get("seq"), 0)}
                for event in events[-_TICKER_LENGTH:]
            ],
        }


# --- layouts (stateless) -------------------------------------------------------------


def _layout_radial_tree(
    ids: list[str],
    spec: Mapping[str, Any],
    relations: list[Mapping[str, Any]],
    selected: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, tuple[float, float]], str]:
    relation_type = str(spec.get("relation") or "contains")
    parent_of = {
        str(r.get("to")): str(r.get("from"))
        for r in relations
        if str(r.get("type")) == relation_type
    }
    root = str(spec.get("root") or "")
    if root not in ids:
        candidates = [node_id for node_id in ids if parent_of.get(node_id) not in ids]
        root = candidates[0] if candidates else ids[0]
    parents = set(parent_of.values())
    leaves = [node_id for node_id in ids if node_id != root and node_id not in parents]
    hubs_of_leaf = {leaf: _nearest_in(parent_of, leaf, set(ids) | {root}, root) for leaf in leaves}
    by_hub: dict[str, list[str]] = {}
    for leaf in leaves:
        by_hub.setdefault(hubs_of_leaf[leaf], []).append(leaf)
    positions: dict[str, tuple[float, float]] = {root: (0.5, 0.5)}
    hubs = sorted(by_hub)
    total = sum(len(by_hub[hub]) + 1.5 for hub in hubs) or 1.0
    angle = -math.pi / 2
    for hub in hubs:
        members = sorted(by_hub[hub])
        wedge = (len(members) + 1.5) / total * math.tau
        mid = angle + wedge / 2
        if hub != root:
            positions[hub] = _polar(mid, 0.24)
        spread = wedge * 0.72
        for index, leaf in enumerate(members):
            t = 0.5 if len(members) == 1 else index / (len(members) - 1)
            positions[leaf] = _polar(mid + (t - 0.5) * spread, 0.45)
        angle += wedge
    # interior nodes that are neither root, hubs, nor leaves sit on the hub ring
    for node_id in ids:
        if node_id not in positions:
            positions[node_id] = _polar((_fnv(node_id) % 6283) / 1000.0, 0.24)
    return positions, root


def _nearest_in(parent_of: Mapping[str, str], node_id: str, allowed: set[str], root: str) -> str:
    current = parent_of.get(node_id, root)
    hops = 0
    while current not in allowed and hops < 8:
        current = parent_of.get(current, root)
        hops += 1
    return current if current in allowed else root


def _layout_grid(
    ids: list[str], spec: Mapping[str, Any], selected: Mapping[str, Mapping[str, Any]]
) -> dict[str, tuple[float, float]]:
    sort_attr = str(spec.get("sort") or "")

    def sort_key(node_id: str) -> tuple[float, str]:
        attrs = _dict(selected.get(node_id, {}).get("attrs"))
        raw = attrs.get(sort_attr)
        value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
        return (-value, node_id)

    ordered = sorted(ids, key=sort_key) if sort_attr else ids
    columns = max(1, math.ceil(math.sqrt(len(ordered))))
    rows = max(1, math.ceil(len(ordered) / columns))
    return {
        node_id: (
            round(0.08 + ((index % columns) + 0.5) / columns * 0.84, 4),
            round(0.08 + ((index // columns) + 0.5) / rows * 0.84, 4),
        )
        for index, node_id in enumerate(ordered)
    }


def _layout_ring(ids: list[str]) -> dict[str, tuple[float, float]]:
    return {
        node_id: _polar(-math.pi / 2 + index / max(1, len(ids)) * math.tau, 0.4)
        for index, node_id in enumerate(ids)
    }


def _polar(theta: float, radius: float) -> tuple[float, float]:
    return (round(0.5 + math.cos(theta) * radius, 4), round(0.5 + math.sin(theta) * radius * 0.82, 4))


# --- selection / edges / derived targets -------------------------------------------


def _select(entities: Mapping[str, Mapping[str, Any]], select: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    types = select.get("types")
    ids = select.get("ids")
    chosen: dict[str, Mapping[str, Any]] = {}
    for entity_id, entity in entities.items():
        if isinstance(ids, list) and entity_id in {str(item) for item in ids}:
            chosen[entity_id] = entity
        elif isinstance(types, list) and str(entity.get("type")) in {str(item) for item in types}:
            chosen[entity_id] = entity
    return chosen


def _layer_edges(
    layer: Mapping[str, Any],
    relations: list[Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
    latest_seq: int,
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for rule in _list(layer.get("edges")):
        if not isinstance(rule, Mapping):
            continue
        relation_type = str(rule.get("relation") or "")
        style = str(rule.get("style") or "skeleton")
        recent_only = bool(rule.get("recent"))
        for relation in relations:
            if str(relation.get("type")) != relation_type:
                continue
            source, target = str(relation.get("from")), str(relation.get("to"))
            if recent_only and _int(relation.get("lastSeq"), 0) < latest_seq - 2:
                continue
            if style == "flow" and source.startswith("command:"):
                source = "agent"  # causality flows from the cursor in spatial views
            if source not in nodes or target not in nodes:
                continue
            edges.append({"from": source, "to": target, "style": style,
                          "tone": str(rule.get("tone") or ("accent" if style != "skeleton" else "base"))})
    return edges


def _focus_target(relations: list[Mapping[str, Any]]) -> str:
    for relation in relations:
        if str(relation.get("type")) == "focused_on" and str(relation.get("from")) == "agent":
            return str(relation.get("to"))
    return ""


def _blast_targets(entities: Mapping[str, Mapping[str, Any]], relations: list[Mapping[str, Any]]) -> set[str]:
    failing = ""
    failing_seq = -1
    for entity_id, entity in entities.items():
        if str(entity.get("type")) != "check":
            continue
        attrs = _dict(entity.get("attrs"))
        if str(attrs.get("status")) == "error" and _int(attrs.get("seq"), 0) > failing_seq:
            failing, failing_seq = entity_id, _int(attrs.get("seq"), 0)
    return _blast_for_check(failing, relations)


def _blast_for_check(check_id: str, relations: list[Mapping[str, Any]]) -> set[str]:
    if not check_id:
        return set()
    command_id = next(
        (str(r.get("from")) for r in relations
         if str(r.get("type")) == "produced" and str(r.get("to")) == check_id),
        "",
    )
    if not command_id:
        return set()
    return {
        str(r.get("to"))
        for r in relations
        if str(r.get("type")) == "touched" and str(r.get("from")) == command_id
    }


# --- node channel helpers ------------------------------------------------------------


def _node_tone(
    node_id: str,
    kind: str,
    attrs: Mapping[str, Any],
    tone_rule: Mapping[str, Any],
    blast: set[str],
    mood: Mapping[str, Any],
) -> str:
    if tone_rule:
        raw = attrs.get(str(tone_rule.get("attr") or ""))
        mapped = _dict(tone_rule.get("map")).get(str(raw))
        if isinstance(mapped, str) and mapped in _SEMANTIC_TONES:
            return mapped
        default = tone_rule.get("default")
        if isinstance(default, str) and default in _SEMANTIC_TONES:
            return default
    if node_id in blast and mood.get("alert"):
        return "alarm"
    if kind == "agent":
        return "accent"
    if kind == "dir":
        return "base"
    touch_count = _int(attrs.get("touchCount"), 0)
    if touch_count == 0:
        return "ghost"
    if attrs.get("dirty") is True:
        return "warn"
    return "base"


def _node_label(node_id: str, attrs: Mapping[str, Any], label_rule: Mapping[str, Any]) -> str:
    if label_rule:
        raw = attrs.get(str(label_rule.get("attr") or ""))
        if isinstance(raw, str) and raw:
            return raw[:18].upper()
    tail = node_id.rsplit("/", 1)[-1]
    tail = tail.split(":", 1)[-1] if "/" not in node_id else tail
    return (tail or node_id)[:18].upper().replace("_", "-")


def _default_size(kind: str) -> float:
    return {"dir": 0.45, "agent": 0.55, "repo": 0.6}.get(kind, 0.3)


def _kind_from_id(node_id: str) -> str:
    return node_id.split(":", 1)[0] if ":" in node_id else node_id


# --- renderer adapter -------------------------------------------------------------------


class ProjectionSceneRenderer:
    """Adapts the projection engine to the existing SceneRenderer protocol."""

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.engine = ProjectionEngine(spec)

    def render(self, requests: Sequence[Any], _scene: SceneState) -> Any:
        return self._plan(requests, {"entities": [], "relations": [], "events": []}, "")

    def render_with_context(self, requests: Sequence[Any], _scene: SceneState, context: Any) -> Any:
        project = _dict(getattr(context, "project", {}))
        perception = _dict(project.get("perceptionModel"))
        return self._plan(requests, perception, str(project.get("name") or ""))

    def _plan(self, requests: Sequence[Any], perception: Mapping[str, Any], project_name: str) -> Any:
        from .rendering import RenderPlan, RenderStep  # adapter-local: avoids an import cycle

        event = requests[-1].event if requests else None
        now_ms = event.timestamp_ms if event is not None else 0
        props = self.engine.resolve(perception, project_name=project_name, now_ms=now_ms)
        mood = _dict(props.get("mood"))
        mutations = (
            SceneMutation(
                op="patch",
                target_id="status",
                props={"text": str(mood.get("label") or ""), "tone": str(mood.get("tone") or "base"),
                       "phase": event.phase if event is not None else "lifecycle"},
            ),
            SceneMutation(
                op="append_log",
                entry={
                    "sequence": event.sequence if event is not None else 0,
                    "phase": event.phase if event is not None else "lifecycle",
                    "eventType": "projection",
                    "title": str(mood.get("name") or "idle").upper(),
                    "summary": str(mood.get("label") or ""),
                },
            ),
            SceneMutation(
                op="upsert",
                primitive=ScenePrimitive(
                    id=PROJECTION_SCENE_ID,
                    kind="projection_scene",
                    region="stage",
                    props=props,
                ),
            ),
        )
        return RenderPlan(
            requests=tuple(requests),
            steps=(RenderStep(mutations=mutations, event_index=max(0, len(requests) - 1)),),
            metadata={
                "renderer": "projection-engine",
                "theme": props["theme"],
                "mood": str(mood.get("name") or ""),
                "nodes": len(_list(props.get("nodes"))),
                "effects": len(_list(props.get("effects"))),
                "projectionRevision": self.engine.revision,
            },
        )


# --- small helpers -------------------------------------------------------------------------


def _merge_spec(spec: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in DEFAULT_PROJECTION.items()}
    for key, value in dict(spec or {}).items():
        merged[key] = value
    return merged


def _fnv(text: str) -> int:
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


__all__ = [
    "DEFAULT_PROJECTION",
    "PROJECTION_SCENE_ID",
    "PROJECTION_SCENE_SCHEMA",
    "PROJECTION_SCHEMA",
    "ProjectionEngine",
    "ProjectionSceneRenderer",
    "load_projection_spec",
]
