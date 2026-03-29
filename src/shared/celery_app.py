from celery import Celery
from .config import get_settings

settings = get_settings()

celery = Celery(
    "building_energy",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=600,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "collect-weather-hourly": {
            "task": "src.fire_safety.tasks.collect_weather_task",
            "schedule": 3600,   # 1시간마다
        },
        "detect-anomalies-hourly": {
            "task": "src.monitor.tasks.detect_anomalies_task",
            "schedule": 3600,   # 1시간마다
        },
        "refresh-mv-daily": {
            "task": "src.data_ingestion.tasks.refresh_mv_task",
            "schedule": 86400,  # 24시간마다
        },
        # ── Phase 4-E: ML 고도화 ───────────────────────────────────────────
        "xgb-retrain-weekly": {
            "task": "src.simulation.ml_retrain.retrain_xgboost_task",
            "schedule": 604800,  # 7일마다 (주간 재학습)
        },
        "batch-predict-weekly": {
            "task": "src.simulation.ml_predict_batch.predict_batch_task",
            "schedule": 604800,  # 7일마다 (재학습 완료 ~1시간 후 실행)
        },
        "ts-daily-retrain": {
            "task": "src.simulation.ts_daily_retrain.ts_daily_retrain_task",
            "schedule": 86400,  # 24시간마다 (Tier C 건물 일단위 재학습)
        },
    },
)

celery.autodiscover_tasks([
    "src.data_ingestion",
    "src.tile_generation",
    "src.simulation",
    "src.fire_safety",
    "src.monitor",
])
