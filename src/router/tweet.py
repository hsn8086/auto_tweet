from fastapi import APIRouter, UploadFile, File, HTTPException
from loguru import logger
from playwright.async_api import FilePayload
from pydantic import ValidationError
from tenacity import RetryError

from ..config import Config
from ..model import State, PostSentError
from ..sender import RETRYABLE_SEND_EXCEPTIONS, describe_send_exception, send

router = APIRouter(prefix="/tweet")


@router.post("/post")
async def post_tweet(
    state: str, spoiler=False, context="", images: list[UploadFile] | None = File(...)
):
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

    config = Config()
    try:
        await send(
            context,
            state_pyd,
            media=imgs,
            proxy=config.proxy,
            spoiler=spoiler,
            headless=True,
        )
        return {"status": "ok"}
    except PostSentError as e:
        warning = describe_send_exception(e)
        return {"status": "ok", "warning": warning}
    except RETRYABLE_SEND_EXCEPTIONS as e:
        detail = describe_send_exception(e)
        logger.warning("Retriable error in post_tweet: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    except RetryError as e:
        detail = describe_send_exception(e)
        logger.warning("RetryError in post_tweet: {}", detail)
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        detail = describe_send_exception(e)
        logger.error("Unexpected error in post_tweet: {}", detail)
        raise HTTPException(status_code=500, detail=detail)
