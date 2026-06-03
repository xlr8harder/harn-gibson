from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from harn_gibson import auth
from harn_gibson.auth import (
    default_codex_auth_path,
    default_harn_auth_path,
    harn_credential_from_codex_auth,
    import_codex_auth,
)


def fake_jwt(payload: dict[str, Any]) -> str:
    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def codex_auth(
    access: str | None = None,
    refresh: str = "refresh",
    account_id: str = "acct_fallback",
) -> dict[str, Any]:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access
            or fake_jwt(
                {
                    "exp": 123,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_claim"},
                }
            ),
            "refresh_token": refresh,
            "account_id": account_id,
        },
    }


def test_import_codex_auth_writes_harn_oauth_entry(tmp_path: Path) -> None:
    source = tmp_path / "codex" / "auth.json"
    target = tmp_path / "harn" / "auth.json"
    write_json(source, codex_auth())
    write_json(target, {"anthropic": {"type": "api_key", "key": "ANTHROPIC_API_KEY"}})

    result = import_codex_auth(source, target)

    assert result.ok is True
    assert result.available is True
    assert result.changed is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(data) == ["anthropic", "openai-codex"]
    assert data["openai-codex"]["type"] == "oauth"
    assert data["openai-codex"]["expires"] == 123000
    assert data["openai-codex"]["accountId"] == "acct_claim"
    assert oct(os.stat(target).st_mode & 0o777) == "0o600"

    repeated = import_codex_auth(source, target)
    assert repeated.ok is True
    assert repeated.available is True
    assert repeated.changed is False
    assert "already available" in repeated.message


def test_import_codex_auth_uses_env_harn_dir(tmp_path: Path) -> None:
    source = tmp_path / "codex.json"
    harn_dir = tmp_path / "agent"
    write_json(source, codex_auth())

    result = import_codex_auth(source, environ={"HARN_CODING_AGENT_DIR": str(harn_dir)})

    assert result.target_path == harn_dir / "auth.json"
    assert result.available is True
    assert json.loads((harn_dir / "auth.json").read_text(encoding="utf-8"))["openai-codex"]["refresh"] == "refresh"
    assert default_harn_auth_path({"HARN_CODING_AGENT_DIR": str(harn_dir)}) == harn_dir / "auth.json"


def test_import_codex_auth_failures(tmp_path: Path) -> None:
    missing = import_codex_auth(tmp_path / "missing.json", tmp_path / "target.json")
    assert missing.available is False
    assert "not found" in missing.message

    invalid_source = tmp_path / "invalid-source.json"
    invalid_source.write_text("{", encoding="utf-8")
    assert "Could not read Codex" in import_codex_auth(invalid_source, tmp_path / "target.json").message

    list_source = tmp_path / "list-source.json"
    list_source.write_text("[]", encoding="utf-8")
    assert "Could not read Codex" in import_codex_auth(list_source, tmp_path / "target.json").message

    no_tokens = tmp_path / "no-tokens.json"
    write_json(no_tokens, {"tokens": {}})
    assert "usable OAuth" in import_codex_auth(no_tokens, tmp_path / "target.json").message

    valid_source = tmp_path / "valid-source.json"
    write_json(valid_source, codex_auth())
    invalid_target = tmp_path / "invalid-target.json"
    invalid_target.write_text("{", encoding="utf-8")
    assert "Could not read harn" in import_codex_auth(valid_source, invalid_target).message


def test_import_codex_auth_write_failure(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    write_json(source, codex_auth())

    def broken_write(_path: Path, _payload: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auth, "_write_json_secure", broken_write)

    result = import_codex_auth(source, target)
    assert result.available is False
    assert "disk full" in result.message


def test_harn_credential_from_codex_auth_validation() -> None:
    assert harn_credential_from_codex_auth({}) is None
    assert harn_credential_from_codex_auth({"tokens": {"access_token": "", "refresh_token": "r"}}) is None
    assert harn_credential_from_codex_auth({"tokens": {"access_token": "a.b.c", "refresh_token": ""}}) is None
    assert harn_credential_from_codex_auth({"tokens": {"access_token": "not.jwt", "refresh_token": "r"}}) is None
    assert harn_credential_from_codex_auth({"tokens": {"access_token": "a.b.c", "refresh_token": "r"}}) is None

    no_account_claim = fake_jwt({"exp": 456})
    credential = harn_credential_from_codex_auth(codex_auth(no_account_claim, account_id="acct_token"))
    assert credential is not None
    assert credential["expires"] == 456000
    assert credential["accountId"] == "acct_token"

    no_account = harn_credential_from_codex_auth(codex_auth(no_account_claim, account_id=""))
    assert no_account is not None
    assert "accountId" not in no_account


def test_default_auth_paths() -> None:
    assert default_codex_auth_path().name == "auth.json"
    assert default_codex_auth_path().parent.name == ".codex"
    assert default_harn_auth_path().name == "auth.json"
    assert default_harn_auth_path().parent.name == "agent"
