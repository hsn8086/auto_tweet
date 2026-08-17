import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from loguru import logger

from .model import State

_TWID_RE = re.compile(r"u=(\d+)")
_UNAVAILABLE_TYPENAMES = {
    "TweetTombstone",
    "TweetUnavailable",
    "TweetDeleted",
}


def parse_viewer_user_id(state: State) -> str:
    """Return the authenticated X user id without exposing cookie contents."""
    user_ids: set[str] = set()
    for cookie in state.cookies:
        domain = cookie.domain.lower().lstrip(".")
        if cookie.name != "twid" or not (
            domain == "x.com"
            or domain.endswith(".x.com")
            or domain == "twitter.com"
            or domain.endswith(".twitter.com")
        ):
            continue
        value = cookie.value.strip().strip('"')
        for _ in range(2):
            decoded = unquote(value).strip().strip('"')
            if decoded == value:
                break
            value = decoded
        match = _TWID_RE.fullmatch(value)
        if match:
            user_ids.add(match.group(1))
    if len(user_ids) != 1:
        raise ValueError("state 中缺少唯一且有效的 twid")
    return next(iter(user_ids))


def parse_datetime(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unwrap_tweet_node(candidate: Any) -> dict[str, Any] | None:
    node = candidate
    while isinstance(node, dict):
        typename = str(node.get("__typename") or "")
        if typename in _UNAVAILABLE_TYPENAMES or "Tombstone" in typename:
            return None
        if typename == "TweetWithVisibilityResults":
            node = node.get("tweet")
            continue
        if "result" in node and not isinstance(node.get("legacy"), dict):
            node = node.get("result")
            continue
        break
    if not isinstance(node, dict) or not isinstance(node.get("legacy"), dict):
        return None
    return node


def _is_pinned_container(value: dict[str, Any]) -> bool:
    entry_id = str(value.get("entryId") or "").lower()
    return value.get("type") == "TimelinePinEntry" or entry_id.startswith(
        ("pin-", "pinned-")
    )


def _iter_tweet_candidates(
    value: Any, *, pinned: bool = False
) -> Iterator[tuple[dict[str, Any], bool]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_tweet_candidates(item, pinned=pinned)
        return
    if not isinstance(value, dict):
        return
    pinned = pinned or _is_pinned_container(value)
    if "rest_id" in value and isinstance(value.get("legacy"), dict):
        yield value, pinned
    elif value.get("__typename") == "TweetWithVisibilityResults":
        wrapped = _unwrap_tweet_node(value)
        if wrapped is not None:
            yield wrapped, pinned
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_tweet_candidates(child, pinned=pinned)


def _iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)
        return
    if not isinstance(value, dict):
        return
    yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_dicts(child)


def _notification_tweet_candidates(payload: Any) -> Iterator[dict[str, Any]]:
    """Join TimelineNotification target tweets with their separate author objects."""
    for container in _iter_dicts(payload):
        template = container.get("template")
        if not isinstance(template, dict):
            continue
        raw_users = template.get("from_users")
        raw_targets = template.get("target_objects")
        if not isinstance(raw_users, list) or not isinstance(raw_targets, list):
            continue
        users: dict[str, dict[str, Any]] = {}
        for raw_user in raw_users:
            if not isinstance(raw_user, dict):
                continue
            user_ref = raw_user.get("user", raw_user)
            if not isinstance(user_ref, dict):
                continue
            user_results = user_ref.get("user_results", user_ref)
            user = _unwrap_user(user_results)
            user_id = _string_id(user.get("rest_id")) if user else None
            if user_id:
                users[user_id] = user_results

        for target in raw_targets:
            if not isinstance(target, dict):
                continue
            tweet_ref = target.get("tweet", target)
            if not isinstance(tweet_ref, dict):
                continue
            tweet_results = tweet_ref.get("tweet_results", tweet_ref)
            node = _unwrap_tweet_node(tweet_results)
            if node is None:
                continue
            core = node.get("core")
            embedded_id: str | None = None
            if isinstance(core, dict):
                embedded_user_results = core.get("user_results")
                embedded_user = _unwrap_user(embedded_user_results)
                embedded_legacy = (
                    embedded_user.get("legacy")
                    if isinstance(embedded_user, dict)
                    else None
                )
                embedded_id = (
                    _string_id(embedded_user.get("rest_id"))
                    if isinstance(embedded_user, dict)
                    else _wrapped_rest_id(embedded_user_results)
                )
                embedded_screen_name = (
                    str(embedded_legacy.get("screen_name") or "").strip()
                    if isinstance(embedded_legacy, dict)
                    else ""
                )
                if embedded_id and embedded_screen_name:
                    yield node
                    continue
            legacy = node.get("legacy")
            author_id = embedded_id or (
                _string_id(legacy.get("user_id_str") or legacy.get("user_id"))
                if isinstance(legacy, dict)
                else None
            )
            user_results = users.get(author_id or "")
            if user_results is None and len(users) == 1:
                user_results = next(iter(users.values()))
            if user_results is None:
                continue
            joined = dict(node)
            joined["core"] = {"user_results": user_results}
            yield joined


def _unwrap_user(candidate: Any) -> dict[str, Any] | None:
    node = candidate
    while isinstance(node, dict):
        typename = str(node.get("__typename") or "")
        if "Unavailable" in typename or "Tombstone" in typename:
            return None
        if typename == "UserWithVisibilityResults":
            node = node.get("user")
            continue
        if "result" in node and not isinstance(node.get("legacy"), dict):
            node = node.get("result")
            continue
        break
    return node if isinstance(node, dict) else None


def _string_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value).strip()
    return result or None


def _wrapped_rest_id(candidate: Any) -> str | None:
    node = candidate
    while isinstance(node, dict):
        rest_id = _string_id(node.get("rest_id"))
        if rest_id:
            return rest_id
        next_node = node.get("result")
        if not isinstance(next_node, dict):
            next_node = node.get("user")
        if not isinstance(next_node, dict):
            return None
        node = next_node
    return None


def _media_dimensions(item: dict[str, Any]) -> tuple[int | None, int | None]:
    original = item.get("original_info")
    if isinstance(original, dict):
        width = original.get("width")
        height = original.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    sizes = item.get("sizes")
    if isinstance(sizes, dict):
        for key in ("large", "medium", "small", "thumb"):
            size = sizes.get(key)
            if isinstance(size, dict):
                width = size.get("w")
                height = size.get("h")
                if isinstance(width, int) and isinstance(height, int):
                    return width, height
    return None, None


def normalize_media(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    extended = legacy.get("extended_entities")
    entities = legacy.get("entities")
    raw_media: Any = None
    if isinstance(extended, dict):
        raw_media = extended.get("media")
    if not isinstance(raw_media, list) and isinstance(entities, dict):
        raw_media = entities.get("media")
    if not isinstance(raw_media, list):
        return []

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_media:
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type") or "photo")
        preview_url = str(item.get("media_url_https") or item.get("media_url") or "")
        url = preview_url
        video_info = item.get("video_info")
        if isinstance(video_info, dict):
            variants = video_info.get("variants")
            if isinstance(variants, list):
                mp4_variants = [
                    variant
                    for variant in variants
                    if isinstance(variant, dict)
                    and variant.get("content_type") == "video/mp4"
                    and isinstance(variant.get("url"), str)
                ]
                if mp4_variants:
                    best = max(
                        mp4_variants,
                        key=lambda variant: (
                            variant.get("bitrate")
                            if isinstance(variant.get("bitrate"), int)
                            else -1
                        ),
                    )
                    url = str(best["url"])
        if not url:
            continue
        key = (media_type, url)
        if key in seen:
            continue
        seen.add(key)
        width, height = _media_dimensions(item)
        duration = (
            video_info.get("duration_millis") if isinstance(video_info, dict) else None
        )
        results.append(
            {
                "type": media_type,
                "url": url,
                "preview_url": preview_url or url,
                "width": width,
                "height": height,
                "duration_ms": duration if isinstance(duration, int) else None,
            }
        )
    return results


def normalize_tweet(
    candidate: Any, *, fallback_author: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    node = _unwrap_tweet_node(candidate)
    if node is None:
        return None
    tweet_id = _string_id(node.get("rest_id"))
    legacy = node.get("legacy")
    if tweet_id is None or not isinstance(legacy, dict):
        return None

    core = node.get("core")
    user_results = core.get("user_results") if isinstance(core, dict) else None
    user = _unwrap_user(user_results)
    user_legacy = user.get("legacy") if isinstance(user, dict) else None
    user_core = user.get("core") if isinstance(user, dict) else None
    # X 的现代 user 对象没有 legacy：用户名/昵称在 core，粉丝数在
    # relationship_counts。缺 legacy 不代表缺身份，不能直接跳到 fallback。
    if isinstance(user, dict):
        user_fields = user_legacy if isinstance(user_legacy, dict) else {}
        user_id = _string_id(user.get("rest_id"))
        screen_name = (
            str(
                user_fields.get("screen_name")
                or (user_core.get("screen_name") if isinstance(user_core, dict) else "")
                or ""
            )
            .strip()
            .lstrip("@")
        )
    else:
        user_fields = {}
        user_id = None
        screen_name = ""
    if (user_id is None or not screen_name) and fallback_author is not None:
        fallback_id = _string_id(fallback_author.get("user_id"))
        fallback_name = (
            str(fallback_author.get("screen_name") or "").strip().lstrip("@")
        )
        # fallback 只能补缺，不能覆盖 payload 自己给出的身份。
        if user_id is not None and fallback_id is not None and user_id != fallback_id:
            return None
        if (
            screen_name
            and fallback_name
            and screen_name.casefold() != fallback_name.casefold()
        ):
            return None
        user_id = user_id or fallback_id
        screen_name = screen_name or fallback_name
        user_fields = {
            **user_fields,
            "name": user_fields.get("name") or fallback_author.get("name"),
            "followers_count": user_fields.get("followers_count")
            or fallback_author.get("followers_count"),
        }
    if user_id is None or not screen_name:
        return None

    verification = user.get("verification") if isinstance(user, dict) else None
    verified_type = (
        user.get("verified_type") or user_fields.get("verified_type")
        if isinstance(user, dict)
        else None
    )
    if not verified_type and isinstance(verification, dict):
        verified_type = verification.get("verified_type")
    affiliate = (
        user.get("affiliates_highlighted_label") if isinstance(user, dict) else None
    )
    is_blue_verified = (
        bool(user.get("is_blue_verified")) if isinstance(user, dict) else False
    )
    is_verified = bool(
        is_blue_verified
        or (isinstance(user, dict) and user.get("verified"))
        or user_fields.get("verified")
        or (isinstance(verification, dict) and verification.get("verified"))
        or verified_type
        or (isinstance(affiliate, dict) and affiliate)
    )
    followers_count = user_fields.get("followers_count")
    if not isinstance(followers_count, int) or isinstance(followers_count, bool):
        relationship_counts = user.get("relationship_counts") if user else None
        if isinstance(relationship_counts, dict):
            followers_count = relationship_counts.get("followers")
    if not isinstance(followers_count, int) or isinstance(followers_count, bool):
        followers_count = None
    created = parse_datetime(str(legacy.get("created_at") or ""))
    created_at = created.isoformat().replace("+00:00", "Z") if created else ""
    text = legacy.get("full_text") or legacy.get("text") or ""
    note_tweet = node.get("note_tweet")
    if isinstance(note_tweet, dict):
        note_results = note_tweet.get("note_tweet_results")
        note_result = (
            note_results.get("result") if isinstance(note_results, dict) else None
        )
        if isinstance(note_result, dict) and isinstance(note_result.get("text"), str):
            text = note_result["text"]
    return {
        "tweet_id": tweet_id,
        "text": str(text),
        "created_at": created_at,
        "url": f"https://x.com/{screen_name}/status/{tweet_id}",
        "in_reply_to_tweet_id": _string_id(
            legacy.get("in_reply_to_status_id_str")
            or legacy.get("in_reply_to_status_id")
        ),
        "in_reply_to_user_id": _string_id(
            legacy.get("in_reply_to_user_id_str") or legacy.get("in_reply_to_user_id")
        ),
        "conversation_id": _string_id(
            legacy.get("conversation_id_str") or legacy.get("conversation_id")
        ),
        "author": {
            "user_id": user_id,
            "screen_name": screen_name,
            "name": str(
                user_fields.get("name")
                or (user_core.get("name") if isinstance(user_core, dict) else "")
                or ""
            ),
            "is_blue_verified": is_blue_verified,
            "is_verified": is_verified,
            "verified_type": str(verified_type) if verified_type else None,
            "followers_count": followers_count,
        },
        "media": normalize_media(legacy),
        "_is_verified": is_verified,
        "_is_retweet": "retweeted_status_result" in legacy,
    }


def parse_graphql_tweets(
    payload: Any, *, fallback_author: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Extract normalized tweets from timeline instructions of any known shape."""
    if not isinstance(payload, dict):
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate, pinned in _iter_tweet_candidates(payload):
        tweet = normalize_tweet(candidate, fallback_author=fallback_author)
        if tweet is None:
            continue
        if tweet["tweet_id"] in seen:
            if pinned:
                for existing in results:
                    if existing["tweet_id"] == tweet["tweet_id"]:
                        existing["_pinned"] = True
                        break
            continue
        tweet["_pinned"] = pinned
        seen.add(tweet["tweet_id"])
        results.append(tweet)
    for candidate in _notification_tweet_candidates(payload):
        tweet = normalize_tweet(candidate, fallback_author=fallback_author)
        if tweet is None or tweet["tweet_id"] in seen:
            continue
        tweet["_pinned"] = False
        seen.add(tweet["tweet_id"])
        results.append(tweet)
    return results


def _is_after_since(
    tweet: dict[str, Any], *, since_id: str | None, since_time: datetime | None
) -> bool:
    tweet_id = str(tweet.get("tweet_id") or "")
    if since_id:
        try:
            if int(tweet_id) <= int(since_id):
                return False
        except ValueError:
            if tweet_id == since_id:
                return False
    if since_time is not None:
        created_at = parse_datetime(str(tweet.get("created_at") or ""))
        if created_at is None or created_at <= since_time:
            return False
    return True


def reached_since(
    tweets: list[dict[str, Any]],
    *,
    since_id: str | None,
    since_time: datetime | None,
) -> bool:
    if not since_id and since_time is None:
        return False
    return any(
        not _is_after_since(tweet, since_id=since_id, since_time=since_time)
        for tweet in tweets
    )


def newest_tweet_id(tweets: list[dict[str, Any]]) -> str | None:
    ids = [str(tweet.get("tweet_id") or "") for tweet in tweets]
    ids = [tweet_id for tweet_id in ids if tweet_id]
    if not ids:
        return None
    try:
        return max(ids, key=int)
    except ValueError:
        return ids[0]


def _original_photo_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
        hostname == "pbs.twimg.com" or hostname.endswith(".twimg.com")
    ):
        return url
    query = [(key, val) for key, val in parse_qsl(parts.query) if key != "name"]
    query.append(("name", "orig"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def filter_user_media_tweets(
    tweets: list[dict[str, Any]],
    *,
    screen_name: str,
    target_user_id: str | None = None,
    since_id: str | None = None,
    since_time: datetime | None = None,
    max_tweets: int = 32,
) -> dict[str, Any]:
    """Return photo tweets authored by one verified target profile."""
    name = screen_name.strip().lstrip("@")
    verified_user_id = _string_id(target_user_id)
    if target_user_id is not None and verified_user_id is None:
        raise ValueError("target_user_id 必须有效")

    matching: list[dict[str, Any]] = []
    matching_user_ids: set[str] = set()
    for tweet in tweets:
        author = tweet.get("author")
        if not isinstance(author, dict):
            continue
        author_name = str(author.get("screen_name") or "").strip().lstrip("@")
        user_id = _string_id(author.get("user_id"))
        if verified_user_id is not None:
            if author_name.casefold() == name.casefold() and user_id not in (
                None,
                verified_user_id,
            ):
                raise ValueError(
                    f"@{name} 的时间线推文作者 user_id={user_id} "
                    f"与已验证身份 {verified_user_id} 不一致"
                )
            if user_id != verified_user_id:
                continue
        else:
            if author_name.casefold() != name.casefold():
                continue
            if user_id is not None:
                matching_user_ids.add(user_id)
        matching.append(tweet)

    if verified_user_id is None:
        if not matching or not matching_user_ids:
            raise ValueError(
                f"无法从 @{name} 的时间线确认 target_user_id；"
                "请确认账号存在且时间线可访问"
            )
        if len(matching_user_ids) != 1:
            raise ValueError(
                f"@{name} 的时间线返回了多个 target_user_id；拒绝返回可能混入的数据"
            )
        verified_user_id = next(iter(matching_user_ids))

    results: list[dict[str, Any]] = []
    for tweet in matching:
        if tweet.get("_is_retweet") or not _is_after_since(
            tweet, since_id=since_id, since_time=since_time
        ):
            continue
        photos: list[dict[str, Any]] = []
        media = tweet.get("media")
        if isinstance(media, list):
            for item in media:
                if (
                    not isinstance(item, dict)
                    or str(item.get("type")).lower() != "photo"
                ):
                    continue
                url = _original_photo_url(item.get("url"))
                if not url:
                    continue
                photos.append(
                    {
                        "url": url,
                        "preview_url": str(item.get("preview_url") or url),
                        "width": item.get("width")
                        if isinstance(item.get("width"), int)
                        else None,
                        "height": item.get("height")
                        if isinstance(item.get("height"), int)
                        else None,
                    }
                )
        if not photos:
            continue
        author = tweet["author"]
        results.append(
            {
                "tweet_id": str(tweet.get("tweet_id") or ""),
                "text": str(tweet.get("text") or ""),
                "created_at": str(tweet.get("created_at") or ""),
                "url": str(tweet.get("url") or ""),
                "author": {
                    "user_id": str(author.get("user_id") or ""),
                    "screen_name": str(author.get("screen_name") or ""),
                    "followers_count": author.get("followers_count")
                    if isinstance(author.get("followers_count"), int)
                    else None,
                },
                "media": photos,
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
        created = parse_datetime(str(item.get("created_at") or ""))
        try:
            tweet_id = int(str(item.get("tweet_id") or "0"))
        except ValueError:
            tweet_id = 0
        return created or datetime.min.replace(tzinfo=timezone.utc), tweet_id

    ordered = sorted(results, key=sort_key, reverse=True)
    limit = max(1, min(max_tweets, 32))
    return {
        "target_user_id": verified_user_id,
        "newest_id": newest_tweet_id(matching),
        "tweets": ordered[:limit],
        "_truncated": len(ordered) > limit,
    }


def filter_verified_replies(
    tweets: list[dict[str, Any]],
    *,
    expected_user_id: str,
    parent_window_hours: int = 48,
    since_id: str | None = None,
    since_time: datetime | None = None,
    now: datetime | None = None,
    candidate_reply_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parent_cutoff = now - timedelta(hours=parent_window_hours)
    by_id = {str(tweet["tweet_id"]): tweet for tweet in tweets}
    results: list[dict[str, Any]] = []
    stats = {
        "total": len(tweets),
        "candidates": 0,
        "verified": 0,
        "direct": 0,
        "after_since": 0,
        "parent_refs": 0,
        "parents_found": 0,
        "parent_authors": 0,
        "recent_parents": 0,
        "accepted": 0,
    }
    for tweet in tweets:
        if (
            candidate_reply_ids is not None
            and str(tweet.get("tweet_id")) not in candidate_reply_ids
        ):
            continue
        stats["candidates"] += 1
        if not tweet.get("_is_verified"):
            continue
        stats["verified"] += 1
        if tweet.get("in_reply_to_user_id") != expected_user_id:
            continue
        stats["direct"] += 1
        if not _is_after_since(tweet, since_id=since_id, since_time=since_time):
            continue
        stats["after_since"] += 1
        parent_id = tweet.get("in_reply_to_tweet_id")
        if parent_id:
            stats["parent_refs"] += 1
        parent = by_id.get(str(parent_id)) if parent_id else None
        if parent is None:
            continue
        stats["parents_found"] += 1
        parent_author = parent.get("author")
        if not isinstance(parent_author, dict) or (
            parent_author.get("user_id") != expected_user_id
        ):
            continue
        stats["parent_authors"] += 1
        parent_created_at = parse_datetime(str(parent.get("created_at") or ""))
        if parent_created_at is None or parent_created_at < parent_cutoff:
            continue
        stats["recent_parents"] += 1
        public_reply = {
            key: value for key, value in tweet.items() if not key.startswith("_")
        }
        public_reply["parent"] = {
            key: parent.get(key)
            for key in ("tweet_id", "text", "created_at", "url", "media")
        }
        results.append(public_reply)
        stats["accepted"] += 1

    logger.info("Verified reply filter stats: {}", stats)

    def sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
        created = parse_datetime(str(item.get("created_at") or ""))
        try:
            tweet_id = int(str(item.get("tweet_id") or "0"))
        except ValueError:
            tweet_id = 0
        return created or datetime.min.replace(tzinfo=timezone.utc), tweet_id

    return sorted(results, key=sort_key, reverse=True)
