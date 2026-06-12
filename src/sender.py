import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    FilePayload,
    Locator,
    ProxySettings,
    Response,
    StorageState,
    async_playwright,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .model import PostSentError, State

sem = asyncio.Semaphore(1)
# 仅用于可观测性（/tweet/queue）：当前排队/执行中的发送数。
queue_stats = {"waiting": 0, "active": 0}
RETRYABLE_SEND_EXCEPTIONS = (TimeoutError, ConnectionError, OSError, PlaywrightError)
SCREENSHOT_TIMEOUT_SECONDS = 10
BROWSER_CLOSE_TIMEOUT_SECONDS = 15
CREATE_TWEET_RESPONSE_TIMEOUT_MS = 120_000
X_HOME_URL = "https://x.com/home"
X_COMPOSE_URLS = (
    "https://x.com/compose/post",
    "https://x.com/compose/tweet",
    "https://twitter.com/compose/tweet",
)
# X 前端偶发把已登录会话弹到登录/onboarding 页；这种页面 URL 特征明显，
# 检测到就快速重进，而不是傻等 composer 出现。
X_LOGIN_BOUNCE_MARKERS = (
    "mode=login",
    "/i/jf/",
    "/i/flow/login",
    "redirect_after_login",
)
HOME_LOGIN_BOUNCE_RETRIES = 3


def is_login_bounce_url(url: str) -> bool:
    u = (url or "").split("#", 1)[0]
    if any(marker in u for marker in X_LOGIN_BOUNCE_MARKERS):
        return True
    bare = u.split("?", 1)[0].rstrip("/")
    # 登出状态下 /home 会被重定向到裸域首页
    return bare in ("https://x.com", "https://twitter.com")

if TYPE_CHECKING:
    from playwright.async_api import Page


def describe_send_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return exc.__class__.__name__


def is_create_tweet_request_url(url: str) -> bool:
    return "CreateTweet" in url or "CreateNoteTweet" in url


def is_create_tweet_response(response: "Response") -> bool:
    if not is_create_tweet_request_url(response.url):
        return False
    try:
        return response.request.method == "POST"
    except Exception:
        return False


def extract_tweet_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("create_tweet", "create_note_tweet"):
        node = data.get(key)
        if not isinstance(node, dict):
            continue
        for results_key in ("tweet_results", "note_tweet_results"):
            results = node.get(results_key)
            if not isinstance(results, dict):
                continue
            result = results.get("result")
            if isinstance(result, dict):
                rest_id = result.get("rest_id")
                if isinstance(rest_id, (str, int)):
                    rest_id_str = str(rest_id).strip()
                    if rest_id_str:
                        return rest_id_str
            tweet = results.get("tweet")
            if isinstance(tweet, dict):
                rest_id = tweet.get("rest_id")
                if isinstance(rest_id, (str, int)):
                    rest_id_str = str(rest_id).strip()
                    if rest_id_str:
                        return rest_id_str
    return None


async def extract_tweet_id_from_response(response: "Response") -> str | None:
    try:
        body = await response.json()
    except Exception:
        return None
    return extract_tweet_id_from_payload(body)


def is_tweet_metrics_response(response: "Response") -> bool:
    url = response.url
    if (
        "TweetResultByRestId" not in url
        and "TweetDetail" not in url
        and "TweetResultsByRestIds" not in url
    ):
        return False
    try:
        return response.request.method == "GET"
    except Exception:
        return False


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except ValueError:
                return 0
    return 0


def _unwrap_tweet_node(node: Any) -> dict | None:
    if not isinstance(node, dict):
        return None
    if node.get("__typename") == "TweetWithVisibilityResults":
        wrapped = node.get("tweet")
        if isinstance(wrapped, dict):
            return wrapped
    return node


def parse_tweet_metrics_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    candidates: list[Any] = []
    data = payload.get("data")
    if isinstance(data, dict):
        result = data.get("tweetResult")
        if isinstance(result, dict):
            candidates.append(result.get("result"))
        thread = data.get("threaded_conversation_with_injections_v2")
        if isinstance(thread, dict):
            instructions = thread.get("instructions")
            if isinstance(instructions, list):
                for instruction in instructions:
                    if not isinstance(instruction, dict):
                        continue
                    entries = instruction.get("entries")
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        content = entry.get("content")
                        if not isinstance(content, dict):
                            continue
                        item_content = content.get("itemContent")
                        if not isinstance(item_content, dict):
                            continue
                        tweet_results = item_content.get("tweet_results")
                        if isinstance(tweet_results, dict):
                            candidates.append(tweet_results.get("result"))
    for candidate in candidates:
        metrics = _metrics_from_tweet_node(candidate)
        if metrics is not None:
            return metrics
    return {}


def _metrics_from_tweet_node(candidate: Any) -> dict[str, Any] | None:
    """从 GraphQL tweet result 节点提取指标；非 tweet 节点返回 None。"""
    node = _unwrap_tweet_node(candidate)
    if node is None:
        return None
    legacy = node.get("legacy")
    views = node.get("views") if isinstance(node.get("views"), dict) else {}
    if not isinstance(legacy, dict):
        return None
    # 转推：指标属于原推且作者不是本账号，统计无意义，跳过。
    if "retweeted_status_result" in legacy:
        return None
    rest_id = node.get("rest_id")
    if isinstance(rest_id, (str, int)):
        rest_id_str = str(rest_id).strip()
    else:
        rest_id_str = ""
    if not rest_id_str:
        return None
    return {
        "tweet_id": rest_id_str,
        "likes": _coerce_int(legacy.get("favorite_count")),
        "retweets": _coerce_int(legacy.get("retweet_count")),
        "replies": _coerce_int(legacy.get("reply_count")),
        "quotes": _coerce_int(legacy.get("quote_count")),
        "bookmarks": _coerce_int(legacy.get("bookmark_count")),
        "views": _coerce_int(views.get("count") if isinstance(views, dict) else None),
        "created_at": str(legacy.get("created_at") or ""),
    }


def is_user_tweets_response(response: "Response") -> bool:
    # 兼容 UserTweets / UserTweetsAndReplies（X 改版时 operation 名可能切换）
    if "UserTweets" not in response.url:
        return False
    try:
        return response.request.method == "GET"
    except Exception:
        return False


def parse_tweet_created_at(raw: str) -> "datetime | None":
    """解析 X legacy.created_at（如 'Wed Oct 10 20:19:24 +0000 2018'）。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def parse_user_tweets_payload(payload: Any) -> list[dict[str, Any]]:
    """解析 UserTweets 时间线响应，返回推文指标列表。

    - TimelinePinEntry（置顶）单独标 pinned=True，调用方判断时间窗时应忽略；
    - cursor / module 等非 tweet entry 跳过；转推在 _metrics_from_tweet_node 剔除。
    """
    if not isinstance(payload, dict):
        return []
    instructions: list[Any] = []
    user = payload.get("data")
    if isinstance(user, dict):
        user = user.get("user")
    if isinstance(user, dict):
        user = user.get("result")
    if isinstance(user, dict):
        timeline = user.get("timeline") or user.get("timeline_v2")
        if isinstance(timeline, dict):
            inner = timeline.get("timeline")
            if isinstance(inner, dict):
                found = inner.get("instructions")
                if isinstance(found, list):
                    instructions = found
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(candidate: Any, *, pinned: bool) -> None:
        metrics = _metrics_from_tweet_node(candidate)
        if metrics is None or metrics["tweet_id"] in seen:
            return
        seen.add(metrics["tweet_id"])
        metrics["pinned"] = pinned
        results.append(metrics)

    def _collect_entry(entry: Any, *, pinned: bool) -> None:
        if not isinstance(entry, dict):
            return
        content = entry.get("content")
        if not isinstance(content, dict):
            return
        item_content = content.get("itemContent")
        if not isinstance(item_content, dict):
            return
        tweet_results = item_content.get("tweet_results")
        if isinstance(tweet_results, dict):
            _collect(tweet_results.get("result"), pinned=pinned)

    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        kind = instruction.get("type")
        if kind == "TimelinePinEntry":
            _collect_entry(instruction.get("entry"), pinned=True)
            continue
        entries = instruction.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            _collect_entry(entry, pinned=False)
    return results


USER_TWEETS_HYDRATION_WAIT_MS = 6_000
USER_TWEETS_SCROLL_INTERVAL_MS = 7_000
# X 分页由"滚到底部 sentinel"触发且 GraphQL 响应可能 ~10s 才回，容忍多轮空转
USER_TWEETS_IDLE_ROUNDS = 4


async def fetch_user_tweets_metrics(
    screen_name: str,
    state: State,
    *,
    until_hours: int = 96,
    max_scrolls: int = 30,
    proxy: str | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """打开用户主页时间线，滚动翻页批量收集近 until_hours 小时推文的指标。

    一次浏览器会话拿全量（对比逐条开推文页，请求量小几个量级）。
    X 首屏是 SSR/hydration，首个 UserTweets XHR 通常在首次滚动之后才出现，
    所以采用"固定节拍持续滚动 + 并行收响应"而不是"等响应再滚"。
    终止条件：非置顶推文已老于时间窗 / 滚动达上限 / 连续多轮无新增。
    """
    name = (screen_name or "").strip().lstrip("@")
    if not name:
        raise ValueError("screen_name is required")
    url = f"https://x.com/{name}"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=until_hours)
    collected: dict[str, dict[str, Any]] = {}
    reached_cutoff = False
    async with sem:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                channel="msedge",
                proxy=ProxySettings(server=proxy) if proxy else None,
                headless=headless,
                executable_path="/usr/bin/chromium",
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--enable-unsafe-swiftshader",
                ],
            )
            try:
                context = await browser.new_context(
                    storage_state=StorageState(**state.model_dump()), locale="zh-CN"
                )
                page = await context.new_page()
                page.on("console", log_console_message)
                payloads: asyncio.Queue = asyncio.Queue()

                async def _on_response(response: "Response") -> None:
                    if not is_user_tweets_response(response):
                        return
                    try:
                        payloads.put_nowait(await response.json())
                    except Exception as exc:
                        logger.warning(
                            "Failed to read UserTweets response: {}",
                            describe_send_exception(exc),
                        )

                page.on("response", _on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(USER_TWEETS_HYDRATION_WAIT_MS)
                idle_rounds = 0
                for _ in range(max_scrolls):
                    got_new = False
                    while True:
                        try:
                            payload = payloads.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        for item in parse_user_tweets_payload(payload):
                            if item["tweet_id"] in collected:
                                continue
                            collected[item["tweet_id"]] = item
                            got_new = True
                            if not item.get("pinned"):
                                ts = parse_tweet_created_at(
                                    item.get("created_at", "")
                                )
                                if ts is not None and ts < cutoff:
                                    reached_cutoff = True
                    if reached_cutoff:
                        break
                    idle_rounds = 0 if got_new else idle_rounds + 1
                    if idle_rounds >= USER_TWEETS_IDLE_ROUNDS and collected:
                        break
                    # 滚到文档底部才能让虚拟列表的分页 sentinel 进入视口；
                    # 固定增量 wheel 会被 DOM 回收的高度变化耗散，触发不了下一页。
                    await page.evaluate(
                        "window.scrollTo(0, document.documentElement.scrollHeight)"
                    )
                    await page.wait_for_timeout(USER_TWEETS_SCROLL_INTERVAL_MS)
                logger.info(
                    "UserTweets collect for @{}: {} tweets (cutoff_reached={})",
                    name,
                    len(collected),
                    reached_cutoff,
                )
            finally:
                try:
                    await asyncio.wait_for(
                        browser.close(), timeout=BROWSER_CLOSE_TIMEOUT_SECONDS
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to close browser cleanly: {}",
                        describe_send_exception(exc),
                    )

    def _sort_key(item: dict[str, Any]):
        ts = parse_tweet_created_at(item.get("created_at", ""))
        return ts or datetime.fromtimestamp(0, tz=timezone.utc)

    return sorted(collected.values(), key=_sort_key, reverse=True)


async def fetch_tweet_metrics(
    tweet_id: str,
    state: State,
    *,
    proxy: str | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    rest_id = (tweet_id or "").strip()
    if not rest_id:
        raise ValueError("tweet_id is required")
    url = f"https://x.com/i/web/status/{rest_id}"
    async with sem:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                channel="msedge",
                proxy=ProxySettings(server=proxy) if proxy else None,
                headless=headless,
                executable_path="/usr/bin/chromium",
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--enable-unsafe-swiftshader",
                ],
            )
            try:
                context = await browser.new_context(
                    storage_state=StorageState(**state.model_dump()), locale="zh-CN"
                )
                page = await context.new_page()
                page.on("console", log_console_message)
                async with page.expect_response(
                    is_tweet_metrics_response,
                    timeout=60_000,
                ) as resp_info:
                    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                response = await resp_info.value
                try:
                    payload = await response.json()
                except Exception:
                    payload = {}
                metrics = parse_tweet_metrics_payload(payload)
                if not metrics:
                    raise RuntimeError("Tweet metrics payload missing data")
                return metrics
            finally:
                try:
                    await asyncio.wait_for(
                        browser.close(), timeout=BROWSER_CLOSE_TIMEOUT_SECONDS
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to close browser cleanly: {}",
                        describe_send_exception(exc),
                    )


async def wait_e(e: Locator, *, timeout: int = 10, description: str = "element"):
    for _ in range(timeout * 10):
        try:
            if await e.is_enabled():
                return
        except PlaywrightError:
            pass
        await asyncio.sleep(0.1)

    raise TimeoutError(
        f"Timed out waiting for {description} to become enabled after {timeout}s"
    )


async def click_e(e: Locator, *, timeout: int = 10, description: str = "element"):
    await wait_e(e, timeout=timeout, description=description)
    await e.click()


async def wait_first_available(
    locators: list[Locator], *, timeout: int = 10, description: str = "element"
) -> Locator:
    for _ in range(timeout * 10):
        for locator in locators:
            candidate = locator.first
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except PlaywrightError:
                continue
        await asyncio.sleep(0.1)
    raise TimeoutError(
        f"Timed out waiting for {description} to become available after {timeout}s"
    )


async def click_first_available(
    locators: list[Locator], *, timeout: int = 10, description: str = "element"
) -> Locator:
    candidate = await wait_first_available(
        locators, timeout=timeout, description=description
    )
    await candidate.click()
    return candidate


async def click_if_available(
    locators: list[Locator], *, timeout: int = 10, description: str = "element"
) -> bool:
    try:
        await click_first_available(locators, timeout=timeout, description=description)
    except TimeoutError:
        return False
    return True


async def take_debug_screenshot(page: "Page", path: str) -> None:
    try:
        await asyncio.wait_for(
            page.screenshot(path=path), timeout=SCREENSHOT_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.warning(
            "Failed to take debug screenshot {}: {}", path, describe_send_exception(e)
        )


def log_console_message(msg) -> None:
    level = {
        "error": "ERROR",
        "warning": "WARNING",
        "info": "INFO",
        "log": "INFO",
        "debug": "DEBUG",
        "trace": "DEBUG",
        "verbose": "DEBUG",
    }.get(str(msg.type).lower(), "DEBUG")
    logger.log(level, msg.text)


def composer_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_label("帖子文本"),
        page.get_by_label("Post text"),
        page.get_by_test_id("tweetTextarea_0"),
        page.locator("[data-testid='tweetTextarea_0']"),
        page.locator("[data-testid='tweetTextarea_0'] [contenteditable='true']"),
        page.locator("[role='textbox'][contenteditable='true']"),
    ]


async def log_composer_diagnostics(page: "Page", *, stage: str) -> None:
    try:
        title = await page.title()
    except Exception:
        title = "<unavailable>"
    try:
        tweet_textareas = await page.locator("[data-testid='tweetTextarea_0']").count()
    except Exception:
        tweet_textareas = -1
    try:
        contenteditable_textboxes = await page.locator(
            "[role='textbox'][contenteditable='true']"
        ).count()
    except Exception:
        contenteditable_textboxes = -1
    logger.warning(
        "Composer unavailable at {}; url={}, title={}, tweetTextarea={}, contenteditableTextbox={}",
        stage,
        getattr(page, "url", "<unavailable>"),
        title,
        tweet_textareas,
        contenteditable_textboxes,
    )


def post_button_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_test_id("tweetButtonInline"),
        page.get_by_test_id("tweetButton"),
        page.get_by_role("button", name="发帖"),
        page.get_by_role("button", name="Post"),
        page.get_by_label("主页时间线").get_by_text("发帖"),
    ]


def media_back_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_label("返回"),
        page.get_by_role("button", name="返回"),
        page.get_by_label("关闭"),
        page.get_by_role("button", name="关闭"),
    ]


def content_disclosure_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_label("内容披露"),
        page.get_by_label("Content disclosure"),
        page.get_by_role("button", name="内容披露"),
        page.get_by_role("button", name="Content disclosure"),
        page.get_by_text("内容披露"),
        page.get_by_text("Content disclosure"),
    ]


def ai_generated_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_label("AI 生成"),
        page.get_by_label("AI生成"),
        page.get_by_label("Made with AI"),
        page.get_by_label("AI-generated"),
        page.get_by_label("AI generated"),
        page.get_by_text("AI 生成"),
        page.get_by_text("AI生成"),
        page.get_by_text("Made with AI"),
        page.get_by_text("AI-generated"),
        page.get_by_text("AI generated"),
    ]


async def enable_ai_content_disclosure(page: "Page") -> None:
    disclosure = await click_first_available(
        content_disclosure_candidates(page),
        timeout=15,
        description="content disclosure button",
    )
    logger.info("Opened content disclosure control: {}", disclosure)
    await click_first_available(
        ai_generated_candidates(page),
        timeout=15,
        description="AI generated disclosure option",
    )
    await click_if_available(
        media_back_candidates(page),
        timeout=5,
        description="content disclosure back button",
    )
    if "/content_disclosure" in page.url:
        await page.go_back(wait_until="domcontentloaded", timeout=30_000)
    await wait_first_available(
        post_button_candidates(page),
        timeout=30,
        description="post button after AI content disclosure",
    )


async def wait_composer_or_login(page: "Page", *, timeout: int) -> Locator | None:
    """等 composer 出现；若期间发现被弹到登录页则立刻返回 None（快速失败）。"""
    for _ in range(timeout * 10):
        if is_login_bounce_url(page.url):
            return None
        for locator in composer_candidates(page):
            candidate = locator.first
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except PlaywrightError:
                continue
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for post composer after {timeout}s")


async def open_post_composer(page: "Page") -> Locator:
    last_error: BaseException | None = None
    # 第一阶段：home。被弹登录页时不傻等，快速重进几次。
    for attempt in range(1, HOME_LOGIN_BOUNCE_RETRIES + 1):
        try:
            await page.goto(X_HOME_URL, wait_until="domcontentloaded", timeout=90_000)
            logger.info("Page DOM loaded.")
            await take_debug_screenshot(page, "ss/1.png")
            composer = await wait_composer_or_login(page, timeout=45)
            if composer is not None:
                await click_e(composer, timeout=30, description="post composer")
                return composer
            logger.warning(
                "X bounced to login page at {} (attempt {}/{}); retrying home",
                page.url,
                attempt,
                HOME_LOGIN_BOUNCE_RETRIES,
            )
            await log_composer_diagnostics(page, stage="home")
            await asyncio.sleep(2.0 * attempt)
        except (TimeoutError, PlaywrightError) as exc:
            last_error = exc
            logger.warning(
                "Could not open post composer from home: {}; opening compose page.",
                describe_send_exception(exc),
            )
            await log_composer_diagnostics(page, stage="home")
            break
    # 第二阶段：compose 直达 URL fallback。同样对登录页快速失败。
    for compose_url in X_COMPOSE_URLS:
        try:
            await page.goto(compose_url, wait_until="domcontentloaded", timeout=90_000)
        except PlaywrightError as compose_exc:
            last_error = compose_exc
            logger.warning(
                "Compose page navigation failed before DOM loaded for {}: {}",
                compose_url,
                describe_send_exception(compose_exc),
            )
            continue
        await take_debug_screenshot(page, "ss/compose.png")
        try:
            composer = await wait_composer_or_login(page, timeout=30)
        except (TimeoutError, PlaywrightError) as compose_exc:
            last_error = compose_exc
            await log_composer_diagnostics(page, stage=compose_url)
            continue
        if composer is None:
            logger.warning(
                "X bounced to login page at {} for {}; trying next compose url",
                page.url,
                compose_url,
            )
            await log_composer_diagnostics(page, stage=compose_url)
            continue
        await click_e(composer, timeout=30, description="post composer")
        return composer
    raise TimeoutError(
        "Timed out waiting for post composer on compose fallbacks"
    ) from last_error


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(RETRYABLE_SEND_EXCEPTIONS),
    reraise=True,
)
async def send(
    txt: str,
    state: State,
    *,
    media: list[FilePayload] | None = None,
    proxy: str | None = None,
    headless=True,
    spoiler=False,
    made_with_ai=False,
) -> str | None:
    if not media:
        media = []
    if isinstance(spoiler, str):
        spoiler = spoiler in ("True", "true")
    if isinstance(made_with_ai, str):
        made_with_ai = made_with_ai in ("True", "true")

    posted = False
    tweet_id: str | None = None
    create_tweet_dispatched = False

    queue_stats["waiting"] += 1
    try:
        await sem.acquire()
    finally:
        queue_stats["waiting"] -= 1
    queue_stats["active"] += 1
    try:
        async with async_playwright() as p:
            logger.info("Launching browser...")
            browser = await p.chromium.launch(
                channel="msedge",
                proxy=ProxySettings(server=proxy) if proxy else None,
                headless=headless,
                executable_path="/usr/bin/chromium",
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--enable-unsafe-swiftshader",
                ],
            )
            try:
                context = await browser.new_context(
                    storage_state=StorageState(**state.model_dump()), locale="zh-CN"
                )
                page = await context.new_page()
                page.on("console", log_console_message)

                def _on_request(req):
                    nonlocal create_tweet_dispatched
                    try:
                        if (
                            is_create_tweet_request_url(req.url)
                            and req.method == "POST"
                        ):
                            create_tweet_dispatched = True
                            logger.info("CreateTweet request dispatched: {}", req.url)
                    except Exception:
                        pass

                page.on("request", _on_request)
                composer = await open_post_composer(page)
                post_buttons = post_button_candidates(page)

                first = True
                for medium in media:
                    async with page.expect_file_chooser() as fc_info:
                        if first:
                            await click_e(
                                page.get_by_label("添加照片或视频"),
                                description="first media button",
                            )
                        else:
                            await click_e(
                                page.get_by_label("添加媒体"),
                                description="additional media button",
                            )

                    file_chooser = await fc_info.value
                    await file_chooser.set_files(medium)
                    logger.info(
                        "Image uploaded: {} ({})", medium["name"], medium["mimeType"]
                    )

                    if first and spoiler:
                        await click_e(
                            page.get_by_label("编辑媒体"),
                            description="edit media button",
                        )
                        await click_e(
                            page.get_by_label("内容警告"),
                            description="content warning button",
                        )
                        await click_e(
                            page.get_by_text("敏感内容"),
                            description="sensitive content option",
                        )
                        if "video" in medium["mimeType"]:
                            logger.debug("Video detected, clicking 完成 twice.")
                            await click_e(
                                page.get_by_text("完成"), description="done button"
                            )
                            await click_e(
                                page.get_by_text("完成"), description="done button"
                            )
                        else:
                            await click_e(
                                page.get_by_text("保存"), description="save button"
                            )
                            # X 的媒体编辑页按钮文案经常变，保存后不强依赖单一“返回”按钮。
                            await click_if_available(
                                media_back_candidates(page),
                                timeout=5,
                                description="media back button",
                            )
                    first = False

                await take_debug_screenshot(page, "ss/2.png")

                if media:
                    await wait_first_available(
                        post_buttons, timeout=600, description="post button"
                    )

                if made_with_ai:
                    await enable_ai_content_disclosure(page)

                await click_e(composer, timeout=30, description="post composer")
                await composer.wait_for(state="attached")
                await composer.focus()
                await composer.fill(txt + "\n")

                logger.info("Posting...")
                try:
                    async with page.expect_response(
                        is_create_tweet_response,
                        timeout=CREATE_TWEET_RESPONSE_TIMEOUT_MS,
                    ) as resp_info:
                        await click_first_available(
                            post_buttons, timeout=60, description="post button"
                        )
                        posted = True
                    response = await resp_info.value
                    if response.status == 200:
                        tweet_id = await extract_tweet_id_from_response(response)
                        if tweet_id:
                            logger.info("Captured tweet_id={}", tweet_id)
                            logger.info("Post sent.")
                            return tweet_id
                        else:
                            logger.warning(
                                "CreateTweet response captured but no rest_id found"
                            )
                    else:
                        logger.warning(
                            "CreateTweet response returned status {}", response.status
                        )
                except (TimeoutError, PlaywrightError) as exc:
                    if not (posted or create_tweet_dispatched):
                        raise
                    posted = True
                    logger.warning(
                        "Could not capture CreateTweet response (dispatched=%s): %s",
                        create_tweet_dispatched,
                        describe_send_exception(exc),
                    )

                await take_debug_screenshot(page, "ss/3.png")
                logger.info("Post sent.")
            except Exception as e:
                if posted or create_tweet_dispatched:
                    detail = describe_send_exception(e)
                    logger.warning(
                        "Post operation incomplete (dispatched=%s, posted=%s): %s",
                        create_tweet_dispatched,
                        posted,
                        detail,
                    )
                    raise PostSentError(detail, tweet_id=tweet_id)
                raise
            finally:
                try:
                    await asyncio.wait_for(
                        browser.close(), timeout=BROWSER_CLOSE_TIMEOUT_SECONDS
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to close browser cleanly: {}",
                        describe_send_exception(e),
                    )
    finally:
        queue_stats["active"] -= 1
        sem.release()
    return tweet_id
