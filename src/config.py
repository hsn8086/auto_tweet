from pydantic_settings import BaseSettings


class Config(BaseSettings):
    proxy: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"