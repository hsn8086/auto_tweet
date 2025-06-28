from fastapi import APIRouter, UploadFile, File
from playwright.async_api import FilePayload
from ..config import Config
from ..sender import send
from ..model import State

router = APIRouter(prefix="/tweet")


@router.post("/post")
async def post_tweet(
    state: str, spoiler=False, context="", images: list[UploadFile] | None = File(...)
):
    if not images:
        images = []
    state_pyd = State.model_validate_json(state)
    imgs = []

    for image in images:
        assert image.filename and image.content_type
        img = FilePayload(
            name=image.filename, mimeType=image.content_type, buffer=await image.read()
        )
        imgs.append(img)
    config = Config()
    await send(
        context,
        state_pyd,
        media=imgs,
        proxy=config.proxy,
        spoiler=spoiler,
        headless=True,
    )
