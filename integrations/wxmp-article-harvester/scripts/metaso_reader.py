#!/usr/bin/env python3
"""Read a single article through Metaso Reader and save Markdown."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from runtime_paths import normalize_wechat_url, safe_component


METASO_API_URL = "https://metaso.cn/api/v1/reader"
GENERIC_TITLES = {"video", "wechat-article", "微信公众平台", "untitled", "s"}
PAGE_CHROME_MARKERS = (
    "Mini Program",
    "轻点两下取消赞",
    "轻点两下取消在看",
    "继续滑动看下一个",
)
REJECT_MARKERS = (
    "环境异常",
    "访问过于频繁",
    "安全验证",
    "完成验证",
    "该内容已被发布者删除",
    "该内容可能因违规无法查看",
)
MIN_MEANINGFUL_CHARS = 180


@dataclass
class ReaderResult:
    url: str
    markdown: str
    title: str
    published: str
    author: str = ""
    retrieved_via: str = "unknown"


def clean_filename(value: str) -> str:
    return safe_component(value, max_bytes=180)


def extract_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines()[:20]:
        if line.startswith("# "):
            return line[2:].strip()
    for line in markdown.splitlines()[:30]:
        text = line.strip()
        if not text:
            continue
        if text.startswith(("http://", "https://", "![", "|", "- ", "> ")):
            continue
        if 4 <= len(text) <= 120:
            return text
    return fallback


def extract_published(markdown: str) -> str:
    patterns = [
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})",
        r"published:\s*[\"']?([^\"'\n]+)",
        r"date:\s*[\"']?([^\"'\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, markdown, re.I)
        if not match:
            continue
        if len(match.groups()) == 3:
            y, m, d = match.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        return match.group(1).strip()[:10]
    return "unknown-date"


def validate_result(result: ReaderResult, expected_title: str = "") -> ReaderResult:
    title = (result.title or "").strip()
    body = result.markdown or ""
    meaningful = re.sub(r"[^\w\u3400-\u9fff]", "", body)
    chrome_hits = sum(marker in body for marker in PAGE_CHROME_MARKERS)
    if len(meaningful) < MIN_MEANINGFUL_CHARS:
        raise RuntimeError(f"reader returned too little meaningful content: {len(meaningful)} chars")
    if title.lower() in GENERIC_TITLES and expected_title:
        raise RuntimeError(f"reader returned a generic title instead of the expected article: {title}")
    if any(marker in body for marker in REJECT_MARKERS):
        raise RuntimeError("reader returned a blocked, verification, or unavailable page")
    if chrome_hits >= 2:
        raise RuntimeError("reader returned WeChat page chrome instead of article content")
    return result


def read_url(url: str, timeout: int = 60) -> ReaderResult:
    url = normalize_wechat_url(url)
    metaso_credential = os.environ.get("METASO_API_KEY", "")
    if not metaso_credential:
        raise RuntimeError("METASO_API_KEY is not set")

    payload = json.dumps({"url": url}).encode("utf-8")
    request = urllib.request.Request(
        METASO_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {metaso_credential}",
            "Accept": "text/plain",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
        markdown = response.read().decode("utf-8", errors="ignore").strip()

    fallback_title = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1] or "wechat-article"
    title = extract_title(markdown, fallback_title)
    return validate_result(
        ReaderResult(
            url=url,
            markdown=markdown + "\n",
            title=title,
            published=extract_published(markdown),
            retrieved_via="metaso",
        )
    )


def render_markdown(result: ReaderResult) -> str:
    frontmatter = {
        "title": result.title,
        "source": result.url,
        "published": result.published,
        "author": result.author,
        "retrieved_via": result.retrieved_via,
        "clipped": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tags": ["clipping", "wechat", "wxmp"],
    }
    lines = ["---"]
    for key in ("title", "source", "published", "author", "retrieved_via", "clipped"):
        lines.append(f"{key}: {json.dumps(frontmatter[key], ensure_ascii=False)}")
    lines.append("tags:")
    for tag in frontmatter["tags"]:
        lines.append(f"  - {tag}")
    lines.append("---\n")
    if not result.markdown.lstrip().startswith("# "):
        lines.append(f"# {result.title}\n")
    lines.append(result.markdown)
    return "\n".join(lines)


def save_result(result: ReaderResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{result.published} {clean_filename(result.title)}.md"
    path = output_dir / filename
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Read one WeChat article through Metaso Reader")
    parser.add_argument("--url", required=True, help="article URL")
    parser.add_argument("--output-dir", default=str(Path.cwd()), help="directory for saved Markdown")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    try:
        result = read_url(args.url, timeout=args.timeout)
        saved = save_result(result, Path(args.output_dir).expanduser())
    except Exception as exc:
        print(json.dumps({"ok": False, "url": args.url, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)

    payload = {"ok": True, "url": args.url, "title": result.title, "published": result.published, "path": str(saved)}
    print(json.dumps(payload, ensure_ascii=False) if args.json else f"saved: {saved}")


if __name__ == "__main__":
    main()
