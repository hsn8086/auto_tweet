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
