#!/usr/bin/env python3
import argparse
import json
import re
import sys
from typing import Any


ACCOUNT_RE = re.compile(r"Account:\s*(?P<email>[^\s]+@[^\s]+)\s*\((?P<plan>[^)]+)\)")


def _make_label(email: str) -> str:
    s = email.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        s = "account"
    return f"acc_{s}"


def _read_text(path: str | None) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _normalize_plan(plan_text: str) -> str | None:
    p = plan_text.strip().lower()
    if "plus" in p:
        return "plus"
    if "pro" in p:
        return "pro"
    if "team" in p:
        return "team"
    if "business" in p:
        return "business"
    if "enterprise" in p:
        return "enterprise"
    if "edu" in p:
        return "edu"
    if "free" in p:
        return "free"
    return None


def _parse_accounts(text: str, ignore_canceled: bool) -> list[dict[str, Any]]:
    accounts_by_email: dict[str, dict[str, Any]] = {}
    canceled_next = False

    for line in text.splitlines():
        if "解約済み" in line and not ignore_canceled:
            canceled_next = True
            continue

        m = ACCOUNT_RE.search(line)
        if not m:
            continue

        email = m.group("email").strip()
        plan_raw = m.group("plan").strip()
        email_key = email.lower()

        entry = accounts_by_email.get(email_key)
        if entry is None:
            entry = {
                "label": _make_label(email),
                "provider": "codex",
                "enabled": not canceled_next,
                "expected_email": email,
            }
            accounts_by_email[email_key] = entry

        plan_type = _normalize_plan(plan_raw)
        if plan_type and "expected_planType" not in entry:
            entry["expected_planType"] = plan_type

        if canceled_next and not ignore_canceled:
            entry.setdefault("note", "解約済み (memo)")
            entry["enabled"] = False

        canceled_next = False

    return list(accounts_by_email.values())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse Codex /status memo text and generate accounts.json skeleton."
    )
    parser.add_argument(
        "--in",
        dest="in_path",
        default="-",
        help="Input text path (default: stdin). Use '-' for stdin.",
    )
    parser.add_argument("--out", default="-", help="Output path (default: stdout).")
    parser.add_argument("--collector-url", default="http://collector:8080/ingest")
    parser.add_argument("--poll", type=int, default=60, help="Poll interval seconds")
    parser.add_argument("--codex-cli-version", default="0.77.0")
    parser.add_argument(
        "--ignore-canceled",
        action="store_true",
        help="Ignore '解約済み' markers and keep all accounts enabled.",
    )
    args = parser.parse_args()

    text = _read_text(args.in_path)
    accounts = _parse_accounts(text, ignore_canceled=args.ignore_canceled)
    if not accounts:
        raise SystemExit("No accounts found (pattern: 'Account: <email> (<plan>)').")

    config: dict[str, Any] = {
        "collector_url": args.collector_url,
        "collector_in_compose": True,
        "poll_interval_sec": args.poll,
        "codex_cli_version": args.codex_cli_version,
        "accounts": sorted(accounts, key=lambda a: a["label"]),
        "agent": {"rpc_timeout_sec": 10.0},
    }

    out_text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if args.out == "-" or args.out is None:
        sys.stdout.write(out_text)
        return 0

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
