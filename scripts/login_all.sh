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

force="false"
only_labels=""
pass_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      force="true"
      shift
      ;;
    --only-label)
      if [[ "${2:-}" == "" ]]; then
        echo "Missing value for --only-label" >&2
        exit 2
      fi
      only_labels="${only_labels}${2}"$'\n'
      shift 2
      ;;
    *)
      pass_args+=("$1")
      shift
      ;;
  esac
done

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

if [[ "${only_labels}" != "" ]]; then
  wanted="$(printf "%s" "${only_labels}" | awk 'NF{print $0}' | sort -u)"
  labels="$(printf "%s" "${labels}" | awk 'NF{print $0}' | sort -u)"
  labels="$(comm -12 <(printf "%s\n" "${labels}") <(printf "%s\n" "${wanted}"))"
  if [[ "${labels}" == "" ]]; then
    echo "No matching labels to login (check --only-label)" >&2
    exit 1
  fi
fi

while IFS= read -r label; do
  if [[ "${label}" == "" ]]; then
    continue
  fi
  auth_path="${root_dir}/accounts/${label}/.codex/auth.json"
  if [[ -f "${auth_path}" && "${force}" != "true" ]]; then
    echo "==> ${label}: already logged in (${auth_path})"
    continue
  fi
  echo "==> ${label}: login start"
  if [[ -f "${auth_path}" && "${force}" == "true" ]]; then
    ts="$(date -u +"%Y%m%dT%H%M%SZ")"
    bak="${auth_path}.bak.${ts}"
    cp -p "${auth_path}" "${bak}"
    rm -f "${auth_path}"
    echo "==> ${label}: force re-login (backup: ${bak})"
    if (( ${#pass_args[@]} )); then
      if ! ./scripts/init_account.sh "${label}" "${pass_args[@]}"; then
        echo "==> ${label}: login failed; restoring backup (${bak})" >&2
        cp -p "${bak}" "${auth_path}" || true
        exit 1
      fi
    else
      if ! ./scripts/init_account.sh "${label}"; then
        echo "==> ${label}: login failed; restoring backup (${bak})" >&2
        cp -p "${bak}" "${auth_path}" || true
        exit 1
      fi
    fi
    continue
  fi
  if (( ${#pass_args[@]} )); then
    ./scripts/init_account.sh "${label}" "${pass_args[@]}"
  else
    ./scripts/init_account.sh "${label}"
  fi
done <<< "${labels}"
