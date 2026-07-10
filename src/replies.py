import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import unquote

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


def normalize_tweet(candidate: Any) -> dict[str, Any] | None:
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
    if not isinstance(user_legacy, dict):
        return None
    user_id = _string_id(user.get("rest_id")) if isinstance(user, dict) else None
    screen_name = str(user_legacy.get("screen_name") or "").strip().lstrip("@")
    if user_id is None or not screen_name:
        return None

    verification = user.get("verification") if isinstance(user, dict) else None
    verified_type = (
        user.get("verified_type") or user_legacy.get("verified_type")
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
        or user_legacy.get("verified")
        or (isinstance(verification, dict) and verification.get("verified"))
        or verified_type
        or (isinstance(affiliate, dict) and affiliate)
    )
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
            "name": str(user_legacy.get("name") or ""),
            "is_blue_verified": is_blue_verified,
            "is_verified": is_verified,
            "verified_type": str(verified_type) if verified_type else None,
        },
        "media": normalize_media(legacy),
        "_is_verified": is_verified,
    }


def parse_graphql_tweets(payload: Any) -> list[dict[str, Any]]:
    """Extract normalized tweets from timeline instructions of any known shape."""
    if not isinstance(payload, dict):
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate, pinned in _iter_tweet_candidates(payload):
        tweet = normalize_tweet(candidate)
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
    for tweet in tweets:
        if (
            candidate_reply_ids is not None
            and str(tweet.get("tweet_id")) not in candidate_reply_ids
        ):
            continue
        if not tweet.get("_is_verified"):
            continue
        if tweet.get("in_reply_to_user_id") != expected_user_id:
            continue
        if not _is_after_since(tweet, since_id=since_id, since_time=since_time):
            continue
        parent_id = tweet.get("in_reply_to_tweet_id")
        parent = by_id.get(str(parent_id)) if parent_id else None
        if parent is None:
            continue
        parent_author = parent.get("author")
        if not isinstance(parent_author, dict) or (
            parent_author.get("user_id") != expected_user_id
        ):
            continue
        parent_created_at = parse_datetime(str(parent.get("created_at") or ""))
        if parent_created_at is None or parent_created_at < parent_cutoff:
            continue
        public_reply = {
            key: value for key, value in tweet.items() if not key.startswith("_")
        }
        public_reply["parent"] = {
            key: parent.get(key)
            for key in ("tweet_id", "text", "created_at", "url", "media")
        }
        results.append(public_reply)

    def sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
        created = parse_datetime(str(item.get("created_at") or ""))
        try:
            tweet_id = int(str(item.get("tweet_id") or "0"))
        except ValueError:
            tweet_id = 0
        return created or datetime.min.replace(tzinfo=timezone.utc), tweet_id

    return sorted(results, key=sort_key, reverse=True)
