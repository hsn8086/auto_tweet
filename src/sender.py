import asyncio
from pathlib import Path
from playwright.async_api import (
    async_playwright,
    ProxySettings,
    FilePayload,
    StorageState,
)
from tenacity import retry, stop_after_attempt

from loguru import logger
from .model import State
from playwright.async_api import Locator

sem = asyncio.Semaphore(1)


async def click_e(e: Locator, *, timeout: int = 10):
    for _ in range(timeout):
        if await e.is_enabled():
            break
        await asyncio.sleep(1)
    else:
        if not await e.is_enabled():
            raise TimeoutError()

    await e.click()


@retry(stop=stop_after_attempt(5))
@logger.catch()
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
        spoiler = spoiler == "True" or spoiler == "true"
    print(spoiler, type(spoiler))
    async with sem, async_playwright() as p:
        logger.info("Launching browser...")
        browser = await p.firefox.launch(
            proxy=ProxySettings(server=proxy) if proxy else None,
            headless=headless,
        )
        # browser = await p.chromium.launch()
        context = await browser.new_context(
            storage_state=StorageState(**state.model_dump()), locale="zh-CN"
        )
        page = await context.new_page()
        await page.goto("https://x.com", timeout=60 * 10**3)
        logger.info("Waiting for login...")

        await page.get_by_label("帖子文本").click()
        first = True
        for medium in media:
            async with page.expect_file_chooser() as fc_info:
                if first:
                    await click_e(page.get_by_label("添加照片或视频"))
                else:
                    await click_e(page.get_by_label("添加媒体"))

            file_chooser = await fc_info.value
            print(file_chooser.element)
            await file_chooser.set_files(medium)
            logger.info("Image uploaded.")
            print(medium["name"], medium["mimeType"])

            if first and spoiler:
                # await asyncio.sleep(3)
                await click_e(page.get_by_label("编辑媒体"))
                await click_e(page.get_by_label("内容警告"))
                await click_e(page.get_by_text("敏感内容"))
                if "video" in medium["mimeType"]:
                    print("video")
                    await click_e(page.get_by_text("完成"))
                    await click_e(page.get_by_text("完成"))
                else:
                    await click_e(page.get_by_text("保存"))
                    await click_e(page.get_by_label("返回"))
            first = False
            # await asyncio.sleep(3)
        await click_e(page.get_by_label("帖子文本"))
        await asyncio.sleep(1)
        await page.get_by_label("帖子文本").type(txt + "\n")

        logger.info("Posting...")
        # await asyncio.sleep(10000)
        await click_e(page.get_by_label("主页时间线").get_by_text("发帖"), timeout=60)
        await asyncio.sleep(10)
        await page.close()
        await context.close()
        await browser.close()
