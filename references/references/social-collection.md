# Social Collection

## Purpose

Use social media to find practitioner sentiment, product launches, recruiting shifts, client concerns, and emerging legal-tech discussions. It is a discovery layer, not an authority layer.

This monitor has two lanes. Do not replace one with the other.

| Lane | Collection unit | Question answered | Cadence |
| --- | --- | --- | --- |
| `账号监控` | A user-approved list of public institutional/creator accounts | Which known legal-AI actors posted, and which post is unusually strong for that account? | Daily or every workday |
| `关键词发现` | A bounded rotating query set | What new event, product, concern, or vocabulary is appearing outside the watchlist? | Two to four platform/query combinations per day |

The reference design uses the same separation: multi-platform account scans, a rolling per-account baseline, then deeper analysis only for exceptional items. For legal monitoring, the output is a verified industry signal, not a content-production recommendation.

## Platform Matrix

| Platform | Primary role | Collect | Priority | Route |
| --- | --- | --- | --- | --- |
| Official sites / RSS | Facts and confirmation | Regulations, court/agency notices, consultations, product announcements | P0 | Official RSS/API/web pages |
| 小红书 | Lawyer, law student, and in-house workflow signals | Public posts on AI tools, training, contract/document review, practical adoption | P1 | Authorized MediaCrawler |
| 抖音 / 快手 | High-reach practitioner pain points and product demos | Public short-video title/description, visible engagement, creator category | P1 | Authorized MediaCrawler |
| B站 | Demonstrations, tutorials, evaluations | Public video metadata, chapter/description, visible engagement | P1 | Authorized MediaCrawler |
| 知乎 | Long-form explanation, product comparison, implementation debate | Public questions/answers/articles and visible engagement | P1 | Authorized MediaCrawler |
| 微博 | Fast policy/event diffusion | Public posts, repost/like/comment aggregates, linked primary source | P1 | Authorized MediaCrawler |
| 贴吧 | Early but low-confidence chatter | Public thread title, excerpt, visible engagement only | P3 | Authorized MediaCrawler |
| X / YouTube / Reddit | Overseas legal-AI, models, governance, cross-border legaltech | Public posts/videos/threads from approved lists and English keyword discovery | P2 | XCrawl supported scrapers or official APIs |

`P0` must be checked every digest cycle. `P1` is the core Chinese social coverage. `P2` is a daily/weekly complement. `P3` is discovery-only and can never independently create a high-priority alert.

## Daily Delivery Order

For a daily digest, prefer eligible content in this order: approved public articles or official reposts from the WeChat court list, then authorized Xiaohongshu discovery, then other channels. A verified regulatory or judicial development can override this order. Render at most five items, queue up to two following daily slots locally, and preserve a manual `--dry-run` test that does not consume the queue.

## What to Collect

Collect the minimum public post-level record needed for ranking and later verification:

```json
{
  "platform": "zhihu",
  "collection_route": "account_watchlist",
  "account_category": "律所/法律科技厂商/监管机构/律师/企业法务",
  "account_id": "public-platform-id-if-authorized",
  "account_followers": 150000,
  "post_id": "platform-post-id",
  "title": "公开标题",
  "content": "公开正文或短摘要",
  "published_at": "2026-07-30T09:00:00+08:00",
  "url": "https://example.com/post/1",
  "liked_count": 120,
  "comment_count": 18,
  "share_count": 8,
  "collect_count": 42,
  "content_type": "article/video/post",
  "topic_bucket": "法律科技与产品"
}
```

Never collect private messages, contact details, follower lists, commenter identities, audio/video files, or comment bodies for the routine monitor. Keep comments disabled. The value is the post and its aggregate signal, not a dossier on people.

## Account Watchlist Construction

Start with 24–40 approved public accounts, balanced across platforms; add more only after baseline quality is proven. Use categories rather than indiscriminately tracking individuals:

- 8–12: legal-tech vendors and legal-AI product teams.
- 6–10: leading law firms, lawyers, and law-school/legal research organizations that publicly discuss technology.
- 4–8: enterprise legal/compliance teams and professional associations.
- 4–8: legal media, technology media, and trusted industry analysts.
- 2–5 per overseas platform: legal-AI vendors, legal innovation researchers, and AI-governance institutions relevant to China/cross-border work.

Each account must have: platform, public account identifier, category, why it is watched, owner who approved it, and collection cadence. Do not add an account merely because it has high follower count.

For the local WeChat court-source list, use `MediaCrawler/data/legal-monitor-config/court-wechat-watchlist.json`. Its additional reference sources are `上海一中法院`、`上海二中院` and `中国应用法学`; collect only through the approved public-article, official-repost, RSS, or compliant-export routes specified there. Do not use account identifiers, credentials, or a public-account backend to turn the list into a scraper.

Copy [account-watchlist.example.json](account-watchlist.example.json) outside the skill directory and replace its placeholders only with approved public accounts. Keep the real watchlist local because it reflects monitoring intent.

## Signal Scoring for Account Monitoring

Store the latest 20 posts per watched account and a daily follower snapshot. Score a post against its own account history, then apply a cross-account reach check.

```text
core metric: 小红书 = likes + collects; other platforms = likes (add shares when reliable)
R = current core metric / median(core metric of the account's latest 20 posts)
M = current likes / account followers
```

Use follower bands to calibrate `M`: under 10k → 0.30; 10k–100k → 0.15; 100k–1m → 0.08; above 1m → 0.04. Classify an item only when both signals clear the bar:

| Grade | Relative `R` | Reach `M` threshold | Legal-monitor treatment |
| --- | --- | --- | --- |
| `S3` exceptional | >= 8 | >= 3 × band baseline | Immediate editorial review |
| `S2` strong | >= 4 | >= 1.5 × band baseline | Include in daily candidate list |
| `S1` emerging | >= 2 | >= band baseline | Retain as an industry signal |
| weak relative spike | >= 2 | below band baseline | Archive; do not alert by itself |

For new accounts with fewer than 20 posts, mark `baseline_insufficient`; rank by recency and visible engagement but do not apply an S-grade. Freeze the follower snapshot and baseline used when a grade is first issued, so later growth does not rewrite the historical judgment.

## Authorized MediaCrawler Route

The local checkout is `/Users/a1-6/book-workflow/MediaCrawler`. Its documented public-platform search coverage includes Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Tieba, and Zhihu. It can emit JSONL, which this skill consumes.

Before any run, obtain or confirm all of the following:

- The user is authorized to use the selected platform account and the intended collection method complies with the platform rules.
- The target query, platform, date range, and collection volume are agreed.
- The collection is limited to public results and uses the minimum necessary volume.
- `ENABLE_IP_PROXY = False`, `MAX_CONCURRENCY_NUM = 1`, comments are disabled unless explicitly needed, and no feature intended to evade platform detection is enabled.

Never request passwords, cookies, QR codes, or tokens in chat. Let the user complete interactive login locally. Do not change MediaCrawler's license, use it for commercial monitoring without written permission, or automate controls that a platform presents to a user.

## Suggested Queries

Select two to four platform/query combinations from the keyword buckets in [sources-and-scoring.md](sources-and-scoring.md). Run a compact rotation rather than issuing every keyword on every platform. Add a practice-area query only when relevant, for example `知识产权 AI`, `劳动法 AI`, or `金融合规 AI`.

## Export Contract

Put one JSON object per line in a local inbox directory. The normalizer accepts common field names such as `title`, `note_title`, `content`, `desc`, `url`, `note_url`, `publish_time`, `create_time`, `liked_count`, `comment_count`, `share_count`, and `platform`.

Required whenever available:

```json
{
  "title": "法律大模型产品发布",
  "url": "https://example.com/post/1",
  "platform": "weibo",
  "collection_route": "keyword_discovery",
  "topic_bucket": "法律科技与产品",
  "published_at": "2026-07-30T09:00:00+08:00",
  "content": "公开帖正文或摘要",
  "liked_count": 120,
  "comment_count": 18
}
```

Keep `comments` out of the export unless they are necessary for a clearly stated aggregate trend question. Remove personal data before sending an export to another system.
