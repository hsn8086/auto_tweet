import unittest

from src.sender import (
    is_user_profile_response,
    is_user_tweets_response,
    parse_tweet_created_at,
    parse_tweet_metrics_payload,
    parse_user_tweets_payload,
)


def _tweet_node(tweet_id: str, views: int = 100, likes: int = 5, **legacy_extra):
    legacy = {
        "favorite_count": likes,
        "retweet_count": 2,
        "reply_count": 1,
        "quote_count": 0,
        "bookmark_count": 3,
        "created_at": "Wed Jun 10 08:00:00 +0000 2026",
    }
    legacy.update(legacy_extra)
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "legacy": legacy,
        "views": {"count": str(views)},
    }


def _entry(node):
    return {
        "entryId": f"tweet-{node.get('rest_id', 'x')}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"tweet_results": {"result": node}},
        },
    }


def _cursor_entry():
    return {
        "entryId": "cursor-bottom-1",
        "content": {"entryType": "TimelineTimelineCursor", "value": "abc"},
    }


def _user_tweets_payload(instructions):
    return {
        "data": {
            "user": {
                "result": {"timeline": {"timeline": {"instructions": instructions}}}
            }
        }
    }


class ParseUserTweetsPayloadTests(unittest.TestCase):
    def test_parses_add_entries_with_cursor_skipped(self):
        payload = _user_tweets_payload(
            [
                {
                    "type": "TimelineAddEntries",
                    "entries": [
                        _entry(_tweet_node("111", views=500)),
                        _cursor_entry(),
                        _entry(_tweet_node("222", views=10)),
                    ],
                }
            ]
        )
        result = parse_user_tweets_payload(payload)
        self.assertEqual([t["tweet_id"] for t in result], ["111", "222"])
        self.assertEqual(result[0]["views"], 500)
        self.assertFalse(result[0]["pinned"])

    def test_pinned_entry_marked(self):
        payload = _user_tweets_payload(
            [
                {"type": "TimelinePinEntry", "entry": _entry(_tweet_node("999"))},
                {
                    "type": "TimelineAddEntries",
                    "entries": [_entry(_tweet_node("111"))],
                },
            ]
        )
        result = parse_user_tweets_payload(payload)
        by_id = {t["tweet_id"]: t for t in result}
        self.assertTrue(by_id["999"]["pinned"])
        self.assertFalse(by_id["111"]["pinned"])

    def test_retweet_skipped(self):
        rt = _tweet_node("333", retweeted_status_result={"result": {}})
        payload = _user_tweets_payload(
            [{"type": "TimelineAddEntries", "entries": [_entry(rt)]}]
        )
        self.assertEqual(parse_user_tweets_payload(payload), [])

    def test_visibility_wrapped_node(self):
        wrapped = {
            "__typename": "TweetWithVisibilityResults",
            "tweet": _tweet_node("444", views=42),
        }
        payload = _user_tweets_payload(
            [{"type": "TimelineAddEntries", "entries": [_entry(wrapped)]}]
        )
        result = parse_user_tweets_payload(payload)
        self.assertEqual(result[0]["tweet_id"], "444")
        self.assertEqual(result[0]["views"], 42)

    def test_duplicate_tweet_ids_deduped(self):
        payload = _user_tweets_payload(
            [
                {"type": "TimelinePinEntry", "entry": _entry(_tweet_node("111"))},
                {
                    "type": "TimelineAddEntries",
                    "entries": [_entry(_tweet_node("111"))],
                },
            ]
        )
        self.assertEqual(len(parse_user_tweets_payload(payload)), 1)

    def test_timeline_v2_shape(self):
        payload = {
            "data": {
                "user": {
                    "result": {
                        "timeline_v2": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "type": "TimelineAddEntries",
                                        "entries": [_entry(_tweet_node("555"))],
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        result = parse_user_tweets_payload(payload)
        self.assertEqual(result[0]["tweet_id"], "555")

    def test_garbage_payloads(self):
        self.assertEqual(parse_user_tweets_payload(None), [])
        self.assertEqual(parse_user_tweets_payload({}), [])
        self.assertEqual(parse_user_tweets_payload({"data": {"user": None}}), [])


class ParseTweetCreatedAtTests(unittest.TestCase):
    def test_valid(self):
        ts = parse_tweet_created_at("Wed Jun 10 08:00:00 +0000 2026")
        assert ts is not None
        self.assertEqual((ts.year, ts.month, ts.day, ts.hour), (2026, 6, 10, 8))

    def test_invalid(self):
        self.assertIsNone(parse_tweet_created_at(""))
        self.assertIsNone(parse_tweet_created_at("not a date"))


class IsUserTweetsResponseTests(unittest.TestCase):
    class _FakeRequest:
        def __init__(self, method):
            self.method = method

    class _FakeResponse:
        def __init__(self, url, method="GET"):
            self.url = url
            self.request = IsUserTweetsResponseTests._FakeRequest(method)

    def test_matches_user_tweets_get(self):
        resp = self._FakeResponse("https://x.com/i/api/graphql/abc/UserTweets?x=1")
        self.assertTrue(is_user_tweets_response(resp))

    def test_matches_user_tweets_and_replies(self):
        resp = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/UserTweetsAndReplies?x=1"
        )
        self.assertTrue(is_user_tweets_response(resp))

    def test_matches_every_known_profile_timeline_operation(self):
        # 后三个 operation 不是 "UserTweets" 的子串；只匹配子串会在 X 改版后
        # 一条时间线响应都收不到。
        for operation in (
            "UserTweets",
            "UserWithProfileTweetsQueryV2",
            "UserWithProfileTweetsAndRepliesQueryV2",
            "UserOriginalsTimeline",
        ):
            with self.subTest(operation=operation):
                resp = self._FakeResponse(
                    f"https://x.com/i/api/graphql/abc/{operation}?variables=%7B%7D"
                )
                self.assertTrue(is_user_tweets_response(resp))

    def test_rejects_other_operations(self):
        resp = self._FakeResponse("https://x.com/i/api/graphql/abc/TweetDetail")
        self.assertFalse(is_user_tweets_response(resp))

    def test_operation_name_in_query_does_not_match(self):
        resp = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/OtherOperation?"
            "variables=%7B%22hint%22%3A%22UserOriginalsTimeline%22%7D"
        )
        self.assertFalse(is_user_tweets_response(resp))

    def test_rejects_unrelated_profile_operations(self):
        for url in (
            "https://x.com/i/api/graphql/abc/UserByScreenName?x=1",
            "https://x.com/i/api/graphql/abc/UserMedia?x=1",
            "https://x.com/i/api/2/notifications/all.json",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_user_tweets_response(self._FakeResponse(url)))

    def test_rejects_post(self):
        resp = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/UserTweets", method="POST"
        )
        self.assertFalse(is_user_tweets_response(resp))

    def test_matches_user_by_screen_name_profile_response(self):
        resp = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/UserByScreenName?variables=%7B%7D"
        )
        self.assertTrue(is_user_profile_response(resp))

    def test_profile_response_rejects_post_and_other_operations(self):
        post = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/UserByScreenName", method="POST"
        )
        other = self._FakeResponse(
            "https://x.com/i/api/graphql/abc/UserOriginalsTimeline"
        )
        self.assertFalse(is_user_profile_response(post))
        self.assertFalse(is_user_profile_response(other))


class TweetMetricsPayloadRegressionTests(unittest.TestCase):
    """重构提取 _metrics_from_tweet_node 后，原单条解析行为不变。"""

    def test_tweet_result_by_rest_id_shape(self):
        payload = {"data": {"tweetResult": {"result": _tweet_node("777", views=9)}}}
        metrics = parse_tweet_metrics_payload(payload)
        self.assertEqual(metrics["tweet_id"], "777")
        self.assertEqual(metrics["views"], 9)
        self.assertEqual(metrics["likes"], 5)
        self.assertEqual(metrics["bookmarks"], 3)

    def test_thread_shape(self):
        payload = {
            "data": {
                "threaded_conversation_with_injections_v2": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [_entry(_tweet_node("888"))],
                        }
                    ]
                }
            }
        }
        self.assertEqual(parse_tweet_metrics_payload(payload)["tweet_id"], "888")


if __name__ == "__main__":
    unittest.main()
