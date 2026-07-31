#!/usr/bin/env python3
"""Convert exact-account wechat-article-search results into official JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, help="Directory of wechat-article-search JSON results")
    parser.add_argument("--output", required=True, help="Destination JSONL path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser()
    output_path = Path(args.output).expanduser()
    converted: list[dict[str, str]] = []
    rejected = 0
    for path in sorted(input_dir.glob("*.json")):
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        account = str(document.get("query", "")).strip()
        for article in document.get("articles", []):
            if not isinstance(article, dict) or str(article.get("source", "")).strip() != account:
                rejected += 1
                continue
            converted.append({
                "title": str(article.get("title", "")).strip(),
                "content": str(article.get("summary", "")).strip() or "搜索结果未提供摘要，请点击原文查看。",
                "url": str(article.get("url", "")).strip(),
                "source": account,
                "collection_route": "approved_article_discovery",
                "published_at": str(article.get("datetime", "")).strip(),
            })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in converted), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "official_candidates": len(converted), "rejected_reposts": rejected}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
