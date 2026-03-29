"""data_ingestion Celery 태스크

MV 정기 갱신 등 데이터 파이프라인 자동화 태스크.
"""
import logging
from src.shared.celery_app import celery
from src.shared.database import execute_ddl, execute_sql_scalar

logger = logging.getLogger(__name__)


@celery.task(bind=True, max_retries=2, name="src.data_ingestion.tasks.refresh_mv_task")
def refresh_mv_task(self):
    """Materialized View 무중단 갱신.

    실행 순서 (의존 관계 보장):
      1. buildings_enriched       — UNIQUE INDEX idx_enriched_gid_uniq 필요
      2. building_fire_risk       — buildings_enriched 참조, UNIQUE INDEX idx_fire_risk_pnu_uniq 필요
      3. model_accuracy_summary  — 독립적, UNIQUE INDEX idx_model_accuracy_uniq 필요

    CONCURRENTLY: 갱신 중에도 SELECT 가능 (무중단). UNIQUE INDEX 없으면 일반 REFRESH로 폴백.
    """
    _MVS = [
        ("buildings_enriched",      "idx_enriched_gid_uniq"),
        ("building_fire_risk",      "idx_fire_risk_gid_uniq"),
        ("model_accuracy_summary",  "idx_model_accuracy_uniq"),
    ]
    results = {}
    try:
        for mv_name, uniq_idx in _MVS:
            # UNIQUE INDEX 존재 여부 확인 → CONCURRENTLY 사용 여부 결정
            idx_row = execute_sql_scalar(
                "SELECT COUNT(*) FROM pg_indexes "
                "WHERE tablename = :mv AND indexname = :idx",
                {"mv": mv_name, "idx": uniq_idx},
            )
            has_uniq = (idx_row[0] if idx_row else 0) > 0
            mode = "CONCURRENTLY" if has_uniq else ""
            logger.info("%s REFRESH %s 시작", mv_name, mode)
            execute_ddl(f"REFRESH MATERIALIZED VIEW {mode} {mv_name}")
            logger.info("%s REFRESH 완료", mv_name)
            results[mv_name] = "concurrently" if has_uniq else "blocking"

        row = execute_sql_scalar("SELECT COUNT(*) FROM buildings_enriched")
        count = row[0] if row else 0
        logger.info("buildings_enriched 총 %d건", count)
        return {"status": "ok", "enriched_count": count, "modes": results}

    except Exception as exc:
        logger.error("MV REFRESH 실패: %s", exc)
        raise self.retry(exc=exc, countdown=300)  # 5분 후 재시도
