#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _derive_base_url(config: dict[str, Any], explicit_base_url: str | None) -> str:
    if explicit_base_url:
        return explicit_base_url.rstrip("/")

    collector_in_compose = bool(config.get("collector_in_compose", True))
    collector_url = str(config.get("collector_url", "")).strip()

    if collector_in_compose:
        # When running with docker-compose, the collector is published to localhost:8080 by default.
        return "http://localhost:8080"

    if collector_url.endswith("/ingest"):
        return collector_url[: -len("/ingest")].rstrip("/")
    return collector_url.rstrip("/")


def _build_registry_payload(config: dict[str, Any]) -> dict[str, Any]:
    accounts = config.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit("config.accounts must be a non-empty array")

    payload_accounts: list[dict[str, Any]] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        label = (acc.get("label") or acc.get("account_label") or "").strip()
        if not label:
            continue
        payload_accounts.append(
            {
                "account_label": label,
                "enabled": acc.get("enabled", True) is not False,
                "expected_email": acc.get("expected_email"),
                "expected_planType": acc.get("expected_planType") or acc.get("expected_plan_type"),
                "note": acc.get("note"),
            }
        )

    if not payload_accounts:
        raise SystemExit("No valid accounts found in config")

    return {"accounts": payload_accounts}


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push accounts.json registry into Collector DB.")
    parser.add_argument("--config", required=True, help="Path to accounts.json")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Collector base URL (e.g. http://localhost:8080). Optional.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace registry with the config list (deletes other registry rows).",
    )
    args = parser.parse_args()

    config = _read_json(args.config)
    base_url = _derive_base_url(config, args.base_url)
    payload = _build_registry_payload(config)

    if args.dry_run:
        sys.stdout.write(json.dumps({"base_url": base_url, "payload": payload}, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0

    url = f"{base_url}/registry"
    if args.replace:
        url = f"{url}?replace=true"
    try:
        res = _post_json(url, payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTPError {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"URLError: {e}") from e

    sys.stdout.write(json.dumps({"ok": True, "url": url, "response": res}, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
