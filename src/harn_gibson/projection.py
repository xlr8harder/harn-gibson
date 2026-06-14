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

Every spec field is optional; missing pieces take the packaged organic default.
``ProjectionSceneRenderer`` adapts the engine to the existing renderer
protocol, so the pipeline, replay, and review tooling work unchanged. Enable
with ``HARN_GIBSON_RENDERER=default`` or
``HARN_GIBSON_RENDERER=<path.json>``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from importlib import resources
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
    # peeks are paced by WALL time in the browser (~3.2s); the scene-side TTL
    # is generous so fast replays cannot evict the effect mid-animation
    "peek": 15000,
}
_MAX_NODES_PER_LAYER = 150
_FORCE_COLD_ITERATIONS = 360
_FORCE_ITERATIONS_PER_SECOND = 25
_FORCE_MIN_ITERATIONS = 12
_FORCE_MAX_ITERATIONS = 96
_FORCE_SETTLED_EPSILON = 1e-4
_TICKER_LENGTH = 16

_DEFAULT_PROJECTION_RESOURCE = "projections/gibson-organic.json"


def load_packaged_projection(path: str) -> dict[str, Any]:
    """Load a projection spec bundled in the harn_gibson package."""
    text = resources.files("harn_gibson").joinpath(path).read_text(encoding="utf-8")
    return json.loads(text)


DEFAULT_PROJECTION: dict[str, Any] = load_packaged_projection(_DEFAULT_PROJECTION_RESOURCE)


def load_projection_spec(value: str) -> dict[str, Any]:
    """Load a perception renderer spec JSON file."""
    return json.loads(Path(value.strip()).expanduser().read_text(encoding="utf-8"))


class ProjectionEngine:
    """Resolves a projection spec against perception payloads, statefully:
    warm-started layouts, effect lifetimes, and check history live here."""

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.spec = _merge_spec(spec)
        self.revision = 0
        self._positions: dict[str, tuple[float, float]] = {}
        self._effects: list[dict[str, Any]] = []
        self._seen_events: set[tuple[int, str, str, str]] = set()
        self._check_errors_seen: set[str] = set()
        self._grid_seen_events: set[tuple[int, str, str, str]] = set()
        self._grid_pending: dict[str, dict[str, float | str]] = {}
        self._grid_epochs: list[dict[str, Any]] = []
        self._thermal_seen_events: set[tuple[int, str, str, str]] = set()
        self._thermal_heat: dict[str, float] = {}
        self._thermal_samples: list[dict[str, Any]] = []
        self._thermal_focus = ""
        self._attr_max: dict[tuple[str, str], float] = {}
        self._resolve_now_ms = 0
        self._last_force_ms: int | None = None

    def redirect(self, spec: Mapping[str, Any] | None) -> None:
        """Swap the projection while keeping warm positions, effect lifetimes,
        and check history -- so a projection change morphs instead of cutting."""
        self.spec = _merge_spec(spec)

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
        self._resolve_now_ms = now_ms

        mood = self._mood(entities)
        blast = _blast_targets(entities, relations)
        focus = _focus_target(relations, latest_seq)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        root_id = ""

        layers = [layer for layer in _list(self.spec.get("layers")) if isinstance(layer, Mapping)]
        physics_layers: list[str] = []
        for layer in layers:
            if str(_dict(layer.get("layout")).get("kind") or "") == "force":
                physics_layers.append(str(layer.get("id") or "layer"))
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
        # layouts without an intrinsic root (force, grid) still need one for
        # $root effect targets and camera fallback
        if not root_id and "dir:." in nodes:
            root_id = "dir:."
        # the abstract root "dir:." reads as the workspace name, not "."
        if "dir:." in nodes:
            root_name = str(_dict(perception.get("workspace")).get("rootName") or project_name or ".")
            nodes["dir:."]["label"] = _clip_label(root_name)
        # every node carries its tree parent and depth so themes can stage
        # reveals as a wavefront from the root (materialize / budding pulses)
        _annotate_tree(nodes, relations)

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

        resolved = {
            "schema": PROJECTION_SCENE_SCHEMA,
            "theme": str(self.spec.get("theme") or "gibson"),
            "title": str(self.spec.get("title") or project_name or "GIBSON"),
            "seq": latest_seq,
            "revision": self.revision,
            "mood": mood,
            "nodes": [nodes[node_id] for node_id in sorted(nodes)],
            "edges": edges,
            "effects": [dict(effect) for effect in self._effects],
            "camera": self._camera(focus, root_id, nodes, blast, mood),
            "hud": self._hud(perception, entities, relations, events, mood, focus),
            # layers laid out by the force solver: the browser runs a live
            # spring-mass simulation for these between updates, anchored to
            # the engine's deterministic positions
            "physics": {"layers": physics_layers},
        }
        view = _dict(self.spec.get("view"))
        grid = self._epoch_grid(view, entities, relations, events, focus, latest_seq)
        if grid is None:
            grid = self._thermal_roll(view, entities, relations, events, focus, latest_seq, now_ms)
        if grid is not None:
            resolved["grid"] = grid
        return resolved

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
        neighbors: dict[str, list[str]] = {}
        for a, b in springs:
            neighbors.setdefault(a, []).append(b)
            neighbors.setdefault(b, []).append(a)
        positions: dict[str, list[float]] = {}
        for node_id in ids:
            if node_id in self._positions:
                x, y = self._positions[node_id]
            else:
                # bud next to an already-placed neighbor (organic growth);
                # only nodes with no placed connections seed on the hash ring
                anchor = next(
                    (self._positions[other] for other in neighbors.get(node_id, ())
                     if other in self._positions),
                    None,
                )
                angle = (_fnv(node_id) % 6283) / 1000.0
                if anchor is not None:
                    x = anchor[0] + math.cos(angle) * 0.05
                    y = anchor[1] + math.sin(angle) * 0.05
                else:
                    radius = 0.28 + ((_fnv(node_id) >> 8) % 100) / 700.0
                    x, y = 0.5 + math.cos(angle) * radius, 0.5 + math.sin(angle) * radius
            positions[node_id] = [x, y]
        # spring rest length scales with crowding so small graphs stay snug
        # and large ones spread; per-step displacement is capped so the solve
        # cools instead of slingshotting nodes into the boundary clamps
        rest = max(0.10, min(0.20, 0.55 / math.sqrt(max(1, len(ids)))))
        # clock sync with the browser: a cold start converges fully (the first
        # scene must not ship overlaps for the client to anchor onto), but
        # warm resolves advance only as far as the elapsed event time allows,
        # so the server layout never outruns what the client can animate
        cold_start = not any(node_id in self._positions for node_id in ids)
        if cold_start:
            iterations = _FORCE_COLD_ITERATIONS
        else:
            last = self._last_force_ms if self._last_force_ms is not None else self._resolve_now_ms
            elapsed_ms = max(0, self._resolve_now_ms - last)
            iterations = min(
                _FORCE_MAX_ITERATIONS,
                max(_FORCE_MIN_ITERATIONS, int(elapsed_ms / 1000 * _FORCE_ITERATIONS_PER_SECOND)),
            )
        self._last_force_ms = self._resolve_now_ms
        for _ in range(iterations):
            forces = {node_id: [0.0, 0.0] for node_id in ids}
            for index, a in enumerate(ids):
                for b in ids[index + 1:]:
                    dx = positions[a][0] - positions[b][0]
                    dy = positions[a][1] - positions[b][1]
                    dist_sq = max(1e-4, dx * dx + dy * dy)
                    push = 0.0007 / dist_sq
                    forces[a][0] += dx * push
                    forces[a][1] += dy * push
                    forces[b][0] -= dx * push
                    forces[b][1] -= dy * push
            for a, b in springs:
                dx = positions[b][0] - positions[a][0]
                dy = positions[b][1] - positions[a][1]
                dist = math.sqrt(dx * dx + dy * dy) or 1e-4
                pull = (dist - rest) * 0.14
                forces[a][0] += dx / dist * pull
                forces[a][1] += dy / dist * pull
                forces[b][0] -= dx / dist * pull
                forces[b][1] -= dy / dist * pull
            max_step = 0.0
            for node_id in ids:
                forces[node_id][0] += (0.5 - positions[node_id][0]) * 0.022
                forces[node_id][1] += (0.5 - positions[node_id][1]) * 0.022
                step_x = min(0.02, max(-0.02, forces[node_id][0]))
                step_y = min(0.02, max(-0.02, forces[node_id][1]))
                positions[node_id][0] = min(0.96, max(0.04, positions[node_id][0] + step_x))
                positions[node_id][1] = min(0.96, max(0.04, positions[node_id][1] + step_y))
                max_step = max(max_step, abs(step_x), abs(step_y))
            if max_step < _FORCE_SETTLED_EPSILON:
                break
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
            "label": _node_label(node_id, kind, attrs, _dict(encode.get("label"))),
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
        for event in events:
            key = self._event_seen_key(event)
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
                    instance = self._effect_instance(
                        effect_spec, event, rule_index, effect_index,
                        entities, relations, nodes, blast, root_id, focus, now_ms,
                    )
                    if instance["kind"] == "peek" and not instance.get("lines"):
                        continue  # nothing to show: not every change has a diff
                    # a fast agent fires the same beat many times per second;
                    # refresh the live instance instead of stacking duplicates
                    shape = (instance["kind"], tuple(instance["targets"]))
                    self._effects = [
                        effect for effect in self._effects
                        if (effect["kind"], tuple(effect["targets"])) != shape
                    ]
                    self._effects.append(instance)
            if key[1] == "check_completed":
                category = str(event.get("category") or "check")
                if str(event.get("status")) == "error":
                    self._check_errors_seen.add(category)
                elif str(event.get("status")) == "ok":
                    # the red->green transition is consumed once celebrated;
                    # without this, EVERY later green check re-fires recovery
                    # (rings chaining into a single endless fade)
                    self._check_errors_seen.discard(category)
        # prune only keys that have scrolled out of the perception event window:
        # sequences are sparse, so a fixed seq-distance cutoff can forget events
        # that are STILL in the window and re-fire their effects every resolve
        if events:
            min_seq = min(_int(event.get("seq"), 0) for event in events)
            self._seen_events = {key for key in self._seen_events if key[0] >= min_seq}
        if len(self._seen_events) > 2048:
            self._seen_events = set(sorted(self._seen_events)[-1024:])

    def _event_seen_key(self, event: Mapping[str, Any]) -> tuple[int, str, str, str]:
        return (
            _int(event.get("seq"), 0),
            str(event.get("kind") or ""),
            str(event.get("entity") or ""),
            json.dumps(event, sort_keys=True, separators=(",", ":"), default=str),
        )

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
        if isinstance(label, str) and "$" in label:
            label = re.sub(r"\$(\w+)", lambda match: str(event.get(match.group(1)) or ""), label)
        duration_ms = _int(spec.get("ttlMs"), _EFFECT_TTLS_MS.get(kind, 2000))
        instance = {
            "id": f"fx-{rule_index}-{effect_index}-{seq}",
            "kind": kind,
            "targets": [t for t in targets if t],
            "tone": str(spec.get("tone") or ("alarm" if kind in {"breach", "alarm", "shake"} else "accent")),
            "label": str(label or "")[:48],
            "magnitude": round(min(1.0, max(0.0, float(magnitude))), 4),
            "startedAtMs": max(now_ms, _int(event.get("ts"), now_ms)),
            # durationMs paces the browser animation in WALL time; ttlMs only
            # governs scene retention (event time), kept generous so replay
            # speed multipliers cannot evict an effect mid-animation
            "durationMs": duration_ms,
            "delayMs": max(0, _int(spec.get("delayMs"), 0)),
            "ttlMs": duration_ms * 5,
        }
        lines = spec.get("lines")
        if isinstance(lines, str) and lines.startswith("$"):
            lines = event.get(lines[1:])
        if isinstance(lines, list):
            instance["lines"] = [str(line)[:244] for line in lines[:2000] if isinstance(line, str)]
        return instance

    # -- epoch grid ---------------------------------------------------------------

    def _epoch_grid(
        self,
        view: Mapping[str, Any],
        entities: Mapping[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        events: list[Mapping[str, Any]],
        focus: str,
        latest_seq: int,
    ) -> dict[str, Any] | None:
        if str(view.get("kind") or "") != "epoch-grid":
            return None
        entity_spec = _dict(view.get("entities"))
        limit = max(1, _int(entity_spec.get("limit"), 48))
        file_ids = sorted(
            entity_id for entity_id, entity in entities.items()
            if str(entity.get("type")) == "file"
        )[:limit]
        file_set = set(file_ids)
        boundaries = {
            str(item) for item in _list(view.get("boundaryEvents"))
        } or {"command_completed", "check_completed", "commit_created", "turn_end"}
        for event in events:
            key = self._event_seen_key(event)
            if key in self._grid_seen_events:
                continue
            self._grid_seen_events.add(key)
            self._accumulate_grid_event(event, relations, file_set)
            if str(event.get("kind") or "") in boundaries:
                self._close_grid_epoch(event)
        if events:
            min_seq = min(_int(event.get("seq"), 0) for event in events)
            self._grid_seen_events = {key for key in self._grid_seen_events if key[0] >= min_seq}
        window = max(1, _int(view.get("window"), 32))
        visible_epochs = self._grid_epochs[-window:]
        visible_epoch_ids = {str(epoch["id"]) for epoch in visible_epochs}
        columns = [
            {
                "id": file_id,
                "label": file_id.removeprefix("file:"),
                "group": _file_group(file_id),
                "focus": file_id == focus,
            }
            for file_id in file_ids
        ]
        cells = [
            dict(cell)
            for epoch in visible_epochs
            for cell in _list(epoch.get("cells"))
            if str(cell.get("entity") or "") in file_set and str(cell.get("epoch") or "") in visible_epoch_ids
        ]
        pending = [
            _grid_cell("pending", entity_id, metrics, pending=True)
            for entity_id, metrics in sorted(self._grid_pending.items())
            if entity_id in file_set
        ]
        archived = max(window, _int(view.get("archiveWindow"), window * 2))
        self._grid_epochs = self._grid_epochs[-archived:]
        summary = _grid_summary(visible_epochs, pending, columns)
        return {
            "kind": "epoch-grid",
            "seq": latest_seq,
            "window": window,
            "presentation": {
                "stage": str(view.get("stage") or "primary"),
                "narration": bool(view.get("narration", False)),
                "spatial": bool(view.get("spatial", False)),
            },
            "columns": columns,
            "epochs": visible_epochs,
            "cells": cells,
            "pending": pending,
            "summary": summary,
        }

    def _accumulate_grid_event(
        self,
        event: Mapping[str, Any],
        relations: list[Mapping[str, Any]],
        file_set: set[str],
    ) -> None:
        kind = str(event.get("kind") or "")
        entity_id = str(event.get("entity") or "")
        targets: set[str] = set()
        if entity_id.startswith("file:"):
            targets.add(entity_id)
        if entity_id.startswith("command:"):
            targets.update(_touched_files_for_command(entity_id, relations))
        if kind == "check_completed":
            targets.update(_blast_for_check(entity_id, relations))
        for target in sorted(targets & file_set):
            metrics = self._grid_pending.setdefault(target, _blank_grid_metrics())
            metrics["activity"] = float(metrics["activity"]) + 1.0
            if kind == "file_changed":
                churn = event.get("churnFraction")
                metrics["churn"] = float(metrics["churn"]) + (
                    float(churn) if isinstance(churn, (int, float)) and not isinstance(churn, bool) else 0.25
                )
                metrics["edits"] = float(metrics["edits"]) + 1.0
            elif entity_id.startswith("command:"):
                metrics["commands"] = float(metrics["commands"]) + 1.0
                metrics["status"] = str(event.get("status") or metrics["status"])
            elif kind == "check_completed":
                metrics["checks"] = float(metrics["checks"]) + 1.0
                metrics["status"] = str(event.get("status") or "")

    def _close_grid_epoch(self, event: Mapping[str, Any]) -> None:
        if not self._grid_pending:
            return
        seq = _int(event.get("seq"), len(self._grid_epochs) + 1)
        kind = str(event.get("kind") or "epoch")
        status = str(event.get("status") or "")
        epoch_id = f"epoch:{seq}:{len(self._grid_epochs) + 1}"
        cells = [
            _grid_cell(epoch_id, entity_id, metrics)
            for entity_id, metrics in sorted(self._grid_pending.items())
        ]
        totals = _grid_totals(cells)
        self._grid_epochs.append({
            "id": epoch_id,
            "seq": seq,
            "kind": kind,
            "label": _epoch_label(event),
            "tone": _status_tone(status, kind),
            "status": status,
            "cells": cells,
            "activity": totals["activity"],
            "churn": totals["churn"],
            "edits": totals["edits"],
            "commands": totals["commands"],
            "checks": totals["checks"],
        })
        self._grid_pending = {}

    # -- thermal roll ------------------------------------------------------------

    def _thermal_roll(
        self,
        view: Mapping[str, Any],
        entities: Mapping[str, Mapping[str, Any]],
        relations: list[Mapping[str, Any]],
        events: list[Mapping[str, Any]],
        focus: str,
        latest_seq: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        if str(view.get("kind") or "") != "thermal-roll":
            return None
        entity_spec = _dict(view.get("entities"))
        limit = max(1, _int(entity_spec.get("limit"), 48))
        file_ids = sorted(
            entity_id for entity_id, entity in entities.items()
            if str(entity.get("type")) == "file"
        )[:limit]
        file_set = set(file_ids)
        if focus in file_set:
            self._thermal_focus = focus
        heat_gain = max(0.05, _float(view.get("heatGain"), 1.0))
        fallback_heat = max(0.05, _float(view.get("fallbackEditHeat"), 0.22))
        for event in events:
            key = self._event_seen_key(event)
            if key in self._thermal_seen_events:
                continue
            self._thermal_seen_events.add(key)
            self._apply_thermal_event(
                event,
                relations,
                file_set,
                heat_gain=heat_gain,
                fallback_heat=fallback_heat,
                fallback_now_ms=now_ms,
            )
        if events:
            min_seq = min(_int(event.get("seq"), 0) for event in events)
            self._thermal_seen_events = {key for key in self._thermal_seen_events if key[0] >= min_seq}
        if len(self._thermal_seen_events) > 4096:
            self._thermal_seen_events = set(sorted(self._thermal_seen_events)[-2048:])
        self._thermal_heat = {
            entity_id: heat for entity_id, heat in self._thermal_heat.items()
            if entity_id in file_set and heat > 0
        }
        window_ms = max(1000, _int(view.get("windowMs"), 180_000))
        max_samples = max(1, _int(view.get("samples"), 96))
        archive_samples = max(max_samples, _int(view.get("archiveSamples"), max_samples * 2))
        self._thermal_samples = self._thermal_samples[-archive_samples:]
        visible_samples = self._thermal_samples[-max_samples:]
        visible_sample_ids = {str(sample.get("id") or "") for sample in visible_samples}
        visual_window_ms = max(1000, _int(view.get("visualWindowMs"), window_ms))
        idle_coast_ms = max(0, _int(view.get("idleCoastMs"), 4500))
        columns = [
            {
                "id": file_id,
                "label": file_id.removeprefix("file:"),
                "group": _file_group(file_id),
                "focus": file_id == self._thermal_focus,
            }
            for file_id in file_ids
        ]
        cells = [
            dict(cell)
            for sample in visible_samples
            for cell in _list(sample.get("cells"))
            if str(cell.get("entity") or "") in file_set and str(cell.get("sample") or "") in visible_sample_ids
        ]
        heat = [
            {
                "entity": file_id,
                "heat": round(_thermal_visual_heat(self._thermal_heat.get(file_id, 0.0)), 4),
                "rawHeat": round(self._thermal_heat.get(file_id, 0.0), 4),
                "focus": file_id == self._thermal_focus,
            }
            for file_id in file_ids
        ]
        return {
            "kind": "thermal-roll",
            "seq": latest_seq,
            "nowMs": now_ms,
            "windowMs": window_ms,
            "visualWindowMs": visual_window_ms,
            "idleCoastMs": idle_coast_ms,
            "presentation": {
                "stage": str(view.get("stage") or "primary"),
                "narration": bool(view.get("narration", False)),
                "spatial": bool(view.get("spatial", False)),
            },
            "columns": columns,
            "samples": [
                {key: value for key, value in sample.items() if key != "cells"}
                for sample in visible_samples
            ],
            "cells": cells,
            "heat": heat,
            "summary": _thermal_summary(visible_samples, heat),
        }

    def _apply_thermal_event(
        self,
        event: Mapping[str, Any],
        relations: list[Mapping[str, Any]],
        file_set: set[str],
        *,
        heat_gain: float,
        fallback_heat: float,
        fallback_now_ms: int,
    ) -> None:
        kind = str(event.get("kind") or "")
        status = str(event.get("status") or "")
        entity_id = str(event.get("entity") or "")
        targets = _thermal_targets(event, relations, file_set)
        focus_target = entity_id if entity_id in file_set else next(iter(sorted(targets)), "")
        edited: set[str] = set()
        quench = False
        shock = False
        changed = False
        energy_before = sum(self._thermal_heat.values())
        if focus_target and focus_target != self._thermal_focus:
            self._thermal_focus = focus_target
            changed = True
        if kind == "file_changed":
            delta = _thermal_edit_delta(event, heat_gain=heat_gain, fallback_heat=fallback_heat)
            for target in sorted(targets):
                self._thermal_heat[target] = self._thermal_heat.get(target, 0.0) + delta
                edited.add(target)
            changed = bool(edited) or changed
        if kind in {"check_completed", "command_completed"} and status == "ok":
            if kind == "check_completed" or targets:
                quench = True
                self._thermal_heat = {entity_id: 0.0 for entity_id in self._thermal_heat}
                changed = True
        elif kind in {"check_completed", "command_completed"} and status == "error":
            shock = True
            changed = True
        if changed:
            self._thermal_samples.append(
                self._thermal_sample(
                    event,
                    file_set,
                    fallback_now_ms=fallback_now_ms,
                    edited=edited,
                    targets=targets,
                    quench=quench,
                    shock=shock,
                    energy_before=energy_before,
                )
            )

    def _thermal_sample(
        self,
        event: Mapping[str, Any],
        file_set: set[str],
        *,
        fallback_now_ms: int,
        edited: set[str],
        targets: set[str],
        quench: bool,
        shock: bool,
        energy_before: float,
    ) -> dict[str, Any]:
        seq = _int(event.get("seq"), len(self._thermal_samples) + 1)
        ts = _event_ts_ms(event, fallback_now_ms)
        sample_id = f"thermal:{seq}:{len(self._thermal_samples) + 1}"
        cells = [
            {
                "sample": sample_id,
                "entity": entity_id,
                "heat": round(_thermal_visual_heat(self._thermal_heat.get(entity_id, 0.0)), 4),
                "rawHeat": round(self._thermal_heat.get(entity_id, 0.0), 4),
                "focus": entity_id == self._thermal_focus,
                "edited": entity_id in edited,
                "target": entity_id in targets,
                "quench": quench,
                "shock": shock and (not targets or entity_id in targets or self._thermal_heat.get(entity_id, 0) > 0),
            }
            for entity_id in sorted(file_set)
        ]
        return {
            "id": sample_id,
            "seq": seq,
            "ts": ts,
            "kind": str(event.get("kind") or ""),
            "status": str(event.get("status") or ""),
            "focus": self._thermal_focus,
            "quench": quench,
            "shock": shock,
            "energy": round(energy_before, 4) if quench else round(sum(self._thermal_heat.values()), 4),
            "targets": sorted(targets),
            "cells": cells,
        }

    # -- mood / camera / hud --------------------------------------------------------

    def _mood(self, entities: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        # mood derives from entity history locally; the event-driven
        # _check_errors_seen set belongs to the effect rules (where red->green
        # transitions are consumed once celebrated) and must not be re-seeded
        # from historical entities on every resolve
        latest_by_category: dict[str, str] = {}
        errored_categories: set[str] = set()
        for entity in sorted(
            (e for e in entities.values() if str(e.get("type")) == "check"),
            key=lambda e: _int(_dict(e.get("attrs")).get("seq"), 0),
        ):
            attrs = _dict(entity.get("attrs"))
            category = str(attrs.get("category") or "check")
            latest_by_category[category] = str(attrs.get("status") or "")
            if latest_by_category[category] == "error":
                errored_categories.add(category)
        failing = next((c for c, status in latest_by_category.items() if status == "error"), "")
        if failing:
            return {"name": "alert", "label": f"BREACH :: {failing.upper()} RED", "tone": "alarm", "alert": True}
        recovered = next(
            (c for c, status in latest_by_category.items() if status == "ok" and c in errored_categories),
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

    def _camera(
        self,
        focus: str,
        root_id: str,
        nodes: Mapping[str, Mapping[str, Any]],
        blast: set[str],
        mood: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Camera output is a set of points of interest; the browser frames
        their bounding box (so an alert keeps every implicated node on
        screen), gliding and zooming to fit."""
        spec_camera = _dict(self.spec.get("camera"))
        pin = str(spec_camera.get("target") or "")
        if pin and pin in nodes:
            return {"target": pin, "targets": [pin]}
        targets: list[str] = []
        if mood.get("alert"):
            targets.extend(target for target in sorted(blast) if target in nodes)
        follow = str(spec_camera.get("follow") or "")
        if follow == "focused_on" and focus in nodes and focus not in targets:
            targets.append(focus)
        if not targets and root_id in nodes:
            targets.append(root_id)
        return {"target": targets[0] if targets else "", "targets": targets}

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
        agent_attrs = _dict(_dict(entities.get("agent")).get("attrs"))
        if focus.startswith("file:"):
            focus_display = focus[5:]
        elif focus == "dir:.":
            root_name = str(_dict(perception.get("workspace")).get("rootName") or ".")
            focus_display = f"{root_name} (whole project)"
        elif focus.startswith("dir:"):
            focus_display = f"{focus[4:]}/"
        else:
            focus_display = focus
        return {
            "mood": str(mood.get("label") or ""),
            "narration": str(agent_attrs.get("narration") or ""),
            "narrationComplete": bool(agent_attrs.get("narrationComplete", True)),
            "narrationSeq": _int(agent_attrs.get("narrationSeq"), 0),
            "narrationMessageIndex": _int(agent_attrs.get("narrationMessageIndex"), 0),
            "focus": focus_display,
            "command": _clip_text(str(command_attrs.get("preview") or ""), 96),
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
    """Recursive radial tree: every node owns an angular slice of its parent's
    wedge, sized by subtree weight, at a radius set by its true depth. Interior
    nodes (a dir containing only dirs) sit *inside* their own wedge, so a
    subtree can never land across an unrelated one."""
    relation_type = str(spec.get("relation") or "contains")
    parent_of = {
        str(r.get("to")): str(r.get("from"))
        for r in relations
        if str(r.get("type")) == relation_type
    }
    id_set = set(ids)
    root = str(spec.get("root") or "")
    if root not in id_set:
        candidates = [node_id for node_id in ids if parent_of.get(node_id) not in id_set]
        root = candidates[0] if candidates else ids[0]

    children: dict[str, list[str]] = {}
    for node_id in ids:
        if node_id == root:
            continue
        parent = _nearest_in(parent_of, node_id, id_set - {node_id}, root)
        children.setdefault(parent, []).append(node_id)

    # Each non-root node has exactly one parent, so the subtree reachable from
    # the root is a true tree; weights are computed only over that subtree.
    weights: dict[str, float] = {}

    def _weight(node_id: str) -> float:
        if node_id not in weights:
            weights[node_id] = max(1.0, sum(_weight(kid) for kid in children.get(node_id, [])))
        return weights[node_id]

    _weight(root)
    max_depth = 1
    depth_scan: list[tuple[str, int]] = [(root, 0)]
    while depth_scan:
        node_id, depth = depth_scan.pop(0)
        max_depth = max(max_depth, depth)
        depth_scan.extend((kid, depth + 1) for kid in children.get(node_id, []))

    positions: dict[str, tuple[float, float]] = {root: (0.5, 0.5)}
    # radial dendrogram: leaves sit on the outer rim; interior nodes sit at
    # depth-proportional radius at the middle of their own wedge, so a subtree
    # can never cross an unrelated one. Children divide the parent's wedge by
    # subtree weight.
    pending: list[tuple[str, float, float, int]] = [(root, -math.pi / 2, -math.pi / 2 + math.tau, 0)]
    while pending:
        node_id, start, end, depth = pending.pop(0)
        kids = sorted(children.get(node_id, []))
        if not kids:
            continue
        total = sum(_weight(kid) for kid in kids)
        gap = (end - start) * 0.05  # breathing room between sibling subtrees
        cursor = start + gap / 2
        span = (end - start) - gap
        for kid in kids:
            slice_width = span * _weight(kid) / total
            mid = cursor + slice_width / 2
            if children.get(kid):
                radius = min(0.38, max(0.16, 0.45 * (depth + 1) / max_depth))
            else:
                radius = 0.45
            positions[kid] = _polar(mid, radius)
            pending.append((kid, cursor, cursor + slice_width, depth + 1))
            cursor += slice_width
    # nodes unreachable from the root (malformed relation cycles) still get a
    # stable rim position instead of silently vanishing
    for node_id in ids:
        if node_id not in positions:
            positions[node_id] = _polar((_fnv(node_id) % 6283) / 1000.0, 0.45)
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
            # default tones keep the three edge roles distinguishable: cyan
            # structure, magenta causality flow, amber attention beam
            default_tone = {"skeleton": "base", "beam": "warn"}.get(style, "accent")
            edges.append({"from": source, "to": target, "style": style,
                          "tone": str(rule.get("tone") or default_tone)})
    return edges


_FOCUS_STALE_SEQS = 12


def _focus_target(relations: list[Mapping[str, Any]], latest_seq: int = 0) -> str:
    for relation in relations:
        if str(relation.get("type")) == "focused_on" and str(relation.get("from")) == "agent":
            # attention that hasn't been reinforced drifts home: a long quiet
            # stretch should not leave the cursor parked on a stale file
            if latest_seq and _int(relation.get("lastSeq"), 0) < latest_seq - _FOCUS_STALE_SEQS:
                return ""
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


def _touched_files_for_command(command_id: str, relations: list[Mapping[str, Any]]) -> set[str]:
    return {
        str(r.get("to"))
        for r in relations
        if str(r.get("type")) == "touched"
        and str(r.get("from")) == command_id
        and str(r.get("to")).startswith("file:")
    }


def _blank_grid_metrics() -> dict[str, float | str]:
    return {
        "activity": 0.0,
        "churn": 0.0,
        "edits": 0.0,
        "commands": 0.0,
        "checks": 0.0,
        "status": "",
    }


def _grid_cell(epoch_id: str, entity_id: str, metrics: Mapping[str, Any], *, pending: bool = False) -> dict[str, Any]:
    activity = max(0.0, float(metrics.get("activity") or 0.0))
    churn = max(0.0, float(metrics.get("churn") or 0.0))
    commands = max(0.0, float(metrics.get("commands") or 0.0))
    checks = max(0.0, float(metrics.get("checks") or 0.0))
    status = str(metrics.get("status") or "")
    return {
        "epoch": epoch_id,
        "entity": entity_id,
        "activity": round(activity, 4),
        "churn": round(churn, 4),
        "edits": round(max(0.0, float(metrics.get("edits") or 0.0)), 4),
        "commands": round(commands, 4),
        "checks": round(checks, 4),
        "height": round(min(1.0, max(activity / 4.0, churn, commands / 2.0, checks / 2.0, 0.08)), 4),
        "tone": _status_tone(status, "pending" if pending else ""),
        "pending": pending,
    }


def _grid_totals(cells: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    totals = {
        "activity": 0.0,
        "churn": 0.0,
        "edits": 0.0,
        "commands": 0.0,
        "checks": 0.0,
    }
    for cell in cells:
        for key in totals:
            value = cell.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += float(value)
    return {key: round(value, 4) for key, value in totals.items()}


def _grid_summary(
    epochs: Sequence[Mapping[str, Any]],
    pending: Sequence[Mapping[str, Any]],
    columns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    closed_cells = [
        cell
        for epoch in epochs
        for cell in _list(epoch.get("cells"))
        if isinstance(cell, Mapping)
    ]
    closed_totals = _grid_totals(closed_cells)
    pending_totals = _grid_totals(pending)
    active_entities = {
        str(cell.get("entity") or "")
        for cell in [*closed_cells, *pending]
        if isinstance(cell, Mapping) and cell.get("entity")
    }
    return {
        "columnCount": len(columns),
        "epochCount": len(epochs),
        "cellCount": len(closed_cells),
        "pendingCellCount": len(pending),
        "activeColumnCount": len(active_entities),
        "activity": round(closed_totals["activity"] + pending_totals["activity"], 4),
        "churn": round(closed_totals["churn"] + pending_totals["churn"], 4),
        "edits": round(closed_totals["edits"] + pending_totals["edits"], 4),
        "commands": round(closed_totals["commands"] + pending_totals["commands"], 4),
        "checks": round(closed_totals["checks"] + pending_totals["checks"], 4),
    }


def _thermal_targets(event: Mapping[str, Any], relations: list[Mapping[str, Any]], file_set: set[str]) -> set[str]:
    kind = str(event.get("kind") or "")
    entity_id = str(event.get("entity") or "")
    targets: set[str] = set()
    if entity_id.startswith("file:"):
        targets.add(entity_id)
    if entity_id.startswith("command:"):
        targets.update(_touched_files_for_command(entity_id, relations))
    if kind == "check_completed":
        targets.update(_blast_for_check(entity_id, relations))
    return targets & file_set


def _thermal_edit_delta(event: Mapping[str, Any], *, heat_gain: float, fallback_heat: float) -> float:
    churn = event.get("churnFraction")
    if isinstance(churn, (int, float)) and not isinstance(churn, bool):
        return min(1.35, max(0.08, float(churn) * heat_gain))
    return fallback_heat


def _thermal_visual_heat(raw_heat: float) -> float:
    return 1.0 - math.exp(-max(0.0, raw_heat) * 0.45)


def _thermal_summary(samples: Sequence[Mapping[str, Any]], heat: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hot = [
        item for item in heat
        if isinstance(item.get("rawHeat"), (int, float)) and not isinstance(item.get("rawHeat"), bool)
        and float(item.get("rawHeat") or 0) > 0
    ]
    sample_times = [
        _int(sample.get("ts"), 0)
        for sample in samples
        if _int(sample.get("ts"), 0) > 0
    ]
    return {
        "sampleCount": len(samples),
        "hotFileCount": len(hot),
        "maxHeat": round(max((float(item.get("heat") or 0.0) for item in heat), default=0.0), 4),
        "rawHeat": round(sum(float(item.get("rawHeat") or 0.0) for item in hot), 4),
        "quenchCount": sum(1 for sample in samples if bool(sample.get("quench"))),
        "shockCount": sum(1 for sample in samples if bool(sample.get("shock"))),
        "historyStartMs": min(sample_times) if sample_times else 0,
        "historyEndMs": max(sample_times) if sample_times else 0,
    }


def _file_group(file_id: str) -> str:
    path = file_id.removeprefix("file:")
    return path.split("/", 1)[0] if "/" in path else "."


def _epoch_label(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "epoch").replace("_", " ")
    category = str(event.get("category") or "")
    status = str(event.get("status") or "")
    subject = str(event.get("subject") or "")
    if kind == "command completed":
        return "cmd " + status if status else "cmd"
    if kind == "turn end":
        return "turn"
    label = subject or category or kind
    return _clip_label(label)


def _status_tone(status: str, kind: str) -> str:
    if status == "error":
        return "alarm"
    if status == "ok":
        return "good"
    if kind == "commit_created":
        return "warn"
    if kind == "pending":
        return "accent"
    return "base"


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


def _annotate_tree(nodes: Mapping[str, dict[str, Any]], relations: list[Mapping[str, Any]]) -> None:
    parent_of = {
        str(r.get("to")): str(r.get("from"))
        for r in relations
        if str(r.get("type")) == "contains"
    }
    for node_id, node in nodes.items():
        parent = parent_of.get(node_id)
        hops = 0
        while parent is not None and parent not in nodes and hops < 12:
            parent = parent_of.get(parent)
            hops += 1
        if parent in nodes and parent != node_id:
            node["parent"] = parent
        depth = 0
        cursor = node_id
        seen = {node_id}
        while cursor in parent_of and depth < 12:
            cursor = parent_of[cursor]
            if cursor in seen:
                break
            seen.add(cursor)
            depth += 1
        node["depth"] = depth


def _node_label(node_id: str, kind: str, attrs: Mapping[str, Any], label_rule: Mapping[str, Any]) -> str:
    if label_rule:
        raw = attrs.get(str(label_rule.get("attr") or ""))
        if isinstance(raw, str) and raw:
            return _clip_label(raw)
    # labels are literal: real casing, real underscores -- no restyling that
    # could make two distinct names render identically
    tail = node_id.rsplit("/", 1)[-1]
    tail = tail.split(":", 1)[-1] if "/" not in node_id else tail
    return _clip_label(tail or node_id)


def _clip_label(text: str) -> str:
    # visible truncation: a clipped label must not impersonate a shorter name
    return text if len(text) <= 18 else text[:17] + "…"


def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _default_size(kind: str) -> float:
    return {"dir": 0.45, "agent": 0.55, "repo": 0.6}.get(kind, 0.3)


def _kind_from_id(node_id: str) -> str:
    return node_id.split(":", 1)[0] if ":" in node_id else node_id


# --- renderer adapter -------------------------------------------------------------------


class ProjectionSceneRenderer:
    """Adapts the projection engine to the existing SceneRenderer protocol."""

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.engine = ProjectionEngine(spec)
        self._pending_spec: dict[str, Any] | None = None

    def redirect(self, spec: Mapping[str, Any] | None) -> None:
        """Queue a projection change (the director hook). Applied at the start
        of the next plan, under the pipeline lock, so a swap arriving mid-render
        can never produce a scene resolved from two different specs."""
        self._pending_spec = dict(spec or {})

    def reset(self) -> None:
        """Forget session state (positions, effects, event history) but keep
        the active spec -- used when a replay restarts from scratch."""
        self.engine = ProjectionEngine(self.engine.spec)

    def render(self, requests: Sequence[Any], _scene: SceneState) -> Any:
        return self._plan(requests, {"entities": [], "relations": [], "events": []}, "")

    def render_with_context(self, requests: Sequence[Any], _scene: SceneState, context: Any) -> Any:
        project = _dict(getattr(context, "project", {}))
        perception = _dict(project.get("perceptionModel"))
        return self._plan(requests, perception, str(project.get("name") or ""))

    def _plan(self, requests: Sequence[Any], perception: Mapping[str, Any], project_name: str) -> Any:
        from .rendering import RenderPlan, RenderStep  # adapter-local: avoids an import cycle

        pending, self._pending_spec = self._pending_spec, None
        if pending is not None:
            self.engine.redirect(pending)
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


def _float(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def _int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


def _event_ts_ms(event: Mapping[str, Any], fallback: int) -> int:
    timestamp = _int(event.get("ts"), 0)
    if timestamp:
        return timestamp
    return _int(event.get("timestampMs"), fallback)


__all__ = [
    "DEFAULT_PROJECTION",
    "PROJECTION_SCENE_ID",
    "PROJECTION_SCENE_SCHEMA",
    "PROJECTION_SCHEMA",
    "ProjectionEngine",
    "ProjectionSceneRenderer",
    "load_packaged_projection",
    "load_projection_spec",
]
