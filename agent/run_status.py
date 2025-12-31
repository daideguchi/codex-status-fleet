import base64
import json
import os
import re
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

CLIENT_NAME = "codex-status-fleet"
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


def _extract_account_email_from_auth(auth_path: str) -> str | None:
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
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


@dataclass(frozen=True)
class RpcConfig:
    codex_bin: str
    rpc_timeout_sec: float


class CodexAppServer:
    def __init__(self, config: RpcConfig):
        self._config = config
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._user_agent: str | None = None

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._proc = subprocess.Popen(
            [self._config.codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._user_agent = None

        init = {
            "id": self._alloc_id(),
            "method": "initialize",
            "params": {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}},
        }
        result = self._request(init)["result"]
        self._user_agent = result.get("userAgent")

    def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None

        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def fetch_rate_limits(self) -> tuple[dict, str | None]:
        self.start()
        msg = {"id": self._alloc_id(), "method": "account/rateLimits/read", "params": None}
        result = self._request(msg)["result"]
        return result, self._user_agent

    def _alloc_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _request(self, msg: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("codex app-server is not running")
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        request_id = msg["id"]
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

        deadline = time.time() + self._config.rpc_timeout_sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"timeout waiting for response id={request_id}")

            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                continue

            line = self._proc.stdout.readline()
            if not line:
                stderr = ""
                try:
                    assert self._proc.stderr is not None
                    stderr = self._proc.stderr.read().strip()
                except Exception:
                    pass
                raise EOFError(f"codex app-server closed stdout (stderr={stderr!r})")

            parsed = json.loads(line)
            if parsed.get("id") != request_id:
                continue
            if "error" in parsed:
                raise RuntimeError(parsed["error"])
            return parsed


def post_status(collector_url: str, account_label: str, raw: str, parsed: dict) -> None:
    payload = {
        "account_label": account_label,
        "host": socket.gethostname(),
        "raw": raw,
        "parsed": parsed,
        "ts": _now_iso(),
    }
    r = requests.post(collector_url, json=payload, timeout=10)
    r.raise_for_status()


def main() -> int:
    collector_url = os.getenv("COLLECTOR_URL", "http://collector:8080/ingest")
    account_label = os.getenv("ACCOUNT_LABEL", "").strip()
    poll_interval_sec = int(os.getenv("POLL_INTERVAL_SEC", "0"))
    expected_email = os.getenv("EXPECTED_EMAIL", "").strip() or None
    expected_plan_type = os.getenv("EXPECTED_PLAN_TYPE", "").strip() or None
    auth_path = os.getenv("CODEX_AUTH_PATH") or os.path.join(
        os.path.expanduser("~"), ".codex", "auth.json"
    )

    if not account_label:
        raise SystemExit("ACCOUNT_LABEL is required")

    config = RpcConfig(
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        rpc_timeout_sec=float(os.getenv("RPC_TIMEOUT_SEC", "10.0")),
    )

    server = CodexAppServer(config)

    exit_code = 0
    while True:
        account_email = _extract_account_email_from_auth(auth_path)
        expected_email_lc = expected_email.lower() if expected_email else None
        try:
            rate_result, user_agent = server.fetch_rate_limits()
            raw = json.dumps(rate_result, ensure_ascii=False, separators=(",", ":"))

            parsed: dict = {"userAgent": user_agent}

            normalized: dict = {}
            if account_email:
                normalized["account_email"] = account_email
            if expected_email:
                normalized["expected_email"] = expected_email
                normalized["expected_email_match"] = (
                    account_email == expected_email_lc if account_email else None
                )
            if expected_plan_type:
                normalized["expected_planType"] = expected_plan_type
                normalized["expected_planType_match"] = None

            rate_limits = rate_result.get("rateLimits") if isinstance(rate_result, dict) else None
            if isinstance(rate_limits, dict):
                normalized["rate_planType"] = rate_limits.get("planType")
                normalized["credits"] = rate_limits.get("credits")
                if expected_plan_type:
                    normalized["expected_planType_match"] = (
                        rate_limits.get("planType") == expected_plan_type
                        if isinstance(rate_limits.get("planType"), str)
                        else None
                    )

                windows = {}
                for source in ("primary", "secondary"):
                    w = rate_limits.get(source)
                    if not isinstance(w, dict):
                        continue
                    dur = w.get("windowDurationMins")
                    used = w.get("usedPercent")
                    resets_at = w.get("resetsAt")

                    label = source
                    if dur == 300:
                        label = "5h"
                    elif dur == 10080:
                        label = "weekly"

                    left = None
                    if isinstance(used, int):
                        left = max(0, min(100, 100 - used))

                    windows[label] = {
                        "source": source,
                        "usedPercent": used,
                        "leftPercent": left,
                        "windowDurationMins": dur,
                        "resetsAt": resets_at,
                        "resetsAtIsoUtc": _epoch_to_iso(resets_at) if isinstance(resets_at, int) else None,
                    }

                normalized["windows"] = windows

            parsed["normalized"] = normalized
        except Exception as e:
            server.stop()
            raw = f"[probe_error] {type(e).__name__}: {e}"
            error_payload = None
            if getattr(e, "args", None):
                first = e.args[0]
                if isinstance(first, dict):
                    error_payload = first

            error_message = ""
            if isinstance(error_payload, dict):
                error_message = str(error_payload.get("message") or "")
            if not error_message:
                error_message = str(e)

            requires_auth = "authentication required" in error_message.lower()
            parsed = {
                "probe_error": True,
                "error_type": type(e).__name__,
                "error": str(e),
                "error_payload": error_payload,
                "normalized": {
                    "account_email": account_email,
                    "expected_email": expected_email,
                    "expected_email_match": (
                        account_email == expected_email_lc if (account_email and expected_email_lc) else None
                    ),
                    "expected_planType": expected_plan_type,
                    "requiresOpenaiAuth": requires_auth,
                },
            }
            exit_code = 1

        try:
            post_status(collector_url, account_label, raw, parsed)
        except Exception as e:
            print(f"[post_error] {type(e).__name__}: {e}", flush=True)
            exit_code = 1

        if poll_interval_sec <= 0:
            break
        time.sleep(poll_interval_sec)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
