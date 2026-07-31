#!/usr/bin/env python3
"""Deterministic dependency preflight without installing or upgrading anything."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from runtime_paths import BASE_DIR, WCX_COMMIT, WCX_INSTALL_SPEC


def wcx_python_command(wcx_path: str) -> list[str]:
    try:
        first_line = Path(wcx_path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return [sys.executable]
    if not first_line.startswith("#!"):
        return [sys.executable]
    parts = shlex.split(first_line[2:].strip())
    if parts and Path(parts[0]).name == "env" and len(parts) >= 2:
        resolved = shutil.which(parts[-1])
        return [resolved or parts[-1]]
    return parts or [sys.executable]


def check_environment() -> dict:
    checks: dict[str, dict] = {}
    wcx_path = shutil.which("wcx")
    checks["wcx"] = {"ok": bool(wcx_path), "path": wcx_path or ""}
    if wcx_path:
        version = subprocess.run([wcx_path, "--version"], capture_output=True, text=True, timeout=10, check=False)
        checks["wcx"]["version"] = ((version.stdout or "") + (version.stderr or "")).strip()
        compatibility = subprocess.run(
            [
                *wcx_python_command(wcx_path),
                "-c",
                "from wcx import cache, config; from wcx.fetcher import Fetcher; assert hasattr(Fetcher, 'list_articles')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        checks["wcx_api"] = {
            "ok": compatibility.returncode == 0,
            "tested_commit": WCX_COMMIT,
            "install": f"python3 -m pip install -U '{WCX_INSTALL_SPEC}'",
            "error": (compatibility.stderr or compatibility.stdout).strip(),
        }
    else:
        checks["wcx_api"] = {
            "ok": False,
            "tested_commit": WCX_COMMIT,
            "install": f"python3 -m pip install -U '{WCX_INSTALL_SPEC}'",
            "error": "wcx executable is missing",
        }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
            browser_ok = bool(executable and os.path.exists(executable))
        checks["playwright"] = {"ok": browser_ok, "chromium": executable or ""}
    except Exception as exc:
        checks["playwright"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    checks["metaso"] = {
        "ok": bool(os.environ.get("METASO_API_KEY")),
        "required": False,
        "note": "only used with explicit --allow-metaso",
    }
    required_ok = checks["wcx"]["ok"] and checks["wcx_api"]["ok"] and checks["playwright"]["ok"]
    return {
        "ok": required_ok,
        "python": sys.version.split()[0],
        "runtime_root": str(BASE_DIR),
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check wxmp-article-harvester dependencies")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check_environment()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for name, item in result["checks"].items():
            print(f"{'OK' if item['ok'] else 'MISSING'}  {name}")
        print(f"runtime_root: {result['runtime_root']}")
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
