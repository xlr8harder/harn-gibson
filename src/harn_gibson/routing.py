"""Event routing and coalescing contracts before renderer execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from harn_gibson.events import EventPhase, GibsonEvent
from harn_gibson.rendering import RenderRequest
from harn_gibson.scene import SceneAnimation, SceneMutation, ScenePrimitive, default_mutations_for_event

RouteKind = Literal["renderer_agent", "direct_scene", "stream_buffer", "debug_only", "drop"]
RuleRouteKind = Literal["renderer_agent", "direct_scene", "debug_only", "drop"]
RendererFallbackRoute = Literal["direct_scene", "debug_only", "drop"]


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    start_ms: int
    end_ms: int

    @classmethod
    def from_events(cls, events: Sequence[GibsonEvent]) -> TimelineWindow:
        if not events:
            return cls(0, 0)
        timestamps = [event.timestamp_ms for event in events]
        return cls(min(timestamps), max(timestamps))

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def offset_for(self, event: GibsonEvent) -> int:
        return max(0, event.timestamp_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class StreamBinding:
    event_type: str
    stream_id: str
    target_id: str
    title: str
    region: str = "stage"
    kind: str = "text_stream"
    flush_ms: int = 100
    summary_every_ms: int = 1500
    max_chars: int = 4000

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventType": self.event_type,
            "streamId": self.stream_id,
            "targetId": self.target_id,
            "title": self.title,
            "region": self.region,
            "kind": self.kind,
            "flushMs": self.flush_ms,
            "summaryEveryMs": self.summary_every_ms,
            "maxChars": self.max_chars,
        }


@dataclass(slots=True)
class StreamBuffer:
    binding: StreamBinding
    text: str = ""
    update_count: int = 0
    started_at_ms: int | None = None
    updated_at_ms: int | None = None

    def append(self, text: str, timestamp_ms: int) -> None:
        if self.started_at_ms is None:
            self.started_at_ms = timestamp_ms
        self.updated_at_ms = timestamp_ms
        self.update_count += 1
        self.text = _clip_stream_text(f"{self.text}{text}", self.binding.max_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "streamId": self.binding.stream_id,
            "targetId": self.binding.target_id,
            "title": self.binding.title,
            "text": self.text,
            "updateCount": self.update_count,
            "startedAtMs": self.started_at_ms,
            "updatedAtMs": self.updated_at_ms,
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: RouteKind
    reason: str
    renderer_visible: bool = True
    stream_id: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route": self.route,
            "reason": self.reason,
            "rendererVisible": self.renderer_visible,
        }
        if self.stream_id is not None:
            payload["streamId"] = self.stream_id
        if self.target_id is not None:
            payload["targetId"] = self.target_id
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True, slots=True)
class EventRouteRule:
    event_type: str
    route: RuleRouteKind
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EventRouteRule:
        event_type = value.get("eventType", value.get("event_type"))
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event route rule must include eventType")
        route = str(value.get("route") or "renderer_agent")
        if route not in {"renderer_agent", "direct_scene", "debug_only", "drop"}:
            raise ValueError(f"unsupported event route rule route: {route}")
        return cls(
            event_type=event_type,
            route=route,  # type: ignore[arg-type]
            reason=str(value.get("reason") or f"{route} route rule"),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "eventType": self.event_type,
            "route": self.route,
            "reason": self.reason,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True, slots=True)
class RendererEventInterest:
    """Renderer-advertised event subscription before default renderer routing."""

    event_types: tuple[str, ...] = ()
    phases: tuple[EventPhase, ...] = ()
    exclude_event_types: tuple[str, ...] = ()
    fallback_route: RendererFallbackRoute = "direct_scene"
    reason: str = "renderer not interested"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RendererEventInterest:
        fallback_route = str(value.get("fallbackRoute", value.get("fallback_route", "direct_scene")))
        if fallback_route not in {"direct_scene", "debug_only", "drop"}:
            raise ValueError(f"unsupported renderer interest fallback route: {fallback_route}")
        phases = _phase_tuple(value.get("phases", ()))
        return cls(
            event_types=_string_tuple(value.get("eventTypes", value.get("event_types", ()))),
            phases=phases,
            exclude_event_types=_string_tuple(
                value.get("excludeEventTypes", value.get("exclude_event_types", ()))
            ),
            fallback_route=fallback_route,  # type: ignore[arg-type]
            reason=str(value.get("reason") or "renderer not interested"),
            metadata=dict(value.get("metadata") or {}),
        )

    def wants(self, event: GibsonEvent) -> bool:
        if event.event_type in self.exclude_event_types:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        return not (self.phases and event.phase not in self.phases)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fallbackRoute": self.fallback_route,
            "reason": self.reason,
        }
        if self.event_types:
            payload["eventTypes"] = list(self.event_types)
        if self.phases:
            payload["phases"] = list(self.phases)
        if self.exclude_event_types:
            payload["excludeEventTypes"] = list(self.exclude_event_types)
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True, slots=True)
class RenderInputBatch:
    requests: tuple[RenderRequest, ...]
    timeline: TimelineWindow
    route: RouteKind = "renderer_agent"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_requests(
        cls,
        requests: Sequence[RenderRequest],
        *,
        route: RouteKind = "renderer_agent",
        metadata: Mapping[str, Any] | None = None,
    ) -> RenderInputBatch:
        window = TimelineWindow.from_events([request.event for request in requests])
        adjusted = tuple(
            RenderRequest(
                event=request.event,
                decisions=request.decisions,
                route=request.route,
                timeline_offset_ms=window.offset_for(request.event),
                coalesced_count=request.coalesced_count,
                metadata=request.metadata,
            )
            for request in requests
        )
        return cls(adjusted, window, route, dict(metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "harn-gibson.render-input.v1",
            "route": self.route,
            "timeline": self.timeline.to_dict(),
            "requests": [request.to_dict() for request in self.requests],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RouteResult:
    decision: RouteDecision
    request: RenderRequest
    batch: RenderInputBatch
    direct_mutations: tuple[SceneMutation, ...] = ()

    @property
    def uses_renderer(self) -> bool:
        return self.decision.renderer_visible and self.decision.route == "renderer_agent"

    @property
    def dropped(self) -> bool:
        return self.decision.route == "drop"


class EventRouter:
    """Routes events to renderer-agent requests or local scene updates."""

    def __init__(
        self,
        stream_bindings: Sequence[StreamBinding] | None = None,
        route_rules: Sequence[EventRouteRule] | None = None,
        renderer_interest: RendererEventInterest | None = None,
    ) -> None:
        bindings = stream_bindings or default_stream_bindings()
        self.stream_bindings = {binding.event_type: binding for binding in bindings}
        self.route_rules = {rule.event_type: rule for rule in route_rules or ()}
        self.renderer_interest = renderer_interest
        self.stream_buffers: dict[str, StreamBuffer] = {}

    def route(self, event: GibsonEvent, decisions: Sequence[dict[str, Any]] = ()) -> RouteResult:
        rule = self.route_rules.get(event.event_type)
        if rule is not None:
            return self._rule_result(event, decisions, rule)
        binding = self.stream_bindings.get(event.event_type)
        if binding is not None:
            text = stream_text_for_event(event)
            if text:
                return self._stream_result(event, decisions, binding, text)
            return self._debug_result(event, decisions, "stream update without text")
        if self.renderer_interest is not None and not self.renderer_interest.wants(event):
            return self._interest_fallback_result(event, decisions, self.renderer_interest)
        return self._renderer_result(event, decisions, "default renderer route")

    def stream_snapshot(self) -> dict[str, Any]:
        return {
            stream_id: buffer.to_dict()
            for stream_id, buffer in sorted(self.stream_buffers.items())
        }

    def _renderer_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        reason: str,
    ) -> RouteResult:
        decision = RouteDecision(route="renderer_agent", reason=reason)
        request = RenderRequest(event, tuple(decisions), metadata={"route": decision.to_dict()})
        batch = RenderInputBatch.from_requests((request,), metadata={"route": decision.to_dict()})
        return RouteResult(decision, batch.requests[0], batch)

    def _rule_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        rule: EventRouteRule,
    ) -> RouteResult:
        if rule.route == "renderer_agent":
            return self._renderer_result(event, decisions, rule.reason)
        return self._local_route_result(
            event,
            decisions,
            route=rule.route,
            reason=rule.reason,
            metadata={"rule": rule.to_dict()},
        )

    def _interest_fallback_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        interest: RendererEventInterest,
    ) -> RouteResult:
        return self._local_route_result(
            event,
            decisions,
            route=interest.fallback_route,
            reason=interest.reason,
            metadata={"rendererInterest": interest.to_dict()},
        )

    def _local_route_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        *,
        route: RendererFallbackRoute,
        reason: str,
        metadata: dict[str, Any],
    ) -> RouteResult:
        if route == "direct_scene":
            return self._direct_result(
                event,
                decisions,
                RouteDecision(
                    route="direct_scene",
                    reason=reason,
                    renderer_visible=False,
                    metadata=metadata,
                ),
                tuple(default_mutations_for_event(event, decisions)),
            )
        if route == "debug_only":
            return self._direct_result(
                event,
                decisions,
                RouteDecision(
                    route="debug_only",
                    reason=reason,
                    renderer_visible=False,
                    metadata=metadata,
                ),
                (),
            )
        return self._direct_result(
            event,
            decisions,
            RouteDecision(
                route="drop",
                reason=reason,
                renderer_visible=False,
                metadata=metadata,
            ),
            (),
        )

    def _direct_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        decision: RouteDecision,
        mutations: tuple[SceneMutation, ...],
    ) -> RouteResult:
        request = RenderRequest(event, tuple(decisions), route=decision.route, metadata={"route": decision.to_dict()})
        batch = RenderInputBatch.from_requests((request,), route=decision.route, metadata={"route": decision.to_dict()})
        return RouteResult(decision, batch.requests[0], batch, mutations)

    def _debug_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        reason: str,
    ) -> RouteResult:
        decision = RouteDecision(route="debug_only", reason=reason, renderer_visible=False)
        request = RenderRequest(event, tuple(decisions), route="debug_only", metadata={"route": decision.to_dict()})
        batch = RenderInputBatch.from_requests((request,), route="debug_only", metadata={"route": decision.to_dict()})
        return RouteResult(decision, batch.requests[0], batch, ())

    def _stream_result(
        self,
        event: GibsonEvent,
        decisions: Sequence[dict[str, Any]],
        binding: StreamBinding,
        text: str,
    ) -> RouteResult:
        buffer = self.stream_buffers.setdefault(binding.stream_id, StreamBuffer(binding))
        buffer.append(text, event.timestamp_ms)
        decision = RouteDecision(
            route="stream_buffer",
            reason="append streaming text locally",
            renderer_visible=False,
            stream_id=binding.stream_id,
            target_id=binding.target_id,
            metadata={"binding": binding.to_dict()},
        )
        request = RenderRequest(
            event,
            tuple(decisions),
            route="stream_buffer",
            metadata={"route": decision.to_dict(), "stream": buffer.to_dict()},
        )
        batch = RenderInputBatch.from_requests(
            (request,),
            route="stream_buffer",
            metadata={"route": decision.to_dict(), "stream": buffer.to_dict()},
        )
        return RouteResult(decision, batch.requests[0], batch, tuple(stream_buffer_mutations(event, buffer)))


def default_stream_bindings() -> tuple[StreamBinding, ...]:
    return (
        StreamBinding(
            event_type="message_update",
            stream_id="assistant-main",
            target_id="assistant-stream",
            title="Assistant stream",
        ),
    )


def stream_text_for_event(event: GibsonEvent) -> str:
    payload = event.payload
    nested = payload.get("assistantMessageEvent")
    if isinstance(nested, Mapping):
        text = _first_text_field(nested)
        if text:
            return text
    return _first_text_field(payload)


def stream_buffer_mutations(event: GibsonEvent, buffer: StreamBuffer) -> list[SceneMutation]:
    binding = buffer.binding
    stream = buffer.to_dict()
    return [
        SceneMutation(
            op="upsert",
            primitive=ScenePrimitive(
                id=binding.target_id,
                kind=binding.kind,
                region=binding.region,
                props={
                    **stream,
                    "isStreaming": True,
                    "flushMs": binding.flush_ms,
                    "summaryEveryMs": binding.summary_every_ms,
                    "maxChars": binding.max_chars,
                },
            ),
        ),
        SceneMutation(
            op="patch",
            target_id="status",
            props={"text": f"stream:{binding.stream_id}", "phase": event.phase, "tone": "cyan"},
        ),
        SceneMutation(
            op="start_animation",
            animation=SceneAnimation(
                id=f"stream-pulse-{event.sequence}",
                target_id="scan-grid",
                kind="stream-pulse",
                started_at_ms=event.timestamp_ms,
                duration_ms=600,
                props={"streamId": binding.stream_id, "sequence": event.sequence},
            ),
        ),
    ]


def _first_text_field(value: Mapping[str, Any]) -> str:
    for key in ("delta", "text", "content"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
        if isinstance(item, Sequence) and not isinstance(item, str | bytes):
            parts = []
            for child in item:
                if isinstance(child, Mapping):
                    text = child.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            if parts:
                return "".join(parts)
    return ""


def _clip_stream_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-max(0, limit - 3) :]


def renderer_event_interest_from_renderer(renderer: object) -> RendererEventInterest | None:
    value = getattr(renderer, "event_interest", None)
    if value is None:
        return None
    if isinstance(value, RendererEventInterest):
        return value
    if isinstance(value, Mapping):
        return RendererEventInterest.from_mapping(value)
    if callable(value):
        return renderer_event_interest_from_value(value())
    raise ValueError("renderer event_interest must be RendererEventInterest, mapping, callable, or None")


def event_route_rules_from_value(value: object) -> tuple[EventRouteRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("event route rules value must be a list")
    rules = []
    for item in value:
        if isinstance(item, EventRouteRule):
            rules.append(item)
        elif isinstance(item, Mapping):
            rules.append(EventRouteRule.from_mapping(item))
        else:
            raise ValueError("event route rule must be an object")
    return tuple(rules)


def renderer_event_interest_from_value(value: object) -> RendererEventInterest | None:
    if value is None:
        return None
    if isinstance(value, RendererEventInterest):
        return value
    if isinstance(value, Mapping):
        return RendererEventInterest.from_mapping(value)
    raise ValueError("renderer interest value must be RendererEventInterest, mapping, or None")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(str(item) for item in value)


def _phase_tuple(value: object) -> tuple[EventPhase, ...]:
    phases = _string_tuple(value)
    invalid = [phase for phase in phases if phase not in {"before", "during", "after", "lifecycle"}]
    if invalid:
        raise ValueError(f"unsupported renderer interest phase: {invalid[0]}")
    return phases  # type: ignore[return-value]


__all__ = [
    "EventRouteRule",
    "EventRouter",
    "RenderInputBatch",
    "RendererEventInterest",
    "RendererFallbackRoute",
    "RouteDecision",
    "RouteKind",
    "RouteResult",
    "RuleRouteKind",
    "StreamBinding",
    "StreamBuffer",
    "TimelineWindow",
    "default_stream_bindings",
    "event_route_rules_from_value",
    "renderer_event_interest_from_renderer",
    "renderer_event_interest_from_value",
    "stream_buffer_mutations",
    "stream_text_for_event",
]
