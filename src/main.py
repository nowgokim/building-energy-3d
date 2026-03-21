import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.shared.config import get_settings

app = FastAPI(
    title="3D Building Energy Platform",
    description="마포구 건물 에너지 시뮬레이션 3D 지도 플랫폼",
    version="0.1.0",
)

# CORS — 환경변수 기반
settings = get_settings()
allowed_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# 라우터 등록
from src.visualization.buildings import router as buildings_router
from src.visualization.search import router as search_router

app.include_router(buildings_router)
app.include_router(search_router)

# 3D Tiles 정적 파일 서빙
tiles_dir = settings.TILES_LOCAL_DIR
os.makedirs(tiles_dir, exist_ok=True)
app.mount("/tiles", StaticFiles(directory=tiles_dir), name="tiles")
