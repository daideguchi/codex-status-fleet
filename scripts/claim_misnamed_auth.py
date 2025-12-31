#!/usr/bin/env python3
import argparse
import base64
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any


_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _extract_email_from_auth_json(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_codex_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("codex", "openai_codex", "openai")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy auth.json from misnamed dirs like accounts/acc_<email>/.codex/auth.json into canonical accounts/<label>/.codex/auth.json."
    )
    parser.add_argument("--config", default="accounts.json", help="Path to accounts.json (default: accounts.json)")
    parser.add_argument("--label", action="append", help="Only fix this label (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Show planned actions without writing files")
    parser.add_argument("--force", action="store_true", help="Overwrite existing auth.json (backup is created)")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (root_dir / config_path).resolve()
    accounts_dir = root_dir / "accounts"

    cfg = _read_json(config_path)
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("config.accounts must be a non-empty array")

    only = set([l for l in (args.label or []) if l])
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    changed = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        if only and label not in only:
            continue
        enabled = acc.get("enabled", True) is not False
        if not enabled:
            continue
        provider = (acc.get("provider") or "codex").strip().lower()
        if not _is_codex_provider(provider):
            continue

        expected_email = (acc.get("expected_email") or "").strip().lower() or None
        if not expected_email:
            continue

        dest = accounts_dir / label / ".codex" / "auth.json"
        if dest.is_file() and not args.force:
            continue

        candidates = [
            accounts_dir / f"acc_{expected_email}" / ".codex" / "auth.json",
            accounts_dir / expected_email / ".codex" / "auth.json",
        ]
        src = next((p for p in candidates if p.is_file()), None)
        if not src:
            continue

        actual = _extract_email_from_auth_json(src)
        if actual and actual != expected_email:
            print(f"==> {label}: skip (email mismatch) src={src.parent.parent.name} actual={actual} expected={expected_email}")
            continue

        print(f"==> {label}: copy {src} -> {dest}")
        if args.dry_run:
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and args.force:
            bak = dest.with_name(f"auth.json.bak.{timestamp}")
            shutil.copy2(dest, bak)
        shutil.copy2(src, dest)
        changed += 1

    if args.dry_run:
        print("dry-run: no changes applied")
    else:
        print(f"ok: updated {changed} auth.json files")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

