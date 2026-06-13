from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

SENSITIVE_TOKENS = ("api_key", "apikey", "token", "secret", "password", "credential")
ALLOWED_HARN_FILES = {".harn/settings.json", ".harn/extensions/gibson.py"}
BLOCKED_TRACKED_PARTS = {
    ".coverage",
    ".env",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "auth.json",
    "credentials.json",
    "test-artifacts",
}
KEY_LIKE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE|AWS|AZURE)_[A-Z0-9_]*KEY\s*=\s*[^\s]+"),
)


def test_project_harn_settings_select_extension() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / ".harn/settings.json").read_text(encoding="utf-8"))
    extension_paths = settings["extensions"]

    assert extension_paths == ["extensions/gibson.py"]
    assert (root / ".harn" / extension_paths[0]).exists()
    assert find_sensitive_keys(settings) == []


def test_project_extension_shim_exports_harn_default_entrypoint() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / ".harn/extensions/gibson.py"
    spec = importlib.util.spec_from_file_location("harn_gibson_project_extension", path)

    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.default.__name__ == "extension_factory"


def test_harn_package_manifests_point_to_package_extension_shim() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_json = json.loads((root / "package.json").read_text(encoding="utf-8"))
    extension_paths = ["extensions/gibson.py"]

    assert pyproject["tool"]["harn"]["extensions"] == extension_paths
    assert package_json["harn"]["extensions"] == extension_paths
    assert "harn-package" in pyproject["project"]["keywords"]
    assert "harn-package" in package_json["keywords"]
    assert (root / extension_paths[0]).exists()
    assert find_sensitive_keys(package_json) == []


def test_package_extension_shim_exports_harn_default_entrypoint() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "extensions/gibson.py"
    spec = importlib.util.spec_from_file_location("harn_gibson_package_extension", path)

    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.default.__name__ == "extension_factory"


def test_tracked_files_do_not_include_runtime_artifacts_or_key_like_values() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = tracked_files(root)

    blocked_paths = []
    key_like_matches = []
    for relative in tracked:
        path = Path(relative)
        if is_blocked_tracked_path(path):
            blocked_paths.append(relative)
        text = (root / relative).read_text(encoding="utf-8", errors="ignore")
        for pattern in KEY_LIKE_PATTERNS:
            if pattern.search(text):
                key_like_matches.append(f"{relative}: {pattern.pattern}")

    assert blocked_paths == []
    assert key_like_matches == []


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


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [line for line in result.stdout.splitlines() if line]


def is_blocked_tracked_path(path: Path) -> bool:
    normalized = path.as_posix()
    if normalized in ALLOWED_HARN_FILES:
        return False
    if normalized.startswith(".harn/"):
        return True
    return any(part in BLOCKED_TRACKED_PARTS for part in path.parts)
