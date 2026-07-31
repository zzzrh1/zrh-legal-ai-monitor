#!/usr/bin/env python3
"""Run wcx through a small recovery wrapper."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from runtime_paths import DEFAULT_EXPORT_ROOT, WCX_INSTALL_SPEC, secure_directory

SCRIPT_DIR = Path(__file__).resolve().parent
REFRESH_SCRIPT = SCRIPT_DIR / "refresh_token_playwright.py"
MIN_WCX_VERSION = (0, 2, 0)

EXPIRY_MARKERS = (
    "登录失效",
    "凭证无效",
    "凭证失效",
    "token失效",
    "token 失效",
    "cookie失效",
    "cookie 失效",
    "未登录",
    "请先登录",
    "请先执行 wcx login",
    "认证失败",
    "Auth failed",
    "invalid session",
    "Re-login needed",
    "ret=200003",
)

BROKEN_WCX_MARKERS = (
    "HTTP Error 501",
    "501: Not Implemented",
    "501 Not Implemented",
)


def parse_version(text: str) -> Tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def ensure_wcx_version() -> None:
    try:
        result = subprocess.run(["wcx", "--version"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None

    installed = parse_version((result.stdout + result.stderr) if result else "")
    if installed < MIN_WCX_VERSION:
        reason = "not installed or version is unknown" if installed == (0, 0, 0) else f"too old: {installed}"
        raise SystemExit(
            "ERROR: wcx is " + reason + ". Install it explicitly before running this skill:\n"
            f"  python3 -m pip install -U '{WCX_INSTALL_SPEC}'"
        )


def inject_default_out(args: List[str]) -> List[str]:
    if not args or args[0] != "export":
        return args
    if any(arg == "--out" or arg == "-o" or arg.startswith("--out=") for arg in args):
        return args
    secure_directory(DEFAULT_EXPORT_ROOT)
    return args + ["--out", str(DEFAULT_EXPORT_ROOT)]


def run_wcx(args: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        ["wcx", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    rc = proc.wait()
    return subprocess.CompletedProcess(proc.args, rc, "".join(captured), "")


def has_marker(output: str, markers: tuple[str, ...]) -> bool:
    return any(marker in (output or "") for marker in markers)


def classify_result(result: subprocess.CompletedProcess) -> str:
    if has_marker(result.stdout, BROKEN_WCX_MARKERS):
        return "broken_wcx"
    if has_marker(result.stdout, EXPIRY_MARKERS):
        return "expired"
    if result.returncode == 0:
        return "success"
    return "error"


def refresh_credentials(timeout: int, profile: str | None) -> int:
    cmd = [sys.executable, str(REFRESH_SCRIPT), "--timeout", str(timeout)]
    if profile:
        cmd += ["--profile-dir", profile]
    return subprocess.run(cmd).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run wcx with credential refresh and known-bug recovery")
    parser.add_argument("--no-retry", action="store_true", help="disable automatic recovery")
    parser.add_argument("--refresh-timeout", type=int, default=180, help="seconds to wait for QR login")
    parser.add_argument("--profile-dir", help="Playwright profile directory for login refresh")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="wcx args after --")
    args = parser.parse_args()

    wcx_args = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
    if not wcx_args:
        raise SystemExit("ERROR: missing wcx command, e.g. -- search 润宇创业笔记")

    ensure_wcx_version()
    wcx_args = inject_default_out(wcx_args)
    result = run_wcx(wcx_args)

    classification = classify_result(result)
    if classification == "success":
        raise SystemExit(0)

    if classification == "broken_wcx":
        print(
            "ERROR: detected the known wcx 501 body-fetch bug. "
            "Upgrade wcx explicitly, then retry:\n"
            "  python3 -m pip install -U --force-reinstall "
            f"'{WCX_INSTALL_SPEC}'",
            file=sys.stderr,
        )
        raise SystemExit(result.returncode or 1)

    if args.no_retry or classification != "expired":
        raise SystemExit(result.returncode or 1)

    print("[wxmp-harvester] credentials expired; refreshing and retrying once", file=sys.stderr)
    rc = refresh_credentials(args.refresh_timeout, args.profile_dir)
    if rc != 0:
        raise SystemExit(rc)
    retry = run_wcx(wcx_args)
    retry_classification = classify_result(retry)
    if retry_classification != "success":
        print(f"ERROR: wcx retry finished with status={retry_classification}", file=sys.stderr)
        raise SystemExit(retry.returncode or 1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
