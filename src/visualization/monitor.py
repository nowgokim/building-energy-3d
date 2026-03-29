"""
건물 에너지 시계열 모니터링 API 라우터.

실측 계량 데이터(15분~1시간 해상도)를 보유한 건물 목록 조회,
시계열 집계, 다건 비교, 이상치 조회, CSV 수동 업로드를 제공한다.
WebSocket /ws/monitor/{ts_id} 로 실시간 계량값을 push한다.
"""

import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.cache import get_redis as _get_redis_factory
from src.shared.database import get_db_dependency, engine
from src.shared.monitor_models import (
    MonitorBuildingDetail,
    MonitorBuildingListItem,
    MonitorBuildingListResponse,
    MonitorCompareResponse,
    MonitorAnomalyResponse,
    ReadingUploadResponse,
    TimeseriesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor"])

def _get_redis():
    return _get_redis_factory()


# ── 캐시 헬퍼 ────────────────────────────────────────────────────────────────

def _cache_get(key: str) -> Optional[dict]:
    """Redis에서 JSON 캐시 조회. 미스이면 None 반환."""
    try:
        r = _get_redis()
        if r is None:
            return None
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis GET error key=%s: %s", key, exc)
    return None


def _cache_set(key: str, data: dict, ttl: int) -> None:
    """Redis에 JSON 캐시 저장. Redis 장애 시 조용히 무시."""
    try:
        r = _get_redis()
        if r is None:
            return
        r.setex(key, ttl, json.dumps(data, default=str))
    except Exception as exc:
        logger.warning("Redis SET error key=%s: %s", key, exc)


def _cache_delete_pattern(pattern: str) -> None:
    """패턴 매칭 키를 일괄 무효화. 업로드/이상치 갱신 시 사용.

    KEYS 대신 SCAN 커서 방식을 사용한다. KEYS는 Redis를 blocking하므로
    프로덕션 환경에서 대규모 키셋이 있을 경우 응답 지연을 유발한다.
    """
    try:
        r = _get_redis()
        if r is None:
            return
        cursor = 0
        keys_to_delete: list[str] = []
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            keys_to_delete.extend(keys)
            if cursor == 0:
                break
        if keys_to_delete:
            r.delete(*keys_to_delete)
    except Exception as exc:
        logger.warning("Redis DEL error pattern=%s: %s", pattern, exc)


# ── WebSocket 연결 관리 ────────────────────────────────────────────────────────

# tasks.py의 Celery 이상치 감지 태스크가 게시하는 Redis pub/sub 채널명.
# 두 파일 모두 이 값을 사용하므로 변경 시 동시에 수정해야 한다.
_ANOMALY_CHANNEL = "monitor:anomaly:events"

# {ts_id: set[WebSocket]}
_monitor_ws_clients: dict[int, set[WebSocket]] = {}


async def _broadcast_to_ts(ts_id: int, payload: dict) -> None:
    """특정 ts_id를 구독 중인 모든 WebSocket 클라이언트에 메시지 전송."""
    clients = _monitor_ws_clients.get(ts_id, set())
    dead: set[WebSocket] = set()
    for ws in clients:
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            dead.add(ws)
    for ws in dead:
        clients.discard(ws)


# ── 공통 유틸 ────────────────────────────────────────────────────────────────

_RESOLUTION_SQL = {
    "raw": """
        SELECT
            recorded_at                            AS ts,
            value,
            meter_type,
            unit
        FROM metered_readings
        WHERE ts_id = :ts_id
          AND meter_type = :meter
          AND recorded_at BETWEEN :start AND :end
        ORDER BY recorded_at
    """,
    "hourly": """
        SELECT
            date_trunc('hour', recorded_at)        AS ts,
            SUM(value)                             AS value,
            meter_type,
            MAX(unit)                              AS unit
        FROM metered_readings
        WHERE ts_id = :ts_id
          AND meter_type = :meter
          AND recorded_at BETWEEN :start AND :end
        GROUP BY date_trunc('hour', recorded_at), meter_type
        ORDER BY ts
    """,
    "daily": """
        SELECT
            date_trunc('day', recorded_at)         AS ts,
            SUM(value)                             AS value,
            meter_type,
            MAX(unit)                              AS unit
        FROM metered_readings
        WHERE ts_id = :ts_id
          AND meter_type = :meter
          AND recorded_at BETWEEN :start AND :end
        GROUP BY date_trunc('day', recorded_at), meter_type
        ORDER BY ts
    """,
    "weekly": """
        SELECT
            date_trunc('week', recorded_at)        AS ts,
            SUM(value)                             AS value,
            meter_type,
            MAX(unit)                              AS unit
        FROM metered_readings
        WHERE ts_id = :ts_id
          AND meter_type = :meter
          AND recorded_at BETWEEN :start AND :end
        GROUP BY date_trunc('week', recorded_at), meter_type
        ORDER BY ts
    """,
}

_KWH_PER_MJ = 0.27778   # kWh → MJ 변환 시 필요. 계량 원본은 kWh 기준 저장


def _parse_period(period: str) -> timedelta:
    """'30d', '7d', '90d' 형식을 timedelta로 변환."""
    match = re.fullmatch(r"(\d+)d", period.strip().lower())
    if not match:
        raise HTTPException(status_code=400, detail=f"Invalid period format: '{period}'. Use e.g. '30d'")
    days = int(match.group(1))
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="period must be between 1d and 365d")
    return timedelta(days=days)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/monitor/buildings
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/buildings", response_model=MonitorBuildingListResponse)
def list_monitor_buildings(
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    시계열 계량 데이터가 존재하는 건물 목록.

    캐시: Redis, TTL 60초 (건물 등록은 드물게 발생).
    응답: ts_id, pnu, alias, 계량기 종류, 최신 EUI, 좌표.
    EUI = 최근 365일 누적 전기+가스 소비량(kWh) / 연면적(m²).
    """
    cache_key = "monitor:buildings:list"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    sql = text("""
        WITH latest_eui AS (
            SELECT
                mr.ts_id,
                ROUND(
                    (SUM(mr.value) FILTER (WHERE mr.meter_type IN ('electricity', 'gas'))
                    / NULLIF(MAX(mb.total_area), 0))::NUMERIC,
                    1
                ) AS eui_kwh_m2
            FROM metered_readings mr
            JOIN monitored_buildings mb ON mr.ts_id = mb.ts_id
            WHERE mr.recorded_at >= NOW() - INTERVAL '365 days'
            GROUP BY mr.ts_id
        )
        SELECT
            mb.ts_id,
            mb.pnu,
            mb.alias,
            mb.meter_types,
            mb.total_area,
            mb.usage_type,
            mb.built_year,
            le.eui_kwh_m2,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat
        FROM monitored_buildings mb
        LEFT JOIN latest_eui le      ON mb.ts_id = le.ts_id
        LEFT JOIN building_centroids bc ON mb.pnu  = bc.pnu
        ORDER BY mb.alias NULLS LAST, mb.ts_id
    """)
    rows = db.execute(sql).fetchall()
    logger.info("Monitor building list: %d rows", len(rows))

    items = [
        {
            "ts_id":       r.ts_id,
            "pnu":         r.pnu,
            "alias":       r.alias,
            "meter_types": r.meter_types or [],
            "total_area":  float(r.total_area) if r.total_area else None,
            "usage_type":  r.usage_type,
            "built_year":  r.built_year,
            "eui_kwh_m2":  float(r.eui_kwh_m2) if r.eui_kwh_m2 else None,
            "lng":         float(r.lng) if r.lng else None,
            "lat":         float(r.lat) if r.lat else None,
        }
        for r in rows
    ]
    result = {"count": len(items), "buildings": items}
    _cache_set(cache_key, result, ttl=3600)  # 1시간 (모니터링 건물 목록은 자주 변하지 않음)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/monitor/buildings/{ts_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/buildings/{ts_id}", response_model=MonitorBuildingDetail)
def get_monitor_building(
    ts_id: int,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    건물 메타 + 최근 30일 일별 집계.

    캐시: Redis, TTL 300초.
    일별 집계는 meter_type별로 각각 집계해서 반환한다.
    """
    cache_key = f"monitor:building:{ts_id}:detail"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    meta_sql = text("""
        SELECT
            mb.ts_id,
            mb.pnu,
            mb.alias,
            mb.meter_types,
            mb.total_area,
            mb.usage_type,
            mb.built_year,
            mb.data_source,
            mb.created_at,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat
        FROM monitored_buildings mb
        LEFT JOIN building_centroids bc ON mb.pnu = bc.pnu
        WHERE mb.ts_id = :ts_id
    """)
    meta = db.execute(meta_sql, {"ts_id": ts_id}).fetchone()
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Monitored building ts_id={ts_id} not found")

    daily_sql = text("""
        SELECT
            date_trunc('day', recorded_at)::date AS day,
            meter_type,
            SUM(value)                           AS total_value,
            MAX(unit)                            AS unit
        FROM metered_readings
        WHERE ts_id = :ts_id
          AND recorded_at >= NOW() - INTERVAL '30 days'
        GROUP BY date_trunc('day', recorded_at)::date, meter_type
        ORDER BY day, meter_type
    """)
    daily_rows = db.execute(daily_sql, {"ts_id": ts_id}).fetchall()

    # meter_type별로 일별 배열 구성
    daily_by_meter: dict[str, list] = {}
    for r in daily_rows:
        key = r.meter_type
        daily_by_meter.setdefault(key, []).append({
            "day":   str(r.day),
            "value": round(float(r.total_value), 2) if r.total_value else 0.0,
            "unit":  r.unit,
        })

    result = {
        "ts_id":       meta.ts_id,
        "pnu":         meta.pnu,
        "alias":       meta.alias,
        "meter_types": meta.meter_types or [],
        "total_area":  float(meta.total_area) if meta.total_area else None,
        "usage_type":  meta.usage_type,
        "built_year":  meta.built_year,
        "data_source": meta.data_source,
        "created_at":  meta.created_at.isoformat() if meta.created_at else None,
        "lng":         float(meta.lng) if meta.lng else None,
        "lat":         float(meta.lat) if meta.lat else None,
        "daily_30d":   daily_by_meter,
    }
    _cache_set(cache_key, result, ttl=1800)  # 30분 (건물 상세 정보)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/monitor/timeseries/{ts_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/timeseries/{ts_id}", response_model=TimeseriesResponse)
def get_timeseries(
    ts_id: int,
    start: Optional[str] = Query(None, description="ISO 8601 시작일 (예: 2026-01-01)"),
    end: Optional[str] = Query(None, description="ISO 8601 종료일 (예: 2026-03-27)"),
    resolution: str = Query("hourly", description="raw | hourly | daily | weekly"),
    meter: str = Query("electricity", description="electricity | gas | heat | water"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    특정 건물의 시계열 데이터 조회.

    - raw: 원본 15분~1시간 단위 레코드 반환
    - hourly / daily / weekly: date_trunc 기반 GROUP BY SUM 집계

    캐시 전략:
      raw      → TTL 30초  (최신 계량값이 자주 추가됨)
      hourly   → TTL 120초
      daily    → TTL 600초
      weekly   → TTL 1800초

    30일 hourly 기준 약 720행 → < 200ms 목표 (metered_readings 복합 인덱스 필요).
    존재하지 않는 ts_id는 404를 반환한다 (빈 배열 반환 방지).
    """
    # ts_id 존재 여부 검증 — start/end 파라미터 체크보다 먼저 실행 (404 우선)
    ts_exists_sql = text(
        "SELECT 1 FROM monitored_buildings WHERE ts_id = :ts_id LIMIT 1"
    )
    if db.execute(ts_exists_sql, {"ts_id": ts_id}).fetchone() is None:
        raise HTTPException(
            status_code=404,
            detail=f"Monitored building ts_id={ts_id} not found",
        )

    if start is None or end is None:
        raise HTTPException(status_code=422, detail="start and end query parameters are required")

    if resolution not in _RESOLUTION_SQL:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid resolution '{resolution}'. Choices: {list(_RESOLUTION_SQL.keys())}",
        )
    if meter not in ("electricity", "gas", "heat", "water"):
        raise HTTPException(status_code=400, detail=f"Unknown meter type: '{meter}'")

    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="start/end must be ISO 8601 format (YYYY-MM-DD)")

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (end_dt - start_dt).days > 366:
        raise HTTPException(status_code=400, detail="Maximum query range is 366 days")

    ttl_map = {"raw": 30, "hourly": 120, "daily": 600, "weekly": 1800}
    cache_key = (
        f"monitor:ts:{ts_id}:{resolution}:{meter}:"
        f"{start_dt.date()}:{end_dt.date()}"
    )
    cached = _cache_get(cache_key)
    if cached:
        return cached

    sql = text(_RESOLUTION_SQL[resolution])
    rows = db.execute(
        sql,
        {"ts_id": ts_id, "meter": meter, "start": start_dt, "end": end_dt},
    ).fetchall()

    points = [
        {
            "ts":    r.ts.isoformat() if hasattr(r.ts, "isoformat") else str(r.ts),
            "value": round(float(r.value), 4) if r.value is not None else None,
            "unit":  r.unit,
        }
        for r in rows
    ]

    result = {
        "ts_id":      ts_id,
        "meter":      meter,
        "resolution": resolution,
        "start":      start_dt.isoformat(),
        "end":        end_dt.isoformat(),
        "count":      len(points),
        "points":     points,
    }
    _cache_set(cache_key, result, ttl=ttl_map[resolution])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/monitor/compare
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/compare", response_model=MonitorCompareResponse)
def compare_buildings(
    ids: str = Query(..., description="콤마로 구분된 ts_id 목록 (예: 1,2,3)"),
    period: str = Query("30d", description="비교 기간 (예: 30d, 7d, 90d)"),
    metric: str = Query("eui", description="eui | electricity | gas | heat | water"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    여러 건물 에너지 지표 비교.

    - metric=eui: 기간 합산 kWh / 연면적 (kWh/m²)
    - metric=electricity|gas|heat|water: 기간 합산 소비량 (kWh)

    캐시: TTL 120초.
    최대 비교 건물 수: 10개.
    """
    try:
        ts_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
    if not ts_ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    if len(ts_ids) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 buildings per comparison")
    if metric not in ("eui", "electricity", "gas", "heat", "water"):
        raise HTTPException(status_code=400, detail=f"Unknown metric: '{metric}'")

    delta = _parse_period(period)
    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - delta

    cache_key = f"monitor:compare:{','.join(map(str, sorted(ts_ids)))}:{period}:{metric}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # 요청된 ts_id가 모두 monitored_buildings에 존재하는지 검증한다.
    # metered_readings 집계 전에 확인하여 등록되지 않은 ts_id와
    # 등록됐지만 데이터 없는 건물을 명확히 구별한다.
    exists_sql = text(
        "SELECT ts_id FROM monitored_buildings WHERE ts_id = ANY(:ts_ids)"
    )
    existing_ids = {
        r.ts_id for r in db.execute(exists_sql, {"ts_ids": ts_ids}).fetchall()
    }
    missing_ids = [tid for tid in ts_ids if tid not in existing_ids]
    if missing_ids:
        raise HTTPException(
            status_code=400,
            detail=f"존재하지 않는 ts_id가 포함되어 있습니다: {missing_ids}",
        )

    # metric에 따라 필터 방식을 분기한다.
    # f-string으로 meter_type 값을 SQL에 직접 삽입하지 않고,
    # eui 여부를 불리언 파라미터로 처리해 SQL injection 경로를 제거한다.
    if metric == "eui":
        sql = text("""
            SELECT
                mr.ts_id,
                mb.alias,
                mb.pnu,
                mb.total_area,
                SUM(mr.value) AS total_kwh
            FROM metered_readings mr
            JOIN monitored_buildings mb ON mr.ts_id = mb.ts_id
            WHERE mr.ts_id = ANY(:ts_ids)
              AND mr.meter_type IN ('electricity', 'gas')
              AND mr.recorded_at BETWEEN :start AND :end
            GROUP BY mr.ts_id, mb.alias, mb.pnu, mb.total_area
            ORDER BY mr.ts_id
        """)
        rows = db.execute(
            sql, {"ts_ids": ts_ids, "start": start_dt, "end": end_dt}
        ).fetchall()
    else:
        sql = text("""
            SELECT
                mr.ts_id,
                mb.alias,
                mb.pnu,
                mb.total_area,
                SUM(mr.value) AS total_kwh
            FROM metered_readings mr
            JOIN monitored_buildings mb ON mr.ts_id = mb.ts_id
            WHERE mr.ts_id = ANY(:ts_ids)
              AND mr.meter_type = :metric
              AND mr.recorded_at BETWEEN :start AND :end
            GROUP BY mr.ts_id, mb.alias, mb.pnu, mb.total_area
            ORDER BY mr.ts_id
        """)
        rows = db.execute(
            sql, {"ts_ids": ts_ids, "start": start_dt, "end": end_dt, "metric": metric}
        ).fetchall()

    # ts_id → 결과 매핑 (데이터 없는 건물도 포함)
    row_map = {r.ts_id: r for r in rows}
    buildings = []
    for tid in ts_ids:
        r = row_map.get(tid)
        if r is None:
            buildings.append({
                "ts_id":   tid,
                "alias":   None,
                "pnu":     None,
                "value":   None,
                "unit":    "kWh/m²" if metric == "eui" else "kWh",
            })
            continue

        if metric == "eui":
            value = (
                round(float(r.total_kwh) / float(r.total_area), 2)
                if r.total_kwh and r.total_area
                else None
            )
            unit = "kWh/m²"
        else:
            value = round(float(r.total_kwh), 2) if r.total_kwh else None
            unit = "kWh"

        buildings.append({
            "ts_id":  tid,
            "alias":  r.alias,
            "pnu":    r.pnu,
            "value":  value,
            "unit":   unit,
        })

    result = {
        "metric":  metric,
        "period":  period,
        "start":   start_dt.isoformat(),
        "end":     end_dt.isoformat(),
        "buildings": buildings,
    }
    _cache_set(cache_key, result, ttl=120)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/monitor/anomalies
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/anomalies", response_model=MonitorAnomalyResponse)
def get_anomalies(
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    최근 7일 이상치 건물 목록.

    이상치 기준: Celery beat(1시간 주기)가 metered_readings 집계를
    anomaly_log 테이블에 기록한 결과를 반환한다.
    실시간 계산이 아닌 사전 집계 결과 조회이므로 캐시 TTL은 60초.

    anomaly_log 스키마:
        ts_id, meter_type, detected_at,
        window_mean, window_std, offending_value, z_score
    """
    cache_key = "monitor:anomalies:7d"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    sql = text("""
        SELECT
            al.ts_id,
            mb.alias,
            mb.pnu,
            al.meter_type,
            al.detected_at,
            al.window_mean,
            al.window_std,
            al.offending_value,
            al.z_score
        FROM anomaly_log al
        JOIN monitored_buildings mb ON al.ts_id = mb.ts_id
        WHERE al.detected_at >= NOW() - INTERVAL '7 days'
        ORDER BY al.detected_at DESC
        LIMIT 200
    """)
    rows = db.execute(sql).fetchall()

    items = [
        {
            "ts_id":           r.ts_id,
            "alias":           r.alias,
            "pnu":             r.pnu,
            "meter_type":      r.meter_type,
            "detected_at":     r.detected_at.isoformat() if r.detected_at else None,
            "window_mean":     round(float(r.window_mean), 4) if r.window_mean is not None else None,
            "window_std":      round(float(r.window_std), 4)  if r.window_std  is not None else None,
            "offending_value": round(float(r.offending_value), 4) if r.offending_value is not None else None,
            "z_score":         round(float(r.z_score), 2)     if r.z_score     is not None else None,
        }
        for r in rows
    ]
    result = {"count": len(items), "anomalies": items}
    _cache_set(cache_key, result, ttl=600)  # 10분 (이상치는 상대적으로 자주 갱신)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/v1/monitor/readings  — CSV 업로드
# ─────────────────────────────────────────────────────────────────────────────

# 지원 포맷 컬럼 매핑
# KEPCO AMI CSV: "측정시각,전력량(kWh)"
# 가스공사 CSV:  "검침일시,사용량(m3)"
# 범용 포맷:     "ts,value" or "datetime,value"

_KNOWN_TS_COLUMNS = {"측정시각", "검침일시", "datetime", "ts", "timestamp", "date_time", "time"}
_KNOWN_VALUE_COLUMNS = {"전력량(kwh)", "사용량(m3)", "value", "kwh", "consumption", "usage"}


def _detect_columns(header: list[str]) -> tuple[str, str]:
    """
    CSV 헤더에서 타임스탬프 컬럼과 값 컬럼을 자동 탐지.
    탐지 실패 시 HTTPException 400.
    """
    header_lower = [h.strip().lower() for h in header]

    ts_col: Optional[str] = None
    val_col: Optional[str] = None

    for i, h in enumerate(header_lower):
        if h in _KNOWN_TS_COLUMNS:
            ts_col = header[i]
        if h in _KNOWN_VALUE_COLUMNS:
            val_col = header[i]

    # 포맷 미인식 시 첫 두 컬럼을 (ts, value)로 가정
    if ts_col is None:
        ts_col = header[0]
    if val_col is None and len(header) >= 2:
        val_col = header[1]

    if ts_col is None or val_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "CSV 컬럼 자동 탐지 실패. "
                f"지원 타임스탬프 컬럼: {_KNOWN_TS_COLUMNS}. "
                f"지원 값 컬럼: {_KNOWN_VALUE_COLUMNS}"
            ),
        )
    return ts_col, val_col


def _parse_ts(value: str) -> datetime:
    """다양한 날짜 포맷을 파싱. 파싱 실패 시 ValueError."""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y%m%d%H%M",
        "%Y-%m-%d %H:%M",
        "%Y%m%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 파싱 실패: '{value}'")


@router.post("/readings", response_model=ReadingUploadResponse)
async def upload_readings(
    ts_id: int = Query(..., description="대상 monitored_buildings.ts_id"),
    meter: str = Query("electricity", description="electricity | gas | heat | water"),
    unit: str = Query("kWh", description="계량 단위 (kWh, m3, MJ 등)"),
    file: Optional[UploadFile] = File(None, description="CSV 파일 (KEPCO AMI / 가스공사 / 범용 포맷)"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    계량값 CSV 수동 업로드.

    - KEPCO AMI: 측정시각 / 전력량(kWh) 컬럼
    - 가스공사:   검침일시 / 사용량(m3) 컬럼
    - 범용:       첫 번째 컬럼=타임스탬프, 두 번째 컬럼=값

    중복 삽입은 (ts_id, meter_type, recorded_at) UNIQUE 제약으로 무시(ON CONFLICT DO NOTHING).
    업로드 완료 후 해당 건물의 캐시를 무효화한다.
    최대 파일 크기: 10MB / 최대 행 수: 100,000행.

    검증 순서:
      1. meter / unit 파라미터 유효성 (400)
      2. ts_id 존재 여부 (404) — file 유무보다 선행
      3. file 필수 여부 (400)
    """
    if meter not in ("electricity", "gas", "heat", "water"):
        raise HTTPException(status_code=400, detail=f"Unknown meter type: '{meter}'")

    # unit 파라미터 화이트리스트 검증 (임의 문자열이 DB에 저장되는 것을 방지)
    _ALLOWED_UNITS = {"kWh", "m3", "MJ", "Gcal", "Nm3"}
    if unit not in _ALLOWED_UNITS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown unit '{unit}'. Allowed: {sorted(_ALLOWED_UNITS)}",
        )

    # ts_id 존재 여부 검증 (file 검증보다 먼저 실행하여 404가 422보다 우선하도록 함)
    # UploadFile을 Optional로 선언해 FastAPI Request Validation이 file 미첨부 시
    # 422를 즉시 반환하는 것을 방지한다.
    ts_exists_sql = text(
        "SELECT 1 FROM monitored_buildings WHERE ts_id = :ts_id LIMIT 1"
    )
    if db.execute(ts_exists_sql, {"ts_id": ts_id}).fetchone() is None:
        raise HTTPException(
            status_code=404,
            detail=f"Monitored building ts_id={ts_id} not found",
        )

    # ts_id 검증 통과 후 file 필수 검사
    if file is None:
        raise HTTPException(status_code=400, detail="CSV 파일이 필요합니다 (multipart/form-data)")

    # 파일 크기 제한 (10MB)
    MAX_SIZE = 10 * 1024 * 1024
    content = await file.read(MAX_SIZE + 1)
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="파일 크기가 10MB를 초과합니다")

    # BOM 제거 후 디코딩 (EUC-KR / UTF-8-SIG 대응)
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text_content = content.decode("euc-kr")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="파일 인코딩을 인식할 수 없습니다 (UTF-8 또는 EUC-KR 필요)")

    reader = csv.DictReader(io.StringIO(text_content))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV 헤더를 읽을 수 없습니다")

    ts_col, val_col = _detect_columns(list(reader.fieldnames))

    rows_to_insert: list[dict] = []
    parse_errors = 0
    row_count = 0

    for row in reader:
        row_count += 1
        if row_count > 100_000:
            raise HTTPException(status_code=400, detail="최대 100,000행을 초과합니다")

        raw_ts = row.get(ts_col, "").strip()
        raw_val = row.get(val_col, "").strip()

        if not raw_ts or not raw_val:
            parse_errors += 1
            continue

        try:
            recorded_at = _parse_ts(raw_ts)
            value = float(raw_val.replace(",", ""))
        except (ValueError, TypeError):
            parse_errors += 1
            continue

        rows_to_insert.append({
            "ts_id":       ts_id,
            "meter_type":  meter,
            "recorded_at": recorded_at,
            "value":       value,
            "unit":        unit,
        })

    if not rows_to_insert:
        raise HTTPException(
            status_code=422,
            detail=f"유효한 행이 없습니다. 파싱 오류: {parse_errors}행",
        )

    # 배치 삽입 (1,000행 단위 청크)
    inserted = 0
    CHUNK = 1_000
    insert_sql = text("""
        INSERT INTO metered_readings (ts_id, meter_type, recorded_at, value, unit)
        VALUES (:ts_id, :meter_type, :recorded_at, :value, :unit)
        ON CONFLICT (ts_id, meter_type, recorded_at) DO NOTHING
    """)
    for i in range(0, len(rows_to_insert), CHUNK):
        chunk = rows_to_insert[i : i + CHUNK]
        result = db.execute(insert_sql, chunk)
        inserted += result.rowcount
    db.commit()

    # 캐시 무효화: 해당 건물 상세, 시계열, 이상치
    _cache_delete_pattern(f"monitor:ts:{ts_id}:*")
    _cache_delete_pattern(f"monitor:building:{ts_id}:*")
    _cache_delete_pattern("monitor:anomalies:*")

    logger.info(
        "Readings upload ts_id=%d meter=%s: %d inserted / %d parse errors",
        ts_id, meter, inserted, parse_errors,
    )

    return {
        "ts_id":        ts_id,
        "meter":        meter,
        "rows_parsed":  row_count,
        "rows_inserted": inserted,
        "parse_errors": parse_errors,
        "skipped_duplicates": len(rows_to_insert) - inserted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket /ws/monitor/{ts_id}
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_push_latest(ws: WebSocket, ts_id: int) -> None:
    """DB에서 해당 건물의 최신 계량값 1행을 조회해 WebSocket으로 전송.

    engine.connect()는 컨텍스트 매니저로 사용해 커넥션을 즉시 반납한다.
    30초 루프에서 반복 호출되므로 커넥션을 보유한 채 sleep하면 풀이 고갈된다.
    """
    sql = text("""
        SELECT recorded_at, meter_type, value, unit
        FROM metered_readings
        WHERE ts_id = :ts_id
        ORDER BY recorded_at DESC
        LIMIT 1
    """)
    # with 블록이 종료되는 즉시 커넥션이 풀로 반납됨 (sleep 전에 반납)
    with engine.connect() as conn:
        row = conn.execute(sql, {"ts_id": ts_id}).fetchone()

    if row:
        payload = {
            "ts_id":       ts_id,
            "recorded_at": row.recorded_at.isoformat(),
            "meter_type":  row.meter_type,
            "value":       float(row.value) if row.value is not None else None,
            "unit":        row.unit,
        }
        await ws.send_text(json.dumps(payload))


async def _anomaly_subscriber(ws: WebSocket, ts_id: int, stop_event: asyncio.Event) -> None:
    """
    Redis pub/sub 채널 monitor:anomaly:events 를 구독해 해당 ts_id의
    이상치 이벤트를 즉시 WebSocket으로 전달한다.

    redis.asyncio(redis-py 4.2+)를 사용해 이벤트 루프를 블로킹하지 않는다.
    stop_event 가 set되면 구독을 종료한다 (WebSocket 연결 해제 시).
    """
    import redis.asyncio as aioredis

    try:
        async_redis = aioredis.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
        pubsub = async_redis.pubsub()
        await pubsub.subscribe(_ANOMALY_CHANNEL)

        try:
            while not stop_event.is_set():
                # get_message(timeout=1)은 최대 1초 대기 후 None 반환.
                # asyncio.wait_for 없이 직접 timeout 파라미터를 사용한다.
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    try:
                        payload = json.loads(message["data"])
                    except (json.JSONDecodeError, KeyError):
                        continue
                    # 해당 ts_id의 이상치만 전달
                    if payload.get("ts_id") == ts_id:
                        anomaly_msg = {
                            "type":            "anomaly",
                            "ts_id":           payload["ts_id"],
                            "meter_type":      payload.get("meter_type"),
                            "detected_at":     payload.get("detected_at"),
                            "offending_value": payload.get("offending_value"),
                            "z_score":         payload.get("z_score"),
                        }
                        try:
                            await ws.send_text(json.dumps(anomaly_msg))
                        except Exception:
                            # WebSocket이 이미 닫혔으면 루프 종료
                            break
        finally:
            await pubsub.unsubscribe(_ANOMALY_CHANNEL)
            await async_redis.aclose()
    except Exception as exc:
        logger.warning("WS anomaly subscriber error ts_id=%d: %s", ts_id, exc)


async def monitor_ws(ws: WebSocket, ts_id: int) -> None:
    """
    WebSocket 실시간 스트림 핸들러.

    두 가지 push 경로를 병렬로 운영한다:
    1) 30초 폴링 — 최신 계량값을 주기적으로 push
    2) Redis pub/sub — Celery 이상치 감지 결과를 즉시 push
         (tasks.py detect_anomalies_task → Redis monitor:anomaly:events 채널)

    연결 URL: ws://host/ws/monitor/{ts_id}
    계량값 메시지 포맷: {"ts_id", "recorded_at", "meter_type", "value", "unit"}
    이상치 메시지 포맷: {"type":"anomaly", "ts_id", "meter_type", "detected_at",
                         "offending_value", "z_score"}
    """
    await ws.accept()
    _monitor_ws_clients.setdefault(ts_id, set()).add(ws)
    logger.info("WS monitor connect: ts_id=%d", ts_id)

    stop_event = asyncio.Event()

    async def _poll_loop() -> None:
        """30초 주기로 최신 계량값을 push. stop_event 설정 시 종료."""
        try:
            await _ws_push_latest(ws, ts_id)
            while not stop_event.is_set():
                await asyncio.sleep(30)
                await _ws_push_latest(ws, ts_id)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("WS monitor poll error ts_id=%d: %s", ts_id, exc)
        finally:
            stop_event.set()

    try:
        await asyncio.gather(
            _poll_loop(),
            _anomaly_subscriber(ws, ts_id, stop_event),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.warning("WS monitor gather error ts_id=%d: %s", ts_id, exc)
    finally:
        stop_event.set()
        clients = _monitor_ws_clients.get(ts_id, set())
        clients.discard(ws)
        # 해당 ts_id에 구독자가 없으면 빈 set을 딕셔너리에서 제거한다.
        # 제거하지 않으면 건물 수 × 접속 이력만큼 빈 set이 메모리에 누적된다.
        if not clients:
            _monitor_ws_clients.pop(ts_id, None)
        logger.info("WS monitor disconnect: ts_id=%d", ts_id)
