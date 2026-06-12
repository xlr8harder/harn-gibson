"""Prompt fixtures for future model-backed renderers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

RENDERER_PROMPT_SCHEMA = "harn-gibson.renderer-prompt.v1"
RENDERER_PROMPT_MESSAGES_SCHEMA = "harn-gibson.renderer-prompt-messages.v1"

RENDERER_SYSTEM_PROMPT = (
    "You are the harn-gibson cinematic renderer.\n\n"
    "You receive one harn-gibson renderer context and must return exactly one JSON object matching "
    "harn-gibson.render-plan.v1.\n\n"
    "Use scene mutations to dramatize the current agent batch. Favor Hollywood Gibson visuals over utility, "
    "but keep output valid and bounded. Use existing scene target ids for patch/remove operations. Use upsert "
    "before patching new primitives. Use startOffsetMs, delayMs, timeline_cue, and route_trace animations when "
    "the renderInput timeline suggests a sequence. Use breach_wave beats, camera_jolt impacts, camera_path moves, "
    "camera targetRef anchors for named city blocks, spatial-map objects, route hops, graph nodes, ribbon points, "
    "or terrain peaks, "
    "and visualContinuity objectAnchors as stable targetRef candidates with compact opacity/lifecycle hints, "
    "signal_interference overlays, "
    "hologram projections, spatial_map object fields, signal scopes, tunnel grids, wire_landscape terrain, "
    "orbital maps, trace routes, "
    "terminal walls, access matrices, camera-drifting city_block districts, "
    "data rain, and structured svg_layer keyframes, path morphs, filters, or clips when they make the scene "
    "feel cinematic. "
    "Use context.project.perceptionModel as the primary world map: project its entities (file/dir/command/check/"
    "commit/agent) and relations (contains/touched/produced/focused_on) into layouts, derive position from the "
    "contains tree or another real relation, and drive transient beats from its recent events "
    "(file_changed/command_completed/check_completed/commit_created). "
    "If context.project.semanticGraph is present and available, it may add import/test edges, but never make it "
    "load-bearing. "
    "Use context.project.worldModel entity lifecycle fields to keep current/recent facts bright, let aging/stale "
    "facts fade, and distinguish open work from reconciled command/change/health facts. "
    "When a visual property follows a durable repo or world-model fact, attach props.worldBindings entries using "
    "the harn-gibson.world-binding.v1 schema so later turns can preserve that mapping. "
    "For one-shot or temporary animations, set ttlMs or expiresAtMs on the animation so stale effects are pruned "
    "from later visual-continuity context without requiring a separate stop_animation turn. "
    "Use only structured svg_layer data; never emit raw SVG markup, HTML, scripts, event handlers, "
    "foreignObject, or external references. If unsure, produce a small safe plan with status/log updates "
    "plus one visible primitive "
    "or animation.\n\n"
    "Return JSON only. Do not include markdown, commentary, or model-visible reasoning."
)


def renderer_prompt_messages(context: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Build provider-neutral system/user messages for a renderer context."""

    return (
        {"role": "system", "content": RENDERER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Render the current harn-gibson batch using this authoritative context.\n"
                "Live requests in renderInput are authoritative; do not invent or echo request objects in the plan.\n"
                "Return a harn-gibson.render-plan.v1 JSON object with steps and metadata.intent.\n\n"
                "Renderer context JSON:\n"
                f"{_json_block(context)}"
            ),
        },
    )


def renderer_prompt_from_context(
    context: Mapping[str, Any],
    *,
    context_index: int = 0,
) -> dict[str, Any]:
    """Build a reviewable prompt artifact for one renderer context."""

    messages = renderer_prompt_messages(context)
    message_chars = sum(len(message["content"]) for message in messages)
    return {
        "schema": RENDERER_PROMPT_SCHEMA,
        "contextIndex": context_index,
        "mode": str(context.get("mode") or "rolling"),
        "metadata": {
            **_context_metadata(context),
            "messageCount": len(messages),
            "messageChars": message_chars,
        },
        "messages": list(messages),
    }


def renderer_prompt_messages_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the provider-neutral message list for process/API adapters."""

    messages = renderer_prompt_messages(context)
    return {
        "schema": RENDERER_PROMPT_MESSAGES_SCHEMA,
        "messageCount": len(messages),
        "messages": list(messages),
    }


def _context_metadata(context: Mapping[str, Any]) -> dict[str, Any]:
    project = _mapping(context.get("project"))
    agent_attention = _mapping(project.get("agentAttention"))
    attention_action = _mapping(agent_attention.get("action"))
    attention_focus = _mapping(agent_attention.get("focus"))
    semantic_graph = _mapping(project.get("semanticGraph"))
    visual_continuity = _mapping(context.get("visualContinuity"))
    render_input = _mapping(context.get("renderInput"))
    timeline = _mapping(render_input.get("timeline"))
    requests = render_input.get("requests")
    request_items = (
        [request for request in requests if isinstance(request, Mapping)] if isinstance(requests, list) else []
    )
    event_types = []
    routes = []
    for request in request_items:
        event = _mapping(request.get("event"))
        _append_unique(event_types, str(event.get("eventType") or "unknown"))
        _append_unique(routes, str(request.get("route") or render_input.get("route") or "renderer_agent"))
    focus_paths = attention_focus.get("paths")
    return {
        "displayStyle": str(project.get("displayStyle") or "gibson"),
        "attentionAction": str(attention_action.get("kind") or "unknown"),
        "attentionFocusCount": len(focus_paths if isinstance(focus_paths, list) else []),
        "semanticGraphNodeCount": _coerce_int(semantic_graph.get("nodeCount"), 0),
        "semanticGraphEdgeCount": _coerce_int(semantic_graph.get("edgeCount"), 0),
        "perceptionEntityCount": _perception_entity_count(project),
        "perceptionEventCount": _coerce_int(
            _mapping(_mapping(project.get("perceptionModel")).get("counts")).get("events"), 0
        ),
        "eventTypes": event_types,
        "routes": routes,
        "timeline": {
            "startMs": _coerce_int(timeline.get("startMs"), 0),
            "endMs": _coerce_int(timeline.get("endMs"), 0),
            "durationMs": _coerce_int(timeline.get("durationMs"), 0),
        },
        "requestCount": len(request_items),
        "visualAnchorCount": _coerce_int(visual_continuity.get("anchorCount"), 0),
        "worldBindingCount": _coerce_int(visual_continuity.get("worldBindingCount"), 0),
        "activeAnimationCount": _coerce_int(visual_continuity.get("activeAnimationCount"), 0),
        "contextChars": len(_json_text(context)),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_block(value: Mapping[str, Any]) -> str:
    return f"```json\n{_json_text(value)}\n```"


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _perception_entity_count(project: Mapping[str, Any]) -> int:
    entities = _mapping(project.get("perceptionModel")).get("entities")
    return len(entities) if isinstance(entities, list) else 0


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _coerce_int(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "RENDERER_PROMPT_MESSAGES_SCHEMA",
    "RENDERER_PROMPT_SCHEMA",
    "RENDERER_SYSTEM_PROMPT",
    "renderer_prompt_from_context",
    "renderer_prompt_messages",
    "renderer_prompt_messages_payload",
]
