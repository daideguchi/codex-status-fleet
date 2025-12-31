#!/usr/bin/env python3
import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _is_codex_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p in ("codex", "openai_codex", "openai")


def _labels_from_config(config_path: Path) -> list[str]:
    cfg = _read_json(config_path)
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list):
        raise SystemExit("config.accounts must be an array")
    out: list[str] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        if acc.get("enabled", True) is False:
            continue
        provider = (acc.get("provider") or "codex").strip().lower()
        if not _is_codex_provider(provider):
            continue
        out.append(label)
    return out


def _pick_backup(codex_dir: Path) -> Path | None:
    if not codex_dir.is_dir():
        return None
    candidates: list[Path] = []
    candidates.extend(sorted(codex_dir.glob("auth.json.bak.*"), reverse=True))
    candidates.extend(sorted(codex_dir.glob("auth.json.dup_*"), reverse=True))
    for p in candidates:
        if p.is_file():
            return p
    return None


def _restore_one(accounts_dir: Path, label: str, dry_run: bool) -> bool:
    auth_path = accounts_dir / label / ".codex" / "auth.json"
    if auth_path.is_file():
        print(f"==> {label}: already has auth.json")
        return True

    backup = _pick_backup(auth_path.parent)
    if not backup:
        print(f"==> {label}: no backup found (needs login)")
        return False

    print(f"==> {label}: restore {backup.name} -> auth.json")
    if dry_run:
        return True

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, auth_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore accounts/<label>/.codex/auth.json from backups (auth.json.bak.* / auth.json.dup_*)."
    )
    parser.add_argument("--config", default="accounts.json", help="Path to accounts.json (default: accounts.json)")
    parser.add_argument("--label", action="append", help="Restore only this label (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without modifying files")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    accounts_dir = root_dir / "accounts"
    config_path = (Path(args.config).resolve() if Path(args.config).is_absolute() else (root_dir / args.config))

    if args.label:
        labels = [l for l in args.label if l]
    else:
        labels = _labels_from_config(config_path)

    ok = True
    for label in labels:
        if not _restore_one(accounts_dir, label, args.dry_run):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

