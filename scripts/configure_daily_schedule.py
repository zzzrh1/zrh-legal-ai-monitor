#!/usr/bin/env python3
"""Create, inspect, or change the local daily-digest schedule settings."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_CONFIG = Path("MediaCrawler/data/legal-monitor-config/daily-digest-schedule.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Local schedule JSON path")
    parser.add_argument("--show", action="store_true", help="Print the current settings")
    parser.add_argument("--set-time", metavar="HH:MM", help="Set the daily trigger time in Asia/Shanghai")
    parser.add_argument("--disable", action="store_true", help="Disable the scheduled run but preserve the time")
    return parser.parse_args()


def default_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": "china-legal-ai-monitor",
        "timezone": "Asia/Shanghai",
        "schedule": {
            "enabled": False,
            "daily_time": None,
            "status": "needs_user_time",
        },
        "daily_delivery": {
            "limit": 5,
            "defer_days": 2,
            "source_priority": ["微信公众号", "小红书", "其他"],
            "manual_dry_run_enabled": True,
        },
    }


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schedule configuration must be a JSON object")
    config = default_config()
    config.update(payload)
    config["schedule"].update(payload.get("schedule", {}))
    config["daily_delivery"].update(payload.get("daily_delivery", {}))
    return config


def main() -> int:
    args = parse_args()
    if args.set_time and not TIME_PATTERN.fullmatch(args.set_time):
        raise SystemExit("--set-time must use 24-hour HH:MM, for example 08:30")
    path = Path(args.config).expanduser()
    config = load_config(path)
    if args.set_time:
        config["schedule"].update({"enabled": True, "daily_time": args.set_time, "status": "configured"})
    if args.disable:
        config["schedule"].update({"enabled": False, "status": "disabled"})
    if args.set_time or args.disable or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"config": str(path), **config}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
