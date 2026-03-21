from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:devpassword@localhost:5432/buildings"
    REDIS_URL: str = "redis://localhost:6379"
    DATA_GO_KR_API_KEY: str = ""
    VWORLD_API_KEY: str = ""
    JUSO_API_KEY: str = ""
    SEOUL_DATA_API_KEY: str = ""
    S3_TILES_BUCKET: str = "building-energy-tiles"
    TILES_LOCAL_DIR: str = "./output_tiles"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
