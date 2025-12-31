#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "" ]]; then
  echo "Usage: $0 <account_label>" >&2
  exit 2
fi

label="$1"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acc_home="${root_dir}/accounts/${label}"

if [[ ! -d "${acc_home}/.codex" ]]; then
  echo "Auth not found: ${acc_home}/.codex" >&2
  echo "Run: ./scripts/init_account.sh ${label}" >&2
  exit 1
fi

HOME="${acc_home}" python3 - <<'PY'
import json, select, subprocess, time

p = subprocess.Popen(
    ["codex", "app-server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

def send(obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()

send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-status-fleet-probe", "version": "0.1.0"}}})
send({"id": 2, "method": "account/rateLimits/read", "params": None})

deadline = time.time() + 10
rate_limits = None
while time.time() < deadline:
    r, _, _ = select.select([p.stdout], [], [], 0.5)
    if p.stdout in r:
        line = p.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("id") == 2:
            rate_limits = msg
        if rate_limits is not None:
            break

try:
    p.terminate()
    p.wait(timeout=2)
except Exception:
    try:
        p.kill()
    except Exception:
        pass

if rate_limits is None:
    raise SystemExit("No response for account/rateLimits/read")
print(json.dumps(rate_limits, ensure_ascii=False, indent=2))
PY
