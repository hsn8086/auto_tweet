import unittest
from typing import Any, cast

from src import sender


class FakeLocator:
    def __init__(self, *, visible: bool = True, enabled: bool = True) -> None:
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def click(self) -> None:
        self.clicked = True


class SenderHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_e_returns_messageful_timeout(self) -> None:
        blocked = FakeLocator(enabled=False)

        with self.assertRaisesRegex(TimeoutError, "post button"):
            await sender.wait_e(
                cast(Any, blocked), timeout=1, description="post button"
            )

    async def test_click_first_available_uses_first_clickable_locator(self) -> None:
        blocked = FakeLocator(visible=False)
        ready = FakeLocator()

        result = await sender.click_first_available(
            cast(Any, [blocked, ready]), timeout=1
        )

        self.assertIs(result, ready)
        self.assertFalse(blocked.clicked)
        self.assertTrue(ready.clicked)

    async def test_click_if_available_returns_false_when_none_clickable(self) -> None:
        blocked = FakeLocator(visible=False)

        result = await sender.click_if_available(cast(Any, [blocked]), timeout=1)

        self.assertFalse(result)
        self.assertFalse(blocked.clicked)

    def test_describe_send_exception_falls_back_to_exception_type(self) -> None:
        detail = sender.describe_send_exception(TimeoutError())

        self.assertEqual(detail, "TimeoutError")

    async def test_take_debug_screenshot_does_not_raise_on_timeout(self) -> None:
        import asyncio
        from unittest.mock import patch

        class SlowPage:
            async def screenshot(self, *, path: str) -> None:
                await asyncio.sleep(1)

        with patch("src.sender.SCREENSHOT_TIMEOUT_SECONDS", 0.01):
            await sender.take_debug_screenshot(cast(Any, SlowPage()), "ss/1.png")


class FakePage:
    def __init__(self, url: str, *, composer: "FakeLocator | None" = None) -> None:
        self.url = url
        self._composer = composer or FakeLocator(visible=False, enabled=False)

    def get_by_label(self, *_args, **_kwargs) -> "FakeLocator":
        return self._composer

    def get_by_test_id(self, *_args, **_kwargs) -> "FakeLocator":
        return self._composer

    def locator(self, *_args, **_kwargs) -> "FakeLocator":
        return self._composer


class LoginBounceTests(unittest.IsolatedAsyncioTestCase):
    def test_is_login_bounce_url_detects_login_pages(self) -> None:
        bounced = [
            "https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fcompose%2Fpost&mode=login",
            "https://x.com/i/flow/login",
            "https://x.com/?mode=login",
            "https://x.com/",
            "https://twitter.com",
        ]
        for url in bounced:
            self.assertTrue(sender.is_login_bounce_url(url), url)

    def test_is_login_bounce_url_allows_normal_pages(self) -> None:
        normal = [
            "https://x.com/home",
            "https://x.com/compose/post",
            "https://twitter.com/compose/tweet",
            "https://x.com/i/web/status/123",
            "",
        ]
        for url in normal:
            self.assertFalse(sender.is_login_bounce_url(url), url)

    async def test_wait_composer_or_login_fails_fast_on_login_bounce(self) -> None:
        page = FakePage(
            "https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fcompose%2Fpost&mode=login"
        )

        result = await sender.wait_composer_or_login(cast(Any, page), timeout=30)

        self.assertIsNone(result)

    async def test_wait_composer_or_login_returns_composer(self) -> None:
        composer = FakeLocator()
        page = FakePage("https://x.com/home", composer=composer)

        result = await sender.wait_composer_or_login(cast(Any, page), timeout=1)

        self.assertIs(result, composer)

    async def test_wait_composer_or_login_times_out_with_message(self) -> None:
        page = FakePage("https://x.com/home")

        with self.assertRaisesRegex(TimeoutError, "post composer"):
            await sender.wait_composer_or_login(cast(Any, page), timeout=1)


if __name__ == "__main__":
    unittest.main()
