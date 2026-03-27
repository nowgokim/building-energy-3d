"""서울 전역 건축물대장 수집 실행 스크립트.

표제부(title) 커버리지가 낮은 구를 자동 감지하여 재수집.
이미 충분히 수집된 구는 건너뜀 (중복 방지).

Usage (Docker 컨테이너 안에서):
    python scripts/collect_seoul_ledger.py

환경변수:
    DATA_GO_KR_API_KEY  — 공공데이터포털 API 키
    DATABASE_URL        — PostgreSQL 연결 URL
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

api_key = os.environ.get("DATA_GO_KR_API_KEY", "")
db_url  = os.environ.get("DATABASE_URL", "")

if not api_key:
    logger.error("DATA_GO_KR_API_KEY 환경변수가 설정되지 않았습니다.")
    sys.exit(1)
if not db_url:
    logger.error("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    sys.exit(1)

from src.data_ingestion.collect_ledger import SEOUL_SIGUNGU_CODES, collect_seoul_ledger
from sqlalchemy import create_engine, text

engine = create_engine(db_url)

# 구별 표제부(title) 커버리지 확인
# buildings_enriched.built_year 커버리지가 낮은 구 = 표제부 미수집
with engine.connect() as conn:
    row = conn.execute(text(
        "SELECT COUNT(*) as cnt, COUNT(DISTINCT LEFT(pnu,5)) as gu_cnt FROM building_ledger"
    )).fetchone()
    logger.info("현재 building_ledger: %d건, %d개 구", row.cnt, row.gu_cnt)

    coverage_rows = conn.execute(text("""
        SELECT
            LEFT(pnu, 5) AS gu_cd,
            COUNT(*) AS total,
            SUM(CASE WHEN built_year IS NOT NULL THEN 1 ELSE 0 END) AS with_year,
            ROUND(100.0 * SUM(CASE WHEN built_year IS NOT NULL THEN 1 ELSE 0 END)
                  / NULLIF(COUNT(*), 0), 1) AS pct
        FROM buildings_enriched
        WHERE pnu LIKE '11%'
        GROUP BY 1
        ORDER BY 1
    """)).fetchall()

# 표제부 재수집 필요 구: built_year 커버리지 < 50%
COVERAGE_THRESHOLD = 50.0
needs_title = [r.gu_cd for r in coverage_rows if float(r.pct or 0) < COVERAGE_THRESHOLD]
sufficient  = [r.gu_cd for r in coverage_rows if float(r.pct or 0) >= COVERAGE_THRESHOLD]

logger.info("커버리지 충분(≥%d%%): %d개 구 %s", int(COVERAGE_THRESHOLD), len(sufficient), sufficient)
logger.info("표제부 재수집 필요(<%%d%%): %d개 구 %s", int(COVERAGE_THRESHOLD), len(needs_title), needs_title)

if not needs_title:
    logger.info("모든 구 표제부 수집 완료. 종료.")
    sys.exit(0)

# 재수집 전 기존 미완성 데이터 삭제 (중복 방지)
logger.info("재수집 대상 구 기존 데이터 삭제 중...")
with engine.connect() as conn:
    for gu_cd in needs_title:
        result = conn.execute(text(
            "DELETE FROM building_ledger WHERE LEFT(pnu, 5) = :gu"
        ), {"gu": gu_cd})
        logger.info("  %s: %d건 삭제", gu_cd, result.rowcount)
    conn.commit()

# 총괄표제부 수집 (재수집 대상만)
logger.info("=== 총괄표제부(recap) 수집 시작 — %d개 구 ===", len(needs_title))
stats_recap = collect_seoul_ledger(api_key, db_url, ledger_type="recap",
                                   sigungu_codes=needs_title)
logger.info("총괄표제부 완료: %d건, 실패 %d", stats_recap["total_records"], len(stats_recap["failed"]))

# 표제부 수집 (재수집 대상만)
logger.info("=== 표제부(title) 수집 시작 — %d개 구 ===", len(needs_title))
stats_title = collect_seoul_ledger(api_key, db_url, ledger_type="title",
                                   sigungu_codes=needs_title)
logger.info("표제부 완료: %d건, 실패 %d", stats_title["total_records"], len(stats_title["failed"]))

# Materialized view 갱신
logger.info("=== buildings_enriched 뷰 갱신 ===")
with engine.connect() as conn:
    conn.execute(text("REFRESH MATERIALIZED VIEW buildings_enriched"))
    conn.execute(text("REFRESH MATERIALIZED VIEW building_centroids"))
    conn.commit()
    row = conn.execute(text("SELECT COUNT(*) FROM building_ledger")).fetchone()
    logger.info("갱신 완료. building_ledger 총 %d건", row[0])

engine.dispose()
logger.info("=== 수집 완료 ===")
