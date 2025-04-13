import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, ProxySettings, FilePayload
from tenacity import retry, stop_after_attempt

from loguru import logger
from .model import State

sem = asyncio.Semaphore(1)


@retry(stop=stop_after_attempt(5))
@logger.catch()
async def send(
    txt: str, state: State, *, img: FilePayload | None = None, proxy: str = None
):
    async with sem, async_playwright() as p:
        logger.info("Launching browser...")
        browser = await p.chromium.launch(
            proxy=ProxySettings(server=proxy) if proxy else None
        )
        # browser = await p.chromium.launch()
        context = await browser.new_context(
            storage_state=state.model_dump(), locale="zh-CN"
        )
        page = await context.new_page()
        await page.goto("https://x.com")
        logger.info("Waiting for login...")

        await page.get_by_label("帖子文本").click()
        if img:
            async with page.expect_file_chooser() as fc_info:
                await page.get_by_label("添加照片或视频").click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(img)
                logger.info("Image uploaded.")
                await asyncio.sleep(10)
        await page.get_by_label("帖子文本").click()
        await page.get_by_label("帖子文本").fill(txt)
        logger.info("Posting...")
        await page.get_by_label("主页时间线").get_by_text("发帖").click()
        await asyncio.sleep(40)

        await browser.close()
