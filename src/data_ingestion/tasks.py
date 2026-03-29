"""data_ingestion Celery 태스크

MV 정기 갱신 등 데이터 파이프라인 자동화 태스크.
"""
import logging
from src.shared.celery_app import celery
from src.shared.database import execute_ddl, execute_sql_scalar

logger = logging.getLogger(__name__)


@celery.task(bind=True, max_retries=2, name="src.data_ingestion.tasks.refresh_mv_task")
def refresh_mv_task(self):
    """Materialized View 갱신 (buildings_enriched → building_fire_risk 순서 보장)."""
    try:
        logger.info("buildings_enriched REFRESH 시작")
        execute_ddl("REFRESH MATERIALIZED VIEW buildings_enriched")
        logger.info("buildings_enriched REFRESH 완료")

        logger.info("building_fire_risk REFRESH 시작")
        execute_ddl("REFRESH MATERIALIZED VIEW building_fire_risk")
        logger.info("building_fire_risk REFRESH 완료")

        row = execute_sql_scalar("SELECT COUNT(*) FROM buildings_enriched")
        count = row[0] if row else 0
        logger.info("buildings_enriched 총 %d건", count)
        return {"status": "ok", "enriched_count": count}

    except Exception as exc:
        logger.error("MV REFRESH 실패: %s", exc)
        raise self.retry(exc=exc, countdown=300)  # 5분 후 재시도
