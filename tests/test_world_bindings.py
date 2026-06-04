from __future__ import annotations

from pathlib import Path

from harn_gibson.world_bindings import (
    WORLD_BINDING_SCHEMA,
    WORLD_BINDINGS_PROP,
    normalize_world_bindings,
    world_binding,
    world_bindings_from_props,
)


def test_world_binding_helper_builds_minimal_and_optional_payloads() -> None:
    minimal = world_binding("customThing", "value", "blocks[0].h")
    detailed = world_binding(
        "file:src/app.py",
        "entities.files[].activityCount",
        "blocks[1].tone",
        entity_kind="file",
        target_id="repo-city",
        source="worldModel",
        relationship="highlights",
        intent="hot files glow",
        transform={
            "domain": [0, 10],
            "range": ["cyan", "magenta"],
            "nested": {"label": "x" * 200},
            "path": Path("src/app.py"),
            "enabled": True,
            "ratio": 0.5,
            "missing": None,
            "extra": "kept",
            "overflow": "clipped out",
        },
    )

    assert minimal == {
        "schema": WORLD_BINDING_SCHEMA,
        "entityId": "customThing",
        "entityKind": "unknown",
        "fieldPath": "value",
        "targetProp": "blocks[0].h",
    }
    assert detailed["targetId"] == "repo-city"
    assert detailed["source"] == "worldModel"
    assert detailed["relationship"] == "highlights"
    assert detailed["intent"] == "hot files glow"
    assert detailed["transform"]["domain"] == [0, 10]
    assert detailed["transform"]["range"] == ["cyan", "magenta"]
    assert detailed["transform"]["nested"]["label"].endswith("...")
    assert detailed["transform"]["path"] == "src/app.py"
    assert detailed["transform"]["enabled"] is True
    assert detailed["transform"]["ratio"] == 0.5
    assert detailed["transform"]["missing"] is None
    assert "overflow" not in detailed["transform"]


def test_normalize_world_bindings_accepts_mappings_sequences_and_legacy_keys() -> None:
    binding = {
        "entityId": "",
        "entity_id": "repo:src",
        "field_path": "entries[].visibleLineCount",
        "target_prop": "blocks[0].h",
        "target_id": "ignored-target",
        "source": "repoTopology",
        "relation": "scales",
        "reason": "height follows sampled line counts",
    }
    normalized = normalize_world_bindings(binding, target_id="repo-city")
    clipped = normalize_world_bindings(
        {
            "entityId": ":floating",
            "fieldPath": "abcdef",
            "targetProp": "blocks[0].label",
            "transform": {"long": "abcdef"},
        },
        max_text_chars=2,
    )

    assert normalized == (
        {
            "schema": WORLD_BINDING_SCHEMA,
            "entityId": "repo:src",
            "entityKind": "repo",
            "fieldPath": "entries[].visibleLineCount",
            "targetProp": "blocks[0].h",
            "targetId": "repo-city",
            "source": "repoTopology",
            "relationship": "scales",
            "intent": "height follows sampled line counts",
        },
    )
    assert clipped[0]["entityKind"] == "unknown"
    assert clipped[0]["fieldPath"] == "..."
    assert clipped[0]["transform"] == {"...": "..."}


def test_normalize_world_bindings_ignores_invalid_entries_and_stays_bounded() -> None:
    value = [
        "bad",
        {"entityId": "file:missing-target", "fieldPath": "activity"},
        {
            "entityId": "file:src/app.py",
            "fieldPath": "activityCount",
            "targetProp": "tone",
        },
        {
            "entityId": "file:tests/test_app.py",
            "fieldPath": "activityCount",
            "targetProp": "tone",
        },
    ]

    assert normalize_world_bindings("not-bindings") == ()
    assert normalize_world_bindings(value, max_bindings=0) == ()
    assert normalize_world_bindings(value, max_bindings=1) == (
        {
            "schema": WORLD_BINDING_SCHEMA,
            "entityId": "file:src/app.py",
            "entityKind": "file",
            "fieldPath": "activityCount",
            "targetProp": "tone",
        },
    )


def test_world_bindings_from_props_prefers_plural_and_accepts_singular() -> None:
    plural = {
        WORLD_BINDINGS_PROP: [
            {
                "entityId": "file:src/app.py",
                "fieldPath": "activityCount",
                "targetProp": "blocks[0].tone",
            }
        ],
        "worldBinding": {
            "entityId": "file:ignored.py",
            "fieldPath": "activityCount",
            "targetProp": "blocks[1].tone",
        },
    }
    singular = {
        "worldBinding": {
            "entityId": "command:1",
            "fieldPath": "status",
            "targetProp": "label",
        }
    }

    assert world_bindings_from_props({}, target_id="none") == ()
    assert world_bindings_from_props(plural, target_id="repo-city")[0]["entityId"] == "file:src/app.py"
    assert world_bindings_from_props(plural, target_id="repo-city")[0]["targetId"] == "repo-city"
    assert world_bindings_from_props(singular)[0]["entityKind"] == "command"
