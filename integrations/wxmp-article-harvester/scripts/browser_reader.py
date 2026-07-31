#!/usr/bin/env python3
"""Read a WeChat article through a real browser before paid fallbacks."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import metaso_reader
from runtime_paths import DEFAULT_ARTICLE_PROFILE, normalize_wechat_url, secure_directory


BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BAD_PAGE_MARKERS = (
    "环境异常",
    "访问过于频繁",
    "操作频繁",
    "安全验证",
    "完成验证",
    "请在微信客户端打开",
    "该内容已被发布者删除",
    "该内容可能因违规无法查看",
)
MIN_BROWSER_CONTENT_CHARS = 500
MIN_SHORT_ARTICLE_CHARS = 180
CONTENT_READY_SCRIPT = """() => {
  const el = document.querySelector('#js_content') || document.querySelector('article') || document.querySelector('main');
  return !!el && ((el.innerText || el.textContent || '').trim().length > 300);
}"""
EXTRACT_SCRIPT = r"""() => {
  const text = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || el.textContent || '').trim() : '';
  };
  const title = text('#activity-name') || text('h1') || document.title || '';
  const author = text('#js_name') || text('.profile_nickname') || '';
  const published = text('#publish_time') || text('em#publish_time') || '';
  const contentEl = document.querySelector('#js_content') || document.querySelector('article') || document.querySelector('main');
  const content = contentEl ? (contentEl.innerText || contentEl.textContent || '').trim() : '';
  const clean = (value) => (value || '').replace(/\s+/g, ' ');
  const escapeText = (value) => clean(value).replace(/([\\`*_[\]<>])/g, '\\$1');
  const toMarkdown = (node) => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return escapeText(node.textContent || '');
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'svg'].includes(tag)) return '';
    if (tag === 'img') {
      let src = node.getAttribute('data-src') || node.getAttribute('src') || '';
      if (src.startsWith('//')) src = 'https:' + src;
      if (!src || src.startsWith('data:')) return '';
      return `\n\n![${escapeText(node.getAttribute('alt') || '')}](${src})\n\n`;
    }
    if (tag === 'br') return '\n';
    const children = Array.from(node.childNodes).map(toMarkdown).join('');
    const body = children.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').trim();
    if (!body) return '';
    if (/^h[1-6]$/.test(tag)) return `\n\n${'#'.repeat(Number(tag[1]))} ${body}\n\n`;
    if (tag === 'li') return `\n- ${body}`;
    if (tag === 'blockquote') return `\n\n> ${body.replace(/\n/g, '\n> ')}\n\n`;
    if (tag === 'pre') return `\n\n\`\`\`\n${node.innerText || node.textContent || ''}\n\`\`\`\n\n`;
    if (tag === 'code') return `\`${body}\``;
    if (tag === 'strong' || tag === 'b') return `**${body}**`;
    if (tag === 'em' || tag === 'i') return `*${body}*`;
    if (tag === 'a') {
      const href = node.getAttribute('href') || '';
      return href.startsWith('http') ? `[${body}](${href})` : body;
    }
    if (['p', 'div', 'section', 'figure', 'ul', 'ol', 'table'].includes(tag)) return `\n\n${body}\n\n`;
    return body;
  };
  const markdown = contentEl ? toMarkdown(contentEl).replace(/\n{3,}/g, '\n\n').trim() : '';
  return { title, author, published, content, markdown };
}"""


def normalize_text(text: str, title: str = "") -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line in {"微信扫一扫", "关注该公众号", "继续滑动看下一个"}:
            continue
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    if title and lines and lines[0].strip() == title.strip():
        lines = lines[1:]
    text = "\n\n".join(line for line in lines if line)
    for marker in ("\n\n收录于\n\n", "\n\n个人观点，仅供参考", "\n\n微信扫一扫"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def normalize_markdown(markdown: str, title: str = "") -> str:
    value = (markdown or "").replace("\u00a0", " ")
    for marker in ("微信扫一扫", "关注该公众号", "继续滑动看下一个"):
        value = value.replace(marker, "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if title and value.startswith(title):
        value = value[len(title) :].lstrip("\n ")
    return value


class BrowserArticleReader:
    """Reusable Playwright reader for batch WeChat article extraction."""

    def __init__(
        self,
        timeout: int = 45000,
        profile_dir: str | None = None,
        headless: bool = True,
        load_assets: bool = False,
    ) -> None:
        self.timeout = timeout
        self.profile = Path(profile_dir).expanduser() if profile_dir else DEFAULT_ARTICLE_PROFILE
        self.headless = headless
        self.load_assets = load_assets
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self) -> "BrowserArticleReader":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def start(self) -> None:
        if self.context:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Python Playwright is not available") from exc

        secure_directory(self.profile)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            str(self.profile),
            headless=self.headless,
            viewport={"width": 1280, "height": 1200},
        )
        if not self.load_assets:
            self.context.route("**/*", self._route_request)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.timeout)
        self.page.set_default_navigation_timeout(self.timeout)

    def close(self) -> None:
        if self.context:
            self.context.close()
            self.context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self.page = None

    def _route_request(self, route, request) -> None:
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        route.continue_()

    def read(self, url: str) -> metaso_reader.ReaderResult:
        self.start()
        assert self.page is not None
        url = normalize_wechat_url(url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
        try:
            final_url = normalize_wechat_url(self.page.url)
        except ValueError as exc:
            raise RuntimeError(f"browser redirected outside a public WeChat article URL: {self.page.url}") from exc
        try:
            self.page.wait_for_selector("#js_content", timeout=min(self.timeout, 12000))
            self.page.wait_for_function(CONTENT_READY_SCRIPT, timeout=min(self.timeout, 8000))
        except Exception:
            pass
        data = self.page.evaluate(EXTRACT_SCRIPT)
        return result_from_page_data(final_url, data)


def result_from_page_data(url: str, data: dict) -> metaso_reader.ReaderResult:
    url = normalize_wechat_url(url)
    title = (data.get("title") or "").strip() or "wechat-article"
    content = normalize_markdown(data.get("markdown") or "", title=title)
    if not content:
        content = normalize_text(data.get("content") or "", title=title)
    published = metaso_reader.extract_published(data.get("published") or "") or "unknown-date"
    page_text = f"{title}\n{content}"
    if any(marker in page_text for marker in BAD_PAGE_MARKERS):
        raise RuntimeError(f"browser reached a blocked or unavailable page ({title})")
    if len(content) < MIN_BROWSER_CONTENT_CHARS and not (
        len(content) >= MIN_SHORT_ARTICLE_CHARS and title != "wechat-article" and published != "unknown-date"
    ):
        page_hint = title or content[:80]
        raise RuntimeError(f"browser article body too short: {len(content)} chars ({page_hint})")

    markdown_parts = [f"# {title}", "", content]
    return metaso_reader.validate_result(
        metaso_reader.ReaderResult(
            url=url,
            markdown="\n".join(markdown_parts).strip() + "\n",
            title=title,
            published=published,
            author=(data.get("author") or "").strip(),
            retrieved_via="browser",
        ),
        expected_title=title,
    )


def read_url(
    url: str,
    timeout: int = 45000,
    profile_dir: str | None = None,
    headless: bool = True,
    load_assets: bool = False,
) -> metaso_reader.ReaderResult:
    with BrowserArticleReader(timeout=timeout, profile_dir=profile_dir, headless=headless, load_assets=load_assets) as reader:
        return reader.read(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read one WeChat article with Playwright")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", default=str(Path.cwd()))
    parser.add_argument("--profile-dir")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--load-assets", action="store_true", help="load images/fonts/media instead of blocking them")
    parser.add_argument("--timeout", type=int, default=45000, help="navigation timeout in ms")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = read_url(
            args.url,
            timeout=args.timeout,
            profile_dir=args.profile_dir,
            headless=not args.headed,
            load_assets=args.load_assets,
        )
        saved = metaso_reader.save_result(result, Path(args.output_dir).expanduser())
    except Exception as exc:
        print(json.dumps({"ok": False, "url": args.url, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)

    payload = {"ok": True, "url": args.url, "title": result.title, "published": result.published, "path": str(saved)}
    print(json.dumps(payload, ensure_ascii=False) if args.json else f"saved: {saved}")


if __name__ == "__main__":
    main()
