from __future__ import annotations

import base64
import json
import os
import re
import select
import shutil
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
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "20.0"))
FIREWORKS_BALANCE_TTL_SEC = float(os.getenv("FIREWORKS_BALANCE_TTL_SEC", "300"))

ANTHROPIC_API_URL = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
ANTHROPIC_MODEL_DEFAULT = os.getenv("ANTHROPIC_MODEL_DEFAULT", "claude-3-5-haiku-latest")
FIREWORKS_BASE_URL_DEFAULT = os.getenv(
    "FIREWORKS_BASE_URL_DEFAULT", "https://api.fireworks.ai/inference/v1"
).rstrip("/")

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_FIREWORKS_KEY_LINE_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_FIRECTL_BALANCE_RE = re.compile(r"(?im)^Balance:\s*([A-Z]{3})\s*([0-9]+(?:\.[0-9]+)?)\b")

app = FastAPI(title="Codex Status Refresher")

_refresh_cond = threading.Condition()
_refresh_running = False
_refresh_last_result: dict[str, Any] | None = None
_config_lock = threading.Lock()
_fireworks_balance_cache_lock = threading.Lock()
_fireworks_balance_cache: dict[str, dict[str, Any]] = {}

REFRESH_JOIN_TIMEOUT_SEC = float(os.getenv("REFRESH_JOIN_TIMEOUT_SEC", "300"))


def _fail_if_refresh_running() -> None:
    with _refresh_cond:
        if _refresh_running:
            raise HTTPException(
                status_code=409,
                detail="refresh in progress; please wait for it to finish and try again",
            )


def _collector_base_url() -> str:
    url = COLLECTOR_URL.rstrip("/")
    if url.endswith("/ingest"):
        return url[: -len("/ingest")].rstrip("/")
    return url


def _push_registry_from_config() -> None:
    cfg = _read_json(Path(CONFIG_PATH))
    accounts = cfg.get("accounts")
    if accounts is None:
        accounts = []
    if not isinstance(accounts, list):
        raise RuntimeError("config.accounts must be an array")

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
                "provider": (acc.get("provider") or "codex"),
                "expected_email": (acc.get("expected_email") or "").strip() or None,
                "expected_planType": (
                    (acc.get("expected_planType") or acc.get("expected_plan_type") or "").strip()
                    or None
                ),
                "note": (acc.get("note") or "").strip() or None,
            }
        )

    if accounts and not payload_accounts:
        raise RuntimeError("No valid accounts found in config")

    base = _collector_base_url()
    url = f"{base}/registry?replace=true"
    _post_json(url, {"accounts": payload_accounts})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_secret(value: str, keep_start: int = 10, keep_end: int = 6) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if keep_start < 0:
        keep_start = 0
    if keep_end < 0:
        keep_end = 0
    if len(s) <= keep_start + keep_end + 1:
        return s
    return s[:keep_start] + "…" + s[-keep_end:]


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
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(path)
    except OSError:
        # Docker Desktop bind-mounting a single file (./accounts.json:/config/accounts.json)
        # may fail os.replace() with EBUSY. Fall back to a direct overwrite.
        path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


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


def _read_text_file(path: Path) -> str | None:
    try:
        s = path.read_text(encoding="utf-8")
    except Exception:
        return None
    s = s.strip()
    return s or None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _parse_reset_header(value: str | None) -> tuple[int | None, str | None]:
    if not value:
        return None, None
    v = value.strip()
    if not v:
        return None, None

    # 1) int: could be epoch seconds, epoch ms, or seconds-until-reset.
    try:
        n = int(v)
    except Exception:
        n = None

    if isinstance(n, int):
        now_s = int(time.time())
        if n > 1_000_000_000_000:  # epoch ms
            epoch_s = n // 1000
        elif n > 1_000_000_000:  # epoch seconds
            epoch_s = n
        else:  # seconds from now
            epoch_s = now_s + n
        return epoch_s, _epoch_to_iso(epoch_s)

    # 2) ISO timestamp
    try:
        iso = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        epoch_s = int(dt_utc.timestamp())
        return epoch_s, dt_utc.isoformat()
    except Exception:
        return None, None


def _headers_to_dict(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for k, v in headers.items():
            if k and v is not None:
                out[str(k).lower()] = str(v)
    except Exception:
        pass
    return out


def _extract_error_message(body_text: str) -> str | None:
    s = (body_text or "").strip()
    if not s:
        return None
    if len(s) > 4000:
        s = s[:4000]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    return msg.strip() if isinstance(msg, str) and msg.strip() else None


def _anthropic_request_rate_limits(api_key: str, model: str) -> tuple[int, dict[str, str]]:
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "user-agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            # Don't store body; we only want headers for rate limit monitoring.
            _ = resp.read()
            return int(getattr(resp, "status", 200)), _headers_to_dict(resp.headers)
    except urllib.error.HTTPError as e:
        # Even on errors, rate limit headers may still be present.
        _ = e.read()
        return int(getattr(e, "code", 0) or 0), _headers_to_dict(e.headers)


def _parse_int_header(headers: dict[str, str], key: str) -> int | None:
    v = headers.get(key)
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _normalize_anthropic(
    http_status: int,
    headers: dict[str, str],
    expected: AccountConfig,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"provider": "anthropic"}

    if expected.expected_email:
        normalized["expected_email"] = expected.expected_email
        normalized["expected_email_match"] = None

    if expected.expected_plan_type:
        normalized["expected_planType"] = expected.expected_plan_type
        normalized["expected_planType_match"] = None

    # Treat missing/invalid api key as auth required.
    if http_status in (401, 403):
        normalized["requiresAuth"] = True

    # Anthropic rate limit headers (best-effort).
    req_limit = _parse_int_header(headers, "anthropic-ratelimit-requests-limit")
    req_rem = _parse_int_header(headers, "anthropic-ratelimit-requests-remaining")
    req_reset_raw = headers.get("anthropic-ratelimit-requests-reset")
    req_reset_epoch, req_reset_iso = _parse_reset_header(req_reset_raw)

    tok_limit = _parse_int_header(headers, "anthropic-ratelimit-tokens-limit")
    tok_rem = _parse_int_header(headers, "anthropic-ratelimit-tokens-remaining")
    tok_reset_raw = headers.get("anthropic-ratelimit-tokens-reset")
    tok_reset_epoch, tok_reset_iso = _parse_reset_header(tok_reset_raw)

    windows: dict[str, Any] = {}

    if isinstance(req_limit, int) and req_limit > 0:
        used = None
        left = None
        if isinstance(req_rem, int) and req_rem >= 0:
            left = int(max(0, min(100, round((req_rem / req_limit) * 100))))
            used = int(max(0, min(100, 100 - left)))
        windows["requests"] = {
            "source": "requests",
            "limit": req_limit,
            "remaining": req_rem,
            "usedPercent": used,
            "leftPercent": left,
            "resetsAt": req_reset_epoch,
            "resetsAtIsoUtc": req_reset_iso,
            "resetRaw": req_reset_raw,
        }

    if isinstance(tok_limit, int) and tok_limit > 0:
        used = None
        left = None
        if isinstance(tok_rem, int) and tok_rem >= 0:
            left = int(max(0, min(100, round((tok_rem / tok_limit) * 100))))
            used = int(max(0, min(100, 100 - left)))
        windows["tokens"] = {
            "source": "tokens",
            "limit": tok_limit,
            "remaining": tok_rem,
            "usedPercent": used,
            "leftPercent": left,
            "resetsAt": tok_reset_epoch,
            "resetsAtIsoUtc": tok_reset_iso,
            "resetRaw": tok_reset_raw,
        }

    normalized["windows"] = windows
    return normalized


def _fireworks_models_url(base_url: str | None) -> str:
    base = (base_url or FIREWORKS_BASE_URL_DEFAULT).strip().rstrip("/")
    if not base:
        base = FIREWORKS_BASE_URL_DEFAULT
    return f"{base}/models"


def _fireworks_request_rate_limits(
    api_key: str, base_url: str | None
) -> tuple[int, dict[str, str], str | None]:
    url = _fireworks_models_url(base_url)
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {api_key}",
            "user-agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            _ = resp.read()
            return int(getattr(resp, "status", 200)), _headers_to_dict(resp.headers), None
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode("utf-8", errors="replace")
        return int(getattr(e, "code", 0) or 0), _headers_to_dict(e.headers), body


def _fireworks_get_balance(api_key: str) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["firectl", "get", "account", "--api-key", api_key, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    matches = list(_FIRECTL_BALANCE_RE.finditer(combined))
    if not matches:
        if proc.returncode != 0:
            return {"error": "nonzero_exit"}
        return None

    m = matches[-1]
    currency = (m.group(1) or "").strip().upper() or None
    amount_raw = (m.group(2) or "").strip() or None
    amount: float | None = None
    if amount_raw:
        try:
            amount = float(amount_raw)
        except Exception:
            amount = None

    return {
        "currency": currency,
        "amount": amount,
        "amount_raw": amount_raw,
        "source": "firectl",
    }


def _fireworks_get_balance_cached(label: str, api_key: str) -> dict[str, Any] | None:
    ttl = FIREWORKS_BALANCE_TTL_SEC
    if ttl <= 0:
        return _fireworks_get_balance(api_key)

    now = time.time()
    with _fireworks_balance_cache_lock:
        cached = _fireworks_balance_cache.get(label)
        if isinstance(cached, dict):
            ts = cached.get("ts")
            value = cached.get("value")
            if isinstance(ts, (int, float)) and isinstance(value, dict) and (now - float(ts)) < ttl:
                return value

    value = _fireworks_get_balance(api_key)
    if value and isinstance(value, dict) and not value.get("error"):
        with _fireworks_balance_cache_lock:
            _fireworks_balance_cache[label] = {"ts": now, "value": value}
    return value


def _normalize_fireworks(
    http_status: int,
    headers: dict[str, str],
    expected: AccountConfig,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"provider": "fireworks"}

    if expected.expected_email:
        normalized["expected_email"] = expected.expected_email
        normalized["expected_email_match"] = None

    if expected.expected_plan_type:
        normalized["expected_planType"] = expected.expected_plan_type
        normalized["expected_planType_match"] = None

    if http_status in (401, 403):
        normalized["requiresAuth"] = True

    # Fireworks rate limit headers (best-effort).
    # Docs: https://docs.fireworks.ai/guides/quotas_usage/rate-limits
    req_limit = _parse_int_header(headers, "x-ratelimit-limit-requests")
    req_rem = _parse_int_header(headers, "x-ratelimit-remaining-requests")
    over_limit_raw = (headers.get("x-ratelimit-over-limit") or "").strip().lower()
    over_limit = over_limit_raw == "yes"

    windows: dict[str, Any] = {}

    if isinstance(req_limit, int) and req_limit > 0:
        used = None
        left = None
        if isinstance(req_rem, int) and req_rem >= 0:
            left = int(max(0, min(100, round((req_rem / req_limit) * 100))))
            used = int(max(0, min(100, 100 - left)))
        windows["requests"] = {
            "source": "requests",
            "limit": req_limit,
            "remaining": req_rem,
            "usedPercent": used,
            "leftPercent": left,
            "resetsAt": None,
            "resetsAtIsoUtc": None,
            "resetRaw": None,
            "overLimit": over_limit if over_limit_raw else None,
        }

    normalized["windows"] = windows
    return normalized


@dataclass(frozen=True)
class AccountConfig:
    label: str
    provider: str
    expected_email: str | None
    expected_plan_type: str | None
    enabled: bool
    anthropic_model: str | None = None
    fireworks_model: str | None = None
    fireworks_base_url: str | None = None


def _is_codex_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("codex", "openai_codex", "openai")


def _is_anthropic_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("anthropic", "claude", "claude_api", "anthropic_api")


def _is_fireworks_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("fireworks", "fireworks_ai", "fireworks_api")


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
        provider = (acc.get("provider") or "codex").strip().lower()
        expected_email = (acc.get("expected_email") or "").strip() or None
        expected_plan_type = (
            (acc.get("expected_planType") or acc.get("expected_plan_type") or "").strip() or None
        )
        anthropic_model = None
        fireworks_model = None
        fireworks_base_url = None
        if _is_anthropic_provider(provider):
            anthropic_model = (acc.get("anthropic_model") or acc.get("model") or "").strip() or None
        if _is_fireworks_provider(provider):
            fireworks_model = (acc.get("fireworks_model") or acc.get("model") or "").strip() or None
            fireworks_base_url = (
                (acc.get("fireworks_base_url") or acc.get("base_url") or "").strip() or None
            )
            if fireworks_base_url:
                fireworks_base_url = fireworks_base_url.rstrip("/")
        out.append(
            AccountConfig(
                label=label,
                provider=provider,
                expected_email=expected_email,
                expected_plan_type=expected_plan_type,
                enabled=enabled,
                anthropic_model=anthropic_model,
                fireworks_model=fireworks_model,
                fireworks_base_url=fireworks_base_url,
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


def _extract_anthropic_keys(text: str) -> list[str]:
    found = _ANTHROPIC_KEY_RE.findall(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for k in found:
        k = k.strip()
        if not k or k in seen:
            continue
        out.append(k)
        seen.add(k)
    return out


def _extract_fireworks_keys(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not _FIREWORKS_KEY_LINE_RE.match(s):
            continue
        if s in seen:
            continue
        out.append(s)
        seen.add(s)
    return out


def _parse_fireworks_memo(text: str) -> list[dict[str, Any]]:
    """
    Accepts either:
      - one key per line, OR
      - a human memo style like:
          user@example.com
          - fw_XXXX
          - note...
    Returns list of {key,email,note}.
    """
    entries: list[dict[str, Any]] = []

    current_email: str | None = None
    current_key: str | None = None
    current_note_parts: list[str] = []
    pre_note_parts: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_note_parts, pre_note_parts
        if not current_key:
            pre_note_parts = []
            current_note_parts = []
            return
        note = " ".join([p for p in current_note_parts if p]).strip() or None
        entries.append({"key": current_key, "email": current_email, "note": note})
        current_key = None
        current_note_parts = []
        pre_note_parts = []

    for raw_line in (text or "").splitlines():
        raw = raw_line.strip()
        if not raw:
            continue

        emails = _extract_emails(raw)
        if emails:
            flush()
            current_email = emails[0]
            continue

        # Strip common bullets.
        s = re.sub(r"^\s*[-*•・]+\s*", "", raw).strip()
        if not s:
            continue

        if _FIREWORKS_KEY_LINE_RE.match(s):
            if current_key:
                flush()
            current_key = s
            if pre_note_parts and not current_note_parts:
                current_note_parts.extend(pre_note_parts)
                pre_note_parts = []
            continue

        # Treat as note.
        if current_key:
            current_note_parts.append(s)
        else:
            pre_note_parts.append(s)

    flush()
    return entries


def _make_label_from_anthropic_key(key: str, prefix: str | None = None) -> str:
    safe_prefix = re.sub(r"[^a-z0-9]+", "_", (prefix or "claude").strip().lower()).strip("_")
    if not safe_prefix:
        safe_prefix = "claude"
    tail = key.strip().lower()[-10:]
    safe_tail = re.sub(r"[^a-z0-9]+", "", tail).strip("_") or "key"
    return f"{safe_prefix}_{safe_tail}"


def _make_label_from_fireworks_key(key: str, prefix: str | None = None) -> str:
    safe_prefix = re.sub(
        r"[^a-z0-9]+", "_", (prefix or "fireworks").strip().lower()
    ).strip("_")
    if not safe_prefix:
        safe_prefix = "fireworks"
    tail = key.strip().lower()[-10:]
    safe_tail = re.sub(r"[^a-z0-9]+", "", tail).strip("_") or "key"
    return f"{safe_prefix}_{safe_tail}"


def _make_label_from_fireworks_email(email: str, prefix: str | None = None) -> str:
    safe_prefix = re.sub(
        r"[^a-z0-9]+", "_", (prefix or "fireworks").strip().lower()
    ).strip("_")
    if not safe_prefix:
        safe_prefix = "fireworks"
    base = _make_label_from_email(email)
    base = base[len("acc_") :] if base.startswith("acc_") else base
    base = base.strip("_") or "account"
    return f"{safe_prefix}_{base}"


def _make_unique_label(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


class AddAccountsPayload(BaseModel):
    text: str | None = None
    emails: list[str] = Field(default_factory=list)
    expected_planType: str | None = None
    enabled: bool = True


class AddAnthropicKeysPayload(BaseModel):
    text: str | None = None
    keys: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str | None = None
    label_prefix: str | None = None
    expected_email: str | None = None
    anthropic_model: str | None = None


class AddFireworksKeysPayload(BaseModel):
    text: str | None = None
    keys: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str | None = None
    label_prefix: str | None = None
    expected_email: str | None = None
    fireworks_model: str | None = None
    fireworks_base_url: str | None = None


class AppendNotePayload(BaseModel):
    label: str
    append: str
    separator: str | None = None
    replace: bool = False


class SetNotePayload(BaseModel):
    label: str
    note: str | None = None


class PatchAccountPayload(BaseModel):
    label: str
    expected_email: str | None = None
    expected_planType: str | None = None
    enabled: bool | None = None
    provider: str | None = None
    note: str | None = None


class RemoveAccountsPayload(BaseModel):
    labels: list[str] = Field(default_factory=list)
    label: str | None = None
    delete_local_data: bool = False


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
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            cfg["accounts"] = accounts

        expected_plan = (payload.expected_planType or "").strip() or None

        existing_by_label: dict[str, dict[str, Any]] = {}
        existing_by_email_codex: dict[str, dict[str, Any]] = {}
        for a in accounts:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or "").strip()
            if label:
                existing_by_label[label] = a
            provider = (a.get("provider") or "codex").strip().lower()
            exp = (a.get("expected_email") or "").strip().lower()
            if exp and provider in ("codex", "openai_codex", "openai"):
                existing_by_email_codex[exp] = a

        added = 0
        updated = 0
        labels: list[str] = []
        for email in uniq:
            label = _make_label_from_email(email)
            labels.append(label)

            # Only merge-by-email within Codex accounts. The same email may legitimately exist
            # for other providers (e.g., Fireworks API key) and should not block adding Codex.
            entry = existing_by_label.get(label) or existing_by_email_codex.get(email)
            if entry is None:
                entry = {
                    "label": label,
                    "provider": "codex",
                    "enabled": bool(payload.enabled),
                    "expected_email": email,
                }
                if expected_plan:
                    entry["expected_planType"] = expected_plan
                accounts.append(entry)
                existing_by_label[label] = entry
                existing_by_email_codex[email] = entry
                added += 1
            else:
                changed = False
                if not (entry.get("provider") or "").strip():
                    entry["provider"] = "codex"
                    changed = True
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
            (account_home / ".secrets").mkdir(parents=True, exist_ok=True)

        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "added": added, "updated": updated, "labels": sorted(set(labels))}


@app.post("/config/note_append")
def config_note_append(payload: AppendNotePayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    append_text = (payload.append or "").strip()
    if not append_text:
        raise HTTPException(status_code=400, detail="append is required")
    sep = (payload.separator or "").strip()
    if not sep:
        sep = " · "

    with _config_lock:
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            raise HTTPException(status_code=500, detail="config.accounts must be an array")

        target: dict[str, Any] | None = None
        for a in accounts:
            if not isinstance(a, dict):
                continue
            if (a.get("label") or "").strip() == label:
                target = a
                break

        if target is None:
            raise HTTPException(status_code=404, detail=f"label not found: {label}")

        cur = (target.get("note") or "").strip()
        if payload.replace or not cur:
            new_note = append_text
        else:
            new_note = f"{cur}{sep}{append_text}"

        target["note"] = new_note
        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "label": label, "note": new_note}


@app.post("/config/note_set")
def config_note_set(payload: SetNotePayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")

    note = (payload.note or "").strip()

    with _config_lock:
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            raise HTTPException(status_code=500, detail="config.accounts must be an array")

        target: dict[str, Any] | None = None
        for a in accounts:
            if not isinstance(a, dict):
                continue
            if (a.get("label") or "").strip() == label:
                target = a
                break

        if target is None:
            raise HTTPException(status_code=404, detail=f"label not found: {label}")

        if note:
            target["note"] = note
        else:
            target.pop("note", None)

        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "label": label, "note": note}


@app.post("/config/account_patch")
def config_account_patch(payload: PatchAccountPayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")

    exp_email: str | None = None
    if payload.expected_email is not None:
        raw = str(payload.expected_email).strip()
        if raw:
            emails = _extract_emails(raw)
            if not emails:
                raise HTTPException(status_code=400, detail="expected_email must be a valid email (or empty to clear)")
            exp_email = emails[0]
        else:
            exp_email = None

    expected_plan = (payload.expected_planType or "").strip() or None if payload.expected_planType is not None else None
    provider = (payload.provider or "").strip() or None if payload.provider is not None else None
    note = (payload.note or "").strip() if payload.note is not None else None

    changed: dict[str, Any] = {}

    with _config_lock:
        _fail_if_refresh_running()
        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            raise HTTPException(status_code=500, detail="config.accounts must be an array")

        target: dict[str, Any] | None = None
        for a in accounts:
            if not isinstance(a, dict):
                continue
            if (a.get("label") or "").strip() == label:
                target = a
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"label not found: {label}")

        if payload.expected_email is not None:
            if exp_email:
                if (target.get("expected_email") or "").strip().lower() != exp_email:
                    target["expected_email"] = exp_email
                    changed["expected_email"] = exp_email
            else:
                if "expected_email" in target:
                    target.pop("expected_email", None)
                    changed["expected_email"] = None

        if payload.expected_planType is not None:
            if expected_plan:
                if (target.get("expected_planType") or "").strip() != expected_plan:
                    target["expected_planType"] = expected_plan
                    changed["expected_planType"] = expected_plan
            else:
                if "expected_planType" in target:
                    target.pop("expected_planType", None)
                    changed["expected_planType"] = None

        if payload.enabled is not None:
            enabled = bool(payload.enabled)
            if bool(target.get("enabled", True)) != enabled:
                target["enabled"] = enabled
                changed["enabled"] = enabled

        if payload.provider is not None:
            if provider:
                if (target.get("provider") or "").strip() != provider:
                    target["provider"] = provider
                    changed["provider"] = provider
            else:
                if "provider" in target:
                    target.pop("provider", None)
                    changed["provider"] = None

        if payload.note is not None:
            if note:
                if (target.get("note") or "").strip() != note:
                    target["note"] = note
                    changed["note"] = note
            else:
                if "note" in target:
                    target.pop("note", None)
                    changed["note"] = None

        _write_json_atomic(config_path, cfg)

    try:
        _push_registry_from_config()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "label": label, "changed": changed}


@app.post("/config/remove_accounts")
def config_remove_accounts(payload: RemoveAccountsPayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    labels_in: list[str] = []
    if payload.label:
        labels_in.append(payload.label)
    labels_in.extend(payload.labels or [])

    def norm_label(raw: str) -> str:
        label = (raw or "").strip()
        if not label:
            return ""
        if Path(label).name != label:
            raise HTTPException(status_code=400, detail=f"invalid label: {label}")
        return label

    uniq: list[str] = []
    seen: set[str] = set()
    for raw in labels_in:
        label = norm_label(raw)
        if not label or label in seen:
            continue
        seen.add(label)
        uniq.append(label)

    if not uniq:
        raise HTTPException(status_code=400, detail="labels must be non-empty")

    removed: list[str] = []
    missing: list[str] = []
    deleted_local: list[str] = []
    local_errors: dict[str, str] = {}

    with _config_lock:
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            cfg["accounts"] = accounts

        remove_set = set(uniq)
        next_accounts: list[Any] = []
        removed_set: set[str] = set()

        for a in accounts:
            if not isinstance(a, dict):
                next_accounts.append(a)
                continue
            label = (a.get("label") or a.get("account_label") or "").strip()
            if label and label in remove_set:
                removed_set.add(label)
                continue
            next_accounts.append(a)

        for label in uniq:
            if label in removed_set:
                removed.append(label)
            else:
                missing.append(label)

        cfg["accounts"] = next_accounts
        _write_json_atomic(config_path, cfg)

        if payload.delete_local_data:
            for label in uniq:
                try:
                    account_home = Path(ACCOUNTS_DIR) / label
                    if account_home.exists():
                        shutil.rmtree(account_home)
                    deleted_local.append(label)
                except Exception as e:
                    local_errors[label] = str(e)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {
        "ok": True,
        "removed": removed,
        "missing": missing,
        "deleted_local": deleted_local,
        "local_errors": local_errors,
        "remaining": len([a for a in (cfg.get("accounts") or []) if isinstance(a, dict)]),
    }


@app.post("/config/add_anthropic_keys")
def config_add_anthropic_keys(payload: AddAnthropicKeysPayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    keys: list[str] = []
    if payload.text:
        keys.extend(_extract_anthropic_keys(payload.text))
    for k in payload.keys or []:
        if isinstance(k, str):
            keys.extend(_extract_anthropic_keys(k))

    uniq: list[str] = []
    seen: set[str] = set()
    for k in keys:
        if k in seen:
            continue
        uniq.append(k)
        seen.add(k)

    if not uniq:
        raise HTTPException(status_code=400, detail="no anthropic api keys found")

    model = (payload.anthropic_model or "").strip() or None
    label_prefix = (payload.label_prefix or "").strip() or None
    note = (payload.note or "").strip() or None
    exp_email: str | None = None
    if payload.expected_email:
        emails = _extract_emails(payload.expected_email)
        exp_email = emails[0] if emails else None

    with _config_lock:
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            cfg["accounts"] = accounts

        existing_labels: set[str] = set()
        existing_by_label: dict[str, dict[str, Any]] = {}
        for a in accounts:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or "").strip()
            if not label:
                continue
            existing_labels.add(label)
            existing_by_label[label] = a

        added = 0
        updated = 0
        labels: list[str] = []
        for key in uniq:
            base = _make_label_from_anthropic_key(key, prefix=label_prefix)
            if base in existing_by_label:
                label = base
            else:
                label = _make_unique_label(base, existing_labels)
                existing_labels.add(label)
            labels.append(label)

            entry = existing_by_label.get(label)
            if entry is None:
                entry = {
                    "label": label,
                    "provider": "anthropic",
                    "enabled": bool(payload.enabled),
                }
                if exp_email:
                    entry["expected_email"] = exp_email
                if note:
                    entry["note"] = note
                if model:
                    entry["anthropic_model"] = model
                accounts.append(entry)
                existing_by_label[label] = entry
                added += 1
            else:
                changed = False
                if (entry.get("provider") or "").strip().lower() != "anthropic":
                    entry["provider"] = "anthropic"
                    changed = True
                if bool(entry.get("enabled", True)) != bool(payload.enabled):
                    entry["enabled"] = bool(payload.enabled)
                    changed = True
                if exp_email and (entry.get("expected_email") or "").strip().lower() != exp_email:
                    entry["expected_email"] = exp_email
                    changed = True
                if note and (entry.get("note") or "").strip() != note:
                    entry["note"] = note
                    changed = True
                if model and (entry.get("anthropic_model") or entry.get("model") or "").strip() != model:
                    entry["anthropic_model"] = model
                    changed = True
                if changed:
                    updated += 1

            account_home = Path(ACCOUNTS_DIR) / label
            (account_home / ".codex").mkdir(parents=True, exist_ok=True)
            secrets_dir = account_home / ".secrets"
            secrets_dir.mkdir(parents=True, exist_ok=True)
            key_path = secrets_dir / "anthropic_api_key.txt"
            _write_text_atomic(key_path, key.strip() + "\n")

        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "added": added, "updated": updated, "labels": sorted(set(labels))}


@app.post("/config/add_fireworks_keys")
def config_add_fireworks_keys(payload: AddFireworksKeysPayload):
    config_path = Path(CONFIG_PATH)
    if not config_path.exists() or not config_path.is_file():
        raise HTTPException(status_code=500, detail=f"config not found: {CONFIG_PATH}")

    entries: list[dict[str, Any]] = []
    if payload.text:
        entries.extend(_parse_fireworks_memo(payload.text))
    for k in payload.keys or []:
        if isinstance(k, str) and k.strip():
            entries.extend(_parse_fireworks_memo(k))

    uniq: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in entries:
        key = (e.get("key") or "").strip()
        if not key or key in seen:
            continue
        uniq.append(e)
        seen.add(key)

    if not uniq:
        raise HTTPException(status_code=400, detail="no fireworks api keys found")

    label_prefix = (payload.label_prefix or "").strip() or None
    note = (payload.note or "").strip() or None
    model = (payload.fireworks_model or "").strip() or None
    base_url = (payload.fireworks_base_url or "").strip() or None
    default_email: str | None = None
    if payload.expected_email:
        emails = _extract_emails(payload.expected_email)
        default_email = emails[0] if emails else None
    if base_url:
        base_url = base_url.rstrip("/")

    with _config_lock:
        _fail_if_refresh_running()

        cfg = _read_json(config_path)
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            cfg["accounts"] = accounts

        existing_labels: set[str] = set()
        existing_by_label: dict[str, dict[str, Any]] = {}
        existing_by_email: dict[str, dict[str, Any]] = {}
        existing_by_key: dict[str, str] = {}
        for a in accounts:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or "").strip()
            if not label:
                continue
            existing_labels.add(label)
            existing_by_label[label] = a
            provider = (a.get("provider") or "").strip().lower()
            exp = (a.get("expected_email") or "").strip().lower()
            if provider == "fireworks" and exp:
                existing_by_email[exp] = a
            if provider == "fireworks":
                key_path = Path(ACCOUNTS_DIR) / label / ".secrets" / "fireworks_api_key.txt"
                if key_path.is_file():
                    key_text = _read_text_file(key_path)
                    if key_text:
                        candidate: str | None = None
                        for line in key_text.splitlines():
                            s = line.strip()
                            if s:
                                candidate = s
                                break
                        if candidate and _FIREWORKS_KEY_LINE_RE.match(candidate):
                            existing_by_key[candidate] = label

        added = 0
        updated = 0
        labels: list[str] = []
        for e in uniq:
            key = (e.get("key") or "").strip()
            email = (e.get("email") or "").strip().lower() or None
            if not email and default_email:
                email = default_email
            note_from_text = (e.get("note") or "").strip() or None

            existing_label_by_key = existing_by_key.get(key)
            if existing_label_by_key:
                label = existing_label_by_key
            else:
                base = (
                    _make_label_from_fireworks_email(email, prefix=label_prefix)
                    if email
                    else _make_label_from_fireworks_key(key, prefix=label_prefix)
                )

                existing = existing_by_email.get(email) if email else None
                if existing is not None:
                    label = (existing.get("label") or "").strip() or base
                else:
                    label = base if base in existing_by_label else _make_unique_label(base, existing_labels)
                    existing_labels.add(label)
            labels.append(label)

            entry = existing_by_label.get(label)
            if entry is None:
                entry = {
                    "label": label,
                    "provider": "fireworks",
                    "enabled": bool(payload.enabled),
                }
                if email:
                    entry["expected_email"] = email
                if note_from_text or note:
                    entry["note"] = note_from_text or note
                if model:
                    entry["fireworks_model"] = model
                if base_url:
                    entry["fireworks_base_url"] = base_url
                accounts.append(entry)
                existing_by_label[label] = entry
                if email:
                    existing_by_email[email] = entry
                added += 1
            else:
                changed = False
                if (entry.get("provider") or "").strip().lower() != "fireworks":
                    entry["provider"] = "fireworks"
                    changed = True
                if bool(entry.get("enabled", True)) != bool(payload.enabled):
                    entry["enabled"] = bool(payload.enabled)
                    changed = True
                if email and (entry.get("expected_email") or "").strip().lower() != email:
                    entry["expected_email"] = email
                    changed = True
                note_to_set = note_from_text or note
                if note_to_set and (entry.get("note") or "").strip() != note_to_set:
                    entry["note"] = note_to_set
                    changed = True
                if model and (entry.get("fireworks_model") or entry.get("model") or "").strip() != model:
                    entry["fireworks_model"] = model
                    changed = True
                if base_url and (
                    (entry.get("fireworks_base_url") or entry.get("base_url") or "").strip().rstrip("/")
                    != base_url
                ):
                    entry["fireworks_base_url"] = base_url
                    changed = True
                if changed:
                    updated += 1

            account_home = Path(ACCOUNTS_DIR) / label
            (account_home / ".codex").mkdir(parents=True, exist_ok=True)
            secrets_dir = account_home / ".secrets"
            secrets_dir.mkdir(parents=True, exist_ok=True)
            key_path = secrets_dir / "fireworks_api_key.txt"
            _write_text_atomic(key_path, key + "\n")

        _write_json_atomic(config_path, cfg)

        try:
            _push_registry_from_config()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to push registry: {e}") from e

    return {"ok": True, "added": added, "updated": updated, "labels": sorted(set(labels))}


@app.post("/config/push_registry")
def config_push_registry():
    try:
        _push_registry_from_config()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"collector /registry failed: HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"collector /registry failed: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True}


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
    normalized: dict[str, Any] = {"provider": "codex"}
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


def _post_status_event(label: str, raw: str, parsed: dict[str, Any], ts: str) -> dict[str, Any]:
    payload = {
        "account_label": label,
        "host": socket.gethostname(),
        "raw": raw,
        "parsed": parsed,
        "ts": ts,
    }
    _post_json(COLLECTOR_URL, payload)
    return payload


def _refresh_one_codex(acc: AccountConfig) -> tuple[str, dict[str, Any]]:
    account_home = Path(ACCOUNTS_DIR) / acc.label
    account_home.mkdir(parents=True, exist_ok=True)
    (account_home / ".codex").mkdir(parents=True, exist_ok=True)
    (account_home / ".secrets").mkdir(parents=True, exist_ok=True)
    auth_path = account_home / ".codex" / "auth.json"
    account_email = _extract_account_email_from_auth(auth_path) if auth_path.is_file() else None

    ts = _now_iso()
    if not auth_path.is_file():
        raw = "[auth_required] missing auth.json (run codex login for this account)"
        parsed = {
            "probe_error": True,
            "error_type": "AuthRequired",
            "error": f"missing auth.json: {auth_path}",
            "normalized": {
                "provider": "codex",
                "account_email": account_email,
                "expected_email": acc.expected_email,
                "expected_email_match": None,
                "expected_planType": acc.expected_plan_type,
                "expected_planType_match": None,
                "requiresAuth": True,
                "requiresOpenaiAuth": True,
                "windows": {},
            },
        }
        payload = _post_status_event(acc.label, raw, parsed, ts)
        return "auth required", payload

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
        ml = message.lower()
        requires_auth = (
            ("authentication required" in ml)
            or ("unauthorized" in ml)
            or ("token_invalidated" in ml)
            or ("token invalidated" in ml)
            or ("token has been invalidated" in ml)
            or ("please try signing in again" in ml)
            or ("try signing in again" in ml)
            or ("sign in again" in ml)
        )

        parsed = {
            "probe_error": True,
            "error_type": type(e).__name__,
            "error": str(e),
            "error_payload": error_payload,
            "normalized": {
                "provider": "codex",
                "account_email": account_email,
                "expected_email": acc.expected_email,
                "expected_email_match": (
                    account_email == acc.expected_email.lower()
                    if (account_email and acc.expected_email)
                    else None
                ),
                "expected_planType": acc.expected_plan_type,
                "requiresAuth": requires_auth,
                "requiresOpenaiAuth": requires_auth,
            },
        }
        state = "auth required" if requires_auth else "error"

    payload = _post_status_event(acc.label, raw, parsed, ts)
    return state, payload


def _refresh_one_anthropic(acc: AccountConfig) -> tuple[str, dict[str, Any]]:
    account_home = Path(ACCOUNTS_DIR) / acc.label
    account_home.mkdir(parents=True, exist_ok=True)
    (account_home / ".codex").mkdir(parents=True, exist_ok=True)
    secrets_dir = account_home / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    ts = _now_iso()

    key_path = secrets_dir / "anthropic_api_key.txt"
    key_text = _read_text_file(key_path) if key_path.is_file() else None
    api_key: str | None = None
    if key_text:
        m = _ANTHROPIC_KEY_RE.search(key_text)
        if m:
            api_key = m.group(0)

    model = acc.anthropic_model or ANTHROPIC_MODEL_DEFAULT

    if not api_key:
        raw = "[auth_required] missing anthropic_api_key.txt"
        parsed = {
            "probe_error": True,
            "error_type": "AuthRequired",
            "error": f"missing API key: {key_path}",
            "normalized": {
                "provider": "anthropic",
                "requiresAuth": True,
                "expected_email": acc.expected_email,
                "expected_email_match": None,
                "expected_planType": acc.expected_plan_type,
                "expected_planType_match": None,
                "windows": {},
            },
        }
        payload = _post_status_event(acc.label, raw, parsed, ts)
        return "auth required", payload

    try:
        http_status, headers = _anthropic_request_rate_limits(api_key, model=model)
        headers_filtered = {
            k: v
            for (k, v) in headers.items()
            if k.startswith("anthropic-ratelimit-") or k in ("retry-after", "date", "request-id")
        }
        normalized = _normalize_anthropic(http_status=http_status, headers=headers, expected=acc)
        normalized["api_key_hint"] = _mask_secret(api_key, keep_start=12, keep_end=6)
        normalized["model"] = model

        raw = json.dumps(
            {"http_status": http_status, "model": model, "headers": headers_filtered},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        parsed = {
            "http_status": http_status,
            "model": model,
            "headers": headers_filtered,
            "normalized": normalized,
        }

        if normalized.get("requiresAuth"):
            state = "auth required"
        elif normalized.get("windows") or normalized.get("credits") or (200 <= http_status < 400):
            state = "ok"
        else:
            state = "error"
    except Exception as e:
        raw = f"[probe_error] {type(e).__name__}: {e}"
        parsed = {
            "probe_error": True,
            "error_type": type(e).__name__,
            "error": str(e),
            "normalized": {
                "provider": "anthropic",
                "requiresAuth": False,
                "expected_email": acc.expected_email,
                "expected_email_match": None,
                "expected_planType": acc.expected_plan_type,
                "expected_planType_match": None,
                "windows": {},
                "model": model,
                "api_key_hint": _mask_secret(api_key, keep_start=12, keep_end=6),
            },
        }
        state = "error"

    payload = _post_status_event(acc.label, raw, parsed, ts)
    return state, payload


def _refresh_one_fireworks(acc: AccountConfig) -> tuple[str, dict[str, Any]]:
    account_home = Path(ACCOUNTS_DIR) / acc.label
    account_home.mkdir(parents=True, exist_ok=True)
    (account_home / ".codex").mkdir(parents=True, exist_ok=True)
    secrets_dir = account_home / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    ts = _now_iso()

    key_path = secrets_dir / "fireworks_api_key.txt"
    key_text = _read_text_file(key_path) if key_path.is_file() else None
    api_key: str | None = None
    if key_text:
        for line in key_text.splitlines():
            s = line.strip()
            if s:
                api_key = s
                break

    model = acc.fireworks_model or None
    base_url = (acc.fireworks_base_url or FIREWORKS_BASE_URL_DEFAULT).rstrip("/")

    if not api_key or not _FIREWORKS_KEY_LINE_RE.match(api_key):
        raw = "[auth_required] missing fireworks_api_key.txt"
        parsed = {
            "probe_error": True,
            "error_type": "AuthRequired",
            "error": f"missing API key: {key_path}",
            "normalized": {
                "provider": "fireworks",
                "requiresAuth": True,
                "expected_email": acc.expected_email,
                "expected_email_match": None,
                "expected_planType": acc.expected_plan_type,
                "expected_planType_match": None,
                "windows": {},
                "model": model,
                "base_url": base_url,
            },
        }
        payload = _post_status_event(acc.label, raw, parsed, ts)
        return "auth required", payload

    try:
        http_status, headers, err_body = _fireworks_request_rate_limits(api_key, base_url=base_url)
        err_msg = _extract_error_message(err_body or "") or ((err_body or "").strip() or None)
        if err_msg and len(err_msg) > 600:
            err_msg = err_msg[:600] + "…"
        headers_filtered = {
            k: v
            for (k, v) in headers.items()
            if k.startswith("x-ratelimit-") or k in ("retry-after", "date", "request-id")
        }
        normalized = _normalize_fireworks(http_status=http_status, headers=headers, expected=acc)
        normalized["api_key_hint"] = _mask_secret(api_key, keep_start=10, keep_end=6)
        if model:
            normalized["model"] = model
        if base_url:
            normalized["base_url"] = base_url
        if not normalized.get("requiresAuth"):
            balance = _fireworks_get_balance_cached(acc.label, api_key)
            if balance and not balance.get("error"):
                normalized["credits"] = balance
            elif balance and balance.get("error"):
                normalized["credits"] = {"source": "firectl", "error": balance.get("error")}

        raw = json.dumps(
            {
                "http_status": http_status,
                "model": model,
                "base_url": base_url,
                "headers": headers_filtered,
                "credits": normalized.get("credits"),
                "error": err_msg,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        parsed: dict[str, Any] = {
            "http_status": http_status,
            "model": model,
            "base_url": base_url,
            "headers": headers_filtered,
            "normalized": normalized,
        }

        if normalized.get("requiresAuth"):
            state = "auth required"
        elif http_status >= 400:
            parsed["probe_error"] = True
            parsed["error_type"] = "HTTPStatus"
            parsed["error"] = err_msg or f"HTTP {http_status}"
            state = "error"
        elif normalized.get("windows") or normalized.get("credits") or (200 <= http_status < 400):
            state = "ok"
        else:
            state = "error"
    except Exception as e:
        raw = f"[probe_error] {type(e).__name__}: {e}"
        parsed = {
            "probe_error": True,
            "error_type": type(e).__name__,
            "error": str(e),
            "normalized": {
                "provider": "fireworks",
                "requiresAuth": False,
                "expected_email": acc.expected_email,
                "expected_email_match": None,
                "expected_planType": acc.expected_plan_type,
                "expected_planType_match": None,
                "windows": {},
                "model": model,
                "base_url": base_url,
                "api_key_hint": _mask_secret(api_key, keep_start=10, keep_end=6),
            },
        }
        state = "error"

    payload = _post_status_event(acc.label, raw, parsed, ts)
    return state, payload


def _refresh_one(acc: AccountConfig) -> tuple[str, dict[str, Any]]:
    if _is_codex_provider(acc.provider):
        return _refresh_one_codex(acc)
    if _is_anthropic_provider(acc.provider):
        return _refresh_one_anthropic(acc)
    if _is_fireworks_provider(acc.provider):
        return _refresh_one_fireworks(acc)

    ts = _now_iso()
    raw = f"[probe_error] unknown provider: {acc.provider}"
    parsed = {
        "probe_error": True,
        "error_type": "UnknownProvider",
        "error": f"unknown provider: {acc.provider}",
        "normalized": {
            "provider": acc.provider,
            "requiresAuth": False,
            "expected_email": acc.expected_email,
            "expected_email_match": None,
            "expected_planType": acc.expected_plan_type,
            "expected_planType_match": None,
            "windows": {},
        },
    }
    payload = _post_status_event(acc.label, raw, parsed, ts)
    return "error", payload


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
