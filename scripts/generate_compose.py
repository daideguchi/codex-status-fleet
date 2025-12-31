#!/usr/bin/env python3
import argparse
import json
import os
from typing import Any


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _generate(config: dict[str, Any]) -> str:
    manual_refresh = bool(config.get("manual_refresh", False))
    collector_url = config.get("collector_url", "http://collector:8080/ingest")
    poll_interval_sec = int(config.get("poll_interval_sec", 60))
    codex_cli_version = str(config.get("codex_cli_version", "0.77.0"))
    collector_in_compose = bool(config.get("collector_in_compose", True))
    agent_cfg = config.get("agent") or {}
    rpc_timeout_sec = float(agent_cfg.get("rpc_timeout_sec", 10.0))

    accounts = config.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("config.accounts must be a non-empty array")

    if manual_refresh:
        return (
            "# Generated file. Do not edit by hand.\n"
            "# manual_refresh=true: agents are disabled. Use scripts/refresh_all.py to update.\n"
            "services: {}\n"
        )

    lines: list[str] = []
    lines.append("# Generated file. Do not edit by hand.")
    lines.append("services:")

    for account in accounts:
        label = (account.get("label") or "").strip()
        if not label:
            raise SystemExit("Each account needs a non-empty 'label'")

        enabled = account.get("enabled", True)
        if enabled is False:
            continue

        expected_email = (account.get("expected_email") or "").strip()
        expected_plan_type = (account.get("expected_planType") or "").strip()

        svc_name = f"agent_{label}"
        acc_codex_dir = os.path.join(".", "accounts", label, ".codex")

        lines.append(f"  {svc_name}:")
        lines.append(f"    image: codex-status-agent:{codex_cli_version}")
        lines.append("    build:")
        lines.append("      context: .")
        lines.append("      dockerfile: docker/Dockerfile.agent")
        lines.append("      args:")
        lines.append(f"        CODEX_CLI_VERSION: \"{codex_cli_version}\"")
        lines.append("    environment:")
        lines.append(f"      COLLECTOR_URL: \"{collector_url}\"")
        lines.append(f"      ACCOUNT_LABEL: \"{label}\"")
        lines.append(f"      POLL_INTERVAL_SEC: \"{poll_interval_sec}\"")
        lines.append(f"      RPC_TIMEOUT_SEC: \"{rpc_timeout_sec}\"")
        if expected_email:
            lines.append(f"      EXPECTED_EMAIL: \"{expected_email}\"")
        if expected_plan_type:
            lines.append(f"      EXPECTED_PLAN_TYPE: \"{expected_plan_type}\"")
        lines.append("    volumes:")
        lines.append(f"      - {acc_codex_dir}:/root/.codex")
        if collector_in_compose:
            lines.append("    depends_on:")
            lines.append("      - collector")
        lines.append("    restart: unless-stopped")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to accounts.json")
    parser.add_argument("--out", required=True, help="Output path (compose YAML)")
    args = parser.parse_args()

    config = _read_json(args.config)
    content = _generate(config)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(content)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
