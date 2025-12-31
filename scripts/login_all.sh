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

labels="$(python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],"r",encoding="utf-8")); print("\n".join([(a.get("label") or "").strip() for a in cfg.get("accounts",[]) if isinstance(a,dict) and a.get("enabled", True) is not False and (a.get("label") or "").strip()]))' "${config_path}")"

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
