#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLIENT_NAME = "codex-status-fleet-manual"
CLIENT_VERSION = "0.1.0"

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_to_iso(epoch_s: int | None) -> str | None:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _extract_account_email_from_auth(auth_path: Path) -> str | None:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
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


def _derive_ingest_url(config: dict[str, Any], explicit_base_url: str | None) -> str:
    if explicit_base_url:
        return explicit_base_url.rstrip("/") + "/ingest"

    collector_in_compose = bool(config.get("collector_in_compose", True))
    collector_url = str(config.get("collector_url", "")).strip()

    if collector_in_compose:
        return "http://localhost:8080/ingest"

    if collector_url.endswith("/ingest"):
        return collector_url
    return collector_url.rstrip("/") + "/ingest"


@dataclass(frozen=True)
class AccountConfig:
    label: str
    expected_email: str | None
    expected_plan_type: str | None
    enabled: bool


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def _rpc_rate_limits(
    label: str, account_home: Path, timeout_sec: float
) -> tuple[dict[str, Any], str | None]:
    env = os.environ.copy()
    env["HOME"] = str(account_home)

    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    try:
        init_id = 1
        read_id = 2
        send(
            {
                "id": init_id,
                "method": "initialize",
                "params": {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}},
            }
        )
        send({"id": read_id, "method": "account/rateLimits/read", "params": None})

        user_agent: str | None = None
        result: dict[str, Any] | None = None

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == init_id:
                ua = msg.get("result", {}).get("userAgent") if isinstance(msg.get("result"), dict) else None
                if isinstance(ua, str):
                    user_agent = ua
            if msg.get("id") == read_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                res = msg.get("result")
                if isinstance(res, dict):
                    result = res
                else:
                    result = {"result": res}
                break

        if result is None:
            raise TimeoutError(f"timeout waiting for rate limits ({label})")
        return result, user_agent
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _normalize(rate_result: dict[str, Any], expected: AccountConfig, account_email: str | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if account_email:
        normalized["account_email"] = account_email

    expected_email_lc = expected.expected_email.lower() if expected.expected_email else None
    if expected_email_lc:
        normalized["expected_email"] = expected.expected_email
        normalized["expected_email_match"] = account_email == expected_email_lc if account_email else None

    if expected.expected_plan_type:
        normalized["expected_planType"] = expected.expected_plan_type
        normalized["expected_planType_match"] = None

    rate_limits = rate_result.get("rateLimits") if isinstance(rate_result, dict) else None
    if isinstance(rate_limits, dict):
        normalized["rate_planType"] = rate_limits.get("planType")
        normalized["credits"] = rate_limits.get("credits")

        if expected.expected_plan_type and isinstance(rate_limits.get("planType"), str):
            normalized["expected_planType_match"] = rate_limits.get("planType") == expected.expected_plan_type

        windows: dict[str, Any] = {}
        for source in ("primary", "secondary"):
            w = rate_limits.get(source)
            if not isinstance(w, dict):
                continue
            dur = w.get("windowDurationMins")
            used = w.get("usedPercent")
            resets_at = w.get("resetsAt")

            key = source
            if dur == 300:
                key = "5h"
            elif dur == 10080:
                key = "weekly"

            left = None
            if isinstance(used, int):
                left = max(0, min(100, 100 - used))

            windows[key] = {
                "source": source,
                "usedPercent": used,
                "leftPercent": left,
                "windowDurationMins": dur,
                "resetsAt": resets_at,
                "resetsAtIsoUtc": _epoch_to_iso(resets_at) if isinstance(resets_at, int) else None,
            }

        normalized["windows"] = windows

    return normalized


def _build_accounts(config: dict[str, Any], only_label: str | None, include_disabled: bool) -> list[AccountConfig]:
    accounts = config.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("config.accounts must be a non-empty array")

    out: list[AccountConfig] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        if only_label and label != only_label:
            continue

        enabled = acc.get("enabled", True) is not False
        if not enabled and not include_disabled:
            continue

        expected_email = (acc.get("expected_email") or "").strip() or None
        expected_plan_type = (acc.get("expected_planType") or acc.get("expected_plan_type") or "").strip() or None

        out.append(
            AccountConfig(
                label=label,
                expected_email=expected_email,
                expected_plan_type=expected_plan_type,
                enabled=enabled,
            )
        )

    if only_label and not out:
        raise SystemExit(f"label not found in config (or disabled): {only_label}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual refresh: fetch Codex rate limits and POST into Collector.")
    parser.add_argument("--config", default="accounts.json", help="Path to accounts.json")
    parser.add_argument("--base-url", default=None, help="Collector base URL (default: inferred)")
    parser.add_argument("--label", default=None, help="Refresh only this label")
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0, help="RPC timeout seconds per account")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = _read_json(config_path)
    ingest_url = _derive_ingest_url(cfg, args.base_url)

    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    accounts_dir = project_dir / "accounts"

    targets = _build_accounts(cfg, only_label=args.label, include_disabled=args.include_disabled)
    if not targets:
        raise SystemExit("No accounts to refresh")

    ok = 0
    auth_required = 0
    errors = 0

    for acc in targets:
        account_home = accounts_dir / acc.label
        auth_path = account_home / ".codex" / "auth.json"
        account_email = _extract_account_email_from_auth(auth_path) if auth_path.is_file() else None

        ts = _now_iso()
        try:
            rate_result, user_agent = _rpc_rate_limits(acc.label, account_home, timeout_sec=args.timeout)
            raw = json.dumps(rate_result, ensure_ascii=False, separators=(",", ":"))
            parsed: dict[str, Any] = {"userAgent": user_agent, "normalized": _normalize(rate_result, acc, account_email)}
            state = "ok"
            ok += 1
        except Exception as e:
            raw = f"[probe_error] {type(e).__name__}: {e}"
            error_payload = None
            if getattr(e, "args", None):
                first = e.args[0]
                if isinstance(first, dict):
                    error_payload = first
            message = ""
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "")
            if not message:
                message = str(e)

            requires_auth = "authentication required" in message.lower()
            if requires_auth:
                auth_required += 1
            else:
                errors += 1

            parsed = {
                "probe_error": True,
                "error_type": type(e).__name__,
                "error": str(e),
                "error_payload": error_payload,
                "normalized": {
                    "account_email": account_email,
                    "expected_email": acc.expected_email,
                    "expected_email_match": (
                        account_email == acc.expected_email.lower()
                        if (account_email and acc.expected_email)
                        else None
                    ),
                    "expected_planType": acc.expected_plan_type,
                    "requiresOpenaiAuth": requires_auth,
                },
            }
            state = "auth required" if requires_auth else "error"

        payload = {
            "account_label": acc.label,
            "host": socket.gethostname(),
            "raw": raw,
            "parsed": parsed,
            "ts": ts,
        }

        if args.dry_run:
            print(json.dumps({"label": acc.label, "state": state, "payload": payload}, ensure_ascii=False))
            continue

        try:
            _post_json(ingest_url, payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[post_error] {acc.label}: HTTPError {e.code}: {body}", file=sys.stderr)
            errors += 1
        except urllib.error.URLError as e:
            print(f"[post_error] {acc.label}: URLError: {e}", file=sys.stderr)
            errors += 1

        print(f"{acc.label}: {state}")

    summary = {"ok": ok, "auth_required": auth_required, "errors": errors, "total": len(targets)}
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

