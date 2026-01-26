#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _run_cmd(argv: list[str], timeout_sec: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return 125, "", f"exec_error: {e}"


@dataclass(frozen=True)
class DiskStat:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_pct: float
    mountpoint: str
    filesystem: str


def _df_stat(path: Path, timeout_sec: float) -> DiskStat | None:
    rc, out, _err = _run_cmd(["/bin/df", "-kP", str(path)], timeout_sec=timeout_sec)
    if rc != 0:
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = re.split(r"\s+", lines[1])
    if len(parts) < 6:
        return None
    filesystem = parts[0]
    try:
        total_k = int(parts[1])
        used_k = int(parts[2])
        avail_k = int(parts[3])
    except Exception:
        return None
    mountpoint = parts[5]
    total_b = total_k * 1024
    used_b = used_k * 1024
    free_b = max(0, avail_k * 1024)
    used_pct = (used_b / total_b * 100.0) if total_b > 0 else 0.0
    return DiskStat(
        total_bytes=total_b,
        used_bytes=used_b,
        free_bytes=free_b,
        used_pct=used_pct,
        mountpoint=mountpoint,
        filesystem=filesystem,
    )


def _default_iface(timeout_sec: float) -> str | None:
    rc, out, _err = _run_cmd(["/sbin/route", "-n", "get", "default"], timeout_sec=timeout_sec)
    if rc != 0:
        return None
    for ln in out.splitlines():
        if ln.strip().startswith("interface:"):
            return ln.split(":", 1)[1].strip() or None
    return None


def _net_bytes(interface: str, timeout_sec: float) -> tuple[int, int] | None:
    rc, out, _err = _run_cmd(["/usr/sbin/netstat", "-ibn"], timeout_sec=timeout_sec)
    if rc != 0:
        return None
    lines = [ln.rstrip("\n") for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None
    header = re.split(r"\s+", lines[0].strip())
    try:
        idx_name = header.index("Name")
        idx_ibytes = header.index("Ibytes")
        idx_obytes = header.index("Obytes")
    except ValueError:
        return None
    rx = 0
    tx = 0
    for ln in lines[1:]:
        parts = re.split(r"\s+", ln.strip())
        if len(parts) <= max(idx_obytes, idx_ibytes, idx_name):
            continue
        if parts[idx_name] != interface:
            continue
        try:
            rx += int(parts[idx_ibytes])
            tx += int(parts[idx_obytes])
        except Exception:
            continue
    return rx, tx


def _parse_tailscale_ping(out: str) -> dict:
    # Example: pong from laptop-jdbeqa2t (100.127.188.120) via 192.168.11.4:41641 in 18ms
    m = re.search(r"pong from (?P<name>[^ ]+) .*? in (?P<ms>[0-9.]+)ms", out)
    via_m = re.search(r" via (?P<via>[^ ]+) ", out)
    ip_m = re.search(r"\((?P<ip>[0-9a-fA-F:.]+)\)", out)
    return {
        "ok": bool(m),
        "latency_ms": float(m.group("ms")) if m else None,
        "via": via_m.group("via") if via_m else None,
        "ip": ip_m.group("ip") if ip_m else None,
        "raw": out.strip()[:240],
    }


def _tailscale_app_bin() -> Path | None:
    p = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return p if p.exists() else None


def _tailscale_ping(target: str, timeout_sec: float) -> dict:
    ts = _tailscale_app_bin()
    if not ts:
        return {"ok": False, "error": "tailscale_app_missing"}
    rc, out, err = _run_cmd([str(ts), "ping", "-c", "1", target], timeout_sec=timeout_sec)
    if rc != 0:
        return {"ok": False, "error": (err or out or "").strip()[:240]}
    return _parse_tailscale_ping(out)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _probe_lenovo_drives_via_ssh(timeout_sec: float) -> dict:
    # Keep this cheap: a single PowerShell command, no WMI.
    cmd = (
        "[System.IO.DriveInfo]::GetDrives() | "
        "Where-Object { $_.IsReady -and $_.DriveType -eq 'Fixed' } | "
        "ForEach-Object { '{0}|{1}|{2}' -f $_.Name,$_.TotalSize,$_.AvailableFreeSpace }"
    )
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-J",
        "acer",
        "lenovo-doraemon",
        "powershell",
        "-NoProfile",
        "-Command",
        cmd,
    ]
    rc, out, err = _run_cmd(argv, timeout_sec=timeout_sec)
    if rc != 0:
        return {"ok": False, "error": (err or out or "").strip()[:240]}
    drives: list[dict] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        name, total_s, free_s = (ln.split("|", 2) + ["", ""])[:3]
        try:
            total = int(float(total_s))
            free = int(float(free_s))
        except Exception:
            continue
        used = max(0, total - free)
        used_pct = (used / total * 100.0) if total > 0 else 0.0
        drives.append(
            {
                "name": name,
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": used,
                "used_pct": used_pct,
            }
        )
    return {"ok": True, "drives": drives}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample lightweight infra metrics for mobile dashboard.")
    parser.add_argument(
        "--out",
        default=str(Path("~/doraemon_hq/magic_files/_reports/infra_dashboard.json").expanduser()),
        help="Output JSON path (served by /files).",
    )
    parser.add_argument(
        "--state",
        default=str(Path("~/.cache/codex_status_fleet/infra_dashboard_state.json").expanduser()),
        help="Local state for rate calculation / backoff.",
    )
    parser.add_argument("--timeout-sec", type=float, default=2.0, help="Per-command timeout (seconds).")
    parser.add_argument("--probe-lenovo-sec", type=float, default=15.0, help="Lenovo probe timeout (seconds).")
    parser.add_argument("--lenovo-min-interval-sec", type=float, default=600.0, help="Min interval between Lenovo probes.")
    args = parser.parse_args()

    out_path = Path(args.out).expanduser()
    state_path = Path(args.state).expanduser()

    now_epoch = time.time()
    state: dict = _read_json(state_path) or {}

    errors: list[str] = []

    # Storage targets (keep small; df is O(1), but network mounts can still hang → timeout).
    targets: list[tuple[str, Path, bool]] = [
        ("Mac Data", Path("/System/Volumes/Data"), True),
        ("Mac Root", Path("/"), True),
        ("Acer Workspace (SMB)", Path("~/mounts/workspace").expanduser(), False),
        ("Lenovo Share (SMB)", Path("~/mounts/lenovo_share_real").expanduser(), False),
        ("Lenovo Share (alias)", Path("~/mounts/lenovo_share").expanduser(), False),
    ]
    storage: list[dict] = []
    for name, p, is_local in targets:
        st: dict = {"name": name, "path": str(p), "is_local": is_local}
        if is_local:
            try:
                st["exists"] = p.exists()
            except Exception:
                st["exists"] = None
        df = _df_stat(p, timeout_sec=args.timeout_sec)
        if df:
            # os.path.ismount can be flaky with APFS volumes and symlinks; trust df().
            st["mounted"] = str(p) == df.mountpoint
            st.update(
                {
                    "filesystem": df.filesystem,
                    "mountpoint": df.mountpoint,
                    "total_bytes": df.total_bytes,
                    "used_bytes": df.used_bytes,
                    "free_bytes": df.free_bytes,
                    "used_pct": df.used_pct,
                }
            )
        else:
            st["mounted"] = False
            st["error"] = "df_failed_or_timeout"
        storage.append(st)

    # Network throughput (rx/tx based on interface counters).
    iface = _default_iface(timeout_sec=args.timeout_sec) or ""
    rx = tx = None
    rate = {}
    if iface:
        nb = _net_bytes(iface, timeout_sec=args.timeout_sec)
        if nb:
            rx, tx = nb
            last = state.get("net") or {}
            last_ts = float(last.get("ts_epoch") or 0.0)
            last_rx = int(last.get("rx_bytes") or 0)
            last_tx = int(last.get("tx_bytes") or 0)
            dt = now_epoch - last_ts if last_ts else 0.0
            if dt >= 1.0 and dt <= 600.0:
                rate = {
                    "sample_interval_sec": dt,
                    "rx_bps": max(0.0, (rx - last_rx) / dt),
                    "tx_bps": max(0.0, (tx - last_tx) / dt),
                }
            state["net"] = {"ts_epoch": now_epoch, "rx_bytes": rx, "tx_bytes": tx}
        else:
            errors.append("netstat_failed")
    else:
        errors.append("default_iface_unknown")

    # Tailscale reachability (cheap pings; 1 packet).
    pings: dict[str, dict] = {}
    for target in ("acer-dai", "laptop-jdbeqa2t"):
        pings[target] = _tailscale_ping(target, timeout_sec=min(3.0, args.timeout_sec + 1.0))

    # Existing watchdogs (optional).
    reports_dir = out_path.parent
    wifi_watchdog = _read_json(reports_dir / "wifi_watchdog.json")

    # Remote Lenovo drive free (low frequency; backoff via state).
    lenovo = state.get("lenovo") or {}
    last_probe = float(lenovo.get("ts_epoch") or 0.0)
    do_probe = (now_epoch - last_probe) >= float(args.lenovo_min_interval_sec)
    lenovo_result = lenovo.get("result") if isinstance(lenovo.get("result"), dict) else None
    if do_probe:
        res = _probe_lenovo_drives_via_ssh(timeout_sec=args.probe_lenovo_sec)
        lenovo_result = {"ts": _now_iso_utc(), **res}
        state["lenovo"] = {"ts_epoch": now_epoch, "result": lenovo_result}

    payload = {
        "ts": _now_iso_utc(),
        "ts_epoch": now_epoch,
        "host": {
            "hostname": os.uname().nodename,
            "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        },
        "storage": storage,
        "network": {
            "default_iface": iface or None,
            "rx_bytes": rx,
            "tx_bytes": tx,
            "rate": rate,
            "tailscale_ping": pings,
            "wifi_watchdog": wifi_watchdog,
        },
        "remote": {"lenovo": lenovo_result},
        "errors": errors,
    }

    try:
        _atomic_write_json(out_path, payload)
    except Exception as e:  # noqa: BLE001
        print(f"ERR: failed to write out JSON: {e}", file=sys.stderr)
        return 1

    try:
        _atomic_write_json(state_path, state)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
