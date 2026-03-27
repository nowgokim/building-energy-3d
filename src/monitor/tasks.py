"""
모니터링 Celery 태스크.

detect_anomalies_task: 1시간 주기 beat 태스크.
  - metered_readings에서 직전 48시간 데이터 조회
  - 건물·계량기 단위로 rolling 24h window mean/std 계산
  - |z-score| > 2 인 값을 anomaly_log에 upsert
  - 이상치 발생 시 monitor WebSocket 구독자에게 push (asyncio event loop 없는
    환경이므로 Redis pub/sub 채널에 게시, FastAPI 측에서 구독 후 전달)
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.shared.celery_app import celery
from src.shared.config import get_settings
from src.shared.database import engine

logger = logging.getLogger(__name__)

# Redis pub/sub 채널명 (FastAPI WebSocket 핸들러가 구독)
_ANOMALY_CHANNEL = "monitor:anomaly:events"


# ─────────────────────────────────────────────────────────────────────────────
# 이상치 감지 Celery 태스크
# ─────────────────────────────────────────────────────────────────────────────


@celery.task(name="src.monitor.tasks.detect_anomalies_task", bind=True, max_retries=2)
def detect_anomalies_task(self) -> dict:
    """
    Rolling 24h window 기반 이상치 감지.

    알고리즘:
    1. 직전 48시간 metered_readings를 (ts_id, meter_type) 단위로 집계
    2. 각 계량기에 대해 PostgreSQL window function으로 rolling 24h mean/std 계산
    3. |z-score| > 2 인 레코드를 anomaly_log에 INSERT (중복 무시)
    4. 신규 이상치를 Redis pub/sub 채널에 게시

    PostgreSQL window function을 사용해 Python 레벨 루프를 최소화한다.
    """
    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(hours=48)

    # ── Step 1: window function으로 z-score 계산 ─────────────────────────────
    # 48h 윈도우에서 각 포인트의 앞 24h 행 기준 mean/std를 구한다.
    # ROWS BETWEEN 96 PRECEDING AND 1 PRECEDING = 15분 해상도 기준 약 24h (96 = 24*4).
    # 1시간 해상도면 ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING.
    # 두 해상도를 모두 지원하기 위해 RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND
    # INTERVAL '1 second' PRECEDING 을 사용한다.

    anomaly_sql = text("""
        WITH windowed AS (
            SELECT
                ts_id,
                meter_type,
                recorded_at,
                value,
                AVG(value) OVER (
                    PARTITION BY ts_id, meter_type
                    ORDER BY recorded_at
                    RANGE BETWEEN INTERVAL '24 hours' PRECEDING
                              AND INTERVAL '1 second' PRECEDING
                ) AS win_mean,
                STDDEV_POP(value) OVER (
                    PARTITION BY ts_id, meter_type
                    ORDER BY recorded_at
                    RANGE BETWEEN INTERVAL '24 hours' PRECEDING
                              AND INTERVAL '1 second' PRECEDING
                ) AS win_std,
                COUNT(*) OVER (
                    PARTITION BY ts_id, meter_type
                    ORDER BY recorded_at
                    RANGE BETWEEN INTERVAL '24 hours' PRECEDING
                              AND INTERVAL '1 second' PRECEDING
                ) AS win_count
            FROM metered_readings
            WHERE recorded_at BETWEEN :start AND :end
        ),
        anomalies AS (
            SELECT
                ts_id,
                meter_type,
                recorded_at,
                value,
                win_mean,
                win_std,
                CASE
                    WHEN win_std > 0
                    THEN ABS(value - win_mean) / win_std
                    ELSE 0
                END AS z_score
            FROM windowed
            -- 최소 6개 포인트 이상 관측 후에만 이상치 판정 (초기 데이터 노이즈 방지)
            WHERE win_count >= 6
        )
        SELECT *
        FROM anomalies
        WHERE z_score > 2.0
        ORDER BY ts_id, meter_type, recorded_at
    """)

    # ── Step 2: anomaly_log upsert ────────────────────────────────────────────
    insert_sql = text("""
        INSERT INTO anomaly_log
            (ts_id, meter_type, detected_at, window_mean, window_std,
             offending_value, z_score)
        VALUES
            (:ts_id, :meter_type, :detected_at, :window_mean, :window_std,
             :offending_value, :z_score)
        ON CONFLICT (ts_id, meter_type, detected_at) DO NOTHING
    """)

    inserted_count = 0
    new_anomalies: list[dict] = []
    # DB 예외 시 len(rows) 참조가 NameError가 되는 것을 방지한다.
    rows: list = []

    with engine.connect() as conn:
        rows = conn.execute(
            anomaly_sql, {"start": start_dt, "end": end_dt}
        ).fetchall()

        if rows:
            # ON CONFLICT DO NOTHING의 신규 삽입 여부를 rowcount로 추적하기 위해
            # 1건씩 execute한다. rows는 이상치 건수(수십~수백)이므로
            # 루프 오버헤드보다 정확한 inserted_count 추적이 중요하다.
            # (최대 48h × 모든 건물 × 이상치 발생률을 고려하면 수백 건 수준)
            for r in rows:
                params = {
                    "ts_id":           r.ts_id,
                    "meter_type":      r.meter_type,
                    "detected_at":     r.recorded_at,
                    "window_mean":     float(r.win_mean) if r.win_mean is not None else None,
                    "window_std":      float(r.win_std)  if r.win_std  is not None else None,
                    "offending_value": float(r.value)    if r.value    is not None else None,
                    "z_score":         float(r.z_score)  if r.z_score  is not None else None,
                }
                result = conn.execute(insert_sql, params)
                if result.rowcount > 0:
                    inserted_count += 1
                    new_anomalies.append({
                        "ts_id":           r.ts_id,
                        "meter_type":      r.meter_type,
                        "detected_at":     r.recorded_at.isoformat(),
                        "offending_value": float(r.value) if r.value is not None else None,
                        "z_score":         round(float(r.z_score), 2),
                    })

        conn.commit()

    # ── Step 3: 신규 이상치 Redis pub/sub 게시 ────────────────────────────────
    # Redis 클라이언트를 한 번만 생성해 pub/sub와 캐시 무효화에 재사용한다.
    # 이전 코드는 두 개의 try 블록에서 각각 from_url()을 호출해 커넥션을 낭비했다.
    if new_anomalies:
        try:
            import redis as redis_lib
            redis_client = redis_lib.from_url(
                get_settings().REDIS_URL, decode_responses=True
            )
            for anomaly in new_anomalies:
                redis_client.publish(_ANOMALY_CHANNEL, json.dumps(anomaly))
            # 이상치 목록 캐시 무효화 (같은 클라이언트 재사용)
            redis_client.delete("monitor:anomalies:7d")
        except Exception as exc:
            logger.warning("Redis publish/cache invalidation failed: %s", exc)

    # rows는 fetchall() 결과로 항상 리스트이므로 None 방어 불필요
    summary = {
        "checked_window": f"{start_dt.isoformat()} ~ {end_dt.isoformat()}",
        "total_anomalies_found": len(rows),
        "new_anomalies_inserted": inserted_count,
    }
    logger.info("Anomaly detection complete: %s", summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# beat schedule 등록 패치 (celery_app.py에 수동 추가 필요)
# ─────────────────────────────────────────────────────────────────────────────
#
# src/shared/celery_app.py의 beat_schedule에 아래 항목을 추가한다:
#
#   "detect-anomalies-hourly": {
#       "task": "src.monitor.tasks.detect_anomalies_task",
#       "schedule": 3600,
#   },
#
# celery_app.py의 autodiscover_tasks에 "src.monitor"를 추가한다.
