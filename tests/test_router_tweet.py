import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.router.tweet import router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TweetRouterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
