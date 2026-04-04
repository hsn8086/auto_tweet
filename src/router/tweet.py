from fastapi import APIRouter, UploadFile, File, HTTPException
from loguru import logger
from playwright.async_api import FilePayload
from pydantic import ValidationError
from tenacity import RetryError

from ..config import Config
from ..model import State, PostSentError
from ..sender import send

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
    except PostSentError:
        return {"status": "ok", "warning": "post sent but post-send operations failed"}
    except RetryError:
        raise HTTPException(status_code=502, detail="发送失败，已重试5次")
    except Exception as e:
        logger.error("Unexpected error in post_tweet: {}", e)
        raise HTTPException(status_code=500, detail="内部错误")
