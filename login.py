import asyncio
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import Stealth


async def login():
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=False,
            # args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            storage_state="custom-state.json",  # Optional: load existing state
        )

        page = await context.new_page()
        await page.goto("https://x.com")
        input("请手动完成登录后按回车...")
        await context.storage_state(path=f"state{datetime.now().isoformat()}.json")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(login())
