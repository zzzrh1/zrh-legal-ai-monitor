#!/usr/bin/env python3
"""Normalize authorized monitoring exports into a local incremental Markdown digest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LEGAL_KEYWORDS = (
    "律师", "律所", "司法", "法院", "检察", "法务", "合规", "法律服务", "法治", "电子证据", "数据出境",
)
AI_LAW_KEYWORDS = (
    "法律科技", "法律大模型", "法律人工智能", "ai律师", "律师ai", "法律智能体", "司法人工智能",
    "人工智能", "生成式人工智能", "算法治理", "深度合成", "模型备案", "数据合规",
)
CASE_RULE_KEYWORDS = ("入库案例", "指导性案例", "公报案例", "裁判要旨", "裁判规则")
CASE_MENTION_KEYWORDS = ("案例", "判决", "裁定", "庭审", "法院认为")
NORM_PRIMARY_KEYWORDS = ("司法解释", "法释", "法律", "条例", "规定", "办法", "意见", "通知")
NORM_MENTION_KEYWORDS = ("法条", "条文", "依据", "适用")
ACTIONABLE_KEYWORDS = ("裁判规则", "审查路径", "审理思路", "认定", "处理规则", "适用规则", "合规要点")
ANALYSIS_KEYWORDS = ("分析", "解读", "探析", "研究", "评析", "指南")
FRAMEWORK_KEYWORDS = ("审理思路", "审查路径", "裁判规则", "理解与适用", "体系", "框架")
AI_SYSTEMIC_KEYWORDS = ("监管", "备案", "司法", "法院", "部署", "上线", "发布", "模型", "智能体")
AI_APPLICATION_KEYWORDS = ("试点", "落地", "应用", "产品", "工作流", "合同审查", "法律检索")
AI_DETAIL_KEYWORDS = ("功能", "范围", "指标", "部署", "安全", "评测", "数据", "流程", "案例")
OFFICIAL_MARKERS = (
    "gov.cn", "court.gov.cn", "spp.gov.cn", "cac.gov.cn", "moj.gov.cn", "npc.gov.cn", "csrc.gov.cn",
)
OFFICIAL_SOURCE_NAMES = (
    "最高人民法院", "最高法", "京法网事", "浦江天平", "浙江天平", "江苏高院", "江苏知产视野",
    "山东高法", "上海一中法院", "上海二中院", "中国应用法学",
)
TRACKING_PARAMETERS = {
    "from", "source", "spm", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "xsec_token", "xsec_source", "share_id", "security_token", "access_token", "auth_token",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="JSON, JSONL, or directory containing exports; repeatable")
    parser.add_argument("--state", required=True, help="Local JSON state file for incremental deduplication")
    parser.add_argument("--output", required=True, help="Markdown digest path")
    parser.add_argument("--source", help="Fallback source/platform label for exports that do not include one")
    parser.add_argument("--source-keyword", help="Only process records whose source_keyword exactly matches this value")
    parser.add_argument("--collection-route", help="Fallback collection route, for example keyword_discovery")
    parser.add_argument("--topic-bucket", help="Fallback topic bucket for exports that do not include one")
    parser.add_argument("--limit", type=int, default=5, help="Maximum items to render in the daily digest")
    parser.add_argument("--max-per-source", type=int, default=2, help="Maximum selected candidates from one source when alternatives exist")
    parser.add_argument("--defer-days", type=int, default=2, help="Maximum future daily slots reserved for excess eligible items")
    parser.add_argument("--dry-run", action="store_true", help="Render a manual test without consuming or updating the local queue")
    parser.add_argument("--practice-keyword", action="append", default=[], help="Explicit user-approved practice-area or locality keyword; repeatable")
    parser.add_argument("--now", help="ISO-8601 timestamp for reproducible scoring")
    return parser.parse_args()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = str(value).replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalized_text(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())


def canonical_url(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.casefold() not in TRACKING_PARAMETERS]
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), urlencode(query), ""))


def first_value(record: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(float(value), 0.0)
    text = clean_text(value).casefold().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([wk万]?)", text)
    if not match:
        return 0.0
    amount = float(match.group(1))
    return amount * (10000 if match.group(2) in {"w", "万"} else 1000 if match.group(2) == "k" else 1)


def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = clean_text(value)
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00"), text.replace("/", "-")):
        try:
            result = dt.datetime.fromisoformat(candidate)
            return result.replace(tzinfo=dt.timezone.utc) if result.tzinfo is None else result.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


def classify(text: str) -> tuple[int, int, list[str]]:
    normalized = normalized_text(text)
    legal_hits = sum(keyword in normalized for keyword in LEGAL_KEYWORDS)
    ai_hits = sum(keyword in normalized for keyword in AI_LAW_KEYWORDS)
    labels: list[str] = []
    if legal_hits and ai_hits:
        labels.append("AI 法律")
    elif ai_hits:
        labels.append("AI")
    elif legal_hits:
        labels.append("法律行业")
    if any(token in normalized for token in ("法规", "条例", "办法", "意见", "通知", "监管", "立法", "备案")):
        labels.append("政策与监管")
    if any(token in normalized for token in ("案例", "判决", "裁定", "庭审", "证据")):
        labels.append("实务与案例")
    return legal_hits, ai_hits, labels or ["行业信号"]


def source_kind(source: str, url: str) -> str:
    combined = f"{source} {url}".casefold()
    return "官方" if any(marker in combined for marker in OFFICIAL_MARKERS) or any(marker in source for marker in OFFICIAL_SOURCE_NAMES) or any(marker in combined for marker in ("政府", "法院", "检察院", "司法部", "网信")) else "社媒/专业"


def keyword_level(text: str, strong: Iterable[str], medium: Iterable[str] = ()) -> int:
    normalized = normalized_text(text)
    if any(normalized_text(keyword) in normalized for keyword in strong):
        return 1
    if any(normalized_text(keyword) in normalized for keyword in medium):
        return 2
    return 3


def legal_rubric(text: str, kind: str, practice_keywords: Iterable[str]) -> dict[str, int]:
    case_density = keyword_level(text, CASE_RULE_KEYWORDS, CASE_MENTION_KEYWORDS)
    norm_anchoring = keyword_level(text, NORM_PRIMARY_KEYWORDS, NORM_MENTION_KEYWORDS)
    actionability = keyword_level(text, ACTIONABLE_KEYWORDS, ANALYSIS_KEYWORDS)
    framework_quality = keyword_level(text, FRAMEWORK_KEYWORDS, ANALYSIS_KEYWORDS)
    author_empirical_depth = 1 if kind == "官方" and case_density == 1 else 2 if kind == "官方" else 3
    relevance_halflife = 1 if any(token in text for token in ("合同", "侵权", "公司", "劳动", "执行", "知识产权")) else 2
    return {
        "case_density": case_density,
        "norm_anchoring": norm_anchoring,
        "actionability": actionability,
        "author_empirical_depth": author_empirical_depth,
        "framework_quality": framework_quality,
        "relevance_halflife": relevance_halflife,
        "practice_area_match": int(any(normalized_text(keyword) in normalized_text(text) for keyword in practice_keywords if clean_text(keyword))),
    }


def ai_law_rubric(text: str) -> dict[str, int]:
    signal_strength = keyword_level(text, AI_SYSTEMIC_KEYWORDS, AI_APPLICATION_KEYWORDS)
    detail_depth = 1 if len(text) >= 160 and any(token in text for token in AI_DETAIL_KEYWORDS) else 2 if any(token in text for token in AI_DETAIL_KEYWORDS) else 3
    practice_relevance = 1 if any(token in text for token in ("律师", "律所", "法务", "合规", "法院", "司法", "合同")) else 2
    china_relevance = 1 if any(token in text for token in ("中国", "国内", "最高人民法院", "网信办", "国家")) else 2
    return {
        "signal_strength": signal_strength,
        "detail_depth": detail_depth,
        "practice_relevance": practice_relevance,
        "china_relevance": china_relevance,
    }


def editorial_score(rubric: dict[str, int], is_ai_law: bool) -> float:
    if is_ai_law:
        base = {1: 9.0, 2: 7.0, 3: 5.0}[rubric["signal_strength"]]
        score = base - (rubric["detail_depth"] - 1) * 0.5 - (rubric["practice_relevance"] - 1) * 0.4
        score -= (rubric["china_relevance"] - 1) * 0.2
    else:
        score = 10.0
        for key, penalty in (
            ("case_density", 0.9),
            ("norm_anchoring", 0.9),
            ("actionability", 0.9),
            ("author_empirical_depth", 0.8),
            ("framework_quality", 0.6),
            ("relevance_halflife", 0.5),
        ):
            score -= (rubric[key] - 1) * penalty
        if rubric["practice_area_match"] == 1:
            score += 0.3
    return round(max(1.0, min(10.0, score)), 1)


def screening_reason(rubric: dict[str, int], is_ai_law: bool) -> str:
    if is_ai_law:
        labels = {1: "格局级", 2: "应用落地级", 3: "市场信号级"}
        parts = [labels[rubric["signal_strength"]]]
        if rubric["detail_depth"] == 1:
            parts.append("细节充分")
        if rubric["practice_relevance"] == 1:
            parts.append("直接关联法律实务")
        if rubric["china_relevance"] == 1:
            parts.append("中国场景可借鉴")
        return "、".join(parts)
    labels = []
    if rubric["case_density"] == 1:
        labels.append("含具体案例与规则")
    if rubric["norm_anchoring"] == 1:
        labels.append("规范锚定充分")
    if rubric["actionability"] == 1:
        labels.append("可提取实务路径")
    if rubric["author_empirical_depth"] == 1:
        labels.append("一手权威来源")
    if rubric["practice_area_match"] == 1:
        labels.append("匹配已声明关注方向")
    return "、".join(labels) or "可保留为法律实务参考"


def select_diverse(items: list[dict[str, Any]], limit: int, max_per_source: int) -> list[dict[str, Any]]:
    if max_per_source <= 0:
        return items[:limit]
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    source_counts: dict[str, int] = defaultdict(int)
    for item in items:
        if source_counts[item["source"]] < max_per_source:
            selected.append(item)
            source_counts[item["source"]] += 1
        else:
            overflow.append(item)
    if len(selected) < limit:
        selected.extend(overflow[: limit - len(selected)])
    return selected[:limit]


def delivery_channel(source: str, collection_route: str, url: str = "") -> str:
    normalized_source = source.casefold()
    normalized_route = collection_route.casefold()
    normalized_url = url.casefold()
    if source in OFFICIAL_SOURCE_NAMES or "wechat" in normalized_source or "公众号" in normalized_source or "approved_article" in normalized_route:
        return "公众号"
    if any(marker in f"{normalized_source} {normalized_url}" for marker in ("xiaohongshu", "xhs", "小红书")):
        return "小红书"
    return "其他"


def delivery_sort_key(item: dict[str, Any]) -> tuple[int, int, int, float, float]:
    channel_rank = {"公众号": 0, "小红书": 1, "其他": 2}[item["delivery_channel"]]
    verified_rank = 0 if item["verification"] == "已核验" else 1
    urgent_rank = 0 if item["verification"] == "已核验" and ("政策与监管" in item["labels"] or item["score"] >= 70) else 1
    return urgent_rank, channel_rank, verified_rank, -item["score"], -item["editorial_score"]


def schedule_deferred_items(
    items: list[dict[str, Any]],
    retained_deferred: list[dict[str, Any]],
    now: dt.datetime,
    daily_limit: int,
    defer_days: int,
) -> list[dict[str, Any]]:
    slot_dates = [(now + dt.timedelta(days=days_ahead)).date().isoformat() for days_ahead in range(1, defer_days + 1)]
    scheduled_count = {slot_date: 0 for slot_date in slot_dates}
    for item in retained_deferred:
        scheduled_for = clean_text(item.get("scheduled_for"))
        if scheduled_for in scheduled_count:
            scheduled_count[scheduled_for] += 1

    scheduled: list[dict[str, Any]] = []
    for item in items:
        available_date = next((slot_date for slot_date in slot_dates if scheduled_count[slot_date] < daily_limit), None)
        if available_date is None:
            break
        item["scheduled_for"] = available_date
        scheduled_count[available_date] += 1
        scheduled.append(item)
    return scheduled


def is_valid_deferred_item(item: dict[str, Any], now: dt.datetime) -> bool:
    scheduled_for = clean_text(item.get("scheduled_for"))
    if not scheduled_for:
        return True
    try:
        return dt.date.fromisoformat(scheduled_for) <= now.date()
    except ValueError:
        return True


def fingerprint(title: str, url: str) -> str:
    basis = canonical_url(url) or normalized_text(title)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def read_records(path: Path) -> Iterable[dict[str, Any]]:
    if path.is_dir():
        for item in sorted(path.rglob("*")):
            if item.suffix.casefold() in {".json", ".jsonl", ".ndjson"}:
                yield from read_records(item)
        return
    if path.suffix.casefold() in {".jsonl", ".ndjson"}:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    yield payload
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        yield from payload.get("items", [payload])
    elif isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))


def normalize_record(
    record: dict[str, Any],
    now: dt.datetime,
    fallback_source: str,
    fallback_collection_route: str,
    fallback_topic_bucket: str,
    practice_keywords: Iterable[str],
) -> dict[str, Any] | None:
    title = clean_text(first_value(record, ("title", "note_title", "aweme_desc", "content", "desc", "text")))
    content = clean_text(first_value(record, ("content", "desc", "note_desc", "aweme_desc", "digest", "summary", "abstract", "text")))
    url = canonical_url(clean_text(first_value(record, ("url", "note_url", "share_url", "link", "video_url"))))
    if not title and not content:
        return None
    if not title:
        title = content[:80]
    if not content:
        content = "摘要获取失败，请点击原文查看"
    source = clean_text(first_value(record, ("platform", "source", "source_name", "channel", "nickname", "author"))) or fallback_source or "未知来源"
    collection_route = clean_text(first_value(record, ("collection_route", "route"))) or fallback_collection_route or "未提供"
    topic_bucket = clean_text(first_value(record, ("topic_bucket", "topic", "category"))) or fallback_topic_bucket or "未分类"
    social_grade = clean_text(first_value(record, ("social_grade", "signal_grade", "grade")))
    published = parse_time(first_value(record, ("published_at", "publish_time", "create_time", "time", "date")))
    legal_hits, ai_hits, labels = classify(f"{title} {content}")
    if source_kind(source, url) != "官方" and not (legal_hits or ai_hits):
        return None
    engagement = sum(number(record.get(key)) for key in ("liked_count", "like_count", "collect_count", "comment_count", "share_count", "repost_count", "view_count"))
    age_days = max((now - published).total_seconds() / 86400, 0) if published else 14
    recency = max(0, 30 - min(age_days, 14) * 2)
    relevance = min(30, legal_hits * 9 + ai_hits * 9 + (12 if legal_hits and ai_hits else 0))
    official_article = source_kind(source, url) == "官方" or "approved_article" in collection_route
    source_score = 20 if official_article else 8
    engagement_score = min(15, math.log10(engagement + 1) * 4)
    kind = "官方" if official_article else "社媒/专业"
    is_ai_law = legal_hits > 0 and ai_hits > 0
    rubric = ai_law_rubric(f"{title} {content}") if is_ai_law else legal_rubric(f"{title} {content}", kind, practice_keywords)
    rubric_score = editorial_score(rubric, is_ai_law)
    verification = "已核验" if kind == "官方" else "待核验"
    channel = delivery_channel(source, collection_route, url)
    return {
        "title": title,
        "content": content,
        "url": url,
        "source": source,
        "collection_route": collection_route,
        "topic_bucket": topic_bucket,
        "social_grade": social_grade,
        "published": published.isoformat() if published else "",
        "kind": kind,
        "verification": verification,
        "delivery_channel": channel,
        "labels": labels,
        "engagement": round(engagement),
        "score": round(recency + relevance + source_score + engagement_score, 1),
        "editorial_score": rubric_score,
        "rubric": rubric,
        "screening_layer": "精读" if verification == "已核验" and rubric_score >= 8 else "雷达" if rubric_score >= 6.5 else "归档",
        "why_it_matters": screening_reason(rubric, is_ai_law),
        "fingerprint": fingerprint(title, url),
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen": [], "deferred": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"seen": [], "deferred": []}
    return {
        "seen": [item for item in payload.get("seen", []) if isinstance(item, str)],
        "deferred": [item for item in payload.get("deferred", []) if isinstance(item, dict)],
    }


def render(items: list[dict[str, Any]], deferred: list[dict[str, Any]], created_at: dt.datetime, dry_run: bool) -> str:
    official_items = [item for item in items if item.get("delivery_channel") == "公众号"]
    social_items = [item for item in items if item.get("delivery_channel") != "公众号"]
    channels = defaultdict(int)
    for item in items:
        channels[item.get("delivery_channel", "其他")] += 1
    lines = [
        f"# 中国法律与 AI 日报 · {created_at.strftime('%Y.%m.%d')}",
        "",
        f"> 今日精选 {len(items)} 条｜公众号 {channels['公众号']} · 小红书 {channels['小红书']} · 其他 {channels['其他']}",
        "",
    ]
    sections = (("官方发布｜法院公众号", official_items), ("社媒观察｜小红书法律 + AI", social_items))
    item_number = 0
    for section, section_items in sections:
        if not section_items:
            continue
        lines.extend((f"## {section}", ""))
        for item in section_items:
            item_number += 1
            published = item["published"][:10] if item["published"] else "时间未提供"
            link = item["url"] or "未提供链接"
            excerpt = item["content"][:140] if item["content"] else "仅有标题，需回看原文。"
            source_line = f"来源：{item['source']} · {published}"
            if item["verification"] == "待核验":
                source_line += " · 社媒线索，待核验"
            lines.extend((
                f"### {item_number:02d}. {item['title']}",
                f"*{source_line}*",
                "",
                excerpt,
                "",
                f"**推荐理由**：{item['why_it_matters']}",
                "",
                f"[阅读原文]({link})",
                "",
            ))
    if any(item["verification"] == "待核验" for item in items):
        lines.extend(("> 社媒内容仅作选题线索，请以原文及官方信源为准。", ""))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    now = parse_time(args.now) if args.now else dt.datetime.now(tz=dt.timezone.utc)
    if now is None:
        raise SystemExit("--now must be an ISO-8601 timestamp")
    state_path, output_path = Path(args.state).expanduser(), Path(args.output).expanduser()
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    candidates: dict[str, dict[str, Any]] = {}
    retained_deferred: list[dict[str, Any]] = []
    for deferred_item in state.get("deferred", []):
        item_fingerprint = clean_text(deferred_item.get("fingerprint"))
        if not item_fingerprint or item_fingerprint in seen:
            continue
        deferred_item.setdefault("delivery_channel", delivery_channel(deferred_item.get("source", ""), deferred_item.get("collection_route", "")))
        if is_valid_deferred_item(deferred_item, now):
            candidates[item_fingerprint] = deferred_item
        else:
            retained_deferred.append(deferred_item)
    failures: list[str] = []
    for value in args.input:
        input_path = Path(value).expanduser()
        if not input_path.exists():
            failures.append(f"not found: {input_path}")
            continue
        try:
            for record in read_records(input_path):
                record_keyword = clean_text(record.get("source_keyword"))
                if args.source_keyword and record_keyword and record_keyword != args.source_keyword:
                    continue
                item = normalize_record(
                    record,
                    now,
                    clean_text(args.source),
                    clean_text(args.collection_route),
                    clean_text(args.topic_bucket),
                    args.practice_keyword,
                )
                if not item or item["fingerprint"] in seen:
                    continue
                previous = candidates.get(item["fingerprint"])
                if not previous or item["score"] > previous["score"]:
                    candidates[item["fingerprint"]] = item
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{input_path}: {exc}")
    daily_limit = max(args.limit, 1)
    ranked = sorted(candidates.values(), key=delivery_sort_key)
    visible = [item for item in ranked if item["screening_layer"] != "归档" or item["verification"] == "待核验"]
    items = select_diverse(visible, daily_limit, args.max_per_source)
    selected_fingerprints = {item["fingerprint"] for item in items}
    remaining = [item for item in visible if item["fingerprint"] not in selected_fingerprints]
    new_deferred = schedule_deferred_items(
        remaining,
        retained_deferred,
        now,
        daily_limit,
        max(args.defer_days, 0),
    )
    deferred = retained_deferred + new_deferred
    archived = [item for item in candidates.values() if item["fingerprint"] not in selected_fingerprints and item not in new_deferred]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render(items, deferred, now, args.dry_run), encoding="utf-8")
    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        updated_seen = list(dict.fromkeys([*seen, *(item["fingerprint"] for item in [*items, *archived])]))[-5000:]
        state_path.write_text(json.dumps({
            "seen": updated_seen,
            "deferred": deferred,
            "updated_at": now.isoformat(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "daily_items": len(items),
        "deferred_items": len(deferred),
        "archived_items": len(archived),
        "skipped_seen": len(seen),
        "dry_run": args.dry_run,
        "warnings": failures,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
