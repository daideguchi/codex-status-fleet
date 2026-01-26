#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/infra_dashboard"
DEST_ROOT="${DEST_ROOT:-$HOME/doraemon_hq/magic_files}"
DEST_DIR="${DEST_DIR:-$DEST_ROOT/infra_dashboard}"

REPORTS_DIR="$DEST_ROOT/_reports"
OUT_JSON="${OUT_JSON:-$REPORTS_DIR/infra_dashboard.json}"

echo "== deploy ui =="
mkdir -p "$DEST_DIR"
rsync -a --delete --exclude '.DS_Store' -- "$SRC_DIR/" "$DEST_DIR/"

echo "== run sampler once =="
mkdir -p "$REPORTS_DIR"
python3 "$(cd "$(dirname "$0")/.." && pwd)/scripts/infra_dashboard_sample.py" --out "$OUT_JSON" >/dev/null || true

if [ "${INSTALL_LAUNCHAGENT:-1}" = "1" ]; then
  echo "== install launchagent =="
  plist_src="$(cd "$(dirname "$0")/.." && pwd)/launchagents/com.doraemon.infra_dashboard_sample.plist"
  plist_dst="$HOME/Library/LaunchAgents/com.doraemon.infra_dashboard_sample.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cp -f "$plist_src" "$plist_dst"
  launchctl unload -w "$plist_dst" >/dev/null 2>&1 || true
  launchctl load -w "$plist_dst" >/dev/null 2>&1 || true
  launchctl list | grep -F "com.doraemon.infra_dashboard_sample" >/dev/null 2>&1 || true
fi

echo "== done =="
echo "UI:   $DEST_DIR"
echo "JSON: $OUT_JSON"
