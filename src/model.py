from pydantic import BaseModel


class CookieItem(BaseModel):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    httpOnly: bool
    secure: bool
    sameSite: str


class State(BaseModel):
    cookies: list[CookieItem]
    # origins:list[dict]


class PostSentError(Exception):
    """推文已发送成功，但后续操作(截图等)失败"""

    def __init__(self, message: str = "", *, tweet_id: str | None = None) -> None:
        super().__init__(message)
        self.tweet_id = tweet_id
