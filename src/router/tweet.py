from typing import Annotated, Awaitable
import asyncio
import re
import secrets

from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Header, Query
from loguru import logger
from playwright.async_api import FilePayload
from pydantic import ValidationError
from tenacity import RetryError

from ..config import Config
from ..model import State, PostSentError
from ..replies import parse_datetime, parse_viewer_user_id
from ..result_store import (
    ResultStore,
    STATUS_FAILED,
    STATUS_SENT_UNCONFIRMED,
    STATUS_SUCCESS,
    get_result_store,
    is_valid_request_id,
)
from ..sender import (
    RETRYABLE_SEND_EXCEPTIONS,
    describe_send_exception,
    fetch_verified_replies,
    send,
)

router = APIRouter(prefix="/tweet")
SEND_TIMEOUT_SECONDS = 60 * 25
# running 记录超过该时长视为陈旧（进程曾中途崩溃/重启），允许重试覆盖。
STALE_RUNNING_SECONDS = SEND_TIMEOUT_SECONDS + 5 * 60
VERIFIED_REPLIES_TIMEOUT_SECONDS = 60 * 45
_SCREEN_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def _require_api_key(config: Config, request_key: str | None) -> None:
    api_key = (config.auto_tweet_api_key or "").strip()
    if not api_key:
        return
    if request_key is None or not secrets.compare_digest(request_key, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _normalize_request_id(request_id: str | None) -> str | None:
    if request_id is None:
        return None
    request_id = request_id.strip()
    if not request_id:
        return None
    if not is_valid_request_id(request_id):
        raise HTTPException(
            status_code=400,
            detail="无效的 request_id（仅允许 [A-Za-z0-9_.:-]，最长 128）",
        )
    return request_id


def _replay_payload(entry: dict) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok", "replayed": True}
    tweet_id = entry.get("tweet_id")
    if isinstance(tweet_id, str) and tweet_id:
        payload["tweet_id"] = tweet_id
    warning = entry.get("warning")
    if isinstance(warning, str) and warning:
        payload["warning"] = warning
    return payload


def _claim_send(
    config: Config, request_id: str | None
) -> tuple[ResultStore | None, dict[str, object] | None]:
    if request_id is None:
        return None, None
    store = get_result_store(config.data_dir + "/tweet_results")
    claim = store.claim(request_id, STALE_RUNNING_SECONDS)
    if claim.outcome == "replay" and claim.entry is not None:
        logger.info(
            "Replaying stored result for request_id={} status={}",
            request_id,
            claim.entry.get("status"),
        )
        return store, _replay_payload(claim.entry)
    if claim.outcome == "active":
        raise HTTPException(
            status_code=409,
            detail=(
                f"该 request_id 仍在处理中，请通过 /tweet/result/{request_id} 轮询结果"
            ),
        )
    return store, None


async def _read_images(
    images: list[UploadFile] | None, *, max_images: int | None = None
) -> list[FilePayload]:
    uploads = images or []
    if max_images is not None and len(uploads) > max_images:
        raise HTTPException(status_code=400, detail=f"最多允许上传 {max_images} 张图片")
    payloads: list[FilePayload] = []
    for image in uploads:
        if not image.filename or not image.content_type:
            raise HTTPException(status_code=400, detail="图片缺少文件名或类型")
        payloads.append(
            FilePayload(
                name=image.filename,
                mimeType=image.content_type,
                buffer=await image.read(),
            )
        )
    return payloads


async def _execute_send(
    operation: Awaitable[str | None],
    *,
    request_id: str | None,
    store: ResultStore | None,
    operation_name: str,
) -> dict[str, object]:
    try:
        tweet_id = await asyncio.wait_for(operation, timeout=SEND_TIMEOUT_SECONDS)
        payload: dict[str, object] = {"status": "ok"}
        if isinstance(tweet_id, str) and tweet_id:
            payload["tweet_id"] = tweet_id
        if request_id and store is not None:
            store.record(
                request_id,
                STATUS_SUCCESS,
                tweet_id=tweet_id if isinstance(tweet_id, str) else None,
            )
        return payload
    except PostSentError as exc:
        warning = describe_send_exception(exc)
        payload = {"status": "ok", "warning": warning}
        if exc.tweet_id:
            payload["tweet_id"] = exc.tweet_id
        if request_id and store is not None:
            store.record(
                request_id,
                STATUS_SENT_UNCONFIRMED,
                tweet_id=exc.tweet_id,
                warning=warning,
            )
        return payload
    except RETRYABLE_SEND_EXCEPTIONS as exc:
        detail = describe_send_exception(exc)
        logger.warning("Retriable error in {}: {}", operation_name, detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=502, detail=detail)
    except RetryError as exc:
        detail = describe_send_exception(exc)
        logger.warning("RetryError in {}: {}", operation_name, detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        detail = describe_send_exception(exc)
        logger.error("Unexpected error in {}: {}", operation_name, detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=500, detail=detail)


def _validate_state(raw_state: str) -> State:
    try:
        return State.model_validate_json(raw_state)
    except ValidationError:
        raise HTTPException(status_code=400, detail="无效的 state 参数")


def _validate_expected_user(state: State, expected_user_id: str) -> str:
    expected = expected_user_id.strip()
    if not expected.isdigit():
        raise HTTPException(status_code=400, detail="expected_user_id 必须为数字")
    try:
        viewer_user_id = parse_viewer_user_id(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if viewer_user_id != expected:
        raise HTTPException(
            status_code=409, detail="state 登录账号与 expected_user_id 不匹配"
        )
    return viewer_user_id


@router.post("/post")
async def post_tweet(
    state_query: Annotated[str | None, Query(alias="state")] = None,
    context_query: Annotated[str | None, Query(alias="context")] = None,
    spoiler_query: Annotated[bool | None, Query(alias="spoiler")] = None,
    made_with_ai_query: Annotated[bool | None, Query(alias="made_with_ai")] = None,
    request_id_query: Annotated[str | None, Query(alias="request_id")] = None,
    state_form: Annotated[str | None, Form(alias="state")] = None,
    context_form: Annotated[str | None, Form(alias="context")] = None,
    spoiler_form: Annotated[bool | None, Form(alias="spoiler")] = None,
    made_with_ai_form: Annotated[bool | None, Form(alias="made_with_ai")] = None,
    request_id_form: Annotated[str | None, Form(alias="request_id")] = None,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
    images: list[UploadFile] | None = File(None),
):
    config = Config()
    _require_api_key(config, api_key)

    state = state_form if state_form is not None else state_query
    if state is None:
        raise HTTPException(status_code=400, detail="缺少 state 参数")
    context = context_form if context_form is not None else context_query or ""
    spoiler = spoiler_form if spoiler_form is not None else spoiler_query or False
    made_with_ai = (
        made_with_ai_form
        if made_with_ai_form is not None
        else made_with_ai_query or False
    )
    request_id = _normalize_request_id(
        request_id_form if request_id_form is not None else request_id_query
    )
    state_pyd = _validate_state(state)
    imgs = await _read_images(images)
    store, replay = _claim_send(config, request_id)
    if replay is not None:
        return replay
    return await _execute_send(
        send(
            context,
            state_pyd,
            media=imgs,
            proxy=config.proxy,
            spoiler=spoiler,
            made_with_ai=made_with_ai,
            headless=True,
        ),
        request_id=request_id,
        store=store,
        operation_name="post_tweet",
    )


@router.post("/reply")
async def reply_tweet(
    state_form: Annotated[str | None, Form(alias="state")] = None,
    expected_user_id_form: Annotated[str | None, Form(alias="expected_user_id")] = None,
    in_reply_to_tweet_id_form: Annotated[
        str | None, Form(alias="in_reply_to_tweet_id")
    ] = None,
    context_form: Annotated[str | None, Form(alias="context")] = None,
    request_id_form: Annotated[str | None, Form(alias="request_id")] = None,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
    images: list[UploadFile] | None = File(None),
):
    config = Config()
    _require_api_key(config, api_key)
    if state_form is None:
        raise HTTPException(status_code=400, detail="缺少 state 参数")
    if not expected_user_id_form:
        raise HTTPException(status_code=400, detail="缺少 expected_user_id 参数")
    target_tweet_id = (in_reply_to_tweet_id_form or "").strip()
    if not target_tweet_id.isdigit():
        raise HTTPException(status_code=400, detail="in_reply_to_tweet_id 必须为数字")
    request_id = _normalize_request_id(request_id_form)
    if request_id is None:
        raise HTTPException(status_code=400, detail="request_id 为必填")
    state_pyd = _validate_state(state_form)
    _validate_expected_user(state_pyd, expected_user_id_form)
    imgs = await _read_images(images, max_images=4)
    context = context_form or ""
    if not context.strip() and not imgs:
        raise HTTPException(status_code=400, detail="context 和 images 至少提供一项")
    store, replay = _claim_send(config, request_id)
    if replay is not None:
        return replay
    return await _execute_send(
        send(
            context,
            state_pyd,
            media=imgs,
            proxy=config.proxy,
            headless=True,
            reply_to_tweet_id=target_tweet_id,
        ),
        request_id=request_id,
        store=store,
        operation_name="reply_tweet",
    )


@router.post("/verified_replies")
async def verified_replies(
    state_form: Annotated[str | None, Form(alias="state")] = None,
    screen_name_form: Annotated[str | None, Form(alias="screen_name")] = None,
    expected_user_id_form: Annotated[str | None, Form(alias="expected_user_id")] = None,
    since_id_form: Annotated[str | None, Form(alias="since_id")] = None,
    since_time_form: Annotated[str | None, Form(alias="since_time")] = None,
    parent_window_hours_form: Annotated[int, Form(alias="parent_window_hours")] = 48,
    max_scrolls_form: Annotated[int, Form(alias="max_scrolls")] = 0,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
):
    config = Config()
    _require_api_key(config, api_key)
    if state_form is None or not screen_name_form or not expected_user_id_form:
        raise HTTPException(
            status_code=400,
            detail="state、screen_name 和 expected_user_id 都是必填",
        )
    screen_name = screen_name_form.strip().lstrip("@")
    if not _SCREEN_NAME_RE.fullmatch(screen_name):
        raise HTTPException(status_code=400, detail="无效的 screen_name")
    since_id = (since_id_form or "").strip() or None
    if since_id is not None and not since_id.isdigit():
        raise HTTPException(status_code=400, detail="since_id 必须为数字")
    since_time = parse_datetime(since_time_form)
    if since_time_form and since_time is None:
        raise HTTPException(status_code=400, detail="since_time 必须为 ISO 时间")
    if not 1 <= parent_window_hours_form <= 24 * 30:
        raise HTTPException(
            status_code=400, detail="parent_window_hours 必须在 1 到 720 之间"
        )
    if not 0 <= max_scrolls_form <= 60:
        raise HTTPException(status_code=400, detail="max_scrolls 必须在 0 到 60 之间")
    state_pyd = _validate_state(state_form)
    viewer_user_id = _validate_expected_user(state_pyd, expected_user_id_form)
    try:
        result = await asyncio.wait_for(
            fetch_verified_replies(
                screen_name,
                viewer_user_id,
                state_pyd,
                since_id=since_id,
                since_time=since_time,
                parent_window_hours=parent_window_hours_form,
                max_scrolls=max_scrolls_form,
                proxy=config.proxy,
                headless=True,
            ),
            timeout=VERIFIED_REPLIES_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="收集认证回复超时")
    except RETRYABLE_SEND_EXCEPTIONS as exc:
        detail = describe_send_exception(exc)
        logger.warning("Retriable error in verified_replies: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        detail = describe_send_exception(exc)
        logger.error("Unexpected error in verified_replies: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    return {
        "status": "ok",
        "screen_name": screen_name,
        "viewer_user_id": viewer_user_id,
        "newest_id": result.get("newest_id"),
        "observed_newest_id": result.get("observed_newest_id", result.get("newest_id")),
        "complete": bool(result.get("complete")),
        "replies": result.get("replies", []),
    }


@router.get("/result/{request_id}")
async def get_tweet_result(
    request_id: str,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
):
    """结果对账接口：按 request_id 查询发送结果。

    供上游在 POST /tweet/post 响应丢失（网络超时/连接被掐）后轮询，
    判断推文是否实际已发出，避免漏记或重复发帖。
    """
    config = Config()
    _require_api_key(config, api_key)
    normalized = _normalize_request_id(request_id)
    if normalized is None:
        raise HTTPException(status_code=400, detail="无效的 request_id")
    store = get_result_store(config.data_dir + "/tweet_results")
    entry = store.get(normalized)
    if entry is None:
        raise HTTPException(status_code=404, detail="未知的 request_id")
    return entry


@router.get("/queue")
async def get_queue_stats(
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
):
    """当前发送队列状态（排队中/执行中），用于运维观测。"""
    from ..sender import queue_stats

    config = Config()
    _require_api_key(config, api_key)
    return dict(queue_stats)


METRICS_TIMEOUT_SECONDS = 60 * 4


@router.post("/metrics")
async def get_tweet_metrics(
    state_query: Annotated[str | None, Query(alias="state")] = None,
    tweet_id_query: Annotated[str | None, Query(alias="tweet_id")] = None,
    state_form: Annotated[str | None, Form(alias="state")] = None,
    tweet_id_form: Annotated[str | None, Form(alias="tweet_id")] = None,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
):
    from ..sender import fetch_tweet_metrics

    config = Config()
    _require_api_key(config, api_key)

    state = state_form if state_form is not None else state_query
    tweet_id = tweet_id_form if tweet_id_form is not None else tweet_id_query
    if state is None or not tweet_id or not tweet_id.strip():
        raise HTTPException(status_code=400, detail="state 和 tweet_id 都是必填")
    try:
        state_pyd = State.model_validate_json(state)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"无效的 state 参数: {e}")

    try:
        metrics = await asyncio.wait_for(
            fetch_tweet_metrics(
                tweet_id.strip(), state_pyd, proxy=config.proxy, headless=True
            ),
            timeout=METRICS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="获取 tweet 数据超时")
    except RETRYABLE_SEND_EXCEPTIONS as e:
        detail = describe_send_exception(e)
        logger.warning("Retriable error in get_tweet_metrics: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        detail = describe_send_exception(e)
        logger.error("Unexpected error in get_tweet_metrics: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    return {"status": "ok", **metrics}


USER_METRICS_TIMEOUT_SECONDS = 60 * 8


@router.post("/user_metrics")
async def get_user_tweets_metrics_route(
    state_form: Annotated[str | None, Form(alias="state")] = None,
    screen_name_form: Annotated[str | None, Form(alias="screen_name")] = None,
    until_hours_form: Annotated[int, Form(alias="until_hours")] = 96,
    max_scrolls_form: Annotated[int, Form(alias="max_scrolls")] = 30,
    api_key: Annotated[str | None, Header(alias="X-Auto-Tweet-Key")] = None,
):
    """批量收集某账号时间线近 until_hours 小时推文的指标（一次浏览器会话）。"""
    from ..sender import fetch_user_tweets_metrics

    config = Config()
    _require_api_key(config, api_key)

    if state_form is None or not screen_name_form or not screen_name_form.strip():
        raise HTTPException(status_code=400, detail="state 和 screen_name 都是必填")
    until_hours = max(1, min(until_hours_form, 24 * 30))
    max_scrolls = max(1, min(max_scrolls_form, 60))
    try:
        state_pyd = State.model_validate_json(state_form)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"无效的 state 参数: {e}")

    try:
        tweets = await asyncio.wait_for(
            fetch_user_tweets_metrics(
                screen_name_form.strip(),
                state_pyd,
                until_hours=until_hours,
                max_scrolls=max_scrolls,
                proxy=config.proxy,
                headless=True,
            ),
            timeout=USER_METRICS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="收集时间线数据超时")
    except RETRYABLE_SEND_EXCEPTIONS as e:
        detail = describe_send_exception(e)
        logger.warning("Retriable error in get_user_tweets_metrics: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        detail = describe_send_exception(e)
        logger.error("Unexpected error in get_user_tweets_metrics: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    return {
        "status": "ok",
        "screen_name": screen_name_form.strip().lstrip("@"),
        "count": len(tweets),
        "tweets": tweets,
    }
