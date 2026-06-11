import unittest
from os import environ
from unittest.mock import ANY
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.router.tweet import router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TweetRouterTests(unittest.TestCase):
    def tearDown(self) -> None:
        environ.pop("AUTO_TWEET_API_KEY", None)

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_returns_retryable_detail(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = TimeoutError("Timed out opening x.com home")
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            params={"state": '{"cookies": []}', "context": "hello", "spoiler": False},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Timed out opening x.com home")

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_returns_warning_when_post_sent(
        self, mock_send: AsyncMock
    ) -> None:
        from src.model import PostSentError

        mock_send.side_effect = PostSentError("post sent but screenshot failed")
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            params={"state": '{"cookies": []}', "context": "hello", "spoiler": False},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "warning": "post sent but screenshot failed"},
        )

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_returns_tweet_id_when_captured(
        self, mock_send: AsyncMock
    ) -> None:
        mock_send.return_value = "1234567890"
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "context": "hello"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "tweet_id": "1234567890"},
        )

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_returns_warning_and_tweet_id_when_post_sent(
        self, mock_send: AsyncMock
    ) -> None:
        from src.model import PostSentError

        mock_send.side_effect = PostSentError(
            "post sent but screenshot failed", tweet_id="555"
        )
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            params={"state": '{"cookies": []}', "context": "hello"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "warning": "post sent but screenshot failed",
                "tweet_id": "555",
            },
        )

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_accepts_form_fields(self, mock_send: AsyncMock) -> None:
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "context": "hello", "spoiler": "true"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        mock_send.assert_awaited_once_with(
            "hello",
            ANY,
            media=ANY,
            proxy=ANY,
            spoiler=True,
            made_with_ai=False,
            headless=True,
        )

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_accepts_made_with_ai_form_field(
        self, mock_send: AsyncMock
    ) -> None:
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={
                "state": '{"cookies": []}',
                "context": "hello",
                "made_with_ai": "true",
            },
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        mock_send.assert_awaited_once_with(
            "hello",
            ANY,
            media=ANY,
            proxy=ANY,
            spoiler=False,
            made_with_ai=True,
            headless=True,
        )

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_requires_api_key_when_configured(
        self, mock_send: AsyncMock
    ) -> None:
        environ["AUTO_TWEET_API_KEY"] = "secret"
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}'},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 401)
        mock_send.assert_not_awaited()

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_accepts_correct_api_key(self, mock_send: AsyncMock) -> None:
        environ["AUTO_TWEET_API_KEY"] = "secret"
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            headers={"X-Auto-Tweet-Key": "secret"},
            data={"state": '{"cookies": []}'},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 200)
        mock_send.assert_awaited_once()

    @patch("src.router.tweet.SEND_TIMEOUT_SECONDS", 0.01)
    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_post_tweet_times_out_stuck_send(self, mock_send: AsyncMock) -> None:
        import asyncio

        async def slow_send(*args, **kwargs) -> None:
            await asyncio.sleep(1)

        mock_send.side_effect = slow_send
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}'},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()


class TweetMetricsRouterTests(unittest.TestCase):
    def tearDown(self) -> None:
        environ.pop("AUTO_TWEET_API_KEY", None)

    @patch("src.sender.fetch_tweet_metrics", new_callable=AsyncMock)
    def test_metrics_returns_payload(self, mock_metrics: AsyncMock) -> None:
        mock_metrics.return_value = {
            "tweet_id": "1234567890",
            "likes": 56,
            "retweets": 7,
            "replies": 2,
            "quotes": 1,
            "bookmarks": 4,
            "views": 1234,
            "created_at": "Mon May 26 12:34:56 +0000 2026",
        }
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/metrics",
            data={"state": '{"cookies": []}', "tweet_id": "1234567890"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["likes"], 56)
        self.assertEqual(body["views"], 1234)

    @patch("src.sender.fetch_tweet_metrics", new_callable=AsyncMock)
    def test_metrics_requires_state_and_tweet_id(self, mock_metrics: AsyncMock) -> None:
        client = _build_client()
        response = client.post("/api/v1/tweet/metrics", data={"tweet_id": "1"})
        self.assertEqual(response.status_code, 400)
        mock_metrics.assert_not_awaited()

    @patch("src.sender.fetch_tweet_metrics", new_callable=AsyncMock)
    def test_metrics_returns_502_on_send_error(self, mock_metrics: AsyncMock) -> None:
        mock_metrics.side_effect = RuntimeError("Tweet metrics payload missing data")
        client = _build_client()
        response = client.post(
            "/api/v1/tweet/metrics",
            data={"state": '{"cookies": []}', "tweet_id": "1"},
        )
        self.assertEqual(response.status_code, 502)


class TweetReconcileTests(unittest.TestCase):
    """request_id 幂等 + 结果对账接口."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        environ["DATA_DIR"] = self._tmp.name
        # 隔离模块级 store 缓存
        from src import result_store

        result_store._stores.clear()

    def tearDown(self) -> None:
        environ.pop("DATA_DIR", None)
        environ.pop("AUTO_TWEET_API_KEY", None)
        self._tmp.cleanup()

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_success_result_is_stored_and_queryable(
        self, mock_send: AsyncMock
    ) -> None:
        mock_send.return_value = "9876"
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-ok-1"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)

        result = client.get("/api/v1/tweet/result/req-ok-1")
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["tweet_id"], "9876")

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_duplicate_request_id_replays_without_resend(
        self, mock_send: AsyncMock
    ) -> None:
        mock_send.return_value = "111"
        client = _build_client()

        first = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-dup"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(mock_send.await_count, 1)

        second = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-dup"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json(),
            {"status": "ok", "replayed": True, "tweet_id": "111"},
        )
        self.assertEqual(mock_send.await_count, 1)

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_failed_request_id_allows_retry(self, mock_send: AsyncMock) -> None:
        mock_send.side_effect = [TimeoutError("boom"), "222"]
        client = _build_client()

        first = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-retry"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(first.status_code, 502)
        result = client.get("/api/v1/tweet/result/req-retry")
        self.assertEqual(result.json()["status"], "failed")

        second = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-retry"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"status": "ok", "tweet_id": "222"})
        self.assertEqual(mock_send.await_count, 2)

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_sent_unconfirmed_is_stored_with_warning(
        self, mock_send: AsyncMock
    ) -> None:
        from src.model import PostSentError

        mock_send.side_effect = PostSentError("screenshot failed", tweet_id="333")
        client = _build_client()

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-warn"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)
        result = client.get("/api/v1/tweet/result/req-warn").json()
        self.assertEqual(result["status"], "sent_unconfirmed")
        self.assertEqual(result["tweet_id"], "333")
        self.assertIn("screenshot failed", result["warning"])

    def test_unknown_request_id_returns_404(self) -> None:
        client = _build_client()
        response = client.get("/api/v1/tweet/result/never-seen")
        self.assertEqual(response.status_code, 404)

    def test_invalid_request_id_rejected(self) -> None:
        client = _build_client()
        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "bad id with spaces"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 400)

    @patch("src.router.tweet.send", new_callable=AsyncMock)
    def test_running_request_id_returns_409(self, mock_send: AsyncMock) -> None:
        from src import result_store as rs

        client = _build_client()
        store = rs.get_result_store(environ["DATA_DIR"] + "/tweet_results")
        store.record("req-running", rs.STATUS_RUNNING)

        response = client.post(
            "/api/v1/tweet/post",
            data={"state": '{"cookies": []}', "request_id": "req-running"},
            files={"images": ("test.jpg", b"img", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 409)
        mock_send.assert_not_awaited()

    def test_queue_stats_endpoint(self) -> None:
        client = _build_client()
        response = client.get("/api/v1/tweet/queue")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("waiting", body)
        self.assertIn("active", body)
