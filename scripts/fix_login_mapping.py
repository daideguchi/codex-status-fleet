#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _extract_email_from_auth_json(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None

    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    id_token = id_token.strip()
    if not _JWT_RE.match(id_token):
        return None

    try:
        payload_raw = _b64url_decode(id_token.split(".")[1])
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    for key in ("email", "preferred_username", "upn", "unique_name"):
        value = payload.get(key)
        if isinstance(value, str):
            candidate = value.strip().lower()
            if _EMAIL_RE.match(candidate):
                return candidate
    return None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AuthFile:
    src_label: str
    expected_email: str | None
    actual_email: str | None
    src_path: Path
    mtime: float
    tmp_path: Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fix misplaced per-account ~/.codex/auth.json files by decoding tokens.id_token email and moving into the correct label dir."
    )
    parser.add_argument("--config", default="accounts.json", help="Path to accounts.json")
    parser.add_argument(
        "--accounts-dir",
        default=None,
        help="Override accounts dir (default: ../accounts relative to this script)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = _read_json(config_path)
    accounts = cfg.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("config.accounts must be a non-empty array")

    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    accounts_dir = Path(args.accounts_dir).resolve() if args.accounts_dir else project_dir / "accounts"

    # label -> expected_email
    expected_by_label: dict[str, str | None] = {}
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or "").strip()
        if not label:
            continue
        expected = (acc.get("expected_email") or "").strip().lower() or None
        expected_by_label[label] = expected

    timestamp = time.strftime("%Y%m%d_%H%M%S")

    auth_files: list[AuthFile] = []
    for label, expected in expected_by_label.items():
        src_path = accounts_dir / label / ".codex" / "auth.json"
        if not src_path.is_file():
            continue
        actual = _extract_email_from_auth_json(src_path)
        mtime = src_path.stat().st_mtime
        tmp_path = src_path.with_name(f"auth.json.tmp_{timestamp}_{label}")
        auth_files.append(
            AuthFile(
                src_label=label,
                expected_email=expected,
                actual_email=actual,
                src_path=src_path,
                mtime=mtime,
                tmp_path=tmp_path,
            )
        )

    if not auth_files:
        print("ok: no auth.json files found; nothing to fix")
        return 0

    # Group by actual_email for assignment.
    by_email: dict[str, list[AuthFile]] = {}
    unknown: list[AuthFile] = []
    for f in auth_files:
        if not f.actual_email:
            unknown.append(f)
            continue
        by_email.setdefault(f.actual_email, []).append(f)

    planned: list[str] = []

    # Phase 1: move all known+unknown auth.json to tmp to break cycles.
    for f in auth_files:
        planned.append(f"mv {f.src_path} -> {f.tmp_path}")

    # Phase 2: for each label, place the tmp file whose actual_email matches expected_email.
    assignments: list[tuple[AuthFile, Path]] = []
    dup_moves: list[tuple[AuthFile, Path]] = []
    missing_expected: list[tuple[str, str]] = []

    for label, expected in expected_by_label.items():
        if not expected:
            continue
        candidates = by_email.get(expected, [])
        if not candidates:
            missing_expected.append((label, expected))
            continue

        # Choose the most recently modified auth as the primary.
        candidates_sorted = sorted(candidates, key=lambda x: x.mtime, reverse=True)
        primary = candidates_sorted[0]
        dest = accounts_dir / label / ".codex" / "auth.json"
        assignments.append((primary, dest))

        for dup in candidates_sorted[1:]:
            dup_dest = accounts_dir / dup.src_label / ".codex" / f"auth.json.dup_{timestamp}"
            dup_moves.append((dup, dup_dest))

    # Any tmp auth files with emails that aren't expected anywhere should be restored as backup in place.
    expected_emails = {e for e in expected_by_label.values() if e}
    unassigned: list[AuthFile] = []
    for email, files in by_email.items():
        if email in expected_emails:
            continue
        unassigned.extend(files)

    for f in unassigned:
        backup_dest = accounts_dir / f.src_label / ".codex" / f"auth.json.unmapped_{timestamp}"
        dup_moves.append((f, backup_dest))

    # Build planned output.
    for src, dest in assignments:
        planned.append(f"mv {src.tmp_path} -> {dest}  # {src.actual_email}")
    for src, dest in dup_moves:
        planned.append(f"mv {src.tmp_path} -> {dest}  # dup {src.actual_email}")
    for f in unknown:
        dest = accounts_dir / f.src_label / ".codex" / f"auth.json.unknown_{timestamp}"
        planned.append(f"mv {f.tmp_path} -> {dest}  # unknown email")

    print("== Planned actions ==")
    for line in planned:
        print(line)

    if args.dry_run:
        print("dry-run: no changes applied")
        return 0

    # Apply Phase 1
    for f in auth_files:
        f.tmp_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f.src_path), str(f.tmp_path))

    # Apply assignments (place primaries). Ensure dest dirs exist.
    used_tmp: set[Path] = set()
    for src, dest in assignments:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            existing_backup = dest.with_name(f"auth.json.preexisting_{timestamp}")
            shutil.move(str(dest), str(existing_backup))
        shutil.move(str(src.tmp_path), str(dest))
        used_tmp.add(src.tmp_path)

    # Move duplicates/unmapped/unknown to backups.
    for src, dest in dup_moves:
        if src.tmp_path in used_tmp:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.tmp_path.exists():
            shutil.move(str(src.tmp_path), str(dest))
            used_tmp.add(src.tmp_path)

    for f in unknown:
        if f.tmp_path in used_tmp:
            continue
        dest = accounts_dir / f.src_label / ".codex" / f"auth.json.unknown_{timestamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f.tmp_path.exists():
            shutil.move(str(f.tmp_path), str(dest))
            used_tmp.add(f.tmp_path)

    # Any leftover tmp files (shouldn't happen) are restored as backup.
    leftovers = [f for f in auth_files if f.tmp_path.exists()]
    for f in leftovers:
        dest = accounts_dir / f.src_label / ".codex" / f"auth.json.leftover_{timestamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f.tmp_path), str(dest))

    print("ok: applied")
    if missing_expected:
        print("missing logins (no token for expected email):")
        for label, expected in missing_expected:
            print(f"- {label}: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
