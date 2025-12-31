import base64
import json
import os
import re
import select
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CLIENT_NAME = "codex-status-fleet-refresher"
CLIENT_VERSION = "0.1.0"

CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/accounts.json")
ACCOUNTS_DIR = os.getenv("ACCOUNTS_DIR", "/accounts")
COLLECTOR_URL = os.getenv("COLLECTOR_URL", "http://collector:8080/ingest")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
RPC_TIMEOUT_SEC = float(os.getenv("RPC_TIMEOUT_SEC", "10.0"))

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

app = FastAPI(title="Codex Status Refresher")

_refresh_cond = threading.Condition()
_refresh_running = False
_refresh_last_result: dict[str, Any] | None = None
_config_lock = threading.Lock()

REFRESH_JOIN_TIMEOUT_SEC = float(os.getenv("REFRESH_JOIN_TIMEOUT_SEC", "300"))


def _collector_base_url() -> str:
    url = COLLECTOR_URL.rstrip("/")
    if url.endswith("/ingest"):
        return url[: -len("/ingest")].rstrip("/")
    return url


def _push_registry_from_config() -> None:
    cfg = _read_json(Path(CONFIG_PATH))
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("config.accounts must be a non-empty array")

    payload_accounts: list[dict[str, Any]] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or acc.get("account_label") or "").strip()
        if not label:
            continue
        payload_accounts.append(
            {
                "account_label": label,
                "enabled": acc.get("enabled", True) is not False,
                "expected_email": (acc.get("expected_email") or "").strip() or None,
                "expected_planType": (
                    (acc.get("expected_planType") or acc.get("expected_plan_type") or "").strip()
                    or None
                ),
                "note": (acc.get("note") or "").strip() or None,
            }
        )

    if not payload_accounts:
        raise RuntimeError("No valid accounts found in config")

    base = _collector_base_url()
    url = f"{base}/registry?replace=true"
    _post_json(url, {"accounts": payload_accounts})


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


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


@dataclass(frozen=True)
class AccountConfig:
    label: str
    expected_email: str | None
    expected_plan_type: str | None
    enabled: bool


def _load_accounts(only_label: str | None, include_disabled: bool) -> list[AccountConfig]:
    cfg = _read_json(Path(CONFIG_PATH))
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("config.accounts must be a non-empty array")

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
        expected_plan_type = (
            (acc.get("expected_planType") or acc.get("expected_plan_type") or "").strip() or None
        )
        out.append(
            AccountConfig(
                label=label,
                expected_email=expected_email,
                expected_plan_type=expected_plan_type,
                enabled=enabled,
            )
        )

    if only_label and not out:
        raise RuntimeError(f"label not found (or disabled): {only_label}")
    return out


def _make_label_from_email(email: str) -> str:
    s = email.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return f"acc_{s}" if s else "acc_account"


def _extract_emails(text: str) -> list[str]:
    found = _EMAIL_FIND_RE.findall(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for e in found:
        e = e.strip().lower()
        if not e or e in seen:
            continue
        if not _EMAIL_RE.match(e):
            continue
        out.append(e)
        seen.add(e)
    return out


class AddAccountsPayload(BaseModel):
    text: str | None = None
    emails: list[str] = Field(default_factory=list)
    expected_planType: str | None = None
    enabled: bool = True


@app.post("/config/add_accounts")
def config_add_accounts(payload: AddAccountsPayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    emails: list[str] = []
    if payload.text:
        emails.extend(_extract_emails(payload.text))
    for e in payload.emails or []:
        if isinstance(e, str):
            emails.extend(_extract_emails(e))

    uniq: list[str] = []
    seen: set[str] = set()
    for e in emails:
        if e in seen:
            continue
        uniq.append(e)
        seen.add(e)

    if not uniq:
        raise HTTPException(status_code=400, detail="no emails found")

    # Avoid races with refresh and keep config writes serialized.
    with _config_lock:
        with _refresh_cond:
            if _refresh_running:
                ok = _refresh_cond.wait_for(
                    lambda: not _refresh_running, timeout=REFRESH_JOIN_TIMEOUT_SEC
                )
                if not ok:
                    raise HTTPException(status_code=409, detail="refresh already running")

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            cfg["accounts"] = accounts

        expected_plan = (payload.expected_planType or "").strip() or None

        existing_by_label: dict[str, dict[str, Any]] = {}
        existing_by_email: dict[str, dict[str, Any]] = {}
        for a in accounts:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or "").strip()
            if label:
                existing_by_label[label] = a
            exp = (a.get("expected_email") or "").strip().lower()
            if exp:
                existing_by_email[exp] = a

        added = 0
        updated = 0
        labels: list[str] = []
        for email in uniq:
            label = _make_label_from_email(email)
            labels.append(label)

            entry = existing_by_email.get(email) or existing_by_label.get(label)
            if entry is None:
                entry = {
                    "label": label,
                    "enabled": bool(payload.enabled),
                    "expected_email": email,
                }
                if expected_plan:
                    entry["expected_planType"] = expected_plan
                accounts.append(entry)
                existing_by_label[label] = entry
                existing_by_email[email] = entry
                added += 1
            else:
                changed = False
                if (entry.get("expected_email") or "").strip().lower() != email:
                    entry["expected_email"] = email
                    changed = True
                if expected_plan and (entry.get("expected_planType") or "").strip() != expected_plan:
                    entry["expected_planType"] = expected_plan
                    changed = True
                if bool(entry.get("enabled", True)) != bool(payload.enabled):
                    entry["enabled"] = bool(payload.enabled)
                    changed = True
                if changed:
                    updated += 1

            account_home = Path(ACCOUNTS_DIR) / label
            (account_home / ".codex").mkdir(parents=True, exist_ok=True)

        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "added": added, "updated": updated, "labels": sorted(set(labels))}


def _rpc_rate_limits(account_home: Path) -> tuple[dict[str, Any], str | None]:
    env = os.environ.copy()
    env["HOME"] = str(account_home)

    proc = subprocess.Popen(
        [CODEX_BIN, "app-server"],
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

        deadline = time.time() + RPC_TIMEOUT_SEC
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
                ua = (
                    msg.get("result", {}).get("userAgent")
                    if isinstance(msg.get("result"), dict)
                    else None
                )
                if isinstance(ua, str):
                    user_agent = ua
            if msg.get("id") == read_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                res = msg.get("result")
                result = res if isinstance(res, dict) else {"result": res}
                break

        if result is None:
            raise TimeoutError("timeout waiting for rate limits")
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


def _refresh_one(acc: AccountConfig) -> tuple[str, dict[str, Any]]:
    account_home = Path(ACCOUNTS_DIR) / acc.label
    account_home.mkdir(parents=True, exist_ok=True)
    (account_home / ".codex").mkdir(parents=True, exist_ok=True)
    auth_path = account_home / ".codex" / "auth.json"
    account_email = _extract_account_email_from_auth(auth_path) if auth_path.is_file() else None

    ts = _now_iso()
    try:
        rate_result, user_agent = _rpc_rate_limits(account_home)
        raw = json.dumps(rate_result, ensure_ascii=False, separators=(",", ":"))
        parsed: dict[str, Any] = {
            "userAgent": user_agent,
            "normalized": _normalize(rate_result, acc, account_email),
        }
        state = "ok"
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
    _post_json(COLLECTOR_URL, payload)
    return state, payload


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/refresh")
def refresh(label: str | None = None, include_disabled: bool = False):
    global _refresh_running, _refresh_last_result

    # If a refresh is already running, join it and return its result (no 409 for double-clicks).
    with _refresh_cond:
        if _refresh_running:
            ok = _refresh_cond.wait_for(lambda: not _refresh_running, timeout=REFRESH_JOIN_TIMEOUT_SEC)
            if not ok:
                raise HTTPException(status_code=409, detail="refresh already running")
            if _refresh_last_result is not None:
                joined = dict(_refresh_last_result)
                joined["joined"] = True
                return joined
            return {"ok": True, "joined": True}

        _refresh_running = True

    started_at = _now_iso()
    response: dict[str, Any] | None = None
    try:
        try:
            _push_registry_from_config()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise HTTPException(status_code=502, detail=f"collector /registry failed: HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise HTTPException(status_code=502, detail=f"collector /registry failed: {e}") from e

        try:
            accounts = _load_accounts(only_label=label, include_disabled=include_disabled)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if not accounts:
            raise HTTPException(status_code=400, detail="no accounts to refresh")

        results: list[dict[str, Any]] = []
        summary = {"ok": 0, "auth_required": 0, "errors": 0, "total": len(accounts)}
        for acc in accounts:
            try:
                state, payload = _refresh_one(acc)
                results.append({"label": acc.label, "state": state, "ts": payload.get("ts")})
                if state == "ok":
                    summary["ok"] += 1
                elif state == "auth required":
                    summary["auth_required"] += 1
                else:
                    summary["errors"] += 1
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                summary["errors"] += 1
                results.append(
                    {"label": acc.label, "state": "post_error", "error": f"HTTPError {e.code}: {body}"}
                )
            except urllib.error.URLError as e:
                summary["errors"] += 1
                results.append({"label": acc.label, "state": "post_error", "error": f"URLError: {e}"})
            except Exception as e:
                summary["errors"] += 1
                results.append({"label": acc.label, "state": "error", "error": f"{type(e).__name__}: {e}"})

        response = {
            "ok": summary["errors"] == 0,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "summary": summary,
            "results": results,
        }
        return response
    finally:
        with _refresh_cond:
            _refresh_last_result = response or {
                "ok": False,
                "started_at": started_at,
                "finished_at": _now_iso(),
                "error": "refresh failed",
            }
            _refresh_running = False
            _refresh_cond.notify_all()
