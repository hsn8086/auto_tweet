# AGENTS.md — auto_tweet

供 AI 编码代理与维护者使用。项目说明与 API 语义见 `README.md`（必读）。

## 这是什么

X (Twitter) 浏览器自动化发推服务（FastAPI + Playwright），部署在
sg-server（192.168.14.3）容器 `auto_tweet-auto-twi-1`，上游是 qj-server 的
MusekeToolsBot（backend 即时发帖 + auto_send 定时 worker，**另一个仓库**）。

## 常用命令

```bash
uv sync
uv run python -m unittest discover tests     # 测试（unittest 风格，pytest 也可）
uv run ruff check src tests                  # lint
docker compose up -d                         # 部署
# Docker Hub 不可达时的热更（sg 上常态）：
docker cp src/. auto_tweet-auto-twi-1:/app/src/ && docker restart auto_tweet-auto-twi-1
docker commit auto_tweet-auto-twi-1 auto_tweet-auto-twi:latest   # 固化镜像
```

测试注意：本机 `.env` 里有 `proxy=`，断言不要写死 `proxy=None`（用 `ANY`）。

代理按部署主机固定，构建参数与容器 `.env` 必须一致：sg 使用
`http://192.168.14.3:7893`，qj 使用 `http://192.168.13.149:20172`。禁止跨主机
借用代理；实测会造成 Playwright `ERR_CONNECTION_CLOSED` 和时间线空加载。

## 重启前的队列闸门（必须执行）

执行 `docker compose up`、`docker restart`、热更或替换容器前，先协调 qj 停止
产生新的发帖/回复请求，再检查本机发送队列：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/tweet/queue
```

只有返回 `{"waiting":0,"active":0}` 才能重启。`waiting` 或 `active` 任一非零时
必须等待请求结束，并让上游通过 `GET /tweet/result/{request_id}` 完成对账；禁止
强停浏览器或容器。还要在 qj 确认 `auto_send_history.status='leased'` 以及
`x_reply_send_attempt.status in ('pending','sending')` 的数量均为 0，避免检查后的
竞态。qj 的 `bot-auto-send-1` 有 5 分钟 watchdog，单独 `docker stop` 会被拉起；
且 auto_send 每次进程启动会立即随机发送一次，禁止在维护中反复 stop/start。

## 不可破坏的不变量

1. **绝不重复发帖**：
   - 点击发送之后（`posted`/`create_tweet_dispatched` 为真）发生的任何异常都
     必须包成 `PostSentError`（HTTP 200 + warning），不能进 tenacity 重试，
     不能返回 5xx（上游会当作"没发出去"）。
   - 同一 `request_id` 已有 success/sent_unconfirmed 记录时必须回放，不能再发
     （`src/router/tweet.py` 的幂等检查）。
2. **结果必须落盘**：带 request_id 的请求，无论成败都要写 result_store
   （running → 终态）。这是上游对账（防"响应丢失"漏记）的唯一依据。
3. **Semaphore(1) 串行**：sg 机器扛不住并发浏览器；改并发度前先确认内存与
   僵尸进程回收（compose `init: true` 必须保留）。
4. **登录页弹跳 ≠ 风控**：`is_login_bounce_url()` 的快速重进逻辑是实测结论
   （同 cookie 成功夹在失败中间）；不要改成"遇登录页就冷却/报废 cookie"。
5. API 协议变更（参数/响应字段）必须同步上游仓库 MusekeToolsBot 的
   `backend/src/x_reconcile.py`、`backend/src/router/img.py`、
   `auto_send/src/main.py` 及其 AGENTS.md。
6. **时间线采集 fail-closed**：`user_media` 的 `complete` 是上游推进游标的唯一
   依据。响应被拒（非 200 / GraphQL `errors`）、结果被 `max_tweets` 截断、只靠
   空转退出，一律 `complete=false`；判完成度前先结算在途响应任务。作者身份只能
   来自实际抓到的 profile `rest_id`；leaf 自带别的作者 id 时不得用 profile 身份
   顶替（详见 README「时间线采集的坑」）。

## 安全红线

- `state*.json` / `custom-state*.json` / `.env` / `data/` / `ss/` 永不提交
  （.gitignore 已覆盖）。cookie（auth_token/ct0）绝不写进代码、测试、文档。
  曾发生 pyproject.toml 里粘进整段 cookie 的事故——提交前 `git diff --staged`
  逐行看。
- 调试截图（ss/）可能包含账号信息，不要外发。

## 代码地图

- `src/router/tweet.py` — 路由：post（含幂等/对账记录）、result、queue、
  reply（幂等回复）、verified_replies（认证回复收件箱）、metrics（单条）、
  user_metrics（批量时间线，上游日报/加权数据源）
- `src/sender.py` — Playwright 发送主流程：登录弹跳处理(:47)、composer 打开
  (:open_post_composer)、媒体上传+spoiler、AI 声明、CreateTweet 响应捕获、
  queue_stats；回复 composer；fetch_verified_replies；fetch_user_tweets_metrics（开 profile 页滚动收 UserTweets
  响应——X 首屏 SSR 首个 XHR 在滚动后才出现、分页必须 scrollTo 文档底部，
  这两个坑别"优化"回去）；timeline operation 白名单在
  `USER_TIMELINE_OPERATIONS` / `is_replies_timeline_response`，profile 版
  operation 不是 `UserTweets` 的子串，别退回子串匹配
- `src/result_store.py` — request_id → 结果 的文件存储（原子写、TTL 清理）
- `src/replies.py` — state twid 校验、GraphQL tweet/user/media 归一化、认证直接
  回复与父推文 48h 过滤
- `src/model.py` — State/CookieItem/PostSentError
- `login.py` — 手工登录生成 state JSON（运维用）
