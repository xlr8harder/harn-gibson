from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SENSITIVE_TOKENS = ("api_key", "apikey", "token", "secret", "password", "credential")


def test_project_harn_settings_select_codex_and_extension() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / ".harn/settings.json").read_text(encoding="utf-8"))

    assert settings["defaultProvider"] == "openai-codex"
    assert settings["defaultModel"] == "gpt-5.5"
    assert settings["defaultThinkingLevel"] == "high"
    assert settings["extensions"] == ["src/harn_gibson/extension.py"]
    assert (root / settings["extensions"][0]).exists()
    assert find_sensitive_keys(settings) == []


def find_sensitive_keys(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        matches = []
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(token in str(key).lower() for token in SENSITIVE_TOKENS):
                matches.append(path)
            matches.extend(find_sensitive_keys(child, path))
        return matches
    if isinstance(value, list):
        matches = []
        for index, child in enumerate(value):
            matches.extend(find_sensitive_keys(child, f"{prefix}[{index}]"))
        return matches
    return []
