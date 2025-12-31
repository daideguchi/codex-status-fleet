#!/usr/bin/env python3
import base64
import argparse
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_mtime(path: str) -> str:
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return "-"


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _extract_email_from_auth_json(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None

    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    id_token = id_token.strip()
    if not _JWT_RE.match(id_token):
        return None

    try:
        payload_raw = _b64url_decode(id_token.split(".")[1])
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    for key in ("email", "preferred_username", "upn", "unique_name"):
        value = payload.get(key)
        if isinstance(value, str):
            candidate = value.strip().lower()
            if _EMAIL_RE.match(candidate):
                return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Show which accounts have auth.json saved.")
    parser.add_argument("--config", required=True, help="Path to accounts.json")
    parser.add_argument(
        "--need-login",
        action="store_true",
        help="Print labels that need login (one per line) and exit.",
    )
    args = parser.parse_args()

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    accounts_root = os.path.join(root_dir, "accounts")

    cfg = _read_json(args.config)
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list):
        raise SystemExit("config.accounts must be an array")

    rows: list[dict[str, Any]] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        provider = (acc.get("provider") or "codex").strip().lower()

        expected_email = (acc.get("expected_email") or "").strip().lower() or None
        row: dict[str, Any] = {
            "label": label,
            "provider": provider,
            "enabled": acc.get("enabled", True) is not False,
            "expected_email": expected_email,
        }

        if provider in ("codex", "openai_codex", "openai"):
            auth_path = os.path.join(accounts_root, label, ".codex", "auth.json")
            actual_email = _extract_email_from_auth_json(auth_path) if os.path.isfile(auth_path) else None
            row.update(
                {
                    "actual_email": actual_email,
                    "expected_email_match": (actual_email == expected_email)
                    if (actual_email and expected_email)
                    else None,
                    "logged_in": os.path.isfile(auth_path),
                    "auth_mtime_utc": _fmt_mtime(auth_path) if os.path.isfile(auth_path) else None,
                }
            )
        elif provider in ("anthropic", "claude", "claude_api"):
            key_path = os.path.join(accounts_root, label, ".secrets", "anthropic_api_key.txt")
            row.update(
                {
                    "has_api_key": os.path.isfile(key_path),
                    "api_key_mtime_utc": _fmt_mtime(key_path) if os.path.isfile(key_path) else None,
                }
            )
        else:
            row["note"] = "unknown provider"

        rows.append(row)

    if args.need_login:
        for it in rows:
            if it.get("provider") == "codex" and it.get("enabled") and not it.get("logged_in"):
                print(it.get("label") or "")
        return 0

    print(json.dumps({"items": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
