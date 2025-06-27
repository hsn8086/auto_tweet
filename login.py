from playwright.async_api import async_playwright

import asyncio


async def login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://x.com")
        input("Press any key to continue...")
        await context.storage_state(path="state.json")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(login())
