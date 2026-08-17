import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import AsyncMock, patch

from src import sender
from src.model import State
from src.result_store import STATUS_SENT_UNCONFIRMED, ResultStore
from src.router.tweet import _execute_send


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
    def test_reply_timeline_response_supports_profile_v2_operation(self) -> None:
        response = type(
            "Response",
            (),
            {
                "url": (
                    "https://x.com/i/api/graphql/hash/"
                    "UserWithProfileTweetsAndRepliesQueryV2"
                ),
                "request": type("Request", (), {"method": "GET"})(),
            },
        )()

        self.assertTrue(sender.is_replies_timeline_response(cast(Any, response)))

    def test_reply_timeline_response_supports_user_originals_operation(self) -> None:
        response = type(
            "Response",
            (),
            {
                "url": "https://x.com/i/api/graphql/hash/UserOriginalsTimeline",
                "request": type("Request", (), {"method": "GET"})(),
            },
        )()

        self.assertTrue(sender.is_replies_timeline_response(cast(Any, response)))

    def test_reply_timeline_response_rejects_unrelated_operation(self) -> None:
        response = type(
            "Response",
            (),
            {
                "url": "https://x.com/i/api/graphql/hash/UserByScreenName",
                "request": type("Request", (), {"method": "GET"})(),
            },
        )()

        self.assertFalse(sender.is_replies_timeline_response(cast(Any, response)))

    async def test_timeline_retry_stops_after_first_matched_response(self) -> None:
        empty = sender.TimelineCollection([], False, False, 0)
        matched = sender.TimelineCollection([], True, False, 1)
        with (
            patch(
                "src.sender._collect_replies_timeline",
                AsyncMock(side_effect=[empty, matched]),
            ) as collect,
            patch("src.sender.asyncio.sleep", AsyncMock()) as sleep,
        ):
            result = await sender._collect_replies_timeline_with_retry(
                object(), "https://x.com/target", max_scrolls=1
            )

        self.assertIs(result, matched)
        self.assertEqual(collect.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_timeline_retry_recovers_from_navigation_error(self) -> None:
        matched = sender.TimelineCollection([], True, False, 1)
        with (
            patch(
                "src.sender._collect_replies_timeline",
                AsyncMock(side_effect=[sender.PlaywrightError("closed"), matched]),
            ) as collect,
            patch("src.sender.asyncio.sleep", AsyncMock()) as sleep,
        ):
            result = await sender._collect_replies_timeline_with_retry(
                object(), "https://x.com/target", max_scrolls=1
            )

        self.assertIs(result, matched)
        self.assertEqual(collect.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_timeline_waits_for_async_response_reader_before_close(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.closed = False

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None
                self.response_handler(Response(self))

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                pass

            async def close(self) -> None:
                self.closed = True

        class Response:
            url = "https://x.com/i/api/graphql/id/NotificationsTimeline"
            request = type("Request", (), {"method": "GET"})()
            status = 200

            def __init__(self, page: Page) -> None:
                self.page = page

            async def json(self) -> dict[str, bool]:
                await asyncio.sleep(0)
                self_test.assertFalse(self.page.closed)
                return {"ready": True}

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        self_test = self
        page = Page()
        parsed = [
            {
                "tweet_id": "200",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "in_reply_to_user_id": "10",
                "author": {"user_id": "20"},
            }
        ]
        with patch("src.sender.parse_graphql_tweets", return_value=parsed):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(page)),
                "https://x.com/notifications/verified",
                max_scrolls=1,
            )

        self.assertTrue(page.closed)
        self.assertEqual(result.matched_responses, 1)
        self.assertEqual([item["tweet_id"] for item in result.tweets], ["200"])

    async def test_timeline_settles_in_flight_response_before_completeness(
        self,
    ) -> None:
        """An in-flight page must reset idle state before completeness is decided."""

        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.closed = False
                self.scrolls = 0

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                # 第 3 次滚动才收到新页。若下一轮不 settle，扫描会把它误记为
                # 第 4 次空转并提前宣告 complete。
                self.scrolls += 1
                if self.scrolls == 3:
                    assert self.response_handler is not None
                    self.response_handler(SlowResponse())

            async def close(self) -> None:
                self.closed = True

        class SlowResponse:
            url = "https://x.com/i/api/graphql/id/NotificationsTimeline"
            request = type("Request", (), {"method": "GET"})()
            status = 200

            async def json(self) -> dict[str, bool]:
                # 放大 response 事件与 body 读取完成之间的竞态窗口。
                for _ in range(5):
                    await asyncio.sleep(0)
                return {"ready": True}

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        page = Page()
        parsed = [
            {
                "tweet_id": "900",
                "created_at": "2026-08-17T00:00:00+00:00",
                "in_reply_to_user_id": "10",
                "author": {"user_id": "20"},
            }
        ]
        with patch("src.sender.parse_graphql_tweets", return_value=parsed):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(page)),
                "https://x.com/notifications/verified",
                max_scrolls=3,
            )

        self.assertFalse(result.complete)
        self.assertEqual([item["tweet_id"] for item in result.tweets], ["900"])

    async def test_timeline_rejected_responses_force_incomplete(self) -> None:
        class Response:
            url = "https://x.com/i/api/graphql/id/NotificationsTimeline"
            request = type("Request", (), {"method": "GET"})()

            def __init__(self, status: int, payload: dict[str, Any]) -> None:
                self.status = status
                self.payload = payload

            async def json(self) -> dict[str, Any]:
                return self.payload

        class Page:
            def __init__(self) -> None:
                self.response_handler = None

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None
                await self.response_handler(Response(200, {"data": {}}))
                await self.response_handler(Response(200, {"errors": [{"code": 1}]}))
                await self.response_handler(Response(429, {"data": {}}))

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                pass

            async def close(self) -> None:
                pass

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        parsed = [
            {
                "tweet_id": "901",
                "created_at": "2026-08-17T00:00:00+00:00",
                "in_reply_to_user_id": "10",
                "author": {"user_id": "20"},
            }
        ]
        with patch("src.sender.parse_graphql_tweets", return_value=parsed):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(Page())),
                "https://x.com/notifications/verified",
                max_scrolls=0,
            )

        self.assertEqual(result.matched_responses, 1)
        self.assertEqual(result.response_errors, 2)
        self.assertFalse(result.complete)
        self.assertEqual([item["tweet_id"] for item in result.tweets], ["901"])

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

    async def test_wait_for_cancellation_after_dispatch_is_sent_unconfirmed(
        self,
    ) -> None:
        class Composer(FakeLocator):
            async def wait_for(self, **_kwargs) -> None:
                pass

            async def focus(self) -> None:
                pass

            async def fill(self, _text: str) -> None:
                pass

        class ResponseWaiter:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_args) -> None:
                await __import__("asyncio").sleep(60)

            @property
            async def value(self):
                raise AssertionError("response should not arrive")

        class Page:
            def __init__(self) -> None:
                self.request_handlers = []

            def on(self, event: str, handler) -> None:
                if event == "request":
                    self.request_handlers.append(handler)

            def expect_response(self, *_args, **_kwargs) -> ResponseWaiter:
                return ResponseWaiter()

        class Button(FakeLocator):
            def __init__(self, page: Page) -> None:
                super().__init__()
                self.page = page

            async def click(self) -> None:
                await super().click()
                request = type(
                    "Request",
                    (),
                    {
                        "url": "https://x.com/i/api/graphql/id/CreateTweet",
                        "method": "POST",
                    },
                )()
                for handler in self.page.request_handlers:
                    handler(request)

        page = Page()
        composer = Composer()
        button = Button(page)
        context = type("Context", (), {"new_page": AsyncMock(return_value=page)})()
        browser = type(
            "Browser",
            (),
            {
                "new_context": AsyncMock(return_value=context),
                "close": AsyncMock(),
            },
        )()
        chromium = type("Chromium", (), {"launch": AsyncMock(return_value=browser)})()

        class PlaywrightContext:
            async def __aenter__(self):
                return type("Playwright", (), {"chromium": chromium})()

            async def __aexit__(self, *_args) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory))
            self.assertEqual(store.claim("cancelled-send", 60).outcome, "claimed")
            with (
                patch("src.sender.async_playwright", return_value=PlaywrightContext()),
                patch(
                    "src.sender.open_post_composer", AsyncMock(return_value=composer)
                ),
                patch("src.sender.post_button_candidates", return_value=[button]),
                patch("src.sender.take_debug_screenshot", AsyncMock()),
                patch("src.router.tweet.SEND_TIMEOUT_SECONDS", 0.01),
            ):
                result = await _execute_send(
                    sender.send("reply", State(cookies=[])),
                    request_id="cancelled-send",
                    store=store,
                    operation_name="test_cancelled_send",
                )

            self.assertEqual(result["status"], "ok")
            self.assertIn("warning", result)
            entry = store.get("cancelled-send")
            assert entry is not None
            self.assertEqual(entry["status"], STATUS_SENT_UNCONFIRMED)
            self.assertEqual(chromium.launch.await_count, 1)

    async def _assert_teardown_cancellation_is_sent_unconfirmed(
        self, teardown_phase: str
    ) -> None:
        class Composer(FakeLocator):
            async def wait_for(self, **_kwargs) -> None:
                pass

            async def focus(self) -> None:
                pass

            async def fill(self, _text: str) -> None:
                pass

        class ResponseWaiter:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_args) -> None:
                pass

            @property
            async def value(self):
                return type("Response", (), {"status": 200})()

        class Page:
            def __init__(self) -> None:
                self.request_handlers = []

            def on(self, event: str, handler) -> None:
                if event == "request":
                    self.request_handlers.append(handler)

            def expect_response(self, *_args, **_kwargs) -> ResponseWaiter:
                return ResponseWaiter()

        class Button(FakeLocator):
            def __init__(self, page: Page) -> None:
                super().__init__()
                self.page = page

            async def click(self) -> None:
                await super().click()
                request = type(
                    "Request",
                    (),
                    {
                        "url": "https://x.com/i/api/graphql/id/CreateTweet",
                        "method": "POST",
                    },
                )()
                for handler in self.page.request_handlers:
                    handler(request)

        page = Page()
        composer = Composer()
        button = Button(page)
        context = type("Context", (), {"new_page": AsyncMock(return_value=page)})()

        async def close_browser() -> None:
            if teardown_phase == "browser_close":
                await asyncio.sleep(60)

        browser = type(
            "Browser",
            (),
            {
                "new_context": AsyncMock(return_value=context),
                "close": AsyncMock(side_effect=close_browser),
            },
        )()
        chromium = type("Chromium", (), {"launch": AsyncMock(return_value=browser)})()

        class PlaywrightContext:
            async def __aenter__(self):
                return type("Playwright", (), {"chromium": chromium})()

            async def __aexit__(self, *_args) -> None:
                if teardown_phase == "playwright_exit":
                    await asyncio.sleep(60)
                if teardown_phase == "playwright_error":
                    raise sender.PlaywrightError("teardown failed")

        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory))
            request_id = f"teardown-{teardown_phase}"
            self.assertEqual(store.claim(request_id, 60).outcome, "claimed")
            with (
                patch("src.sender.async_playwright", return_value=PlaywrightContext()),
                patch(
                    "src.sender.open_post_composer", AsyncMock(return_value=composer)
                ),
                patch("src.sender.post_button_candidates", return_value=[button]),
                patch("src.sender.take_debug_screenshot", AsyncMock()),
                patch(
                    "src.sender.extract_tweet_id_from_response",
                    AsyncMock(return_value="123"),
                ),
                patch("src.router.tweet.SEND_TIMEOUT_SECONDS", 0.01),
            ):
                result = await _execute_send(
                    sender.send("reply", State(cookies=[])),
                    request_id=request_id,
                    store=store,
                    operation_name="test_teardown_send",
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["tweet_id"], "123")
            self.assertIn("warning", result)
            entry = store.get(request_id)
            assert entry is not None
            self.assertEqual(entry["status"], STATUS_SENT_UNCONFIRMED)
            self.assertEqual(entry["tweet_id"], "123")
            self.assertEqual(chromium.launch.await_count, 1)

    async def test_browser_close_cancellation_after_success_is_sent_unconfirmed(
        self,
    ) -> None:
        await self._assert_teardown_cancellation_is_sent_unconfirmed("browser_close")

    async def test_playwright_exit_cancellation_after_success_is_sent_unconfirmed(
        self,
    ) -> None:
        await self._assert_teardown_cancellation_is_sent_unconfirmed("playwright_exit")

    async def test_playwright_error_after_success_is_sent_unconfirmed(self) -> None:
        await self._assert_teardown_cancellation_is_sent_unconfirmed("playwright_error")

    async def test_open_reply_composer_targets_requested_tweet(self) -> None:
        reply_button = FakeLocator()
        composer = FakeLocator()

        class Article:
            @property
            def first(self) -> "Article":
                return self

            def get_by_test_id(self, test_id: str) -> FakeLocator:
                self_test.assertEqual(test_id, "reply")
                return reply_button

        class ReplyPage:
            def __init__(self) -> None:
                self.goto_url = ""
                self.selector = ""

            async def goto(self, url: str, **_kwargs) -> None:
                self.goto_url = url

            def locator(self, selector: str) -> Article:
                self.selector = selector
                return Article()

        self_test = self
        page = ReplyPage()
        with patch("src.sender.composer_candidates", return_value=[composer]):
            result = await sender.open_reply_composer(cast(Any, page), "123456")

        self.assertIs(result, composer)
        self.assertEqual(page.goto_url, "https://x.com/i/web/status/123456")
        self.assertIn("/status/123456", page.selector)
        self.assertTrue(reply_button.clicked)
        self.assertTrue(composer.clicked)

    async def test_notification_parent_does_not_trigger_since_boundary(self) -> None:
        class Request:
            method = "GET"

        class Response:
            url = "https://x.com/i/api/graphql/id/NotificationsTimeline"
            request = Request()
            status = 200

            async def json(self) -> dict:
                return {}

        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.scrolled = False

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None
                await self.response_handler(Response())

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                self.scrolled = True

            async def close(self) -> None:
                pass

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        now = datetime.now(timezone.utc)
        parsed = [
            {
                "tweet_id": "200",
                "created_at": now.isoformat(),
                "in_reply_to_user_id": "10",
                "author": {"user_id": "20"},
            },
            {
                "tweet_id": "100",
                "created_at": (now - timedelta(hours=2)).isoformat(),
                "in_reply_to_user_id": None,
                "author": {"user_id": "10"},
            },
        ]
        page = Page()
        with patch("src.sender.parse_graphql_tweets", return_value=parsed):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(page)),
                "https://x.com/notifications/verified",
                max_scrolls=1,
                since_time=now - timedelta(hours=1),
                boundary_reply_to_user_id="10",
            )

        self.assertTrue(page.scrolled)
        self.assertFalse(result.complete)

    async def test_finite_scroll_cap_reports_incomplete(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.scrolls = 0

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                pass

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                self.scrolls += 1

            async def close(self) -> None:
                pass

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        page = Page()
        result = await sender._collect_replies_timeline(
            cast(Any, Context(page)),
            "https://x.com/notifications/verified",
            max_scrolls=1,
            since_id="100",
            boundary_reply_to_user_id="10",
        )

        self.assertEqual(page.scrolls, 1)
        self.assertFalse(result.complete)
        self.assertFalse(result.boundary_reached)

    async def test_unlimited_scroll_reaches_boundary(self) -> None:
        class Response:
            url = "https://x.com/i/api/graphql/id/NotificationsTimeline"
            request = type("Request", (), {"method": "GET"})()
            status = 200

            def __init__(self, page_no: int) -> None:
                self.page_no = page_no

            async def json(self) -> dict[str, int]:
                return {"page": self.page_no}

        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.scrolls = 0

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None
                await self.response_handler(Response(1))

            async def wait_for_timeout(self, _timeout: int) -> None:
                if self.scrolls:
                    assert self.response_handler is not None
                    await self.response_handler(Response(2))

            async def evaluate(self, _script: str) -> None:
                self.scrolls += 1

            async def close(self) -> None:
                pass

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        now = datetime.now(timezone.utc)

        def parse(payload: dict[str, int], **_kwargs: Any) -> list[dict[str, Any]]:
            tweet_id = "200" if payload["page"] == 1 else "100"
            return [
                {
                    "tweet_id": tweet_id,
                    "created_at": now.isoformat(),
                    "in_reply_to_user_id": "10",
                    "author": {"user_id": "20"},
                }
            ]

        page = Page()
        with patch("src.sender.parse_graphql_tweets", side_effect=parse):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(page)),
                "https://x.com/notifications/verified",
                max_scrolls=0,
                since_id="100",
                boundary_reply_to_user_id="10",
            )

        self.assertEqual(page.scrolls, 1)
        self.assertTrue(result.complete)
        self.assertTrue(result.boundary_reached)

    async def test_old_pinned_parent_does_not_stop_profile_scan(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.response_handler = None
                self.scrolled = False

            def on(self, event: str, handler) -> None:
                if event == "response":
                    self.response_handler = handler

            async def goto(self, *_args, **_kwargs) -> None:
                assert self.response_handler is not None
                response = type(
                    "Response",
                    (),
                    {
                        "url": "https://x.com/i/api/graphql/id/UserTweetsAndReplies",
                        "request": type("Request", (), {"method": "GET"})(),
                        "status": 200,
                        "json": lambda _self: async_value({}),
                    },
                )()
                await self.response_handler(response)

            async def wait_for_timeout(self, _timeout: int) -> None:
                pass

            async def evaluate(self, _script: str) -> None:
                self.scrolled = True

            async def close(self) -> None:
                pass

        async def async_value(value):
            return value

        class Context:
            def __init__(self, page: Page) -> None:
                self.page = page

            async def new_page(self) -> Page:
                return self.page

        now = datetime.now(timezone.utc)
        parsed = [
            {
                "tweet_id": "1",
                "created_at": (now - timedelta(days=30)).isoformat(),
                "author": {"user_id": "10"},
                "_pinned": True,
            },
            {
                "tweet_id": "2",
                "created_at": (now - timedelta(hours=2)).isoformat(),
                "author": {"user_id": "10"},
                "_pinned": False,
            },
        ]
        page = Page()
        with patch("src.sender.parse_graphql_tweets", return_value=parsed):
            result = await sender._collect_replies_timeline(
                cast(Any, Context(page)),
                "https://x.com/target/with_replies",
                max_scrolls=1,
                oldest_time=now - timedelta(hours=48),
                boundary_author_user_id="10",
            )

        self.assertTrue(page.scrolled)
        self.assertFalse(result.boundary_reached)


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
