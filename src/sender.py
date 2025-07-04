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

sem = asyncio.Semaphore(2)


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


@logger.catch()
@retry(stop=stop_after_attempt(5))
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
    async with async_playwright() as p:
        async with sem:
            logger.info("Launching browser...")
            browser = await p.chromium.launch(
                proxy=ProxySettings(server=proxy) if proxy else None,
                headless=headless,
                # headless=False,
                # devtools=True,
                executable_path="/usr/bin/chromium",
            )
            # browser = await p.chromium.launch()
            context = await browser.new_context(
                storage_state=StorageState(**state.model_dump()), locale="zh-CN"
            )
            page = await context.new_page()
            await page.goto("https://x.com", timeout=60 * 10**3)
            logger.info("Waiting for login...")
            await page.screenshot(path="ss/1.png")
            await page.get_by_label("帖子文本").click()
            first = True
            for medium in media:
                async with page.expect_file_chooser() as fc_info:
                    if first:
                        # js_handle = await page.get_by_label("添加照片或视频").evaluate_handle(
                        #     """element => element""",)

                        # print(js_handle)
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
            await page.screenshot(path="ss/2.png")
            await click_e(page.get_by_label("帖子文本"))

            await page.get_by_label("帖子文本").wait_for(state="attached")
            await page.get_by_label("帖子文本").focus()

            await page.get_by_label("帖子文本").fill(txt + "\n")
            logger.info("Posting...")
            await click_e(page.get_by_label("主页时间线").get_by_text("发帖"), timeout=60)
            await page.screenshot(path="ss/3.png")
            logger.info("Post sent.")
        await asyncio.sleep(60)
