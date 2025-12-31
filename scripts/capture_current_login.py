#!/usr/bin/env python3
import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from typing import Any


def _make_label_from_email(email: str) -> str:
    s = email.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return f"acc_{s}" if s else "acc_account"


def _rpc_request(method: str, params: Any) -> dict[str, Any]:
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "codex-status-fleet-capture", "version": "0.1.0"}},
            }
        )
        send({"id": 2, "method": method, "params": params})

        deadline = time.time() + 10
        while time.time() < deadline:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") != 2:
                continue
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result") if isinstance(msg.get("result"), dict) else {"result": msg.get("result")}

        raise TimeoutError(f"timeout waiting for {method}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _upsert_account_in_config(config_path: str, account: dict[str, Any]) -> None:
    cfg = _read_json(config_path)
    accounts = cfg.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
        cfg["accounts"] = accounts

    label = account["label"]
    for i, a in enumerate(accounts):
        if isinstance(a, dict) and (a.get("label") or "").strip() == label:
            merged = dict(a)
            merged.update(account)
            accounts[i] = merged
            _write_json(config_path, cfg)
            return

    accounts.append(account)
    _write_json(config_path, cfg)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy ~/.codex/auth.json into 08_codex_status_fleet/accounts/<label>/.codex/auth.json to preserve the current session."
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Destination label (must match accounts.json label). If omitted, derived from --expected-email.",
    )
    parser.add_argument(
        "--expected-email",
        default=None,
        help="Optional email (for accounts.json upsert). If omitted, accounts.json won't be updated.",
    )
    parser.add_argument(
        "--expected-plan-type",
        default=None,
        help="Optional planType like plus/pro (for accounts.json upsert).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional accounts.json path to upsert this account entry into.",
    )
    parser.add_argument(
        "--accounts-dir",
        default=None,
        help="Override accounts dir (default: ../accounts relative to this script)",
    )
    args = parser.parse_args()

    label = (args.label or "").strip()
    if not label:
        if args.expected_email:
            label = _make_label_from_email(args.expected_email)
        else:
            raise SystemExit("Provide --label or --expected-email (to derive label).")

    script_dir = os.path.abspath(os.path.dirname(__file__))
    project_dir = os.path.abspath(os.path.join(script_dir, ".."))
    accounts_dir = (
        os.path.abspath(args.accounts_dir)
        if args.accounts_dir
        else os.path.join(project_dir, "accounts")
    )

    src_auth = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    if not os.path.isfile(src_auth):
        raise SystemExit(f"Source auth not found: {src_auth}")

    dest_home = os.path.join(accounts_dir, label)
    dest_codex = os.path.join(dest_home, ".codex")
    os.makedirs(dest_codex, exist_ok=True)
    dest_auth = os.path.join(dest_codex, "auth.json")

    shutil.copy2(src_auth, dest_auth)
    print(f"ok: copied {src_auth} -> {dest_auth}")

    if args.config and args.expected_email:
        account_entry: dict[str, Any] = {
            "label": label,
            "provider": "codex",
            "enabled": True,
            "expected_email": args.expected_email,
        }
        if args.expected_plan_type:
            account_entry["expected_planType"] = args.expected_plan_type
        _upsert_account_in_config(args.config, account_entry)
        print(f"ok: upserted into {args.config}")
    elif args.config and not args.expected_email:
        print("note: --config was given but --expected-email was not; accounts.json was not modified.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
