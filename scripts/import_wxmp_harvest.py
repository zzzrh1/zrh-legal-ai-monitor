#!/usr/bin/env python3
"""Convert a wxmp-article-harvester export into official-digest JSONL."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", required=True, help="Harvester account export directory containing index.json")
    parser.add_argument("--account", required=True, help="Verified official public-account name")
    parser.add_argument("--output", required=True, help="Destination JSONL path")
    return parser.parse_args()


def article_text(path: Path) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\A---\s*.*?\n---\s*", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]+\]\([^)]*\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    args = parse_args()
    export_dir = Path(args.export_dir).expanduser()
    index_path = export_dir / "index.json"
    if not index_path.is_file():
        raise SystemExit(f"missing harvester index: {index_path}")
    records: Any = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("harvester index.json must be an array")
    output_path = Path(args.output).expanduser()
    converted: list[dict[str, str]] = []
    skipped = 0
    for record in records:
        if not isinstance(record, dict) or record.get("status") in {"partial", "failed", "index-only"}:
            skipped += 1
            continue
        body = article_text(export_dir / str(record.get("path", "")))
        if not body:
            skipped += 1
            continue
        converted.append({
            "title": str(record.get("title", "")).strip(),
            "content": body,
            "url": str(record.get("url", "")).strip(),
            "source": args.account,
            "collection_route": "approved_article_harvest",
            "published_at": str(record.get("published", "")).strip(),
        })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in converted), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "official_articles": len(converted), "skipped": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
