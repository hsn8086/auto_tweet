import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    FilePayload,
    Locator,
    ProxySettings,
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

sem = asyncio.Semaphore(2)

if TYPE_CHECKING:
    from playwright.async_api import Page


async def wait_e(e: Locator, *, timeout: int = 10):
    for _ in range(timeout * 10):
        if await e.is_enabled():
            break
        await asyncio.sleep(0.1)
    else:
        if not await e.is_enabled():
            raise TimeoutError()


async def click_e(e: Locator, *, timeout: int = 10):
    await wait_e(e, timeout=timeout)
    await e.click()


async def wait_first_available(locators: list[Locator], *, timeout: int = 10) -> Locator:
    for _ in range(timeout * 10):
        for locator in locators:
            candidate = locator.first
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except PlaywrightError:
                continue
        await asyncio.sleep(0.1)
    raise TimeoutError()


async def click_first_available(locators: list[Locator], *, timeout: int = 10) -> Locator:
    candidate = await wait_first_available(locators, timeout=timeout)
    await candidate.click()
    return candidate


async def click_if_available(locators: list[Locator], *, timeout: int = 10) -> bool:
    try:
        await click_first_available(locators, timeout=timeout)
    except TimeoutError:
        return False
    return True


def post_button_candidates(page: "Page") -> list[Locator]:
    return [
        page.get_by_test_id("tweetButtonInline"),
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


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (TimeoutError, ConnectionError, OSError, PlaywrightError)
    ),
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
):
    if not media:
        media = []
    if isinstance(spoiler, str):
        spoiler = spoiler in ("True", "true")

    posted = False

    async with sem:
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
                page.on("console", lambda msg: logger.log(msg.type.upper(), msg.text))
                await page.goto("https://x.com", timeout=60_000)
                logger.info("Page loaded.")
                await page.screenshot(path="ss/1.png")
                await page.get_by_label("帖子文本").click()
                post_buttons = post_button_candidates(page)

                first = True
                for medium in media:
                    async with page.expect_file_chooser() as fc_info:
                        if first:
                            await click_e(page.get_by_label("添加照片或视频"))
                        else:
                            await click_e(page.get_by_label("添加媒体"))

                    file_chooser = await fc_info.value
                    await file_chooser.set_files(medium)
                    logger.info(
                        "Image uploaded: {} ({})", medium["name"], medium["mimeType"]
                    )

                    if first and spoiler:
                        await click_e(page.get_by_label("编辑媒体"))
                        await click_e(page.get_by_label("内容警告"))
                        await click_e(page.get_by_text("敏感内容"))
                        if "video" in medium["mimeType"]:
                            logger.debug("Video detected, clicking 完成 twice.")
                            await click_e(page.get_by_text("完成"))
                            await click_e(page.get_by_text("完成"))
                        else:
                            await click_e(page.get_by_text("保存"))
                            # X 的媒体编辑页按钮文案经常变，保存后不强依赖单一“返回”按钮。
                            await click_if_available(media_back_candidates(page), timeout=5)
                    first = False

                await page.screenshot(path="ss/2.png")

                if media:
                    await wait_first_available(post_buttons, timeout=600)

                await click_e(page.get_by_label("帖子文本"))
                await page.get_by_label("帖子文本").wait_for(state="attached")
                await page.get_by_label("帖子文本").focus()
                await page.get_by_label("帖子文本").fill(txt + "\n")

                logger.info("Posting...")
                await click_first_available(post_buttons, timeout=60)
                posted = True

                await page.screenshot(path="ss/3.png")
                logger.info("Post sent.")

                await asyncio.sleep(60)
            except Exception as e:
                if posted:
                    logger.warning("Post sent but post-send operations failed: {}", e)
                    raise PostSentError(str(e))
                raise
            finally:
                await browser.close()
