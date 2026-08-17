# auto_tweet

无 API 配额的 X (Twitter) 发推服务：FastAPI + Playwright(Chromium) 模拟真实
浏览器会话发帖，供上游（MusekeToolsBot backend / auto_send worker）通过 HTTP
调用。支持多图、敏感内容标记（spoiler）、AI 内容声明、tweet_id 捕获，以及
**request_id 幂等 + 结果对账**（解决长请求响应丢失导致的"已发出但上游不知道"）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tweet/post` | 发推。form/query: `state`(必填, Playwright storage_state JSON)、`context`、`spoiler`、`made_with_ai`、`request_id`(可选, `[A-Za-z0-9_.:-]{1,128}`)；files: `images`（最多 4 张） |
| POST | `/api/v1/tweet/reply` | 回复。form: `state`、`expected_user_id`、`in_reply_to_tweet_id`、`request_id`（均必填），`context`（可空）；files: `images`（最多 4 张，允许纯图片） |
| POST | `/api/v1/tweet/verified_replies` | 拉取认证账号的直接回复。form: `state`、`screen_name`、`expected_user_id`，可选 `since_id`、`since_time`、`parent_window_hours=48`、`max_scrolls=0`（0 表示不限滚动，1-60 表示有限上限） |
| POST | `/api/v1/tweet/user_media` | 拉取目标账号原创图片推文。form: `state`、`screen_name`、`expected_user_id`、`expected_target_user_id`，可选 `since_id`、`since_time`、`max_scrolls=8`、`max_tweets=32`（硬上限 32） |
| GET | `/api/v1/tweet/result/{request_id}` | 结果对账：返回 `{status, tweet_id?, warning?, error?, updated_at}`，status ∈ `running/success/sent_unconfirmed/failed`；未知 404 |
| GET | `/api/v1/tweet/queue` | 当前排队/执行中的发送数 |
| POST | `/api/v1/tweet/metrics` | 拉取推文数据（浏览/点赞/转发等）。form: `state`、`tweet_id` |

所有端点支持可选 `X-Auto-Tweet-Key` 头（配置 `auto_tweet_api_key` 后强制）。

### 发送语义（上游必读）

- 同一 `request_id` 重复 POST：已 success/sent_unconfirmed → 直接回放结果
  （响应带 `"replayed": true`），**不会二次发帖**；running → 409；failed →
  允许重试。
- `/tweet/reply` 与 `/tweet/post` 共用以上 result_store 与发送错误处理；回复强制
  携带 `request_id`。两条需要指定账号的接口都会在启动浏览器前解析 state 的
  `twid` 并核对 `expected_user_id`，账号不匹配返回 409。
- 200 + `warning`（含 `tweet_id` 缺失的情形）= 推文已点击发送但后续确认不完整
  （`sent_unconfirmed`），上游不应自动重发。
- 502 = 可重试错误（没发出去）；500 = 未知错误（默认按已发出对待，先对账）。
- 全局 `Semaphore(1)`：请求会排队，弱网慢机下单条可达 3-25 分钟。上游请用
  TCP keepalive + 超时后走 `/tweet/result/` 对账，而不是干等。

## 运行

```bash
uv sync                                  # Python 3.11
playwright 由系统 chromium 提供（/usr/bin/chromium，见 Dockerfile）
uv run python main.py                    # 或 docker compose up -d
```

`.env`：`proxy=http://...`（可选）、`auto_tweet_api_key`（可选）、
`data_dir`（默认 `data`，存放 `tweet_results/` 对账记录，TTL 7 天）。

获取 state（cookie）：`uv run python login.py` 手工登录后生成
`state<时间戳>.json`，把 JSON 字符串放进上游配置。**state 含 auth_token，
绝不提交进仓库。**

## 测试

```bash
uv run python -m unittest discover tests          # 或 pytest
# 容器内：
docker exec auto_tweet-auto-twi-1 sh -c 'cd /app && uv run python -m unittest discover tests'
```

## 实现要点 / 已知行为

- **登录页弹跳**：X 偶发把有效会话弹到 `mode=login`/`/i/jf/`/裸域首页，不是
  风控。`is_login_bounce_url()` 秒级识别 → home 重进 ≤3 次 → compose URL
  fallback。
- **tweet_id 捕获**：监听 CreateTweet GraphQL 响应取 `rest_id`；点击发送后任何
  异常 → `PostSentError`（200+warning），避免上游误重发。
- **僵尸进程**：compose `init: true` 必须保留（Chromium 子进程回收）。
- 调试截图写到 `ss/`（挂载卷）。
- 弱网重试：发送前的可重试异常由 tenacity 处理（5 次指数退避）；点击发送后
  绝不重试。
- `verified_replies` 同时采集 `/{screen_name}/with_replies` 与
  `/notifications/verified` 的 GraphQL timeline；只返回认证作者对目标账号的直接
  回复，且父推文在指定时间窗内。收集总超时为 45 分钟；响应为
  `{status, screen_name, viewer_user_id, newest_id, observed_newest_id, complete, replies}`。
  `newest_id`/`observed_newest_id` 是本轮看到的顶部通知 tweet id；上游只有在
  `complete=true` 时才能推进持久化游标，有限滚动耗尽时返回 `complete=false`。
- `user_media` 返回
  `{status, screen_name, viewer_user_id, target_user_id, newest_id, complete, tweets}`。
  `target_user_id` 来自实际 UserTweets profile 响应，并必须等于
  `expected_target_user_id`；缺失或不一致均失败，profile 身份核验前不会给缺少作者
  的 tweet leaf 注入配置中的账号。只有扫描到 `since_id`/`since_time` 边界，或
  GraphQL 明确返回时间线底部终止，且所有新图片推文都装得进 `max_tweets` 时，
  `complete` 才为 `true`。达到滚动/响应上限、仅连续空转、或新图片超过 32 条时
  均返回 `complete=false`，上游不得推进游标。当前协议没有分页 token；超过上限
  的积压会持续 fail-closed，需人工处理后再恢复增量扫描。
- **时间线采集的坑**（改 `sender.py` 前必读）：
  - profile timeline 的 GraphQL operation 名会在
    `UserTweets`/`UserWithProfileTweetsQueryV2`/
    `UserWithProfileTweetsAndRepliesQueryV2`/`UserOriginalsTimeline` 之间切换，
    后三个不是 `UserTweets` 的子串——按子串匹配会在 X 改版后一条响应都收不到，
    表现为"账号没发新图"。
  - 非 200 或带 `errors` 的响应一律不计入采集：这类响应意味着本轮可能漏页，
    必须 `complete=false`；全部响应都被拒时直接报错，不返回空结果。
  - 判定完成度前必须先结算在途响应任务，否则刚回来的那一页会被漏掉。
  - X 的现代 user 对象没有 `legacy`：用户名/昵称在 `core`，粉丝数在
    `relationship_counts.followers`；缺 `legacy` 不代表缺身份，不能据此改用
    配置里的账号。
