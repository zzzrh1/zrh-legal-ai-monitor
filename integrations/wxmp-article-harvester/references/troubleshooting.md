# wxmp-article-harvester troubleshooting

## 总原则

先恢复索引抓取，再补正文。`wcx` 正文失败不等于整批失败。

## 常见问题

### 微信公众号链接静态抓取失败

现象：

- 普通网页抓取返回验证页
- `WebFetch` 拿不到正文
- HTML 很短或没有正文节点

处理：

1. 保留 `mp.weixin.qq.com/s/...` 原始链接。
2. 先用 `scripts/browser_reader.py --url <url>` 走真实浏览器抽正文。
3. 浏览器正文仍然过短时标记为 `partial`，保留标题、日期和原文 URL。
4. 只有用户明确接受费用和第三方 URL 传输后，才用 `--allow-metaso` 兜底；没有授权时不调用。

### `wcx --content` 只抓到摘要

现象：

- 已经有 `articles/*.md`
- 正文很短
- 只有摘要、标题或引导文字

处理：

1. 默认不要跑 `wcx fetch --content`；`wcx` 只负责搜索、fetch 元数据、导出索引。
2. 从 `index.json/index.csv` 读取文章 URL。
3. 对正文不足 800 字的文章走 browser reader。
4. browser reader 失败后再逐篇走 Metaso Reader。
5. 只有明确想复用 `wcx` 正文缓存时才加 `--fetch-content`。

### 年份任务出现重叠目录或漏抓

现象：

- 用户要求“抓 2026 年全部文章”
- Agent 手工拆成“近 60 天”和“60-120 天”
- 输出出现中间目录
- index 里日期范围重叠

处理：

1. 优先使用 `--year YYYY`。
2. 指定输出目录时直接传最终目录，例如 `--output-dir "00_收件箱/Clippings"`。
3. 不要手工拆日期批次，除非目标公众号一年文章超过 80 篇。
4. 需要精确范围时使用 `--from-date YYYY-MM-DD --to-date YYYY-MM-DD`。

示例：

```bash
python3 scripts/harvest_wxmp.py --account "润宇创业笔记" --year 2026 --fulltext --output-dir "00_收件箱/Clippings"
```

### 短文章被误判失败

现象：

- 浏览器能读到标题、日期和正文
- 文章本身只有几百字
- 旧逻辑因为低于长度阈值判为失败

处理：

1. 新版 browser reader 会接受正常短文。
2. 如果页面包含微信验证、访问频繁、内容不可见等异常标记，仍会判失败。
3. 对短通知、节日感怀、图片配短文，不要只用字数判断失败。

### 浏览器批量抓全文速度慢

现象：

- 一批文章能抓成功，但整体耗时长
- 进程没有实时日志，看起来像卡住
- 每篇文章都重新打开浏览器

处理：

1. 使用 `scripts/harvest_wxmp.py --fulltext`，不要逐篇调用 `browser_reader.py`。
2. `harvest_wxmp.py` 会复用一个 Playwright 浏览器上下文，并默认拦截图片、字体、视频资源。
3. 如果需要完整图片加载，再加 `--browser-load-assets`，速度会变慢。
4. 如果怀疑 headless 被拦截，用 `--browser-headed` 观察页面。
5. 默认会逐篇输出进度，确认它是在抓正文，不是在卡死。

### 浏览器统一跳转验证码

现象：

- 页面跳到 `/mp/wappoc_appmsgcaptcha`
- 连续文章都出现安全验证或环境异常

处理：

1. 新版在第一次验证码后打开本轮熔断，不再连续请求剩余文章。
2. `--refresh-fulltext` 会保留已经通过质量门的旧 Markdown，并在报告写入 refresh warning。
3. 没有可信旧正文的文章保持 `partial`。等待微信风险控制恢复后，用 `--skip-fetch --fulltext` 重试。

### 登录态失效

现象：

- `未登录`
- `请先登录`
- `凭证无效`
- `token失效`
- `cookie失效`

处理：

1. 运行 `scripts/wcx_run.py`，不要直接运行 `wcx`。
2. runner 会调用 `scripts/refresh_token_playwright.py`。
3. 首次登录会打开浏览器，扫码后写入 `wcx login`。

### `HTTP Error 501`

这是旧版 `wcx` 的正文抓取问题，不是微信接口彻底不可用。

处理：

1. 继续通过 `scripts/wcx_run.py` 执行。
2. runner 会停止并打印显式升级命令，不会在运行中修改 Python 环境。
3. 用户确认后手动安装 README 中固定 commit 的 wcx 兼容版本，再重试。

### 微信频控

现象：

- 连续抓多个公众号后请求失败
- 某个号抓到一半停止，输出 `freq control` 或 `Rate limited (ret=200013)`
- 后续账号全部被拒
- `curl 28 timeout`
- `OperationalError: database is locked`

处理：

1. 单轮最多 80 篇元数据。更多文章使用 `--batch` 分批模式。
2. 触发风控后，脚本会自动检测并输出建议等待时间。
3. 已抓到的索引先落盘（wcx 缓存和 `.harvest-state.json` 都会保留）。
4. 等待 `.harvest-state.json` 的 `resume_after` 后用 `--resume` 续抓；过早恢复会返回 `cooldown`。状态同时校验远端总量、头部文章 ID 和上一批边界 ID；无法证明安全时返回 `cursor_drift`。
5. 不要并发跑多个 `wcx fetch/export`，SQLite cache 会锁。
6. 如果已经抓过索引，只是补正文或重导出，加 `--skip-fetch`，不要再次触发 `wcx fetch`。

分批续抓示例：

```bash
# 风控中断后，等待冷却，续抓
python3 scripts/harvest_wxmp.py --resume
```

### 年份任务过滤后零结果

现象：

- 用 `--year 2025` 抓取，输出 0 篇文章
- 日志显示缓存有几十篇，但过滤后都是别的年份

原因：

- 账号发文频率高，80 篇只覆盖最近 3 个月
- wcx 从最新往旧抓，还没抓到目标年份就被 limit 截断了

处理：

1. 使用 `--batch` 模式自动分批，脚本会持续往前挖直到覆盖目标年份。
2. 等待冷却后运行 `--resume`，不要手动把 `--limit` 提高到 80 以上。

```bash
python3 scripts/harvest_wxmp.py --account "老愚进化笔记" --year 2025 --fulltext --batch
```

### 输出目录有重复文件

现象：

- articles 目录下文件数 > 实际文章数
- 同标题出现两份，一份有完整正文、一份只有摘要

原因：旧版本按模糊标题去重，可能把同名文章混在一起；wcx 与浏览器的日期格式也可能不同。

处理：新版优先按 frontmatter 中的规范化原文 URL 去重，只有缺少来源 URL 时才使用精确标题键。旧目录先备份，再重新运行 `--skip-fetch --fulltext` 生成规范索引；不要直接批量删除。

### Metaso API key 缺失

现象：

- `METASO_API_KEY is not set`
- 单篇 fallback 全部失败

处理：

1. 索引仍然算成功。
2. 在 `harvest-report.md` 记录失败 URL。
3. 用户明确授权后配置 `METASO_API_KEY`，并加 `--allow-metaso` 重跑 `--fulltext`。

## 建议排查顺序

1. `python3 scripts/preflight.py --json`
2. `python3 scripts/wcx_run.py -- status`
3. `python3 scripts/wcx_run.py -- search "<公众号名>"`
4. `python3 scripts/harvest_wxmp.py --account "<公众号名>" --limit 10 --no-fulltext`
5. `python3 scripts/browser_reader.py --url "https://mp.weixin.qq.com/s/..." --output-dir /tmp`
6. `python3 scripts/harvest_wxmp.py --account "<公众号名>" --limit 10 --fulltext`
