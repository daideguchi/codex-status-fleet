#!/usr/bin/env bash
if [[ -z "${BASH_VERSION:-}" ]]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

if [[ "${1:-}" == "" ]]; then
  echo "Usage: $0 <account_label|email> [codex login args...]" >&2
  exit 2
fi

input="$1"
shift

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_path="${root_dir}/accounts.json"

label="${input}"
if [[ "${input}" == *"@"* ]]; then
  email="${input}"
  if [[ "${email}" == acc_* ]]; then
    candidate="${email#acc_}"
    if [[ "${candidate}" == *"@"* ]]; then
      email="${candidate}"
    fi
  fi

  label="$(
    python3 - "${email}" "${config_path}" <<'PY'
import json
import os
import re
import sys

email = (sys.argv[1] or "").strip().lower()
config_path = sys.argv[2]

def make_label_from_email(e: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", e).strip("_")
    return f"acc_{s}" if s else "acc_account"

label = make_label_from_email(email)
try:
    if os.path.isfile(config_path):
        cfg = json.load(open(config_path, "r", encoding="utf-8"))
        for a in cfg.get("accounts", []):
            if not isinstance(a, dict):
                continue
            provider = (a.get("provider") or "codex").strip().lower()
            if provider not in ("codex", "openai_codex", "openai"):
                continue
            exp = (a.get("expected_email") or "").strip().lower()
            if exp and exp == email:
                l = (a.get("label") or "").strip()
                if l:
                    label = l
                break
except Exception:
    pass

print(label)
PY
  )"
  label="$(printf "%s" "${label}" | tr -d '\n')"
  echo "==> Interpreted as email: ${email}"
  echo "==> Using label: ${label}"
fi

acc_home="${root_dir}/accounts/${label}"

mkdir -p "${acc_home}"

echo "==> Logging in for account '${label}'"
echo "    HOME=${acc_home}"
echo "    Auth will be stored under: ${acc_home}/.codex/"
echo

if [[ -r /dev/tty ]]; then
  HOME="${acc_home}" codex login "$@" < /dev/tty
else
  HOME="${acc_home}" codex login "$@"
fi

label_q="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "${label}" 2>/dev/null || true)"
if [[ "${label_q}" != "" ]] && command -v curl >/dev/null 2>&1; then
  # Trigger a refresh without blocking the login flow. (Refresh can take 10s+ across many accounts.)
  # Prefer /refresh_async when available; fall back to /refresh but with a short timeout.
  (
    curl -fsS -X POST "http://localhost:8080/refresh_async?label=${label_q}" >/dev/null 2>&1 \
      || curl -fsS -m 2 -X POST "http://localhost:8080/refresh?label=${label_q}" >/dev/null 2>&1 \
      || true
  ) &
fi
