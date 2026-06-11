from pydantic_settings import BaseSettings


class Config(BaseSettings):
    proxy: str | None = None
    auto_tweet_api_key: str | None = None
    data_dir: str = "data"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
