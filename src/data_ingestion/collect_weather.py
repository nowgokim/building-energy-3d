"""
Phase F4-01: 기상청 API 연동 모듈
- apihub.kma.go.kr fct_afs_dl.php (단기예보, 우선)
- data.go.kr VilageFcstInfoService2.0 (폴백)
- API 키 없으면 더미 데이터 반환 (개발용)
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# ── apihub.kma.go.kr 서울 예보구역 코드 → 대표 좌표 ──────────────────────────
# apihub에서 서울 도시 단위 예보는 11B10101 하나만 제공
SEOUL_REGIONS = [
    {"reg": "11B10101", "lng": 126.978, "lat": 37.566, "name": "서울"},
]

# 8방위 → 각도
_COMPASS_DEG = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}

# 풍속 레벨(T) → m/s 대표값
_WIND_LEVEL_MS = {0: 0.0, 1: 2.0, 2: 5.0, 3: 9.0, 4: 13.0}

KMA_HUB_URL = "https://apihub.kma.go.kr/api/typ01/url/fct_afs_dl.php"
KMA_DATAGOV_URL = (
    "http://apis.data.go.kr/1360000/VilageFcstInfoService2.0/getUltraSrtFcst"
)

# data.go.kr 격자 (폴백용)
SEOUL_GRIDS = [
    {"x": 60, "y": 127, "lng": 126.978, "lat": 37.566},
    {"x": 59, "y": 127, "lng": 126.923, "lat": 37.556},
    {"x": 61, "y": 127, "lng": 127.032, "lat": 37.556},
    {"x": 60, "y": 128, "lng": 126.978, "lat": 37.610},
    {"x": 60, "y": 126, "lng": 126.978, "lat": 37.500},
    {"x": 61, "y": 126, "lng": 127.047, "lat": 37.494},
]


def fetch_kma_hub_wind(auth_key: str, region: dict) -> Optional[dict]:
    """apihub.kma.go.kr fct_afs_dl.php → 현재 풍향/풍속."""
    try:
        resp = requests.get(
            KMA_HUB_URL,
            params={"tmfc": 0, "reg": region["reg"], "authKey": auth_key},
            timeout=10,
        )
        resp.raise_for_status()
        text_body = resp.content.decode("euc-kr", errors="replace")

        # 데이터 첫 행(NE=0, 현재 예보)
        for line in text_body.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 12:
                continue
            # 컬럼: REG_ID TM_FC TM_EF MOD NE STN C MAN_ID MAN_FC W1 T W2 TA ...
            # NE(index4)=0 이 현재 시각 예보
            ne = parts[4]
            w1 = parts[9]    # 풍향 (W, S, NW ...)
            t  = parts[10]   # 풍속 레벨
            w2 = parts[11]   # 보조 풍향 (더 세분화)

            direction = _COMPASS_DEG.get(w2) or _COMPASS_DEG.get(w1)
            speed     = _WIND_LEVEL_MS.get(int(t), 2.0)
            if direction is not None:
                return {"wind_direction": direction, "wind_speed": speed}
    except Exception as e:
        logger.warning("KMA hub fetch failed reg=%s: %s", region["reg"], e)
    return None


def _get_base_time() -> tuple[str, str]:
    """data.go.kr 초단기예보 base_date, base_time 계산."""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    if now.minute < 45:
        now -= timedelta(hours=1)
    return now.strftime("%Y%m%d"), now.strftime("%H") + "30"


def fetch_kma_datagov_wind(api_key: str, grid: dict) -> Optional[dict]:
    """data.go.kr 초단기예보 VEC/WSD 조회 (폴백)."""
    base_date, base_time = _get_base_time()
    try:
        resp = requests.get(
            KMA_DATAGOV_URL,
            params={
                "serviceKey": api_key,
                "pageNo": 1, "numOfRows": 60,
                "dataType": "JSON",
                "base_date": base_date, "base_time": base_time,
                "nx": grid["x"], "ny": grid["y"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        items = (
            resp.json()
            .get("response", {}).get("body", {})
            .get("items", {}).get("item", [])
        )
        vec = wsd = None
        for item in items:
            if item.get("category") == "VEC":
                vec = float(item["fcstValue"])
            elif item.get("category") == "WSD":
                wsd = float(item["fcstValue"])
        if vec is not None and wsd is not None:
            return {"wind_direction": vec, "wind_speed": wsd}
    except Exception as e:
        logger.warning("KMA datagov fetch failed grid=%s: %s", grid, e)
    return None


def _dummy_wind() -> dict:
    """API 키 없을 때 계절 기반 더미 바람 (개발용)."""
    month = datetime.now().month
    if 3 <= month <= 5:
        direction = 210.0
    elif 6 <= month <= 8:
        direction = 180.0
    elif 9 <= month <= 11:
        direction = 0.0
    else:
        direction = 315.0
    return {"wind_direction": direction, "wind_speed": 3.0}


def collect_and_save(
    conn: Connection,
    api_key: str = "",
    kma_hub_key: str = "",
) -> int:
    """
    서울 기상 수집 후 weather_snapshots 저장.
    우선순위: apihub.kma.go.kr → data.go.kr → 더미
    반환: 저장된 행 수
    """
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).replace(minute=0, second=0, microsecond=0)

    rows_saved = 0

    # ── apihub.kma.go.kr (서울 7개 구역) ──────────────────────────────────────
    if kma_hub_key:
        for region in SEOUL_REGIONS:
            result = fetch_kma_hub_wind(kma_hub_key, region)
            if result is None:
                result = _dummy_wind()
            conn.execute(text("""
                INSERT INTO weather_snapshots
                    (grid_x, grid_y, lng, lat, wind_direction, wind_speed, measured_at)
                VALUES
                    (:gx, :gy, :lng, :lat, :wd, :ws, :ts)
                ON CONFLICT DO NOTHING
            """), {
                "gx":  0, "gy": rows_saved,
                "lng": region["lng"], "lat": region["lat"],
                "wd":  result["wind_direction"],
                "ws":  result["wind_speed"],
                "ts":  now,
            })
            rows_saved += 1
        logger.info("weather_snapshots (kma_hub): %d rows (measured_at=%s)", rows_saved, now)
        return rows_saved

    # ── data.go.kr 폴백 ────────────────────────────────────────────────────────
    for grid in SEOUL_GRIDS:
        if api_key:
            result = fetch_kma_datagov_wind(api_key, grid)
        else:
            result = None
        if result is None:
            result = _dummy_wind()
        conn.execute(text("""
            INSERT INTO weather_snapshots
                (grid_x, grid_y, lng, lat, wind_direction, wind_speed, measured_at)
            VALUES
                (:gx, :gy, :lng, :lat, :wd, :ws, :ts)
            ON CONFLICT DO NOTHING
        """), {
            "gx":  grid["x"], "gy": grid["y"],
            "lng": grid["lng"], "lat": grid["lat"],
            "wd":  result["wind_direction"],
            "ws":  result["wind_speed"],
            "ts":  now,
        })
        rows_saved += 1

    logger.info("weather_snapshots (datagov): %d rows (measured_at=%s)", rows_saved, now)
    return rows_saved


def get_current_seoul_wind(conn: Connection) -> dict:
    """최신 서울 평균 풍향/풍속 반환."""
    row = conn.execute(text(
        "SELECT wind_direction, wind_speed, measured_at, grid_count "
        "FROM current_seoul_wind"
    )).fetchone()

    if row and row.grid_count:
        return {
            "wind_direction": round(float(row.wind_direction), 1),
            "wind_speed":     round(float(row.wind_speed), 1),
            "measured_at":    row.measured_at.isoformat() if row.measured_at else None,
            "source":         "kma_api",
        }
    dummy = _dummy_wind()
    dummy["measured_at"] = None
    dummy["source"] = "dummy"
    return dummy
