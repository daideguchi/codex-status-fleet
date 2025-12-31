#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

config_path="${1:-accounts.json}"
out_path="docker-compose.accounts.yml"

python3 scripts/ensure_account_dirs.py --config "${config_path}"
python3 scripts/generate_compose.py --config "${config_path}" --out "${out_path}"

export CODEX_CLI_VERSION="$(python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],"r",encoding="utf-8")); print(str(cfg.get("codex_cli_version","0.77.0")))' "${config_path}")"
export RPC_TIMEOUT_SEC="$(python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],"r",encoding="utf-8")); agent=cfg.get("agent") or {}; print(str(agent.get("rpc_timeout_sec",10.0)))' "${config_path}")"

collector_in_compose="$(python3 -c 'import json,sys; cfg=json.load(open(sys.argv[1],"r",encoding="utf-8")); print("true" if cfg.get("collector_in_compose", True) else "false")' "${config_path}")"
if [[ "${collector_in_compose}" == "true" ]]; then
  docker compose -f docker-compose.yml -f "${out_path}" up -d --build --remove-orphans
else
  docker compose -f "${out_path}" up -d --build --remove-orphans
fi

if [[ "${collector_in_compose}" == "true" ]]; then
  echo "==> waiting for collector on http://localhost:8080/healthz"
  for _ in {1..30}; do
    if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

echo "==> pushing registry"
python3 scripts/push_registry.py --config "${config_path}" --replace
