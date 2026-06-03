"""Credential import helpers for dogfooding with harn."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_PROVIDER_ID = "openai-codex"
CODEX_AUTH_CLAIM = "https://api.openai.com/auth"


@dataclass(frozen=True, slots=True)
class CodexAuthImportResult:
    ok: bool
    available: bool
    changed: bool
    source_path: Path
    target_path: Path
    message: str


def import_codex_auth(
    source_path: str | Path | None = None,
    target_path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> CodexAuthImportResult:
    source = Path(source_path).expanduser() if source_path is not None else default_codex_auth_path()
    target = Path(target_path).expanduser() if target_path is not None else default_harn_auth_path(environ)
    try:
        codex_auth = _read_json_object(source)
    except FileNotFoundError:
        return _result(False, False, False, source, target, f"Codex auth file not found: {source}")
    except (OSError, json.JSONDecodeError) as error:
        return _result(False, False, False, source, target, f"Could not read Codex auth file: {error}")

    credential = harn_credential_from_codex_auth(codex_auth)
    if credential is None:
        return _result(False, False, False, source, target, "Codex auth file does not contain usable OAuth tokens")

    try:
        existing = _read_json_object(target) if target.exists() else {}
    except (OSError, json.JSONDecodeError) as error:
        return _result(False, False, False, source, target, f"Could not read harn auth file: {error}")

    current = existing.get(CODEX_PROVIDER_ID)
    if current == credential:
        return _result(True, True, False, source, target, f"harn Codex credentials already available in {target}")

    merged = dict(existing)
    merged[CODEX_PROVIDER_ID] = credential
    try:
        _write_json_secure(target, merged)
    except OSError as error:
        return _result(False, False, False, source, target, f"Could not write harn auth file: {error}")
    return _result(True, True, True, source, target, f"Imported Codex OAuth credentials into {target}")


def harn_credential_from_codex_auth(data: dict[str, Any]) -> dict[str, Any] | None:
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    expires = _jwt_expiry_ms(access)
    if expires is None:
        return None
    credential: dict[str, Any] = {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": expires,
    }
    account_id = _account_id_from_access_token(access) or tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        credential["accountId"] = account_id
    return credential


def default_codex_auth_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def default_harn_auth_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    agent_dir = env.get("HARN_CODING_AGENT_DIR")
    if agent_dir:
        return Path(agent_dir).expanduser() / "auth.json"
    return Path.home() / ".harn" / "agent" / "auth.json"


def _result(
    ok: bool,
    available: bool,
    changed: bool,
    source_path: Path,
    target_path: Path,
    message: str,
) -> CodexAuthImportResult:
    return CodexAuthImportResult(
        ok=ok,
        available=available,
        changed=changed,
        source_path=source_path,
        target_path=target_path,
        message=message,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("JSON value must be an object", path.read_text(encoding="utf-8"), 0)
    return payload


def _write_json_secure(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


def _jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_expiry_ms(token: str) -> int | None:
    payload = _jwt_payload(token)
    expires = payload.get("exp") if payload else None
    if not isinstance(expires, int | float):
        return None
    return int(expires * 1000)


def _account_id_from_access_token(token: str) -> str | None:
    payload = _jwt_payload(token)
    auth_claim = payload.get(CODEX_AUTH_CLAIM) if payload else None
    if not isinstance(auth_claim, dict):
        return None
    account_id = auth_claim.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


__all__ = [
    "CODEX_PROVIDER_ID",
    "CodexAuthImportResult",
    "default_codex_auth_path",
    "default_harn_auth_path",
    "harn_credential_from_codex_auth",
    "import_codex_auth",
]
