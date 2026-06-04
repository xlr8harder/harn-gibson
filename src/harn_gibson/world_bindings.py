"""Bindings between durable project facts and scene primitive properties."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

WORLD_BINDING_SCHEMA = "harn-gibson.world-binding.v1"
WORLD_BINDINGS_PROP = "worldBindings"

_SEQUENCE_SCALAR_TYPES = (str, bytes, bytearray)
_OPTIONAL_TEXT_FIELDS = (
    ("source", ("source",)),
    ("relationship", ("relationship", "relation")),
    ("intent", ("intent", "reason")),
)


def world_binding(
    entity_id: str,
    field_path: str,
    target_prop: str,
    *,
    entity_kind: str | None = None,
    target_id: str | None = None,
    source: str | None = None,
    relationship: str | None = None,
    intent: str | None = None,
    transform: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a normalized visual binding payload for renderer-authored props."""

    payload: dict[str, Any] = {
        "schema": WORLD_BINDING_SCHEMA,
        "entityId": str(entity_id),
        "entityKind": str(entity_kind or _entity_kind_from_id(str(entity_id)) or "unknown"),
        "fieldPath": str(field_path),
        "targetProp": str(target_prop),
    }
    if target_id:
        payload["targetId"] = str(target_id)
    if source:
        payload["source"] = str(source)
    if relationship:
        payload["relationship"] = str(relationship)
    if intent:
        payload["intent"] = str(intent)
    if transform:
        payload["transform"] = _preview_mapping(transform, 160)
    return payload


def world_bindings_from_props(
    props: Mapping[str, Any],
    *,
    target_id: str | None = None,
    max_bindings: int = 8,
    max_text_chars: int = 160,
) -> tuple[dict[str, Any], ...]:
    """Extract a bounded, normalized `worldBindings` list from primitive props."""

    bindings = props.get(WORLD_BINDINGS_PROP, props.get("worldBinding"))
    return normalize_world_bindings(
        bindings,
        target_id=target_id,
        max_bindings=max_bindings,
        max_text_chars=max_text_chars,
    )


def normalize_world_bindings(
    value: Any,
    *,
    target_id: str | None = None,
    max_bindings: int = 8,
    max_text_chars: int = 160,
) -> tuple[dict[str, Any], ...]:
    """Normalize model- or renderer-authored binding metadata into a compact contract."""

    if isinstance(value, Mapping):
        raw_items: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, _SEQUENCE_SCALAR_TYPES):
        raw_items = value
    else:
        return ()

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if len(normalized) >= max(0, max_bindings):
            break
        if not isinstance(item, Mapping):
            continue
        binding = _binding_from_mapping(item, target_id=target_id, max_text_chars=max_text_chars)
        if binding:
            normalized.append(binding)
    return tuple(normalized)


def _binding_from_mapping(
    item: Mapping[str, Any],
    *,
    target_id: str | None,
    max_text_chars: int,
) -> dict[str, Any] | None:
    entity_id = _text_field(item, ("entityId", "entity_id"), max_text_chars)
    field_path = _text_field(item, ("fieldPath", "field_path"), max_text_chars)
    target_prop = _text_field(item, ("targetProp", "target_prop"), max_text_chars)
    if not entity_id or not field_path or not target_prop:
        return None

    entity_kind = _text_field(item, ("entityKind", "entity_kind"), max_text_chars)
    payload: dict[str, Any] = {
        "schema": WORLD_BINDING_SCHEMA,
        "entityId": entity_id,
        "entityKind": entity_kind or _entity_kind_from_id(entity_id) or "unknown",
        "fieldPath": field_path,
        "targetProp": target_prop,
    }
    binding_target_id = _clip_text(str(target_id), max_text_chars) if target_id else None
    binding_target_id = binding_target_id or _text_field(item, ("targetId", "target_id"), max_text_chars)
    if binding_target_id:
        payload["targetId"] = binding_target_id
    for output_key, input_keys in _OPTIONAL_TEXT_FIELDS:
        value = _text_field(item, input_keys, max_text_chars)
        if value:
            payload[output_key] = value
    transform = item.get("transform")
    if isinstance(transform, Mapping) and transform:
        payload["transform"] = _preview_mapping(transform, max_text_chars)
    return payload


def _text_field(item: Mapping[str, Any], keys: tuple[str, ...], max_text_chars: int) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = _clip_text(str(value), max_text_chars)
        if text:
            return text
    return None


def _entity_kind_from_id(entity_id: str) -> str | None:
    if ":" not in entity_id:
        return None
    prefix = entity_id.split(":", 1)[0].strip()
    return prefix or None


def _preview_mapping(value: Mapping[str, Any], max_text_chars: int) -> dict[str, Any]:
    return {
        _clip_text(str(key), max_text_chars): _preview_value(child, max_text_chars)
        for key, child in list(value.items())[:8]
    }


def _preview_value(value: Any, max_text_chars: int) -> Any:
    if isinstance(value, str):
        return _clip_text(value, max_text_chars)
    if type(value) in {bool, int, float} or value is None:
        return value
    if isinstance(value, Mapping):
        return _preview_mapping(value, max_text_chars)
    if isinstance(value, Sequence) and not isinstance(value, _SEQUENCE_SCALAR_TYPES):
        return [_preview_value(item, max_text_chars) for item in value[:4]]
    return _clip_text(str(value), max_text_chars)


def _clip_text(value: str, max_text_chars: int) -> str:
    limit = max(0, max_text_chars)
    return value if len(value) <= limit else f"{value[: max(0, limit - 3)]}..."


__all__ = [
    "WORLD_BINDING_SCHEMA",
    "WORLD_BINDINGS_PROP",
    "normalize_world_bindings",
    "world_binding",
    "world_bindings_from_props",
]
