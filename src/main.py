from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="3D Building Energy Platform",
    description="마포구 건물 에너지 시뮬레이션 3D 지도 플랫폼",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
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
