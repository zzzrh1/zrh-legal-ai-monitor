# Sources and Scoring

## Source Tiers

| Tier | Use | Examples |
| --- | --- | --- |
| Primary | Confirm facts and determine priority | National People's Congress, State Council, CAC, Ministry of Justice, Supreme People's Court, Supreme People's Procuratorate, regulators, court/law-firm official notices |
| Professional | Track analysis and market movement | Bar associations, legal-tech vendors, research bodies, accredited legal media |
| Social | Discover signals and discussion | Authorized public results from Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Zhihu, Tieba; X, YouTube, Reddit where XCrawl supports the route |

Do not elevate a social post to a regulatory, judicial, product, or transaction fact without a primary source. Where an official source conflicts with a social post, retain the official source and explain the conflict.

## Topic Routing

Classify each candidate into one or more of these groups:

- `政策与监管`: legislation, judicial interpretation, enforcement, professional regulation.
- `AI 法律`: legal models, legal agents, legal-tech products, AI governance, data/algorithm/deep-synthesis rules, judicial AI.
- `行业与市场`: law firms, legal departments, talent, procurement, financing, partnerships.
- `实务与案例`: public cases, court practice, evidence, compliance implementation.

## Initial Score

The bundled script applies a transparent initial score:

- 0–30: recency, decaying after seven days.
- 0–30: combined hits from legal and AI-law keyword groups.
- 0–20: source signal; official sources receive the highest value.
- 0–15: visible social engagement on a logarithmic scale.
- 0–5: cross-source corroboration after normalization.

The score only prioritizes review. It does not represent credibility, legal significance, or truth. Override it with documented editorial judgment when necessary.

## Weekly Screening And Coverage

Use the initial score to sort a review queue, then apply this separate, explainable selection layer. It borrows the useful editorial structure of `legal-weekly-briefing` without adopting any credential-based collection route.

### Three Layers

| Layer | Inclusion rule | Treatment |
| --- | --- | --- |
| `精读` | Verified, high-scoring legal/AI-law item with clear practice value | Main digest item; explain the evidence and practical consequence. |
| `雷达` | Verified or credible professional legal item not selected for `精读` | Retain as a short signal; low rank does not mean it is discarded. |
| `归档` | Duplicate, off-topic, stale, unverifiable low-signal material | Keep only the local raw record and exclusion reason. |

Select no more than two `精读` candidates from one source per digest when other qualified sources exist. If a candidate set is too small, relax the cap and state that the source mix is limited.

### Legal-Practice Rubric

Score each field from `1` (strong) to `3` (weak); use `practice_area_match` only if the user has explicitly supplied a practice area or locality. The rubric ranks reading priority and never verifies a legal proposition by itself.

| Dimension | 1 | 2 | 3 |
| --- | --- | --- | --- |
| `case_density` | Specific case and holding/rule | Case mentioned without enough detail | No concrete case |
| `norm_anchoring` | Primary rule, judicial interpretation, or authoritative case | Specific legal provision cited | No clear normative anchor |
| `actionability` | Usable rule, review path, or handling approach | Analysis requiring extraction | Describes an issue only |
| `author_empirical_depth` | Primary authority or strong adjudicative/research evidence | Credible court/professional analysis | Generic commentary |
| `framework_quality` | Clear issue-rule-analysis framework | Some structure | Material pile-up or news-only |
| `relevance_halflife` | Durable method/rule | Medium-term reference | Fast-decaying event signal |
| `practice_area_match` | Explicitly matches the user's configured focus | — | No configured match |

Useful anchors: an authoritative case with a holding and an official judicial interpretation normally enters `精读`; a court article with sound analysis can enter `精读` or `雷达`; pure conference news normally remains a signal unless corroborated by a primary release.

### AI-Law Rubric

AI-law content is assessed separately so a product launch is not mistaken for a judicial rule.

| Dimension | 1 | 2 | 3 |
| --- | --- | --- | --- |
| `signal_strength` | Regulatory, judicial, flagship product, or material institutional deployment | Verifiable application or pilot | Financing, marketing, or weakly sourced claim |
| `detail_depth` | Concrete capability, scope, safeguards, or impact evidence | Some implementation detail | Headline-only |
| `practice_relevance` | Direct legal, compliance, law-firm, or judicial workflow implication | Indirectly useful | Generic AI news |
| `china_relevance` | China-specific or transferable with a stated reason | Context-dependent | No demonstrated relevance |

Use the labels `格局级`, `应用落地级`, and `市场信号级` for `signal_strength` values 1, 2, and 3. Financing or social buzz alone must not create a `必须关注` item.

### Candidate Completeness And Duplicates

- Every candidate must preserve a non-empty summary. Prefer an authorized source summary; otherwise write `摘要获取失败，请点击原文查看` rather than inventing content.
- Deduplicate by canonical URL first. Titles that only differ in punctuation, routine issuance words, or tracking parameters are review duplicates.
- Preserve the source link, collection route, verification state, rubric values, and the reason selected or placed in radar.

## Keywords

Rotate only one or two keyword buckets per platform scan. Keep the account watchlist scan separate from keyword discovery so a hot query does not crowd out durable sources.

| Bucket | Core queries | What it is for |
| --- | --- | --- |
| `法律科技与产品` | `法律大模型`, `法律智能体`, `AI律师`, `合同审查`, `法律检索`, `文书自动化`, `律所 AI`, `法务 AI` | Product releases, workflow adoption, user feedback |
| `AI 治理与数据` | `生成式 AI 合规`, `模型备案`, `算法治理`, `深度合成`, `数据合规`, `个人信息保护`, `数据出境`, `人工智能法` | Regulation, compliance changes, implementation questions |
| `司法与证据` | `司法人工智能`, `智慧法院`, `AI 证据`, `电子证据`, `类案检索`, `互联网法院`, `检察 AI` | Judicial practice and evidentiary developments |
| `行业与经营` | `律所 数字化`, `律师 效率`, `企业法务`, `法律服务`, `法律科技 融资`, `法律科技 招聘` | Firm, legal-department, talent, procurement and market signals |
| `跨境观察` | `legal AI`, `AI governance`, `AI Act`, `legaltech`, `contract review AI`, `AI regulation China` | Overseas product/regulatory shifts relevant to China or cross-border work |

Prefer exact Chinese phrases in Chinese platforms. Use the English bucket only in X, YouTube, Reddit, official English-language material, or an authorized search provider.
