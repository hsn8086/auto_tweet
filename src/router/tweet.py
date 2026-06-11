from typing import Annotated
import asyncio
import secrets

from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Header, Query
from loguru import logger
from playwright.async_api import FilePayload
from pydantic import ValidationError
from tenacity import RetryError

from ..config import Config
from ..model import State, PostSentError
from ..result_store import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SENT_UNCONFIRMED,
    STATUS_SUCCESS,
    TERMINAL_SENT_STATUSES,
    get_result_store,
    is_valid_request_id,
)
from ..sender import RETRYABLE_SEND_EXCEPTIONS, describe_send_exception, send

router = APIRouter(prefix="/tweet")
SEND_TIMEOUT_SECONDS = 60 * 25
# running 记录超过该时长视为陈旧（进程曾中途崩溃/重启），允许重试覆盖。
STALE_RUNNING_SECONDS = SEND_TIMEOUT_SECONDS + 5 * 60


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


def _is_stale_running(entry: dict) -> bool:
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return True
    import time

    return time.time() - updated_at > STALE_RUNNING_SECONDS


def _replay_payload(entry: dict) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok", "replayed": True}
    tweet_id = entry.get("tweet_id")
    if isinstance(tweet_id, str) and tweet_id:
        payload["tweet_id"] = tweet_id
    warning = entry.get("warning")
    if isinstance(warning, str) and warning:
        payload["warning"] = warning
    return payload


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
    store = (
        get_result_store(config.data_dir + "/tweet_results") if request_id else None
    )
    if request_id and store is not None:
        existing = store.get(request_id)
        if existing:
            existing_status = existing.get("status")
            if existing_status in TERMINAL_SENT_STATUSES:
                # 幂等回放：同 request_id 已发出过，绝不二次发帖。
                logger.info(
                    "Replaying stored result for request_id={} status={}",
                    request_id,
                    existing_status,
                )
                return _replay_payload(existing)
            if existing_status == STATUS_RUNNING and not _is_stale_running(existing):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "该 request_id 仍在处理中，请通过 "
                        f"/tweet/result/{request_id} 轮询结果"
                    ),
                )
            # failed 或陈旧 running：允许重试，覆盖记录。

    if not images:
        images = []
    try:
        state_pyd = State.model_validate_json(state)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"无效的 state 参数: {e}")

    imgs = []
    for image in images:
        if not image.filename or not image.content_type:
            raise HTTPException(status_code=400, detail="图片缺少文件名或类型")
        img = FilePayload(
            name=image.filename,
            mimeType=image.content_type,
            buffer=await image.read(),
        )
        imgs.append(img)

    if request_id and store is not None:
        store.record(request_id, STATUS_RUNNING)

    try:
        tweet_id = await asyncio.wait_for(
            send(
                context,
                state_pyd,
                media=imgs,
                proxy=config.proxy,
                spoiler=spoiler,
                made_with_ai=made_with_ai,
                headless=True,
            ),
            timeout=SEND_TIMEOUT_SECONDS,
        )
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
    except PostSentError as e:
        warning = describe_send_exception(e)
        payload = {"status": "ok", "warning": warning}
        if getattr(e, "tweet_id", None):
            payload["tweet_id"] = e.tweet_id
        if request_id and store is not None:
            store.record(
                request_id,
                STATUS_SENT_UNCONFIRMED,
                tweet_id=getattr(e, "tweet_id", None),
                warning=warning,
            )
        return payload
    except RETRYABLE_SEND_EXCEPTIONS as e:
        detail = describe_send_exception(e)
        logger.warning("Retriable error in post_tweet: {}", detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=502, detail=detail)
    except RetryError as e:
        detail = describe_send_exception(e)
        logger.warning("RetryError in post_tweet: {}", detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        detail = describe_send_exception(e)
        logger.error("Unexpected error in post_tweet: {}", detail)
        if request_id and store is not None:
            store.record(request_id, STATUS_FAILED, error=detail)
        raise HTTPException(status_code=500, detail=detail)


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
