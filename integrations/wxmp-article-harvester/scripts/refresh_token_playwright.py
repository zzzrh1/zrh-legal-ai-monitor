#!/usr/bin/env python3
"""Refresh wcx credentials from mp.weixin.qq.com using Playwright."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from typing import Iterable

from runtime_paths import DEFAULT_LOGIN_PROFILE, WCX_INSTALL_SPEC, secure_directory

MP_HOME = "https://mp.weixin.qq.com/"
TOKEN_RE = re.compile(r"[?&]token=(\d+)")


def require_wcx() -> None:
    if shutil.which("wcx") is None:
        raise SystemExit(
            f"ERROR: wcx not found. Install with: python3 -m pip install '{WCX_INSTALL_SPEC}'"
        )


def cookie_header(cookies: Iterable[dict]) -> str:
    pairs = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain", "")
        if not name or value is None:
            continue
        normalized_domain = domain.lstrip(".").lower()
        if normalized_domain != "weixin.qq.com" and not normalized_domain.endswith(".weixin.qq.com"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def wcx_login(session_id: str, cookie: str) -> None:
    result = subprocess.run(
        ["wcx", "login"],
        input=f"{session_id}\n{cookie}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "凭证有效" not in output:
        sys.stderr.write(output)
        raise SystemExit("ERROR: wcx rejected refreshed credentials")
    print(output.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh wcx token/cookie with Playwright")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for QR login")
    parser.add_argument("--profile-dir", default=str(DEFAULT_LOGIN_PROFILE), help="persistent browser profile")
    parser.add_argument("--headless", action="store_true", help="try headless; only works with a live session")
    parser.add_argument("--keep-open", action="store_true", help="leave browser open after login")
    args = parser.parse_args()

    require_wcx()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(
            "ERROR: Python Playwright is not available. Install with: "
            f"{sys.executable} -m pip install playwright && {sys.executable} -m playwright install chromium"
        ) from exc

    from pathlib import Path

    profile_dir = secure_directory(Path(args.profile_dir).expanduser())

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MP_HOME, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + args.timeout
        dashboard_session_id = None
        notified = False
        while time.time() < deadline:
            token_match = TOKEN_RE.search(page.url)
            if token_match:
                dashboard_session_id = token_match.group(1)
                break
            if not args.headless and not notified:
                print(f"请在打开的 Chrome 窗口扫码登录微信公众号后台，最多等待 {args.timeout} 秒。", file=sys.stderr)
                notified = True
            page.wait_for_timeout(3000)

        if not dashboard_session_id:
            context.close()
            raise SystemExit("ERROR: login timed out; no token found in current URL")

        cookie = cookie_header(context.cookies())
        if not cookie:
            context.close()
            raise SystemExit("ERROR: no mp.weixin.qq.com cookies found in Playwright context")

        print(f"cookie_pairs={cookie.count('=')}", file=sys.stderr)
        wcx_login(dashboard_session_id, cookie)

        if not args.keep_open:
            context.close()


if __name__ == "__main__":
    main()
