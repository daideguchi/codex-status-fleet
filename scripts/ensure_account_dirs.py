#!/usr/bin/env python3
import argparse
import json
import os
from typing import Any


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create per-account dirs under ./accounts/")
    parser.add_argument("--config", required=True, help="Path to accounts.json")
    args = parser.parse_args()

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    accounts_root = os.path.join(root_dir, "accounts")
    os.makedirs(accounts_root, exist_ok=True)

    config = _read_json(args.config)
    accounts = config.get("accounts") or []
    if not isinstance(accounts, list):
        raise SystemExit("config.accounts must be an array")

    created = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        # Always create dirs even if disabled, so login can be done later.
        acc_home = os.path.join(accounts_root, label)
        codex_dir = os.path.join(acc_home, ".codex")
        os.makedirs(codex_dir, exist_ok=True)
        created += 1

    print(f"ok: created/ensured {created} account dirs under {accounts_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

