import asyncio
import json
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from src.shared.config import get_settings

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="3D Building Energy Platform",
    description="마포구 건물 에너지 시뮬레이션 3D 지도 플랫폼",
    version="0.1.0",
)

# CORS — 환경변수 기반
# ALLOWED_ORIGINS 환경변수에 프로덕션 도메인을 명시한다.
# 기본값은 로컬 개발 서버만 허용. "*" 와일드카드 절대 사용 금지.
# 프로덕션: ALLOWED_ORIGINS=https://your-domain.com
settings = get_settings()
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:3000",
)
allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
if "*" in allowed_origins:
    logger.warning(
        "ALLOWED_ORIGINS contains '*' (wildcard). "
        "This is a security risk in production. "
        "Set explicit origins in ALLOWED_ORIGINS environment variable."
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # allow_credentials=True 는 allow_origins=["*"] 와 함께 사용 불가.
    # 명시적 origin 목록을 사용할 때만 credentials=True 설정 가능.
    allow_credentials="*" not in allowed_origins,
)


@app.on_event("startup")
async def startup_check():
    """Verify database connectivity at startup.

    async 이벤트 핸들러로 선언한다. 동기 함수로 선언하면 FastAPI가
    threadpool에서 실행하므로 startup 순서 보장에 문제가 생길 수 있다.
    SQLAlchemy 동기 엔진은 run_in_executor 없이 async 함수에서 직접 호출 가능
    (startup은 요청 처리 루프 진입 전이므로 이벤트 루프 블로킹 허용).
    """
    from sqlalchemy import text
    from src.shared.database import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as e:
        logger.error("Database connection FAILED: %s", e)


@app.get("/health")
def health():
    return {"status": "ok"}


# 라우터 등록
from src.visualization.buildings import router as buildings_router
from src.visualization.search import router as search_router
from src.fire_safety.risk import router as fire_router
from src.visualization.monitor import router as monitor_router
from src.visualization.monitor import monitor_ws
from src.visualization.simulation import router as simulation_router

app.include_router(buildings_router)
app.include_router(search_router)
app.include_router(fire_router)
app.include_router(monitor_router)
app.include_router(simulation_router)

# 3D Tiles 정적 파일 서빙
tiles_dir = settings.TILES_LOCAL_DIR
os.makedirs(tiles_dir, exist_ok=True)
app.mount("/tiles", StaticFiles(directory=tiles_dir), name="tiles")


# ── Phase F4: WebSocket 기상 업데이트 + 모니터링 실시간 스트림 ─────────────────


@app.websocket("/ws/monitor/{ts_id}")
async def monitor_building_ws(ws: WebSocket, ts_id: int):
    """
    건물 에너지 계량 실시간 스트림.
    연결 즉시 최신 계량값 전송, 이후 30초마다 최신값 push.
    CSV 업로드 직후에는 _broadcast_to_ts()를 통해 즉시 push된다.
    """
    await monitor_ws(ws, ts_id)


# ── Phase F4: WebSocket 기상 업데이트 ─────────────────────────────────────────

_ws_clients: set[WebSocket] = set()


@app.websocket("/ws/weather")
async def weather_ws(ws: WebSocket):
    """
    클라이언트 연결 시 현재 기상 즉시 전송.
    이후 60초마다 최신 기상 push.
    """
    await ws.accept()
    _ws_clients.add(ws)
    try:
        from src.data_ingestion.collect_weather import get_current_seoul_wind
        from src.shared.database import engine

        # 연결 직후 현재 기상 전송
        with engine.connect() as conn:
            data = get_current_seoul_wind(conn)
        await ws.send_text(json.dumps(data))

        # 60초 간격으로 업데이트 push
        while True:
            await asyncio.sleep(60)
            with engine.connect() as conn:
                data = get_current_seoul_wind(conn)
            await ws.send_text(json.dumps(data))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
