#!/usr/bin/env python3
"""Fetch one cursor-safe wcx metadata batch through wcx's installed Python."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time


MAX_BATCH_SIZE = 80
RATE_LIMIT_MARKERS = ("freq control", "rate limited", "ret=200013", "频率控制", "触发风控", "操作频繁")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch one offset-based wcx metadata batch")
    parser.add_argument("--account", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--previous-total", type=int, default=0)
    parser.add_argument("--head-aid", default="")
    parser.add_argument("--boundary-aid", default="")
    parser.add_argument("--boundary-create-time", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=5)
    parser.add_argument("--min-delay", type=float, default=5.0)
    parser.add_argument("--max-delay", type=float, default=15.0)
    args = parser.parse_args()
    if args.offset < 0:
        raise SystemExit("ERROR: --offset must be non-negative")
    if not 1 <= args.limit <= MAX_BATCH_SIZE:
        raise SystemExit(f"ERROR: --limit must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.page_size <= 5:
        raise SystemExit("ERROR: --page-size must be between 1 and 5")

    try:
        from wcx import cache, config
        from wcx.fetcher import Fetcher
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "status": "dependency_error",
            "error": f"cannot import installed wcx package: {exc}",
        }, ensure_ascii=False))
        raise SystemExit(1) from exc

    begin = args.offset
    fetched = 0
    remote_total = 0
    current_head_aid = ""
    last_aid = args.boundary_aid
    last_create_time = args.boundary_create_time
    account_name = args.account

    try:
        credentials = config.load_credentials()
        if not credentials:
            raise RuntimeError("wcx credentials are missing; run the login refresh first")
        fetcher = Fetcher(credentials.token, credentials.cookie)
        account = fetcher.resolve(args.account)
        account_name = account.nickname
        head_articles, remote_total = fetcher.list_articles(account.fakeid, begin=0, count=1)
        current_head_aid = head_articles[0].aid if head_articles else ""
        if args.offset and args.previous_total:
            delta = remote_total - args.previous_total
            drift_error = ""
            drift_payload: dict[str, object] = {}
            if delta < 0:
                drift_error = "remote article total decreased; safe offset continuation cannot be proven"
            elif args.head_aid and current_head_aid != args.head_aid and delta == 0:
                drift_error = "remote head changed without a total-count increase"
            else:
                begin += delta
                if args.boundary_aid and begin > 0:
                    boundary, _ = fetcher.list_articles(account.fakeid, begin=begin - 1, count=1)
                    actual_boundary = boundary[0].aid if boundary else ""
                    if actual_boundary != args.boundary_aid:
                        drift_error = "saved batch boundary no longer matches the remote article list"
                        drift_payload = {
                            "expected_boundary_aid": args.boundary_aid,
                            "actual_boundary_aid": actual_boundary,
                        }
            if drift_error:
                print(json.dumps({
                    "ok": False,
                    "status": "cursor_drift",
                    "cursor_drift": True,
                    "error": drift_error,
                    "remote_total": remote_total,
                    "head_aid": current_head_aid,
                    **drift_payload,
                }, ensure_ascii=False))
                raise SystemExit(4)

        with cache.connect() as connection:
            cache.upsert_account(connection, account.to_dict())
            while fetched < args.limit:
                count = min(args.page_size, args.limit - fetched)
                articles, remote_total = fetcher.list_articles(account.fakeid, begin=begin, count=count)
                if not articles:
                    break
                for article in articles:
                    cache.upsert_article(connection, article.to_dict())
                    last_aid = article.aid
                    last_create_time = int(article.create_time)
                fetched += len(articles)
                begin += len(articles)
                print(f"[wxmp-harvester] batch metadata {fetched}/{args.limit} (offset={begin})", file=sys.stderr, flush=True)
                if begin >= remote_total or fetched >= args.limit:
                    break
                time.sleep(random.uniform(args.min_delay, args.max_delay))
    except SystemExit:
        raise
    except Exception as exc:
        message = str(exc)
        rate_limited = any(marker in message.lower() for marker in RATE_LIMIT_MARKERS)
        print(json.dumps({
            "ok": False,
            "status": "rate_limited" if rate_limited else "failed",
            "account": account_name,
            "fetched": fetched,
            "next_offset": begin,
            "remote_total": remote_total,
            "head_aid": current_head_aid,
            "boundary_aid": last_aid,
            "boundary_create_time": last_create_time,
            "rate_limited": rate_limited,
            "error": message,
        }, ensure_ascii=False))
        raise SystemExit(75 if rate_limited else 1)

    print(json.dumps({
        "ok": True,
        "status": "complete",
        "account": account_name,
        "fetched": fetched,
        "next_offset": begin,
        "remote_total": remote_total,
        "head_aid": current_head_aid,
        "boundary_aid": last_aid,
        "boundary_create_time": last_create_time,
        "exhausted": begin >= remote_total,
        "rate_limited": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
