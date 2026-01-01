#!/usr/bin/env bash
if [[ -z "${BASH_VERSION:-}" ]]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

config_path="${1:-accounts.json}"
shift || true

collector_url="${COLLECTOR_URL:-http://localhost:8080}"

pass_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --collector)
      collector_url="${2:-}"
      if [[ "${collector_url}" == "" ]]; then
        echo "Missing value for --collector" >&2
        exit 2
      fi
      shift 2
      ;;
    *)
      pass_args+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "${config_path}" ]]; then
  echo "Config not found: ${config_path}" >&2
  exit 1
fi

need="$(
  {
    python3 scripts/login_status.py --config "${config_path}" --need-login || true
    python3 scripts/login_status.py --config "${config_path}" --collector "${collector_url}" --need-login-latest || true
  } | awk 'NF{print $0}' | sort -u
)"

if [[ "${need}" == "" ]]; then
  echo "No accounts need login (based on files + /latest)." >&2
  exit 0
fi

echo "Need login:"
echo "${need}" | sed 's/^/  - /'
echo

while IFS= read -r label; do
  if [[ "${label}" == "" ]]; then
    continue
  fi
  echo "==> ${label}"
  if (( ${#pass_args[@]} )); then
    ./scripts/init_account.sh "${label}" "${pass_args[@]}"
  else
    ./scripts/init_account.sh "${label}"
  fi
done <<< "${need}"

