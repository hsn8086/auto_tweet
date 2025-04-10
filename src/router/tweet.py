from fastapi import APIRouter, UploadFile, File
from playwright.async_api import FilePayload
from ..config import Config
from ..sender import send
from ..model import State

router = APIRouter(prefix="/tweet")


@router.post("/post")
async def post_tweet(state: str, context="", image: UploadFile = File(...)):
    state = State.model_validate_json(state)
    img = FilePayload(
        name=image.filename, mimeType=image.content_type, buffer=await image.read()
    )
    config = Config()
    await send(context, state, img=img, proxy=config.proxy)
