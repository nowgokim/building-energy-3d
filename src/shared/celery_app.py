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
)

celery.autodiscover_tasks([
    "src.data_ingestion",
    "src.tile_generation",
    "src.simulation",
    "src.fire_safety",
])
