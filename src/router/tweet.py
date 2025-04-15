from fastapi import APIRouter, UploadFile, File
from playwright.async_api import FilePayload
from ..config import Config
from ..sender import send
from ..model import State

router = APIRouter(prefix="/tweet")


@router.post("/post")
async def post_tweet(
    state: str, spoiler=False, context="", images: list[UploadFile] = File(...)
):
    state = State.model_validate_json(state)
    imgs = []
    for image in images:
        img = FilePayload(
            name=image.filename, mimeType=image.content_type, buffer=await image.read()
        )
        imgs.append(img)
    config = Config()
    await send(
        context, state, imgs=imgs, proxy=config.proxy, spoiler=spoiler, headless=True
    )
