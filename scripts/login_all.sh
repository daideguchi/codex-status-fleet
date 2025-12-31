#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

config_path="${1:-accounts.json}"
shift || true

if [[ ! -f "${config_path}" ]]; then
  echo "Config not found: ${config_path}" >&2
  exit 1
fi

labels="$(
  python3 - "${config_path}" <<'PY'
import json
import sys

cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
out: list[str] = []
for a in cfg.get("accounts", []):
    if not isinstance(a, dict):
        continue
    if a.get("enabled", True) is False:
        continue
    label = (a.get("label") or "").strip()
    if not label:
        continue
    provider = (a.get("provider") or "codex").strip().lower()
    if provider not in ("codex", "openai_codex", "openai"):
        continue
    out.append(label)
print("\n".join(out))
PY
)"

if [[ "${labels}" == "" ]]; then
  echo "No enabled accounts in ${config_path}" >&2
  exit 1
fi

while IFS= read -r label; do
  if [[ "${label}" == "" ]]; then
    continue
  fi
  auth_path="${root_dir}/accounts/${label}/.codex/auth.json"
  if [[ -f "${auth_path}" ]]; then
    echo "==> ${label}: already logged in (${auth_path})"
    continue
  fi
  echo "==> ${label}: login start"
  ./scripts/init_account.sh "${label}" "$@"
done <<< "${labels}"
