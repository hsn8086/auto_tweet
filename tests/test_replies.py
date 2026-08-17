import json
import unittest
from datetime import datetime, timedelta, timezone

from src.model import State
from src.replies import (
    filter_verified_replies,
    normalize_media,
    parse_datetime,
    parse_graphql_tweets,
    parse_viewer_user_id,
    reached_since,
)


def _state(twid: str, *, domain: str = ".x.com") -> State:
    return State.model_validate(
        {
            "cookies": [
                {
                    "name": "twid",
                    "value": twid,
                    "domain": domain,
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        }
    )


def state_json(user_id: str) -> str:
    return json.dumps(_state(f"u%3D{user_id}").model_dump())


def _tweet(
    tweet_id: str,
    *,
    author_id: str,
    screen_name: str,
    created_at: datetime,
    blue: bool = False,
    verified: bool = False,
    verified_type: str | None = None,
    affiliate: bool = False,
    reply_to_tweet_id: str | None = None,
    reply_to_user_id: str | None = None,
    media: list[dict] | None = None,
) -> dict:
    legacy = {
        "full_text": f"tweet {tweet_id}",
        "created_at": created_at.strftime("%a %b %d %H:%M:%S %z %Y"),
        "conversation_id_str": reply_to_tweet_id or tweet_id,
    }
    if reply_to_tweet_id:
        legacy["in_reply_to_status_id_str"] = reply_to_tweet_id
    if reply_to_user_id:
        legacy["in_reply_to_user_id_str"] = reply_to_user_id
    if media is not None:
        legacy["extended_entities"] = {"media": media}
    user = {
        "rest_id": author_id,
        "is_blue_verified": blue,
        "verified_type": verified_type,
        "legacy": {
            "screen_name": screen_name,
            "name": screen_name.title(),
            "verified": verified,
        },
    }
    if affiliate:
        user["affiliates_highlighted_label"] = {"label": {"description": "Org"}}
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "legacy": legacy,
        "core": {"user_results": {"result": user}},
    }


def _entry(node: dict) -> dict:
    return {
        "entryId": f"tweet-{node.get('rest_id', 'wrapped')}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"tweet_results": {"result": node}},
        },
    }


class TwidTests(unittest.TestCase):
    def test_parse_percent_encoded_twid(self) -> None:
        self.assertEqual(parse_viewer_user_id(_state("u%3D123456")), "123456")
        self.assertEqual(
            parse_viewer_user_id(_state('"u%253D789"', domain=".twitter.com")),
            "789",
        )

    def test_rejects_untrusted_domain_and_conflicting_values(self) -> None:
        with self.assertRaises(ValueError):
            parse_viewer_user_id(_state("u%3D123", domain="notx.com"))
        state = _state("u%3D123")
        state.cookies.append(_state("u%3D456").cookies[0])
        with self.assertRaises(ValueError):
            parse_viewer_user_id(state)


class GraphqlReplyParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)

    def test_multiple_instruction_shapes_visibility_and_unavailable(self) -> None:
        first = _tweet(
            "200",
            author_id="20",
            screen_name="blue",
            created_at=self.now,
            blue=True,
        )
        wrapped = {
            "__typename": "TweetWithVisibilityResults",
            "tweet": _tweet(
                "201",
                author_id="21",
                screen_name="org",
                created_at=self.now,
                verified_type="Business",
            ),
        }
        tombstone = {"__typename": "TweetTombstone", "rest_id": "202"}
        payload = {
            "data": {
                "timeline": {
                    "instructions": [
                        {"type": "TimelineAddEntries", "entries": [_entry(first)]},
                        {"type": "TimelinePinEntry", "entry": _entry(wrapped)},
                        {
                            "type": "TimelineReplaceEntry",
                            "entry": _entry(tombstone),
                        },
                    ]
                }
            }
        }

        tweets = parse_graphql_tweets(payload)

        self.assertEqual({item["tweet_id"] for item in tweets}, {"200", "201"})
        self.assertTrue(all(item["_is_verified"] for item in tweets))
        self.assertEqual(tweets[1]["author"]["verified_type"], "Business")
        self.assertTrue(tweets[0]["author"]["is_verified"])

    def test_timeline_pin_entry_marks_only_pinned_tweet(self) -> None:
        pinned = _tweet(
            "100",
            author_id="10",
            screen_name="target",
            created_at=self.now - timedelta(days=30),
        )
        current = _tweet(
            "101",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        tweets = parse_graphql_tweets(
            {
                "instructions": [
                    {"type": "TimelinePinEntry", "entry": _entry(pinned)},
                    {"type": "TimelineAddEntries", "entries": [_entry(current)]},
                ]
            }
        )

        by_id = {item["tweet_id"]: item for item in tweets}
        self.assertTrue(by_id["100"]["_pinned"])
        self.assertFalse(by_id["101"]["_pinned"])

    def test_timeline_notification_joins_separate_author_and_target(self) -> None:
        reply = _tweet(
            "220",
            author_id="20",
            screen_name="verified",
            created_at=self.now,
            blue=True,
            reply_to_tweet_id="100",
            reply_to_user_id="10",
        )
        user_results = reply["core"]["user_results"]
        source_user = user_results["result"]
        source_user["core"] = {
            "screen_name": source_user["legacy"].pop("screen_name"),
            "name": source_user["legacy"].pop("name"),
        }
        reply["core"] = {
            "user_results": {
                "result": {
                    "__typename": "UserUnavailable",
                    "rest_id": "20",
                }
            }
        }
        payload = {
            "data": {
                "notification_timeline": {
                    "instructions": [
                        {
                            "entries": [
                                {
                                    "content": {
                                        "itemContent": {
                                            "__typename": "TimelineNotification",
                                            "template": {
                                                "from_users": [
                                                    {
                                                        "__typename": "TimelineNotificationUserRef",
                                                        "user": user_results,
                                                    }
                                                ],
                                                "target_objects": [
                                                    {
                                                        "__typename": "TimelineNotificationTweetRef",
                                                        "tweet": {
                                                            "tweet_results": {
                                                                "result": reply
                                                            }
                                                        },
                                                    }
                                                ],
                                            },
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            }
        }

        tweets = parse_graphql_tweets(payload)

        self.assertEqual([item["tweet_id"] for item in tweets], ["220"])
        self.assertEqual(tweets[0]["author"]["user_id"], "20")
        self.assertTrue(tweets[0]["author"]["is_verified"])

    def test_profile_tweet_uses_known_author_fallback(self) -> None:
        tweet = _tweet(
            "221",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        tweet.pop("core")

        tweets = parse_graphql_tweets(
            {"entries": [_entry(tweet)]},
            fallback_author={"user_id": "10", "screen_name": "target"},
        )

        self.assertEqual([item["tweet_id"] for item in tweets], ["221"])
        self.assertEqual(tweets[0]["author"]["user_id"], "10")
        self.assertFalse(tweets[0]["author"]["is_verified"])

    def test_modern_user_object_without_legacy_is_parsed(self) -> None:
        tweet = _tweet(
            "222",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        user = tweet["core"]["user_results"]["result"]
        user.pop("legacy")
        user["core"] = {"screen_name": "Target", "name": "Target Name"}
        user["relationship_counts"] = {"followers": 4321}

        tweets = parse_graphql_tweets({"entries": [_entry(tweet)]})

        self.assertEqual([item["tweet_id"] for item in tweets], ["222"])
        author = tweets[0]["author"]
        self.assertEqual(author["user_id"], "10")
        self.assertEqual(author["screen_name"], "Target")
        self.assertEqual(author["name"], "Target Name")
        self.assertEqual(author["followers_count"], 4321)
        self.assertEqual(tweets[0]["url"], "https://x.com/Target/status/222")

    def test_modern_user_keeps_payload_identity_over_fallback(self) -> None:
        tweet = _tweet(
            "223",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        user = tweet["core"]["user_results"]["result"]
        user.pop("legacy")
        user["core"] = {"screen_name": "Target", "name": "Target Name"}

        tweets = parse_graphql_tweets(
            {"entries": [_entry(tweet)]},
            fallback_author={"user_id": "99", "screen_name": "someone-else"},
        )

        self.assertEqual(tweets[0]["author"]["user_id"], "10")
        self.assertEqual(tweets[0]["author"]["screen_name"], "Target")

    def test_conflicting_fallback_identity_is_rejected(self) -> None:
        tweet = _tweet(
            "224",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        user = tweet["core"]["user_results"]["result"]
        user.pop("legacy")
        # 只有 rest_id、没有任何用户名：fallback 与 payload 的 id 冲突时必须放弃
        tweets = parse_graphql_tweets(
            {"entries": [_entry(tweet)]},
            fallback_author={"user_id": "99", "screen_name": "someone-else"},
        )
        self.assertEqual(tweets, [])

        # id 一致时才允许用 fallback 补出用户名
        tweets = parse_graphql_tweets(
            {"entries": [_entry(tweet)]},
            fallback_author={"user_id": "10", "screen_name": "target"},
        )
        self.assertEqual([item["tweet_id"] for item in tweets], ["224"])
        self.assertEqual(tweets[0]["author"]["screen_name"], "target")

    def test_unresolvable_identity_returns_nothing(self) -> None:
        tweet = _tweet(
            "225",
            author_id="10",
            screen_name="target",
            created_at=self.now,
        )
        tweet["core"]["user_results"]["result"] = {"legacy": {"name": "no id"}}

        self.assertEqual(parse_graphql_tweets({"entries": [_entry(tweet)]}), [])
        self.assertEqual(
            parse_graphql_tweets({"entries": [_entry(tweet)]}, fallback_author=None), []
        )

    def test_legacy_and_affiliate_verification_is_public(self) -> None:
        legacy = parse_graphql_tweets(
            {
                "entries": [
                    _entry(
                        _tweet(
                            "1",
                            author_id="1",
                            screen_name="legacy",
                            created_at=self.now,
                            verified=True,
                        )
                    )
                ]
            }
        )[0]
        affiliate = parse_graphql_tweets(
            {
                "entries": [
                    _entry(
                        _tweet(
                            "2",
                            author_id="2",
                            screen_name="affiliate",
                            created_at=self.now,
                            affiliate=True,
                        )
                    )
                ]
            }
        )[0]

        self.assertTrue(legacy["author"]["is_verified"])
        self.assertTrue(affiliate["author"]["is_verified"])

    def test_media_normalization_selects_best_video_variant(self) -> None:
        media = [
            {
                "type": "video",
                "media_url_https": "https://pbs.example/preview.jpg",
                "original_info": {"width": 1920, "height": 1080},
                "video_info": {
                    "duration_millis": 3210,
                    "variants": [
                        {
                            "content_type": "video/mp4",
                            "bitrate": 256000,
                            "url": "https://video.example/low.mp4",
                        },
                        {
                            "content_type": "video/mp4",
                            "bitrate": 832000,
                            "url": "https://video.example/high.mp4",
                        },
                    ],
                },
            }
        ]

        result = normalize_media({"extended_entities": {"media": media}})

        self.assertEqual(
            result,
            [
                {
                    "type": "video",
                    "url": "https://video.example/high.mp4",
                    "preview_url": "https://pbs.example/preview.jpg",
                    "width": 1920,
                    "height": 1080,
                    "duration_ms": 3210,
                }
            ],
        )

    def test_verified_direct_parent_window_and_since_filters(self) -> None:
        parent = _tweet(
            "100",
            author_id="10",
            screen_name="target",
            created_at=self.now - timedelta(hours=2),
        )
        accepted = _tweet(
            "205",
            author_id="20",
            screen_name="blue",
            created_at=self.now - timedelta(minutes=5),
            blue=True,
            reply_to_tweet_id="100",
            reply_to_user_id="10",
        )
        unverified = _tweet(
            "206",
            author_id="21",
            screen_name="plain",
            created_at=self.now - timedelta(minutes=4),
            reply_to_tweet_id="100",
            reply_to_user_id="10",
        )
        indirect = _tweet(
            "207",
            author_id="22",
            screen_name="org",
            created_at=self.now - timedelta(minutes=3),
            affiliate=True,
            reply_to_tweet_id="100",
            reply_to_user_id="999",
        )
        old_parent = _tweet(
            "101",
            author_id="10",
            screen_name="target",
            created_at=self.now - timedelta(hours=49),
        )
        old_parent_reply = _tweet(
            "208",
            author_id="23",
            screen_name="legacyverified",
            created_at=self.now - timedelta(minutes=2),
            verified=True,
            reply_to_tweet_id="101",
            reply_to_user_id="10",
        )
        tweets = parse_graphql_tweets(
            {
                "instructions": [
                    {
                        "type": "TimelineAddEntries",
                        "entries": [
                            _entry(parent),
                            _entry(accepted),
                            _entry(unverified),
                            _entry(indirect),
                            _entry(old_parent),
                            _entry(old_parent_reply),
                        ],
                    }
                ]
            }
        )

        result = filter_verified_replies(
            tweets,
            expected_user_id="10",
            parent_window_hours=48,
            since_id="204",
            since_time=self.now - timedelta(hours=1),
            now=self.now,
        )

        self.assertEqual([item["tweet_id"] for item in result], ["205"])
        self.assertEqual(result[0]["parent"]["tweet_id"], "100")
        self.assertNotIn("_is_verified", result[0])
        self.assertTrue(
            reached_since(
                tweets,
                since_id="205",
                since_time=parse_datetime("2026-07-10T11:00:00Z"),
            )
        )

    def test_parent_map_tweets_are_not_reply_candidates(self) -> None:
        parent = _tweet(
            "100",
            author_id="10",
            screen_name="target",
            created_at=self.now - timedelta(hours=2),
        )
        profile_reply = _tweet(
            "201",
            author_id="10",
            screen_name="target",
            created_at=self.now - timedelta(hours=1),
            blue=True,
            reply_to_tweet_id="100",
            reply_to_user_id="10",
        )
        notification_reply = _tweet(
            "202",
            author_id="20",
            screen_name="verified",
            created_at=self.now - timedelta(minutes=5),
            verified=True,
            reply_to_tweet_id="100",
            reply_to_user_id="10",
        )
        tweets = parse_graphql_tweets(
            {
                "entries": [
                    _entry(parent),
                    _entry(profile_reply),
                    _entry(notification_reply),
                ]
            }
        )

        result = filter_verified_replies(
            tweets,
            expected_user_id="10",
            now=self.now,
            candidate_reply_ids={"202"},
        )

        self.assertEqual([item["tweet_id"] for item in result], ["202"])


if __name__ == "__main__":
    unittest.main()
