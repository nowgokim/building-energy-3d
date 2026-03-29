from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # .env 파일 또는 환경변수로 반드시 설정 — 기본값 없음
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"
    DATA_GO_KR_API_KEY: str = ""
    KMA_API_KEY: str = ""          # apihub.kma.go.kr authKey
    VWORLD_API_KEY: str = ""
    JUSO_API_KEY: str = ""
    SEOUL_DATA_API_KEY: str = ""
    S3_TILES_BUCKET: str = "building-energy-tiles"
    TILES_LOCAL_DIR: str = "./output_tiles"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
