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

## 安全红线

- `state*.json` / `custom-state*.json` / `.env` / `data/` / `ss/` 永不提交
  （.gitignore 已覆盖）。cookie（auth_token/ct0）绝不写进代码、测试、文档。
  曾发生 pyproject.toml 里粘进整段 cookie 的事故——提交前 `git diff --staged`
  逐行看。
- 调试截图（ss/）可能包含账号信息，不要外发。

## 代码地图

- `src/router/tweet.py` — 路由：post（含幂等/对账记录）、result、queue、
  metrics（单条）、user_metrics（批量时间线，上游日报/加权数据源）
- `src/sender.py` — Playwright 发送主流程：登录弹跳处理(:47)、composer 打开
  (:open_post_composer)、媒体上传+spoiler、AI 声明、CreateTweet 响应捕获、
  queue_stats；fetch_user_tweets_metrics（开 profile 页滚动收 UserTweets
  响应——X 首屏 SSR 首个 XHR 在滚动后才出现、分页必须 scrollTo 文档底部，
  这两个坑别"优化"回去）
- `src/result_store.py` — request_id → 结果 的文件存储（原子写、TTL 清理）
- `src/model.py` — State/CookieItem/PostSentError
- `login.py` — 手工登录生成 state JSON（运维用）
