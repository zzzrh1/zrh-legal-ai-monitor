#!/usr/bin/env python3
"""Orchestrate wxmp search/fetch/export and full-text fill."""

from __future__ import annotations

import argparse
import atexit
import csv
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import metaso_reader
import browser_reader
from runtime_paths import (
    BASE_DIR,
    DEFAULT_EXPORT_ROOT,
    WCX_INSTALL_SPEC,
    acquire_lock,
    normalize_wechat_url,
    release_lock,
    safe_component,
    secure_directory,
)


SCRIPT_DIR = Path(__file__).resolve().parent
WCX_RUN = SCRIPT_DIR / "wcx_run.py"
WCX_BATCH_FETCH = SCRIPT_DIR / "wcx_batch_fetch.py"
MIN_FULLTEXT_CHARS = 800
MIN_OWN_CLIPPING_CHARS = 300
BATCH_SIZE_DEFAULT = 80
MAX_FETCH_LIMIT = 80
HEARTBEAT_SECONDS = 30
DEFAULT_BATCH_COOLDOWN_MINUTES = 70

RATE_LIMIT_MARKERS = (
    "freq control",
    "Rate limited",
    "ret=200013",
    "频率控制",
    "触发风控",
    "操作频繁",
)

BROWSER_CIRCUIT_MARKERS = (
    "wappoc_appmsgcaptcha",
    "security verification",
    "安全验证",
    "访问过于频繁",
    "环境异常",
)


@dataclass
class ArticleRecord:
    title: str
    url: str
    published: str = "unknown-date"
    author: str = ""
    path: str = ""
    status: str = "pending"
    reason: str = ""


@dataclass
class BatchState:
    """Persistent progress for multi-round harvesting."""
    account: str
    target_year: int | None = None
    target_from_date: str | None = None
    target_to_date: str | None = None
    total_fetched: int = 0
    next_offset: int = 0
    earliest_date: str = ""
    latest_date: str = ""
    round: int = 0
    status: str = "in_progress"
    rate_limited_until: str = ""
    resume_after: str = ""
    last_run: str = ""
    fulltext: bool = False
    allow_metaso: bool = False
    title_regex: str = ""
    output_dir: str = ""
    batch_size: int = BATCH_SIZE_DEFAULT
    config_fingerprint: str = ""
    remote_total: int = 0
    head_aid: str = ""
    boundary_aid: str = ""
    boundary_create_time: int = 0
    cursor_earliest_date: str = ""
    cache_count: int = 0
    task_id: str = ""
    completion_reason: str = "in_progress"


def clean_filename(value: str) -> str:
    return safe_component(value, max_bytes=180)


def run_checked(
    args: list[str],
    cwd: Path | None = None,
    *,
    stream: bool = False,
) -> subprocess.CompletedProcess:
    if not stream:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            raise RuntimeError(f"command failed: {' '.join(args)}")
        return result

    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    chunks: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            chunks.append(chunk)
            sys.stderr.write(chunk)
            sys.stderr.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    started = time.monotonic()
    last_heartbeat = started
    while proc.poll() is None:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            log(f"subprocess still running after {int(now - started)}s: {Path(args[0]).name}")
            last_heartbeat = now
        time.sleep(0.25)
    reader.join(timeout=5)
    result = subprocess.CompletedProcess(args, proc.returncode, "".join(chunks), "")
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{(result.stdout or '')[-4000:]}")
    return result


def wcx(*args: str) -> subprocess.CompletedProcess:
    return run_checked([sys.executable, str(WCX_RUN), "--", *args], stream=True)


def wcx_python_command() -> list[str]:
    wcx_path = shutil.which("wcx")
    if not wcx_path:
        raise RuntimeError("wcx executable not found")
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


def parse_last_json(output: str) -> dict[str, Any]:
    for line in reversed((output or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("subprocess did not return a JSON result")


def wcx_batch(account: str, offset: int, limit: int, state: BatchState | None = None) -> dict[str, Any]:
    command = [
        *wcx_python_command(),
        str(WCX_BATCH_FETCH),
        "--account",
        account,
        "--offset",
        str(offset),
        "--limit",
        str(limit),
    ]
    if state and state.remote_total:
        command += ["--previous-total", str(state.remote_total)]
    if state and state.head_aid:
        command += ["--head-aid", state.head_aid]
    if state and state.boundary_aid:
        command += ["--boundary-aid", state.boundary_aid]
    if state and state.boundary_create_time:
        command += ["--boundary-create-time", str(state.boundary_create_time)]
    result = run_checked(command, stream=True)
    return parse_last_json(result.stdout)


def log(message: str) -> None:
    print(f"[wxmp-harvester] {message}", file=sys.stderr, flush=True)


def detect_rate_limit(output: str) -> tuple[bool, int]:
    """Return (is_rate_limited, suggested_wait_minutes)."""
    for marker in RATE_LIMIT_MARKERS:
        if marker.lower() not in output.lower():
            continue
        match = re.search(r'Wait\s*>=?\s*(\d+)\s*(hour|min|分钟|小时)', output, re.I)
        if not match:
            return True, 60
        num = int(match.group(1))
        unit = match.group(2).lower()
        return True, num * 60 if unit in ("hour", "小时") else num
    return False, 0


def browser_circuit_reason(error: str) -> str:
    lowered = error.lower()
    if any(marker.lower() in lowered for marker in BROWSER_CIRCUIT_MARKERS):
        return "WeChat browser verification/risk-control page detected"
    return ""


def fetch_limit_from_since(
    since: str | None,
    explicit_limit: int | None,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> int:
    if explicit_limit:
        return explicit_limit
    if year or from_date or to_date:
        return BATCH_SIZE_DEFAULT
    if since:
        match = re.fullmatch(r"(\d+)d", since.strip())
        if match:
            return min(int(match.group(1)) * 8, BATCH_SIZE_DEFAULT)
    return 50


@functools.lru_cache(maxsize=1)
def wcx_cache_path() -> Path | None:
    configured = os.environ.get("WCX_CACHE_PATH")
    if configured:
        configured_path = Path(configured).expanduser()
        return configured_path if configured_path.is_file() else None
    try:
        result = subprocess.run(
            [*wcx_python_command(), "-c", "from wcx.config import CACHE_DB; print(CACHE_DB)"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        package_path = Path(result.stdout.strip()).expanduser() if result.returncode == 0 else None
        if package_path and package_path.is_file():
            return package_path
    except (OSError, subprocess.TimeoutExpired):
        pass
    candidates: list[Path] = [
        Path.home() / "Library" / "Application Support" / "wcx" / "cache.db",
        Path.home() / ".local" / "share" / "wcx" / "cache.db",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def query_cache_count(account: str) -> int:
    """Count cached articles for an account in wcx's SQLite DB."""
    cache_path = wcx_cache_path()
    if not cache_path:
        return 0
    try:
        with closing(sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM articles WHERE fakeid IN "
                "(SELECT fakeid FROM accounts WHERE nickname=? OR alias=?)",
                (account, account),
            ).fetchone()
            if count and count[0]:
                return count[0]
            count = db.execute(
                "SELECT COUNT(*) FROM articles WHERE fakeid=?", (account,)
            ).fetchone()
            return count[0] if count else 0
    except Exception:
        return 0


def query_cache_stats(account: str) -> tuple[int, str, str]:
    """Return cached count plus earliest/latest publication dates."""
    cache_path = wcx_cache_path()
    if not cache_path:
        return (0, "", "")
    try:
        with closing(sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT COUNT(*), MIN(create_time), MAX(create_time) FROM articles WHERE fakeid IN "
                "(SELECT fakeid FROM accounts WHERE nickname=? OR alias=? OR fakeid=?)",
                (account, account, account),
            ).fetchone()
            if not row or not row[0]:
                return (0, "", "")
            earliest = datetime.fromtimestamp(int(row[1])).strftime("%Y-%m-%d") if row[1] else ""
            latest = datetime.fromtimestamp(int(row[2])).strftime("%Y-%m-%d") if row[2] else ""
            return (int(row[0]), earliest, latest)
    except Exception:
        return (0, "", "")


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

def state_path_for(export_dir: Path) -> Path:
    return export_dir / ".harvest-state.json"


def load_batch_state(export_dir: Path) -> BatchState | None:
    sp = state_path_for(export_dir)
    if not sp.exists():
        return None
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
        return BatchState(**{k: v for k, v in data.items() if k in BatchState.__dataclass_fields__})
    except Exception:
        return None


def save_batch_state(state: BatchState, export_dir: Path) -> None:
    state.last_run = datetime.now().astimezone().isoformat(timespec="seconds")
    d = {
        "account": state.account,
        "target_year": state.target_year,
        "target_from_date": state.target_from_date,
        "target_to_date": state.target_to_date,
        "total_fetched": state.total_fetched,
        "next_offset": state.next_offset,
        "earliest_date": state.earliest_date,
        "latest_date": state.latest_date,
        "round": state.round,
        "status": state.status,
        "rate_limited_until": state.rate_limited_until,
        "resume_after": state.resume_after,
        "last_run": state.last_run,
        "fulltext": state.fulltext,
        "allow_metaso": state.allow_metaso,
        "title_regex": state.title_regex,
        "output_dir": state.output_dir,
        "batch_size": state.batch_size,
        "config_fingerprint": state.config_fingerprint,
        "remote_total": state.remote_total,
        "head_aid": state.head_aid,
        "boundary_aid": state.boundary_aid,
        "boundary_create_time": state.boundary_create_time,
        "cursor_earliest_date": state.cursor_earliest_date,
        "cache_count": state.cache_count,
        "task_id": state.task_id,
        "completion_reason": state.completion_reason,
    }
    target = state_path_for(export_dir)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


# ---------------------------------------------------------------------------
# dedup helpers
# ---------------------------------------------------------------------------

def _title_key(title: str) -> str:
    """Normalise title to a comparable key."""
    return re.sub(r"[^\w]", "", title)[:80]


def source_url_from_file(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:12000]
    except OSError:
        return ""
    match = re.search(r'^(?:source|link):\s*["\']?([^"\'\n]+)', head, re.M)
    if not match:
        return ""
    try:
        return normalize_wechat_url(match.group(1).strip())
    except ValueError:
        return ""


def find_existing_article(articles_dir: Path, record: ArticleRecord) -> Path | None:
    """Find an existing file by canonical source URL, then exact title key."""
    expected_url = normalize_wechat_url(record.url)
    title_key = _title_key(record.title)
    title_matches: list[Path] = []
    for f in articles_dir.glob("*.md"):
        existing_url = source_url_from_file(f)
        if existing_url == expected_url:
            return f
        file_key = _title_key(re.sub(r"^(unknown-date|\d{4}-\d{2}-\d{2})[\s_]+", "", f.stem))
        if not existing_url and title_key and file_key == title_key:
            title_matches.append(f)
    return title_matches[0] if len(title_matches) == 1 else None


# ---------------------------------------------------------------------------
# record loading / filtering
# ---------------------------------------------------------------------------

def load_json_records(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("articles", "items", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def pick_field(record: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = record.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_published(record: dict[str, Any]) -> str:
    direct = pick_field(record, ("published", "date", "publish_time", "datetime"))
    if direct and direct != "unknown-date":
        return direct[:10]
    for name in ("create_time", "update_time"):
        value = record.get(name)
        if value in (None, ""):
            continue
        try:
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
        except Exception:
            continue
    return "unknown-date"


def parse_date(value: str) -> date | None:
    if not value or value == "unknown-date":
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def require_iso_date(name: str, value: str | None) -> date | None:
    if not value:
        return None
    parsed = parse_date(value)
    if not parsed or len(value) != 10:
        raise SystemExit(f"ERROR: {name} must use YYYY-MM-DD")
    return parsed


def filter_records(
    records: list[ArticleRecord],
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[ArticleRecord]:
    start = date(year, 1, 1) if year else parse_date(from_date or "")
    end = date(year, 12, 31) if year else parse_date(to_date or "")
    if from_date:
        start = parse_date(from_date)
    if to_date:
        end = parse_date(to_date)
    if not start and not end:
        return records

    filtered: list[ArticleRecord] = []
    for record in records:
        published = parse_date(record.published)
        if not published:
            continue
        if start and published < start:
            continue
        if end and published > end:
            continue
        filtered.append(record)
    return filtered


def filter_title_records(records: list[ArticleRecord], pattern: str | None) -> list[ArticleRecord]:
    if not pattern:
        return records
    try:
        matcher = re.compile(pattern, re.I)
    except re.error as exc:
        raise SystemExit(f"ERROR: invalid --title-regex: {exc}") from exc
    return [record for record in records if matcher.search(record.title)]


def effective_since(since: str | None, year: int | None, from_date: str | None) -> str | None:
    if since:
        return since
    if from_date:
        return from_date
    if year:
        return f"{year}-01-01"
    return None


def records_from_index(export_dir: Path) -> list[ArticleRecord]:
    json_path = export_dir / "index.json"
    if not json_path.is_file():
        json_path = None
    records: list[ArticleRecord] = []
    if json_path:
        try:
            json_doc = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            json_doc = None
        account_author = ""
        if isinstance(json_doc, dict) and isinstance(json_doc.get("account"), dict):
            account_author = pick_field(json_doc["account"], ("nickname", "alias"))
        json_records = load_json_records(json_path)
        for item in json_records:
            url = pick_field(item, ("url", "link", "content_url", "appmsg_url"))
            title = pick_field(item, ("title", "name"))
            try:
                url = normalize_wechat_url(url)
            except ValueError:
                continue
            records.append(
                ArticleRecord(
                    title=title or "untitled",
                    url=url,
                    published=normalize_published(item),
                    author=pick_field(item, ("author", "account", "nickname")) or account_author,
                )
            )
    if records:
        return dedupe(records)

    csv_path = export_dir / "index.csv"
    if not csv_path.is_file():
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            url = pick_field(item, ("url", "link", "content_url", "appmsg_url"))
            title = pick_field(item, ("title", "name"))
            try:
                url = normalize_wechat_url(url)
            except ValueError:
                continue
            records.append(
                ArticleRecord(
                    title=title or "untitled",
                    url=url,
                    published=normalize_published(item),
                    author=pick_field(item, ("author", "account", "nickname")),
                )
            )
    return dedupe(records)


def locate_wcx_export_dir(root: Path, account: str) -> Path:
    if (root / "index.json").is_file():
        return root
    candidates = sorted(path.parent for path in root.glob("*/index.json"))
    matches: list[Path] = []
    for candidate in candidates:
        try:
            document = json.loads((candidate / "index.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = document.get("account", {}) if isinstance(document, dict) else {}
        identities = {
            str(metadata.get(key, "")).strip()
            for key in ("nickname", "alias", "fakeid")
            if metadata.get(key)
        }
        if account in identities:
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        f"cannot identify a unique wcx export for account={account!r} under {root}"
    )


def dedupe(records: list[ArticleRecord]) -> list[ArticleRecord]:
    seen = set()
    out = []
    for record in records:
        if record.url in seen:
            continue
        seen.add(record.url)
        out.append(record)
    return out


def candidate_article_path(record: ArticleRecord, *article_dirs: Path) -> Path | None:
    for article_dir in article_dirs:
        path = find_existing_article(article_dir, record)
        if path:
            return path
    return None


def needs_fulltext(path: Path | None) -> bool:
    if not path or not path.exists():
        return True
    text = path.read_text(encoding="utf-8", errors="ignore")
    weak_markers = (
        "阅读全文",
        "继续滑动看下一个",
        "文章已于",
        "该内容可能因违规无法查看",
        "正文尚未抓取",
        "Mini Program",
        "轻点两下取消赞",
    )
    own_clipping = "tags:\n  - clipping\n  - wechat\n  - wxmp" in text and re.search(r'^source:\s*["\']?https?://', text, re.M)
    if own_clipping and len(text.strip()) >= MIN_OWN_CLIPPING_CHARS:
        title_match = re.search(r'^title:\s*["\']?([^"\'\n]+)', text, re.M)
        title = title_match.group(1).strip() if title_match else ""
        meaningful = re.sub(r"[^\w\u3400-\u9fff]", "", text)
        if title.lower() in metaso_reader.GENERIC_TITLES or len(meaningful) < metaso_reader.MIN_MEANINGFUL_CHARS:
            return True
        return any(marker in text for marker in weak_markers)
    if len(text.strip()) < MIN_FULLTEXT_CHARS:
        return True
    return any(marker in text for marker in weak_markers)


def copy_wcx_article(record: ArticleRecord, source: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    normalize_article_source(target)
    return target


def normalize_article_source(path: Path) -> None:
    """Canonicalize an existing clipping source/link without touching its body."""
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'^(source|link):\s*(["\']?)([^"\'\n]+)\2\s*$', text, re.M)
    if not match:
        return
    try:
        canonical = normalize_wechat_url(match.group(3).strip())
    except ValueError:
        return
    replacement = f"{match.group(1)}: {json.dumps(canonical, ensure_ascii=False)}"
    updated = text[: match.start()] + replacement + text[match.end() :]
    if updated == text:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)


def write_article(record: ArticleRecord, result: metaso_reader.ReaderResult, articles_dir: Path) -> Path:
    """Write article, overwriting any existing file for the same article."""
    metaso_reader.validate_result(result, expected_title=record.title)
    if result.retrieved_via == "metaso" and record.published != "unknown-date":
        published = record.published
    else:
        published = result.published if result.published != "unknown-date" else record.published
    title = result.title or record.title
    result.url = normalize_wechat_url(record.url)
    result.title = title
    result.published = published
    result.author = result.author or record.author
    record.title = title
    record.published = published
    record.author = result.author
    filename = f"{published or 'unknown-date'} {clean_filename(title)}.md"
    path = articles_dir / filename
    if path.exists():
        existing_url = source_url_from_file(path)
        if existing_url and existing_url != result.url:
            identity = hashlib.sha256(result.url.encode("utf-8")).hexdigest()[:8]
            path = articles_dir / f"{published or 'unknown-date'} {clean_filename(title)} [{identity}].md"

    # Overwrite any existing file for the same article (handles wcx-export / browser
    # reader filename mismatches: different date separators, different timestamps)
    existing = find_existing_article(articles_dir, record)
    if existing and existing.resolve() != path.resolve():
        existing.unlink()

    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(metaso_reader.render_markdown(result), encoding="utf-8")
    temporary.replace(path)
    return path


def write_indexes(records: list[ArticleRecord], export_dir: Path) -> None:
    index_json = export_dir / "index.json"
    index_csv = export_dir / "index.csv"
    index_md = export_dir / "index.md"

    json_tmp = index_json.with_suffix(".json.tmp")
    csv_tmp = index_csv.with_suffix(".csv.tmp")
    md_tmp = index_md.with_suffix(".md.tmp")
    json_tmp.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "published", "author", "url", "path", "status", "reason"])
        writer.writeheader()
        for item in records:
            row = asdict(item)
            writer.writerow({key: csv_safe(value) for key, value in row.items()})

    lines = ["# 微信公众号文章抓取索引", ""]
    lines.append("| published | title | status | path |")
    lines.append("| --- | --- | --- | --- |")
    for item in records:
        safe_title = item.title.replace("|", "\\|")
        lines.append(f"| {item.published} | {safe_title} | {item.status} | {item.path} |")
    md_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_tmp.replace(index_json)
    csv_tmp.replace(index_csv)
    md_tmp.replace(index_md)


def csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_report(
    records: list[ArticleRecord],
    export_dir: Path,
    account: str,
    since: str | None,
    limit: int,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> Path:
    success = len([item for item in records if item.status in {"existing", "wcx", "browser", "metaso", "index-only"}])
    partial = len([item for item in records if item.status == "partial"])
    failed = len([item for item in records if item.status == "failed"])
    lines = [
        "# harvest report",
        "",
        f"- account: {account}",
        f"- since: {since or ''}",
        f"- year: {year or ''}",
        f"- from_date: {from_date or ''}",
        f"- to_date: {to_date or ''}",
        f"- limit: {limit}",
        f"- generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- success: {success}",
        f"- partial: {partial}",
        f"- failed: {failed}",
        "",
    ]
    if partial or failed:
        lines.append("## unresolved")
        lines.append("")
        for item in records:
            if item.status in {"partial", "failed"}:
                lines.append(f"- [{item.status}] {item.title}: {item.reason} ({item.url})")
    path = export_dir / "harvest-report.md"
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def append_report_warning(report_path: Path, warning: str | None) -> None:
    if not warning:
        return
    with report_path.open("a", encoding="utf-8") as handle:
        handle.write("\n## warnings\n\n")
        handle.write(f"- {warning}\n")


def export_dir_for(account: str, output_dir: str | None) -> Path:
    if output_dir:
        return Path(output_dir).expanduser().resolve()
    return DEFAULT_EXPORT_ROOT / safe_component(account, max_bytes=120)


def portable_path(path: Path | None, export_dir: Path) -> str:
    if not path:
        return ""
    try:
        return str(path.resolve().relative_to(export_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def resolve_resume_state(account: str | None, output_dir: str | None) -> tuple[Path, BatchState]:
    if output_dir:
        export_dir = Path(output_dir).expanduser().resolve()
        state = load_batch_state(export_dir)
        if state:
            return export_dir, state
        raise SystemExit(f"ERROR: no previous state found in {export_dir}")
    if account:
        export_dir = export_dir_for(account, None)
        state = load_batch_state(export_dir)
        if state:
            return export_dir, state
        raise SystemExit(f"ERROR: no previous state found in {export_dir}")
    candidates = []
    if DEFAULT_EXPORT_ROOT.exists():
        candidates = [path.parent for path in DEFAULT_EXPORT_ROOT.glob("*/.harvest-state.json")]
    if len(candidates) == 1:
        state = load_batch_state(candidates[0])
        if state:
            return candidates[0], state
    if not candidates:
        raise SystemExit("ERROR: no resumable harvest state found; start with --account NAME --batch")
    accounts = ", ".join(sorted(path.name for path in candidates))
    raise SystemExit(f"ERROR: multiple resumable accounts found ({accounts}); pass --account or --output-dir")


# ---------------------------------------------------------------------------
# post-export dedup
# ---------------------------------------------------------------------------

def dedupe_articles_dir(articles_dir: Path) -> int:
    """Remove duplicate article files (same article, different filename).

    Keeps the larger file when two files have matching title keys.
    Returns the number of files removed.
    """
    if not articles_dir.exists():
        return 0
    groups: dict[str, list[Path]] = {}
    for f in articles_dir.glob("*.md"):
        source_url = source_url_from_file(f)
        key = f"url:{source_url}" if source_url else ""
        if not key:
            title_key = _title_key(re.sub(r"^(unknown-date|\d{4}-\d{2}-\d{2})[\s_]+", "", f.stem))
            key = f"title:{title_key}" if title_key else ""
        if key:
            groups.setdefault(key, []).append(f)
    removed = 0
    for files in groups.values():
        if len(files) <= 1:
            continue
        files.sort(key=lambda p: p.stat().st_size, reverse=True)
        for dup in files[1:]:
            dup.unlink()
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# batch-mode helpers
# ---------------------------------------------------------------------------

def records_date_range(records: list[ArticleRecord]) -> tuple[str, str]:
    """Return (earliest, latest) date strings for a record list."""
    dates = [parse_date(r.published) for r in records if parse_date(r.published)]
    if not dates:
        return ("", "")
    return (min(dates).isoformat(), max(dates).isoformat())


def target_covered_by_cursor(
    cursor_earliest_date: str,
    *,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    exhausted: bool = False,
) -> bool:
    if exhausted:
        return True
    target = f"{year}-01-01" if year else (from_date or to_date or "")
    if not target:
        return True
    return bool(cursor_earliest_date and cursor_earliest_date <= target)


def wechat_date_from_timestamp(value: int) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def batch_completion_reason(
    cursor_earliest_date: str,
    *,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    exhausted: bool = False,
    rate_limited: bool = False,
) -> str:
    if rate_limited:
        return "rate_limited"
    if exhausted:
        return "remote_exhausted"
    target = f"{year}-01-01" if year else (from_date or to_date or "")
    if not target:
        return "no_lower_bound"
    if cursor_earliest_date and cursor_earliest_date <= target:
        return "crossed_lower_bound"
    return "in_progress"


def batch_config_fingerprint(
    account: str,
    *,
    year: int | None,
    from_date: str | None,
    to_date: str | None,
    title_regex: str | None,
    fulltext: bool,
    allow_metaso: bool,
    output_dir: Path,
) -> str:
    payload = json.dumps(
        {
            "account": account,
            "year": year,
            "from_date": from_date,
            "to_date": to_date,
            "title_regex": title_regex or "",
            "fulltext": fulltext,
            "allow_metaso": allow_metaso,
            "output_dir": str(output_dir.resolve()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def task_id_for_fingerprint(fingerprint: str) -> str:
    return f"wxmp-{fingerprint[:12]}"


def classify_run_result(
    records: list[ArticleRecord],
    *,
    batch_mode: bool,
    batch_done: bool,
    rate_limited: bool,
    fetch_warning: str | None,
) -> tuple[str, bool, int, int, int, int]:
    success = sum(item.status in {"existing", "wcx", "browser", "metaso", "index-only"} for item in records)
    partial = sum(item.status == "partial" for item in records)
    failed = sum(item.status == "failed" for item in records)
    if rate_limited:
        return ("rate_limited", False, 75, success, partial, failed)
    if fetch_warning:
        return ("partial", False, 2, success, partial, failed)
    if batch_mode and not batch_done:
        return ("in_progress", True, 0, success, partial, failed)
    if not records:
        return ("empty", False, 3, success, partial, failed)
    if partial or failed:
        return ("partial", False, 2, success, partial, failed)
    return ("complete", True, 0, success, partial, failed)


def batch_diagnostic(
    records: list[ArticleRecord],
    total_cached: int,
    year: int | None,
    from_date: str | None,
    to_date: str | None,
    batch_mode: bool,
) -> str | None:
    """Return a diagnostic string when the filtered result set is empty,
    or None when everything looks fine."""
    if records:
        return None

    target_desc = ""
    if year:
        target_desc = f"{year} 年"
    elif from_date or to_date:
        target_desc = f"{from_date or '…'} ~ {to_date or '…'}"

    all_earliest, all_latest = "", ""
    if total_cached > 0:
        # We can't easily get the full cached range without exporting everything,
        # but we can report what wcx returned.
        pass

    lines = [
        f"当前缓存共 {total_cached} 篇，但没有一篇落在目标日期范围（{target_desc}）内。",
    ]

    if batch_mode:
        lines.append("批处理模式：请等待冷却后再次运行 --batch 继续往前挖。")
        lines.append("下次运行将自动续抓更早的文章。")
    else:
        lines.append("建议：")
        lines.append(f"  1. 增大 --limit（如 --limit 300），或使用 --batch 模式自动分批抓取")
        lines.append(f"  2. 确认公众号在 {target_desc} 确实有发文")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest WeChat public account articles")
    parser.add_argument("--account", help="public account name or fakeid; optional only with --resume")
    parser.add_argument("--since", help="date filter, e.g. 10d, 2w, YYYY-MM-DD")
    parser.add_argument("--year", type=int, help="keep only articles published in this year, e.g. 2026")
    parser.add_argument("--from-date", help="keep only articles on or after YYYY-MM-DD")
    parser.add_argument("--to-date", help="keep only articles on or before YYYY-MM-DD")
    parser.add_argument("--title-regex", help="keep titles matching this regular expression")
    parser.add_argument("--limit", type=int, help="metadata fetch/export limit")
    parser.add_argument("--output-dir", help="final output directory")
    parser.add_argument("--skip-fetch", action="store_true", help="skip wcx fetch, use existing cache")
    parser.add_argument("--fulltext", action="store_true", help="fill missing/weak article bodies")
    parser.add_argument("--no-fulltext", action="store_true", help="skip fulltext fill")
    parser.add_argument("--refresh-fulltext", action="store_true", help="re-read article pages even when existing text passes quality checks")
    parser.add_argument("--fetch-content", action="store_true", help="also ask wcx to fetch content before export")
    parser.add_argument("--skip-wcx-content", action="store_true", help="legacy no-op unless --fetch-content is also set")
    parser.add_argument("--skip-browser", action="store_true", help="skip Playwright DOM extraction before Metaso")
    parser.add_argument("--allow-metaso", action="store_true", help="explicitly allow paid Metaso fallback and URL sharing")
    parser.add_argument("--skip-metaso", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--browser-profile-dir", help="persistent Playwright profile for article reading")
    parser.add_argument("--browser-headed", action="store_true", help="show browser while reading articles")
    parser.add_argument("--browser-load-assets", action="store_true", help="load images/fonts/media instead of blocking them")
    parser.add_argument("--browser-timeout", type=int, default=45000, help="browser navigation timeout in ms")
    parser.add_argument("--browser-retries", type=int, default=1, help="fresh-context retries after browser extraction failure (0-2)")
    parser.add_argument("--batch", action="store_true", help="batch mode: fetch one safe batch, save state, report progress")
    parser.add_argument("--resume", action="store_true", help="resume from previous --batch state")
    parser.add_argument("--batch-size", type=int, help=f"articles per batch (default {BATCH_SIZE_DEFAULT})")
    parser.add_argument("--force", action="store_true", help="override an active rate-limit cooldown")
    args = parser.parse_args()

    if shutil.which("wcx") is None:
        raise SystemExit(f"ERROR: wcx not found. Install with: python3 -m pip install '{WCX_INSTALL_SPEC}'")

    if args.limit is not None and not 1 <= args.limit <= MAX_FETCH_LIMIT:
        raise SystemExit(f"ERROR: --limit must be between 1 and {MAX_FETCH_LIMIT}")
    if not 0 <= args.browser_retries <= 2:
        raise SystemExit("ERROR: --browser-retries must be between 0 and 2")
    if args.skip_metaso and args.allow_metaso:
        raise SystemExit("ERROR: use either --allow-metaso or --skip-metaso, not both")

    batch_mode = args.batch or args.resume
    state: BatchState | None = None
    if args.resume:
        export_dir, state = resolve_resume_state(args.account, args.output_dir)
        args.account = state.account
        args.year = args.year or state.target_year
        args.from_date = args.from_date or state.target_from_date
        args.to_date = args.to_date or state.target_to_date
        args.title_regex = args.title_regex or state.title_regex
        if not args.fulltext and not args.no_fulltext:
            args.fulltext = state.fulltext
        if not args.allow_metaso and not args.skip_metaso:
            args.allow_metaso = state.allow_metaso
        if args.batch_size is None:
            args.batch_size = state.batch_size
    else:
        if not args.account:
            raise SystemExit("ERROR: --account is required unless --resume is used")
        export_dir = export_dir_for(args.account, args.output_dir)
    if args.batch_size is None:
        args.batch_size = BATCH_SIZE_DEFAULT
    if not 1 <= args.batch_size <= MAX_FETCH_LIMIT:
        raise SystemExit(f"ERROR: --batch-size must be between 1 and {MAX_FETCH_LIMIT}")
    if args.fulltext and args.no_fulltext:
        raise SystemExit("ERROR: --fulltext and --no-fulltext cannot be used together")
    if args.refresh_fulltext and not args.fulltext:
        raise SystemExit("ERROR: --refresh-fulltext requires --fulltext")
    if args.year and (args.from_date or args.to_date or args.since):
        raise SystemExit("ERROR: --year cannot be combined with --since/--from-date/--to-date")
    if args.since and (args.from_date or args.to_date):
        raise SystemExit("ERROR: --since cannot be combined with --from-date/--to-date")
    if args.year and not 2000 <= args.year <= datetime.now().year + 1:
        raise SystemExit("ERROR: --year is outside the supported range")
    start_date = require_iso_date("--from-date", args.from_date)
    end_date = require_iso_date("--to-date", args.to_date)
    if start_date and end_date and start_date > end_date:
        raise SystemExit("ERROR: --from-date must be on or before --to-date")
    if args.since and not (re.fullmatch(r"\d+[dw]", args.since) or parse_date(args.since)):
        raise SystemExit("ERROR: --since must be Nd, Nw, or YYYY-MM-DD")
    current_fingerprint = batch_config_fingerprint(
        args.account,
        year=args.year,
        from_date=args.from_date,
        to_date=args.to_date,
        title_regex=args.title_regex,
        fulltext=bool(args.fulltext and not args.no_fulltext),
        allow_metaso=bool(args.allow_metaso and not args.skip_metaso),
        output_dir=export_dir,
    )
    if args.resume and state and state.config_fingerprint and state.config_fingerprint != current_fingerprint:
        raise SystemExit(
            "ERROR: resume configuration differs from the saved batch state; "
            "start a new output directory instead of mixing two harvest contracts"
        )
    lock_path = BASE_DIR / ".harvest.lock"
    try:
        acquire_lock(lock_path)
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    atexit.register(release_lock, lock_path)
    secure_directory(export_dir)
    articles_dir = export_dir / "articles"
    secure_directory(articles_dir)

    # ---- resume: load previous state --------------------------------------
    if args.resume:
        assert state is not None
        cooldown_value = state.resume_after or state.rate_limited_until
        if cooldown_value:
            log(f"saved batch cooldown: wait until {cooldown_value}")
            try:
                cooldown_until = datetime.fromisoformat(cooldown_value)
            except (TypeError, ValueError):
                cooldown_until = None
            cooldown_now = datetime.now(tz=cooldown_until.tzinfo) if cooldown_until else None
            if cooldown_until and cooldown_now and cooldown_now < cooldown_until and not args.force:
                print(json.dumps({
                    "ok": False,
                    "status": "rate_limited" if state.status == "rate_limited" else "cooldown",
                    "retry_after": cooldown_value,
                    "output_dir": str(export_dir),
                    "resume": ["--resume", "--output-dir", str(export_dir)],
                }, ensure_ascii=False))
                raise SystemExit(75)
        log(f"resuming batch: round {state.round}, {state.total_fetched} articles so far, "
            f"earliest {state.earliest_date}")

    # ---- determine limit ------------------------------------------------
    limit = args.limit
    if args.skip_fetch and not limit:
        cached = query_cache_count(args.account)
        if cached > 0:
            limit = cached
            log(f"auto-detected {cached} cached articles, using as export limit")
        else:
            limit = fetch_limit_from_since(args.since, args.limit, args.year, args.from_date, args.to_date)
    else:
        limit = fetch_limit_from_since(args.since, args.limit, args.year, args.from_date, args.to_date)

    # ---- fetch metadata --------------------------------------------------
    fetch_warning = None
    rate_limited = False
    rate_limit_wait = 0
    batch_result: dict[str, Any] = {}
    cursor_drift = False

    if args.skip_fetch:
        log("skip fetch: using existing wcx cache/index")
    else:
        fetch_limit = min(limit, args.batch_size) if batch_mode else limit
        try:
            if batch_mode:
                offset = state.next_offset if state else 0
                log(f"fetch metadata batch: account={args.account} offset={offset} limit={fetch_limit}")
                batch_result = wcx_batch(args.account, offset, fetch_limit, state)
                rate_limited = bool(batch_result.get("rate_limited"))
            else:
                fetch_args = ["fetch", args.account, "--limit", str(fetch_limit)]
                should_fetch_content = args.fetch_content and not args.no_fulltext and not args.skip_wcx_content
                if should_fetch_content:
                    fetch_args.append("--content")
                log(f"fetch metadata: account={args.account} limit={fetch_limit}")
                result = wcx(*fetch_args)
                combined = result.stdout + result.stderr
                rate_limited, rate_limit_wait = detect_rate_limit(combined)
            if rate_limited:
                fetch_warning = (
                    f"fetch hit rate limit after partial progress. "
                    f"Suggested wait: {rate_limit_wait} min. "
                    f"Re-run with --batch --resume to continue."
                )
                log(f"WARNING: {fetch_warning}")
        except RuntimeError as exc:
            if batch_mode:
                try:
                    batch_result = parse_last_json(str(exc))
                except RuntimeError:
                    batch_result = {}
            cursor_drift = bool(batch_result.get("cursor_drift")) if batch_result else False
            if cursor_drift:
                print(json.dumps({
                    "ok": False,
                    "status": "cursor_drift",
                    "task_id": state.task_id if state and state.task_id else task_id_for_fingerprint(current_fingerprint),
                    "output_dir": str(export_dir),
                    "error": batch_result.get("error", "saved cursor can no longer be proven safe"),
                    "resume": ["--resume", "--output-dir", str(export_dir)],
                }, ensure_ascii=False))
                raise SystemExit(4)
            rate_limited = bool(batch_result.get("rate_limited")) if batch_result else False
            detected_rate_limit, detected_wait = detect_rate_limit(str(exc))
            rate_limited = rate_limited or detected_rate_limit
            rate_limit_wait = detected_wait if detected_rate_limit else (60 if rate_limited else 0)
            if rate_limited:
                fetch_warning = (
                    f"fetch hit rate limit. Suggested wait: {rate_limit_wait} min. "
                    "Cached progress was preserved."
                )
            else:
                fetch_warning = f"fetch failed; continued with existing wcx cache if available: {exc}"
            log(f"WARNING: {fetch_warning}")

    # ---- export -----------------------------------------------------------
    cached_count, cached_earliest, cached_latest = query_cache_stats(args.account)
    export_limit = cached_count if batch_mode and cached_count else limit
    wcx_export_root = secure_directory(export_dir / ".wcx-export")
    export_args = ["export", args.account, "--out", str(wcx_export_root), "--limit", str(export_limit), "--format", "all"]
    export_since = effective_since(args.since, args.year, args.from_date)
    if export_since:
        export_args += ["--since", export_since]
    log(f"export index: {export_dir}")
    wcx(*export_args)

    wcx_export_dir = locate_wcx_export_dir(wcx_export_root, args.account)

    records = records_from_index(wcx_export_dir)
    total_cached = max(len(records), cached_count)

    # ---- post-export dedup (wcx creates underscore-format files alongside
    #     browser-fetched space-format files for the same article) -----------
    removed = dedupe_articles_dir(articles_dir)
    if removed:
        log(f"dedup: removed {removed} duplicate article files")
    removed_wcx = dedupe_articles_dir(wcx_export_dir / "articles")
    if removed_wcx:
        log(f"dedup: removed {removed_wcx} duplicates from wcx export dir")

    # ---- filter by date ---------------------------------------------------
    records = filter_records(records, year=args.year, from_date=args.from_date, to_date=args.to_date)
    records = filter_title_records(records, args.title_regex)

    # ---- zero-result diagnostic -------------------------------------------
    if not records and (args.year or args.from_date or args.to_date):
        diag = batch_diagnostic(records, total_cached, args.year, args.from_date, args.to_date, batch_mode)
        if diag:
            log(diag)

    # ---- date range for state ---------------------------------------------
    earliest_date, latest_date = records_date_range(records)

    # ---- fulltext ----------------------------------------------------------
    fill_fulltext = args.fulltext and not args.no_fulltext
    date_filter = f" year={args.year}" if args.year else ""
    if args.from_date or args.to_date:
        date_filter += f" from={args.from_date or ''} to={args.to_date or ''}"
    log(f"loaded {len(records)} article URLs{date_filter}; fulltext={'on' if fill_fulltext else 'off'}")

    browser_session: browser_reader.BrowserArticleReader | None = None
    browser_circuit = ""
    refresh_failures: list[str] = []
    try:
        for index, record in enumerate(records, start=1):
            current_path = candidate_article_path(record, articles_dir, wcx_export_dir / "articles")
            verified_existing = bool(current_path and not needs_fulltext(current_path))
            log(f"{index}/{len(records)} {record.title[:80]}")
            if not fill_fulltext:
                record.status = "index-only"
                record.path = portable_path(current_path, export_dir)
                continue
            if not args.refresh_fulltext and verified_existing:
                saved = copy_wcx_article(record, current_path, articles_dir) if current_path else None
                selected_path = saved or current_path
                if selected_path:
                    normalize_article_source(selected_path)
                record.status = "existing" if (current_path and current_path.parent == articles_dir) else "wcx"
                record.path = portable_path(selected_path, export_dir)
                log(f"  reused {record.status}")
                continue
            if not args.skip_browser:
                browser_succeeded = False
                browser_errors: list[str] = []
                attempts = 0 if browser_circuit else args.browser_retries + 1
                if browser_circuit:
                    browser_errors.append(f"browser skipped after circuit breaker: {browser_circuit}")
                for attempt in range(attempts):
                    try:
                        if browser_session is None:
                            browser_session = browser_reader.BrowserArticleReader(
                                timeout=args.browser_timeout,
                                profile_dir=args.browser_profile_dir,
                                headless=not args.browser_headed,
                                load_assets=args.browser_load_assets or attempt > 0,
                            )
                            browser_session.start()
                        result = browser_session.read(record.url)
                        saved = write_article(record, result, articles_dir)
                        record.status = "browser"
                        record.path = portable_path(saved, export_dir)
                        log(f"  browser ok: {len(result.markdown)} chars")
                        browser_succeeded = True
                        break
                    except Exception as exc:
                        browser_errors.append(str(exc))
                        log(f"  browser attempt {attempt + 1} failed: {exc}")
                        if browser_session is not None:
                            browser_session.close()
                            browser_session = None
                        circuit_reason = browser_circuit_reason(str(exc))
                        if circuit_reason:
                            browser_circuit = circuit_reason
                            log(f"  browser circuit opened: {browser_circuit}")
                            break
                if browser_succeeded:
                    continue
                record.reason = "browser failed: " + " | ".join(browser_errors)
            use_metaso = args.allow_metaso and not args.skip_metaso
            if not use_metaso:
                if args.refresh_fulltext and verified_existing and current_path:
                    saved = copy_wcx_article(record, current_path, articles_dir)
                    record.status = "existing"
                    record.reason = f"refresh skipped; preserved verified existing text ({record.reason})"
                    record.path = portable_path(saved, export_dir)
                    refresh_failures.append(record.title)
                    log("  preserved verified existing text after refresh failure")
                    continue
                record.status = "partial"
                record.reason = record.reason or "fulltext unavailable; Metaso was not explicitly allowed"
                record.path = portable_path(current_path, export_dir)
                log("  partial: no complete text and Metaso was not explicitly allowed")
                continue
            try:
                result = metaso_reader.read_url(record.url)
                metaso_reader.validate_result(result, expected_title=record.title)
                saved = write_article(record, result, articles_dir)
                record.status = "metaso"
                record.path = portable_path(saved, export_dir)
                log(f"  metaso ok: {len(result.markdown)} chars")
            except Exception as exc:
                if args.refresh_fulltext and verified_existing and current_path:
                    saved = copy_wcx_article(record, current_path, articles_dir)
                    prior = f"{record.reason}; " if record.reason else ""
                    record.status = "existing"
                    record.reason = f"refresh failed; preserved verified existing text ({prior}metaso failed: {exc})"
                    record.path = portable_path(saved, export_dir)
                    refresh_failures.append(record.title)
                    log("  preserved verified existing text after refresh fallback failure")
                    continue
                record.status = "partial"
                prior = f"{record.reason}; " if record.reason else ""
                record.reason = f"{prior}metaso failed: {exc}"
                record.path = portable_path(current_path, export_dir)
                log(f"  failed: {record.reason}")
    finally:
        if browser_session is not None:
            browser_session.close()

    if refresh_failures:
        refresh_warning = (
            f"refresh could not replace {len(refresh_failures)} verified article(s); "
            "the previous validated Markdown was preserved"
        )
        fetch_warning = f"{fetch_warning}; {refresh_warning}" if fetch_warning else refresh_warning

    write_indexes(records, export_dir)
    report = write_report(
        records, export_dir, args.account, args.since, limit,
        year=args.year, from_date=args.from_date, to_date=args.to_date,
    )
    append_report_warning(report, fetch_warning)

    # ---- save batch state -------------------------------------------------
    batch_done = False
    if batch_mode:
        next_offset = int(batch_result.get("next_offset", state.next_offset if state else 0))
        exhausted = bool(batch_result.get("exhausted"))
        boundary_create_time = int(batch_result.get(
            "boundary_create_time",
            state.boundary_create_time if state else 0,
        ))
        cursor_earliest_date = (
            wechat_date_from_timestamp(boundary_create_time)
            or (state.cursor_earliest_date if state else "")
        )
        batch_done = target_covered_by_cursor(
            cursor_earliest_date,
            year=args.year,
            from_date=args.from_date,
            to_date=args.to_date,
            exhausted=exhausted,
        )
        completion_reason = batch_completion_reason(
            cursor_earliest_date,
            year=args.year,
            from_date=args.from_date,
            to_date=args.to_date,
            exhausted=exhausted,
            rate_limited=rate_limited,
        )
        next_wait_minutes = rate_limit_wait or DEFAULT_BATCH_COOLDOWN_MINUTES
        resume_after = (
            (datetime.now().astimezone() + timedelta(minutes=next_wait_minutes)).isoformat(timespec="seconds")
            if rate_limited or not batch_done else ""
        )
        state = BatchState(
            account=args.account,
            target_year=args.year,
            target_from_date=args.from_date,
            target_to_date=args.to_date,
            total_fetched=next_offset,
            next_offset=next_offset,
            earliest_date=cached_earliest or earliest_date,
            latest_date=cached_latest or latest_date,
            round=(state.round + 1) if state else 1,
            status="rate_limited" if rate_limited else ("complete" if batch_done else "in_progress"),
            rate_limited_until=(
                (datetime.now().astimezone() + timedelta(minutes=rate_limit_wait)).isoformat(timespec="seconds")
                if rate_limited else ""
            ),
            resume_after=resume_after,
            fulltext=bool(args.fulltext and not args.no_fulltext),
            allow_metaso=bool(args.allow_metaso and not args.skip_metaso),
            title_regex=args.title_regex or "",
            output_dir=str(export_dir),
            batch_size=args.batch_size,
            config_fingerprint=current_fingerprint,
            remote_total=int(batch_result.get("remote_total", state.remote_total if state else 0)),
            head_aid=str(batch_result.get("head_aid", state.head_aid if state else "")),
            boundary_aid=str(batch_result.get("boundary_aid", state.boundary_aid if state else "")),
            boundary_create_time=boundary_create_time,
            cursor_earliest_date=cursor_earliest_date,
            cache_count=total_cached,
            task_id=(state.task_id if state and state.task_id else task_id_for_fingerprint(current_fingerprint)),
            completion_reason=completion_reason,
        )
        save_batch_state(state, export_dir)
        log(f"batch state saved: round {state.round}, {state.total_fetched} total, "
            f"earliest {earliest_date}, status={state.status}")

    # ---- batch-mode summary ------------------------------------------------
    if batch_mode and not rate_limited:
        log("")
        log("=" * 50)
        if batch_done:
            log("BATCH COMPLETE — target date range covered")
        else:
            log(f"BATCH {state.round} DONE — need more rounds")
        log(f"  fetched this round: {batch_result.get('fetched', 0)}")
        log(f"  total fetched:      {state.total_fetched}")
        log(f"  cache date range:   {state.earliest_date} ~ {state.latest_date}")
        log(f"  cursor boundary:    {state.cursor_earliest_date or 'unknown'}")
        if not batch_done:
            next_wait = rate_limit_wait or DEFAULT_BATCH_COOLDOWN_MINUTES
            next_time = (datetime.now().astimezone() + timedelta(minutes=next_wait)).strftime("%H:%M")
            log(f"  next run after:     ~{next_time} (wait {next_wait} min)")
            log("  re-run:             --resume")
        log("=" * 50)

    final_status, ok, exit_code, success_count, partial_count, failed_count = classify_run_result(
        records,
        batch_mode=batch_mode,
        batch_done=batch_done,
        rate_limited=rate_limited,
        fetch_warning=fetch_warning,
    )

    print(json.dumps({
        "ok": ok,
        "status": final_status,
        "task_id": state.task_id if state else task_id_for_fingerprint(current_fingerprint),
        "output_dir": str(export_dir),
        "articles": len(records),
        "success": success_count,
        "partial": partial_count,
        "failed": failed_count,
        "report": str(report),
        "batch": {
            "round": state.round if state else 1,
            "total_fetched": state.total_fetched if state else len(records),
            "cache_count": state.cache_count if state else total_cached,
            "earliest_date": state.earliest_date,
            "latest_date": state.latest_date,
            "next_offset": state.next_offset,
            "target_reached": batch_done,
            "completion_reason": state.completion_reason,
            "cursor_earliest_date": state.cursor_earliest_date,
            "rate_limited": rate_limited,
            "rate_limit_wait_min": rate_limit_wait,
            "resume_after": state.resume_after,
        } if batch_mode else None,
    }, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
