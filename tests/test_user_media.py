import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.model import State
from src.replies import filter_user_media_tweets, parse_graphql_tweets
from src.router.tweet import router
from src.sender import (
    _is_clean_user_media_response,
    browser_queue_slot,
    fetch_user_media,
    parse_user_media_payload,
    parse_user_media_profile,
    queue_stats,
    user_media_timeline_exhausted,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _photo(url: str, *, width: int = 1200, height: int = 800) -> dict:
    return {
        "type": "photo",
        "media_url_https": url,
        "original_info": {"width": width, "height": height},
    }


def _video(url: str) -> dict:
    return {
        "type": "video",
        "media_url_https": url,
        "video_info": {
            "variants": [
                {"content_type": "video/mp4", "bitrate": 100, "url": f"{url}.mp4"}
            ]
        },
    }


def _tweet(
    tweet_id: str,
    *,
    author_id: str,
    screen_name: str,
    created_at: datetime = NOW,
    media: list[dict] | None = None,
    retweet_of: dict | None = None,
    quote_of: dict | None = None,
    modern_user: bool = False,
) -> dict:
    legacy = {
        "full_text": f"tweet {tweet_id}",
        "created_at": created_at.strftime("%a %b %d %H:%M:%S %z %Y"),
        "conversation_id_str": tweet_id,
    }
    if media is not None:
        legacy["extended_entities"] = {"media": media}
    if retweet_of is not None:
        legacy["retweeted_status_result"] = {"result": retweet_of}
    if quote_of is not None:
        legacy["quoted_status_result"] = {"result": quote_of}
    author: dict[str, Any] = {
        "rest_id": author_id,
        "is_blue_verified": False,
        "legacy": {
            "screen_name": screen_name,
            "name": screen_name.title(),
            "followers_count": 100000,
        },
    }
    if modern_user:
        # X 现代 user 对象：没有 legacy，用户名在 core，粉丝数在 relationship_counts
        author.pop("legacy")
        author["core"] = {"screen_name": screen_name, "name": screen_name.title()}
        author["relationship_counts"] = {"followers": 100000}
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "legacy": legacy,
        "core": {"user_results": {"result": author}},
    }


def _payload(
    nodes: list[dict],
    *,
    profile_id: str | None = None,
    profile_screen_name: str = "yunjiu",
    exhausted: bool = False,
    profile_modern: bool = False,
    visibility_wrapped: bool = False,
) -> dict:
    instructions = [
        {
            "type": "TimelineAddEntries",
            "entries": [
                {
                    "entryId": f"tweet-{node['rest_id']}",
                    "content": {
                        "entryType": "TimelineTimelineItem",
                        "itemContent": {"tweet_results": {"result": node}},
                    },
                }
                for node in nodes
            ],
        }
    ]
    if exhausted:
        instructions.append(
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"}
        )
    profile: dict[str, Any] = {"timeline": {"timeline": {"instructions": instructions}}}
    if profile_id is not None:
        profile["rest_id"] = profile_id
        if profile_modern:
            profile["core"] = {
                "screen_name": profile_screen_name,
                "name": profile_screen_name.title(),
            }
            profile["relationship_counts"] = {"followers": 100000}
        else:
            profile["legacy"] = {
                "screen_name": profile_screen_name,
                "name": profile_screen_name.title(),
                "followers_count": 100000,
            }
    if visibility_wrapped:
        profile = {
            "__typename": "UserWithVisibilityResults",
            "user": {"__typename": "User", **profile},
        }
    return {"data": {"user": {"result": profile}}}


def _parse(nodes: list[dict]) -> list[dict]:
    return parse_graphql_tweets(_payload(nodes))


class _UserMediaResponse:
    request = SimpleNamespace(method="GET")

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        url: str = "https://x.com/i/api/graphql/hash/UserTweets?variables=x",
    ) -> None:
        self.payload = payload
        self.status = status
        self.url = url

    async def json(self) -> dict[str, Any]:
        return self.payload


class _SlowUserMediaResponse(_UserMediaResponse):
    """Response whose body only resolves after a few event-loop turns."""

    def __init__(
        self, payload: dict[str, Any], *, turns: int = 3, **kwargs: Any
    ) -> None:
        super().__init__(payload, **kwargs)
        self.turns = turns

    async def json(self) -> dict[str, Any]:
        for _ in range(self.turns):
            await asyncio.sleep(0)
        return self.payload


class _UserMediaPage:
    def __init__(self, payload_batches: list[list[Any]]) -> None:
        self.payload_batches = list(payload_batches)
        self.response_handler = None
        self.scrolls = 0
        self.closed = False

    def on(self, event: str, handler) -> None:
        if event == "response":
            self.response_handler = handler

    async def goto(self, *_args, **_kwargs) -> None:
        pass

    async def wait_for_timeout(self, _timeout: int) -> None:
        if self.payload_batches:
            assert self.response_handler is not None
            for payload in self.payload_batches.pop(0):
                self.response_handler(
                    payload
                    if isinstance(payload, _UserMediaResponse)
                    else _UserMediaResponse(payload)
                )
        await asyncio.sleep(0)

    async def evaluate(self, _script: str) -> None:
        self.scrolls += 1

    async def close(self) -> None:
        self.closed = True


async def _fetch_with_payloads(
    payload_batches: list[list[dict[str, Any]]], **kwargs: Any
) -> tuple[dict[str, Any], _UserMediaPage]:
    page = _UserMediaPage(payload_batches)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context), close=AsyncMock()
    )
    chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))

    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=chromium)

        async def __aexit__(self, *_args) -> None:
            pass

    with patch("src.sender.async_playwright", return_value=PlaywrightContext()):
        result = await fetch_user_media(
            "yunjiu", "1", State(cookies=[]), max_scrolls=8, **kwargs
        )
    return result, page


class FilterUserMediaTests(unittest.TestCase):
    def test_profile_user_tweets_parser_keeps_author_and_media(self) -> None:
        payload = _payload(
            [
                _tweet(
                    "250",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/profile.jpg")],
                )
            ]
        )

        tweets = parse_user_media_payload(payload)

        self.assertEqual([tweet["tweet_id"] for tweet in tweets], ["250"])
        self.assertEqual(tweets[0]["author"]["user_id"], "1")
        self.assertEqual(tweets[0]["author"]["followers_count"], 100000)
        self.assertEqual(tweets[0]["media"][0]["type"], "photo")

    def test_profile_user_is_fallback_when_tweet_omits_core_author(self) -> None:
        node = _tweet(
            "251",
            author_id="ignored",
            screen_name="ignored",
            media=[_photo("https://pbs.twimg.com/media/fallback.jpg")],
        )
        node.pop("core")
        payload = _payload([node])
        profile = payload["data"]["user"]["result"]
        profile.update(
            {
                "rest_id": "1613079089879076864",
                "legacy": {
                    "screen_name": "YunJiu",
                    "name": "雲鳩",
                    "followers_count": 106500,
                },
            }
        )

        verified_profile = parse_user_media_profile(payload)
        assert verified_profile is not None
        self.assertEqual(verified_profile["user_id"], "1613079089879076864")
        tweets = parse_user_media_payload(payload, verified_author=verified_profile)

        self.assertEqual([tweet["tweet_id"] for tweet in tweets], ["251"])
        self.assertEqual(tweets[0]["author"]["user_id"], "1613079089879076864")
        self.assertEqual(tweets[0]["author"]["screen_name"], "YunJiu")
        self.assertEqual(tweets[0]["author"]["followers_count"], 106500)

    def test_modern_user_without_legacy_keeps_identity_and_followers(self) -> None:
        payload = _payload(
            [
                _tweet(
                    "249",
                    author_id="1613079089879076864",
                    screen_name="YunJiu",
                    media=[_photo("https://pbs.twimg.com/media/modern.jpg")],
                    modern_user=True,
                )
            ]
        )

        tweets = parse_user_media_payload(payload)

        self.assertEqual([tweet["tweet_id"] for tweet in tweets], ["249"])
        self.assertEqual(tweets[0]["author"]["user_id"], "1613079089879076864")
        self.assertEqual(tweets[0]["author"]["screen_name"], "YunJiu")
        self.assertEqual(tweets[0]["author"]["name"], "Yunjiu")
        self.assertEqual(tweets[0]["author"]["followers_count"], 100000)
        self.assertEqual(tweets[0]["media"][0]["type"], "photo")

    def test_modern_profile_without_legacy_is_verifiable(self) -> None:
        node = _tweet(
            "254",
            author_id="ignored",
            screen_name="ignored",
            media=[_photo("https://pbs.twimg.com/media/modern-profile.jpg")],
        )
        node.pop("core")
        payload = _payload(
            [node],
            profile_id="1613079089879076864",
            profile_screen_name="YunJiu",
            profile_modern=True,
        )

        profile = parse_user_media_profile(payload)
        assert profile is not None
        self.assertEqual(profile["user_id"], "1613079089879076864")
        self.assertEqual(profile["screen_name"], "YunJiu")
        self.assertEqual(profile["name"], "Yunjiu")
        self.assertEqual(profile["followers_count"], 100000)

        tweets = parse_user_media_payload(payload, verified_author=profile)
        self.assertEqual([tweet["tweet_id"] for tweet in tweets], ["254"])
        self.assertEqual(tweets[0]["author"]["user_id"], "1613079089879076864")

    def test_visibility_wrapped_profile_timeline_is_parsed(self) -> None:
        node = _tweet(
            "255",
            author_id="ignored",
            screen_name="ignored",
            media=[_photo("https://pbs.twimg.com/media/wrapped.jpg")],
        )
        node.pop("core")
        payload = _payload(
            [node],
            profile_id="1",
            profile_modern=True,
            visibility_wrapped=True,
            exhausted=True,
        )

        profile = parse_user_media_profile(payload)
        assert profile is not None
        self.assertEqual(profile["user_id"], "1")
        self.assertTrue(user_media_timeline_exhausted(payload))
        tweets = parse_user_media_payload(payload, verified_author=profile)
        self.assertEqual([tweet["tweet_id"] for tweet in tweets], ["255"])

    def test_leaf_with_other_author_is_not_relabelled_as_profile(self) -> None:
        # leaf 自带别人的 rest_id，但形状不完整（core 里没有 screen_name）：
        # 既不能被 fallback 顶替，也不能走 verified profile 的兜底路径。
        node = _tweet(
            "256",
            author_id="999",
            screen_name="other",
            media=[_photo("https://pbs.twimg.com/media/foreign.jpg")],
            modern_user=True,
        )
        node["core"]["user_results"]["result"]["core"].pop("screen_name")
        payload = _payload([node], profile_id="1")

        profile = parse_user_media_profile(payload)
        assert profile is not None
        tweets = parse_user_media_payload(payload, verified_author=profile)

        self.assertEqual(tweets, [])

    def test_rejects_http_and_graphql_error_responses(self) -> None:
        self.assertTrue(_is_clean_user_media_response(200, {"data": {}}))
        self.assertFalse(_is_clean_user_media_response(403, {"data": {}}))
        self.assertFalse(_is_clean_user_media_response(500, {"data": {}}))
        self.assertFalse(
            _is_clean_user_media_response(
                200, {"data": {"user": {}}, "errors": [{"message": "rate limited"}]}
            )
        )
        self.assertFalse(_is_clean_user_media_response(200, []))
        self.assertFalse(_is_clean_user_media_response(200, None))

    def test_keeps_only_target_author_photos(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    "300",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/a.jpg")],
                ),
                # 视频不进感知索引
                _tweet(
                    "299",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_video("https://video.twimg.com/b")],
                ),
                # 没有媒体
                _tweet("298", author_id="1", screen_name="yunjiu"),
                # 别人的图
                _tweet(
                    "297",
                    author_id="2",
                    screen_name="someone",
                    media=[_photo("https://pbs.twimg.com/media/c.jpg")],
                ),
            ]
        )

        result = filter_user_media_tweets(tweets, screen_name="yunjiu")

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["300"])
        self.assertEqual(result["target_user_id"], "1")

    def test_excludes_retweets_and_other_authors_originals(self) -> None:
        original = _tweet(
            "100",
            author_id="2",
            screen_name="someone",
            media=[_photo("https://pbs.twimg.com/media/orig.jpg")],
        )
        tweets = _parse(
            [
                _tweet(
                    "400",
                    author_id="1",
                    screen_name="yunjiu",
                    retweet_of=original,
                ),
            ]
        )

        # 转推能确认账号身份，只是没有原创图：这属于"抓到了但没有新图"，
        # 必须区别于抓取失败——上游是 fail-closed，混淆会导致误停发。
        result = filter_user_media_tweets(tweets, screen_name="yunjiu")

        self.assertEqual(result["tweets"], [])
        self.assertEqual(result["target_user_id"], "1")

    def test_raises_when_target_never_appears(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    "410",
                    author_id="2",
                    screen_name="someone",
                    media=[_photo("https://pbs.twimg.com/media/x.jpg")],
                )
            ]
        )

        with self.assertRaises(ValueError):
            filter_user_media_tweets(tweets, screen_name="yunjiu")

    def test_excludes_quoted_tweet_from_other_author(self) -> None:
        quoted = _tweet(
            "150",
            author_id="2",
            screen_name="someone",
            media=[_photo("https://pbs.twimg.com/media/quoted.jpg")],
        )
        tweets = _parse(
            [
                _tweet(
                    "500",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/mine.jpg")],
                    quote_of=quoted,
                ),
            ]
        )

        result = filter_user_media_tweets(tweets, screen_name="yunjiu")

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["500"])
        urls = [media["url"] for media in result["tweets"][0]["media"]]
        self.assertTrue(all("quoted.jpg" not in url for url in urls))

    def test_since_id_boundary_is_exclusive(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    str(tweet_id),
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo(f"https://pbs.twimg.com/media/{tweet_id}.jpg")],
                )
                for tweet_id in (600, 601, 602)
            ]
        )

        result = filter_user_media_tweets(tweets, screen_name="yunjiu", since_id="601")

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["602"])
        # 游标要按观测到的最新推进，而不是按过滤后的结果
        self.assertEqual(result["newest_id"], "602")

    def test_since_time_boundary_is_exclusive(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    "700",
                    author_id="1",
                    screen_name="yunjiu",
                    created_at=NOW,
                    media=[_photo("https://pbs.twimg.com/media/new.jpg")],
                ),
                _tweet(
                    "699",
                    author_id="1",
                    screen_name="yunjiu",
                    created_at=NOW - timedelta(hours=5),
                    media=[_photo("https://pbs.twimg.com/media/old.jpg")],
                ),
            ]
        )

        result = filter_user_media_tweets(
            tweets, screen_name="yunjiu", since_time=NOW - timedelta(hours=1)
        )

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["700"])

    def test_caps_at_max_tweets(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    str(900 + offset),
                    author_id="1",
                    screen_name="yunjiu",
                    created_at=NOW - timedelta(minutes=offset),
                    media=[_photo(f"https://pbs.twimg.com/media/{offset}.jpg")],
                )
                for offset in range(50)
            ]
        )

        result = filter_user_media_tweets(tweets, screen_name="yunjiu", max_tweets=32)

        self.assertEqual(len(result["tweets"]), 32)
        # 保留最新的 32 条
        self.assertEqual(result["tweets"][0]["tweet_id"], "900")

    def test_rejects_ambiguous_target_user_id(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    "800",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/a.jpg")],
                ),
                _tweet(
                    "801",
                    author_id="999",
                    screen_name="YunJiu",
                    media=[_photo("https://pbs.twimg.com/media/b.jpg")],
                ),
            ]
        )

        with self.assertRaises(ValueError):
            filter_user_media_tweets(tweets, screen_name="yunjiu")

    def test_requests_original_quality_photo(self) -> None:
        tweets = _parse(
            [
                _tweet(
                    "850",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/x.jpg?name=small")],
                )
            ]
        )

        result = filter_user_media_tweets(tweets, screen_name="yunjiu")
        url = result["tweets"][0]["media"][0]["url"]

        self.assertIn("name=orig", url)
        self.assertNotIn("name=small", url)
        self.assertTrue(url.startswith("https://pbs.twimg.com/"))


class FetchUserMediaSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_fetched_profile_id_fails_closed(self) -> None:
        node = _tweet(
            "300",
            author_id="1",
            screen_name="yunjiu",
            media=[_photo("https://pbs.twimg.com/media/wrong-profile.jpg")],
        )
        payload = _payload([node], profile_id="999")

        with self.assertRaisesRegex(ValueError, "999.*期望 1"):
            await _fetch_with_payloads([[payload]])

    async def test_authorless_leaf_is_not_pinned_without_verified_profile(self) -> None:
        node = _tweet(
            "301",
            author_id="ignored",
            screen_name="ignored",
            media=[_photo("https://pbs.twimg.com/media/no-profile.jpg")],
        )
        node.pop("core")

        with self.assertRaisesRegex(ValueError, "拒绝使用配置值"):
            await _fetch_with_payloads([[_payload([node])]])

    async def test_more_than_32_new_photo_tweets_is_incomplete(self) -> None:
        nodes = [
            _tweet(
                str(200 - offset),
                author_id="1",
                screen_name="yunjiu",
                media=[_photo(f"https://pbs.twimg.com/media/backlog-{offset}.jpg")],
            )
            for offset in range(40)
        ]
        nodes.append(_tweet("100", author_id="1", screen_name="yunjiu"))

        result, _ = await _fetch_with_payloads(
            [[_payload(nodes, profile_id="1")]], since_id="100", max_tweets=32
        )

        self.assertEqual(result["target_user_id"], "1")
        self.assertEqual(result["newest_id"], "200")
        self.assertEqual(len(result["tweets"]), 32)
        self.assertFalse(result["complete"])

    async def test_idle_exhaustion_before_cursor_is_incomplete(self) -> None:
        payload = _payload(
            [
                _tweet(
                    "300",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/new.jpg")],
                )
            ],
            profile_id="1",
        )

        result, page = await _fetch_with_payloads([[payload]], since_id="100")

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["300"])
        self.assertGreaterEqual(page.scrolls, 1)
        self.assertFalse(result["complete"])

    async def test_verified_empty_timeline_with_bottom_termination_is_complete(
        self,
    ) -> None:
        payload = _payload([], profile_id="1", exhausted=True)

        result, _ = await _fetch_with_payloads([[payload]], since_id="100")

        self.assertEqual(result["target_user_id"], "1")
        self.assertIsNone(result["newest_id"])
        self.assertEqual(result["tweets"], [])
        self.assertTrue(result["complete"])

    async def test_http_error_response_forces_incomplete(self) -> None:
        clean = _payload(
            [
                _tweet(
                    "300",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/ok.jpg")],
                )
            ],
            profile_id="1",
            exhausted=True,
        )
        broken = _UserMediaResponse({"data": {}}, status=429)

        result, _ = await _fetch_with_payloads([[clean, broken]], since_id="100")

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["300"])
        # 时间线明确到底了，但有响应被拒 → 可能漏页 → 不允许上游推进游标
        self.assertFalse(result["complete"])

    async def test_graphql_error_response_forces_incomplete(self) -> None:
        clean = _payload([], profile_id="1", exhausted=True)
        broken = _UserMediaResponse(
            {"data": {"user": None}, "errors": [{"message": "Rate limit exceeded"}]}
        )

        result, _ = await _fetch_with_payloads([[clean, broken]], since_id="100")

        self.assertEqual(result["tweets"], [])
        self.assertFalse(result["complete"])

    async def test_only_error_responses_fail_closed(self) -> None:
        broken = _UserMediaResponse(
            {"errors": [{"message": "Rate limit exceeded"}]}, status=200
        )
        unauthorized = _UserMediaResponse({"data": {}}, status=401)

        with self.assertRaisesRegex(RuntimeError, "未捕获"):
            await _fetch_with_payloads([[broken, unauthorized]], since_id="100")

    async def test_in_flight_response_is_settled_before_completeness(self) -> None:
        payload = _payload(
            [
                _tweet(
                    "300",
                    author_id="1",
                    screen_name="yunjiu",
                    media=[_photo("https://pbs.twimg.com/media/slow.jpg")],
                )
            ],
            profile_id="1",
            exhausted=True,
        )

        result, page = await _fetch_with_payloads(
            [[_SlowUserMediaResponse(payload, turns=3)]], since_id="100"
        )

        self.assertEqual([item["tweet_id"] for item in result["tweets"]], ["300"])
        self.assertTrue(result["complete"])
        # 响应在首轮就被结算，不必再滚动等待
        self.assertEqual(page.scrolls, 0)


class BrowserQueueSlotTests(unittest.TestCase):
    def test_counts_active_and_releases_on_success(self) -> None:
        async def scenario() -> None:
            async with browser_queue_slot():
                self.assertEqual(queue_stats["active"], 1)
                self.assertEqual(queue_stats["waiting"], 0)

        asyncio.run(scenario())
        self.assertEqual(queue_stats, {"waiting": 0, "active": 0})

    def test_releases_on_exception(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(RuntimeError):
                async with browser_queue_slot():
                    raise RuntimeError("boom")

        asyncio.run(scenario())
        self.assertEqual(queue_stats, {"waiting": 0, "active": 0})

    def test_second_caller_is_reported_as_waiting(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            async def hold() -> None:
                async with browser_queue_slot():
                    started.set()
                    await release.wait()

            first = asyncio.create_task(hold())
            await started.wait()

            async def second() -> None:
                async with browser_queue_slot():
                    pass

            queued = asyncio.create_task(second())
            await asyncio.sleep(0)
            self.assertEqual(queue_stats["active"], 1)
            self.assertEqual(queue_stats["waiting"], 1)

            release.set()
            await asyncio.gather(first, queued)

        asyncio.run(scenario())
        self.assertEqual(queue_stats, {"waiting": 0, "active": 0})


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _state_json(user_id: str) -> str:
    return json.dumps(
        {
            "cookies": [
                {
                    "name": "twid",
                    "value": f"u%3D{user_id}",
                    "domain": ".x.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        }
    )


class UserMediaRouterTests(unittest.TestCase):
    def _post(self, client: TestClient, **overrides):
        data = {
            "state": _state_json("42"),
            "screen_name": "yunjiu",
            "expected_user_id": "42",
            "expected_target_user_id": "1",
        }
        data.update(overrides)
        return client.post("/api/v1/tweet/user_media", data=data)

    def test_requires_mandatory_fields(self) -> None:
        client = _build_client()
        response = client.post(
            "/api/v1/tweet/user_media", data={"state": _state_json("42")}
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_invalid_screen_name_and_since_id(self) -> None:
        client = _build_client()
        self.assertEqual(self._post(client, screen_name="bad name").status_code, 400)
        self.assertEqual(self._post(client, since_id="abc").status_code, 400)

    def test_rejects_out_of_range_limits(self) -> None:
        client = _build_client()
        self.assertEqual(self._post(client, max_tweets="33").status_code, 400)
        self.assertEqual(self._post(client, max_tweets="0").status_code, 400)
        self.assertEqual(self._post(client, max_scrolls="21").status_code, 400)

    def test_rejects_expected_user_id_mismatch(self) -> None:
        client = _build_client()
        response = self._post(client, expected_user_id="999")
        self.assertEqual(response.status_code, 409)

    def test_returns_collected_photo_tweets(self) -> None:
        client = _build_client()
        fake = AsyncMock(
            return_value={
                "target_user_id": "1",
                "newest_id": "300",
                "complete": True,
                "tweets": [
                    {
                        "tweet_id": "300",
                        "text": "hi",
                        "created_at": "2026-08-01T12:00:00Z",
                        "url": "https://x.com/yunjiu/status/300",
                        "author": {
                            "user_id": "1",
                            "screen_name": "yunjiu",
                            "followers_count": 100000,
                        },
                        "media": [
                            {
                                "url": "https://pbs.twimg.com/media/a.jpg?name=orig",
                                "preview_url": "https://pbs.twimg.com/media/a.jpg",
                                "width": 1200,
                                "height": 800,
                            }
                        ],
                    }
                ],
            }
        )
        with patch("src.router.tweet.fetch_user_media", fake):
            response = self._post(client, since_id="299")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["target_user_id"], "1")
        self.assertEqual(body["newest_id"], "300")
        self.assertEqual(body["viewer_user_id"], "42")
        self.assertTrue(body["complete"])
        self.assertEqual(len(body["tweets"]), 1)
        self.assertEqual(fake.await_args.kwargs["since_id"], "299")
        self.assertEqual(fake.await_args.kwargs["max_tweets"], 32)

    def test_identity_failure_is_reported_as_bad_gateway(self) -> None:
        client = _build_client()
        fake = AsyncMock(side_effect=ValueError("无法确认 target_user_id"))
        with patch("src.router.tweet.fetch_user_media", fake):
            response = self._post(client)

        self.assertEqual(response.status_code, 502)

    def test_rejects_fetch_result_with_wrong_target_identity(self) -> None:
        client = _build_client()
        fake = AsyncMock(
            return_value={
                "target_user_id": "999",
                "newest_id": None,
                "complete": False,
                "tweets": [],
            }
        )
        with patch("src.router.tweet.fetch_user_media", fake):
            response = self._post(client)

        self.assertEqual(response.status_code, 502)
        self.assertIn("999", response.json()["detail"])

    def test_rejects_fetch_result_over_requested_response_cap(self) -> None:
        client = _build_client()
        fake = AsyncMock(
            return_value={
                "target_user_id": "1",
                "newest_id": "100",
                "complete": False,
                "tweets": [{"tweet_id": str(index)} for index in range(33)],
            }
        )
        with patch("src.router.tweet.fetch_user_media", fake):
            response = self._post(client)

        self.assertEqual(response.status_code, 502)
        self.assertIn("32", response.json()["detail"])

    def test_does_not_coerce_malformed_complete_value_to_true(self) -> None:
        client = _build_client()
        fake = AsyncMock(
            return_value={
                "target_user_id": "1",
                "newest_id": None,
                "complete": "false",
                "tweets": [],
            }
        )
        with patch("src.router.tweet.fetch_user_media", fake):
            response = self._post(client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["complete"], False)

    def test_rejects_bad_api_key(self) -> None:
        from os import environ

        client = _build_client()
        environ["AUTO_TWEET_API_KEY"] = "secret"
        try:
            response = client.post(
                "/api/v1/tweet/user_media",
                data={
                    "state": _state_json("42"),
                    "screen_name": "yunjiu",
                    "expected_user_id": "42",
                    "expected_target_user_id": "1",
                },
                headers={"X-Auto-Tweet-Key": "wrong"},
            )
        finally:
            environ.pop("AUTO_TWEET_API_KEY", None)
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
