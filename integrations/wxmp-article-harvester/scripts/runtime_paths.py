#!/usr/bin/env python3
"""Shared runtime paths and URL guards for wxmp-article-harvester."""

from __future__ import annotations

import os
import platform
import re
import json
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


APP_NAME = "wxmp-article-harvester"
WCX_COMMIT = "37cf4d5fd6a0677c2137601292f6942ff731d4b9"
WCX_INSTALL_SPEC = f"wcx @ git+https://github.com/lovstudio/wcx.git@{WCX_COMMIT}"


def app_data_root() -> Path:
    override = os.environ.get("WXMP_HARVEST_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return Path(xdg_data_home).expanduser() / APP_NAME if xdg_data_home else Path.home() / ".local" / "share" / APP_NAME


BASE_DIR = app_data_root()
DEFAULT_EXPORT_ROOT = BASE_DIR / "exports"
DEFAULT_LOGIN_PROFILE = BASE_DIR / "profiles" / "login"
DEFAULT_ARTICLE_PROFILE = BASE_DIR / "profiles" / "article-browser"


def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def safe_component(value: str, max_bytes: int = 180) -> str:
    original = (value or "").strip()
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " - ", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .") or "untitled"
    cleaned = truncate_utf8(cleaned, max_bytes)
    if cleaned != original:
        digest = sha256(original.encode("utf-8")).hexdigest()[:8]
        cleaned = truncate_utf8(cleaned, max(1, max_bytes - 11)).rstrip() + f" [{digest}]"
    return cleaned


def normalize_wechat_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("http://"):
        value = "https://" + value[len("http://") :]
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "mp.weixin.qq.com" or parsed.username or parsed.password:
        raise ValueError("only public https://mp.weixin.qq.com/s... article URLs are allowed")
    if not re.fullmatch(r"/s(?:/[^/?#]+)?", parsed.path or ""):
        raise ValueError("only public https://mp.weixin.qq.com/s... article URLs are allowed")
    if parsed.path == "/s":
        query = parse_qs(parsed.query, keep_blank_values=False)
        stable_keys = ("__biz", "mid", "idx", "sn")
        missing = [key for key in stable_keys if not query.get(key)]
        if missing:
            raise ValueError("WeChat query-style article URLs require __biz, mid, idx, and sn")
        stable_pairs = [(key, query[key][0]) for key in stable_keys if query.get(key)]
        canonical_query = urlencode(stable_pairs, safe="=")
    else:
        canonical_query = ""
    return urlunsplit(("https", "mp.weixin.qq.com", parsed.path, canonical_query, ""))


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(lock_path: Path) -> None:
    secure_directory(lock_path.parent)
    payload = json.dumps({"pid": os.getpid(), "created": time.time()})
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
            if not process_is_alive(pid):
                raise ProcessLookupError(pid)
        except (OSError, ValueError, json.JSONDecodeError):
            lock_path.unlink(missing_ok=True)
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        else:
            raise RuntimeError(f"another wxmp harvest is active (pid={pid}, lock={lock_path})")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def release_lock(lock_path: Path) -> None:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if int(data.get("pid", 0)) == os.getpid():
            lock_path.unlink(missing_ok=True)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
