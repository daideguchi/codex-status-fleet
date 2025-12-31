#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

config_path="${1:-accounts.json}"

if [[ -f "${config_path}" ]]; then
  collector_in_compose="$(python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],"r",encoding="utf-8")); print("true" if cfg.get("collector_in_compose", True) else "false")' "${config_path}")"
  if [[ "${collector_in_compose}" == "true" ]]; then
    docker compose -f docker-compose.yml -f docker-compose.accounts.yml down --remove-orphans
  else
    docker compose -f docker-compose.accounts.yml down --remove-orphans
  fi
else
  docker compose -f docker-compose.yml -f docker-compose.accounts.yml down --remove-orphans
fi
