#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _make_label_from_email(email: str) -> str:
    s = email.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return f"acc_{s}" if s else "acc_account"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_emails(args: argparse.Namespace) -> list[str]:
    emails: list[str] = []

    if args.in_path:
        text = Path(args.in_path).read_text(encoding="utf-8", errors="replace")
        emails.extend([line.strip() for line in text.splitlines() if line.strip()])

    if args.email:
        emails.extend(args.email)

    if not emails and not sys.stdin.isatty():
        text = sys.stdin.read()
        emails.extend([line.strip() for line in text.splitlines() if line.strip()])

    out: list[str] = []
    seen: set[str] = set()
    for e in emails:
        e = e.strip()
        if not e or e.startswith("#"):
            continue
        if e in seen:
            continue
        out.append(e)
        seen.add(e)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Add accounts (emails) into accounts.json.")
    parser.add_argument("--config", default="accounts.json", help="Path to accounts.json")
    parser.add_argument(
        "--plan",
        default="plus",
        help="expected_planType to set (default: plus). Use empty string to omit.",
    )
    parser.add_argument("--email", action="append", default=[], help="Email to add (repeatable)")
    parser.add_argument("--in", dest="in_path", default=None, help="File with 1 email per line")
    parser.add_argument(
        "--accounts-dir",
        default=None,
        help="Create accounts/<label> dirs under this path (default: ./accounts)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = _read_json(config_path)
    accounts = cfg.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
        cfg["accounts"] = accounts

    emails = _load_emails(args)
    if not emails:
        raise SystemExit("No emails provided. Use --email, --in, or pipe via stdin.")

    expected_plan = (args.plan or "").strip()

    existing_by_label: dict[str, dict[str, Any]] = {}
    existing_by_email: dict[str, dict[str, Any]] = {}
    for a in accounts:
        if not isinstance(a, dict):
            continue
        label = (a.get("label") or "").strip()
        if label:
            existing_by_label[label] = a
        exp = (a.get("expected_email") or "").strip().lower()
        if exp:
            existing_by_email[exp] = a

    accounts_dir = Path(args.accounts_dir).resolve() if args.accounts_dir else Path("accounts").resolve()

    added = 0
    updated = 0
    for email in emails:
        email_lc = email.strip().lower()
        label = _make_label_from_email(email_lc)

        entry = existing_by_email.get(email_lc) or existing_by_label.get(label)
        if entry is None:
            entry = {"label": label, "enabled": True, "expected_email": email_lc}
            if expected_plan:
                entry["expected_planType"] = expected_plan
            accounts.append(entry)
            existing_by_label[label] = entry
            existing_by_email[email_lc] = entry
            added += 1
        else:
            changed = False
            if not (entry.get("label") or "").strip():
                entry["label"] = label
                changed = True
            if (entry.get("expected_email") or "").strip().lower() != email_lc:
                entry["expected_email"] = email_lc
                changed = True
            if expected_plan and (entry.get("expected_planType") or "").strip() != expected_plan:
                entry["expected_planType"] = expected_plan
                changed = True
            if changed:
                updated += 1

        # Ensure per-account dir exists (optional quality-of-life).
        os.makedirs(accounts_dir / label / ".codex", exist_ok=True)

    _write_json(config_path, cfg)
    print(json.dumps({"ok": True, "added": added, "updated": updated, "config": str(config_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

