---
name: china-legal-ai-monitor
description: Collect, deduplicate, score, and summarize China legal-industry and AI-law developments from authorized public web and social-media sources. Use when the user asks to monitor legal-industry trends, 法律行业热点, AI 法律资讯, 法律科技动态, social-media legal discussions, or to prepare a daily/weekly alert digest and optionally push it to WeChat through ClawBot.
---

# China Legal & AI Monitor

Build an evidence-first Chinese digest for legal-industry and AI-law developments. Treat social posts as leads, not legal conclusions.

## Daily Schedule

On the first request to configure the daily monitor, inspect the local schedule file. If `daily_time` is empty or its status is `needs_user_time`, ask exactly: `每天几点（北京时间）生成日报？` Do not create a recurring automation, push a digest, or assume a time before the user answers.

When the user gives or changes a time, validate `HH:MM`, update the local schedule, then use the app's automation tool to create or update one daily run. Keep the same user-facing request available as a manual test; use `--dry-run` so a test never consumes the delivery queue. To pause scheduled runs, set the local schedule to disabled and update the corresponding automation.

The daily digest has a fixed limit of five items. Its reader-facing sections are `官方发布｜法院公众号` and `社媒观察｜小红书法律 + AI`; omit an empty section. Rank routine delivery by `微信公众号` → `小红书`; verified regulatory or judicial material may override this order. Keep up to two later daily slots in the local queue for excess eligible candidates, then archive any remaining low-priority material locally. Do not silently drop a queued item.

The monitor owns collection, filtering, queueing, and local rendering only. A separately configured sender (for example Hermes) owns WeChat delivery. Never block, alter, or omit a digest because a sender is absent; do not test sending unless the user explicitly asks the configured sender to send.

Every daily run must merge the official-public-account queue and the 小红书 queue before the five-item cap. A social `source_keyword` filter applies only to records that contain that field; it must never exclude official input. For a format test, use a fresh state path and `--dry-run`; report both channel counts and treat a one-channel result as a failed integration test when both queues supplied eligible candidates.

## Workflow

1. Read [social-collection.md](references/social-collection.md) before running social collection. Build two separate queues: a fixed **account watchlist** for durable signals and a rotating **keyword discovery** queue for unexpected developments. Use only accounts, public data, access methods, platforms, and volumes the user is authorized to use.
2. Read [sources-and-scoring.md](references/sources-and-scoring.md) to select sources and keywords. Cover both `法律行业` and `AI 法律` unless the user narrows the scope.
3. Collect official court-public-account articles first, then collect the public social queue from `小红书` for legal and AI-law practitioner/workflow signals. Keep the two sources separate through normalization and delivery. Use only the user's authorized MediaCrawler setup and public data; do not collect private, paywalled, or personal data.
4. For every tracked account, retain a rolling 20-post baseline and the account follower snapshot. Use the relative-performance and reach checks in [social-collection.md](references/social-collection.md), rather than comparing raw likes across accounts.
5. Run `scripts/build_digest.py` to normalize records, remove previously-seen items, apply the explainable legal/AI-law screening rubric, cap repeated sources, and render a Markdown brief. Store the state file outside the skill directory, for example `~/legal-monitor/state.json`.

## Official WeChat Pipeline

This Skill bundles the WeChat integration under `integrations/`; Hermes must use these internal paths rather than depend on sibling Skills. Run `npm install` in `integrations/wechat-article-search/` before its first search. Use `integrations/wechat-article-search/scripts/search_wechat.js` only to discover public article links and verified account names. Before harvesting, run `integrations/wxmp-article-harvester/scripts/preflight.py --json`; if `wcx` or Playwright is unavailable, report that the official queue cannot run. Do not claim official monitoring is active without a successful harvester report.

For each approved court account, run the bundled harvester with a conservative date range and its documented login flow. It must stop on CAPTCHA/risk control and retain `partial` items as unresolved. Convert only successful full-text exports before digesting:

```bash
python3 scripts/import_wxmp_harvest.py \
  --export-dir <harvester-account-export> \
  --account "上海一中法院" \
  --output <monitor-inbox>/official-shanghai-no1.jsonl
```

Then pass that JSONL alongside the 小红书 JSONL to `build_digest.py`. The converter excludes `partial`、`failed` and `index-only` records; successful records enter `官方发布｜法院公众号` and are ranked before `社媒观察｜小红书法律 + AI`.

Search-discovery results may be used as a temporary official candidate queue only when the reported account exactly matches the configured watchlist account. Preserve their search-result links and summaries; replace them with full-text harvester records after the cooldown completes.
6. Verify each high-priority social lead against a primary source before stating it as fact. If not verified, label it `待核验` and retain the original URL.
7. Present the digest for review. Send it through `wechat-clawbot-notify` only after its `status` command reports `Ready: True` and the user has authorized the push.

## Commands

Create an incremental digest from JSON/JSONL exports:

```bash
python3 "$SKILL_DIR/scripts/build_digest.py" \
  --input ~/legal-monitor/inbox \
  --state ~/legal-monitor/state.json \
  --output ~/legal-monitor/digests/2026-07-30.md \
  --limit 5 \
  --defer-days 2
```

Manual test without changing the queue:

```bash
python3 "$SKILL_DIR/scripts/build_digest.py" \
  --input ~/legal-monitor/inbox \
  --state ~/legal-monitor/state.json \
  --output ~/legal-monitor/digests/test.md \
  --dry-run
```

Review the output before distribution. To send an approved digest, use the notification skill's documented `send` command; never read or expose its token/configuration.

## Required Output

Use this order in every digest:

1. `必须关注` — verified policy, court, regulatory, or material market event.
2. `AI 法律` — model, product, governance, data compliance, judicial-AI, and legal-tech developments.
3. `行业信号` — firm, practitioner, enterprise, and social-media discussion signals.
4. `雷达补充` — verified or credible professional items retained outside the main reading queue.
5. `待核验` — social-only leads that need a primary source.
6. `行动建议` — one to three specific follow-ups, never personalized legal advice.

For every item preserve: title, source/platform, time (or `时间未提供`), canonical URL, topic bucket, collection route (`账号监控` or `关键词发现`), signal label, and a short why-it-matters. Include relative-performance indicators when they exist. Separate confirmed facts from inference.

Apply the selection policy in [sources-and-scoring.md](references/sources-and-scoring.md): do not silently discard lower-priority official or professional legal items; place them in the radar layer. Never treat an automated score as a legal conclusion.

## Safety Rules

- Do not use the monitor to give legal advice, predict case outcomes, or make regulatory compliance determinations.
- Do not bypass logins, CAPTCHAs, paywalls, rate limits, robots.txt, platform controls, or terms of service. Do not use proxy rotation or stealth features to evade detection.
- Do not collect private messages, personal profiles, contact details, or unnecessary comments. Prefer post-level metadata and public text excerpts.
- Keep raw collection output and deduplication state local. Do not put tokens, cookies, exports, or personal data inside this skill directory or version control.
- Do not use MediaCrawler for commercial monitoring unless its current license and the target platforms' terms explicitly permit the intended use. Offer official APIs, RSS, licensed providers, or manual exports when they do not.
