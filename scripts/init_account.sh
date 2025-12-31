#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "" ]]; then
  echo "Usage: $0 <account_label> [codex login args...]" >&2
  exit 2
fi

label="$1"
shift

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acc_home="${root_dir}/accounts/${label}"

mkdir -p "${acc_home}"

echo "==> Logging in for account '${label}'"
echo "    HOME=${acc_home}"
echo "    Auth will be stored under: ${acc_home}/.codex/"
echo

HOME="${acc_home}" codex login "$@"

