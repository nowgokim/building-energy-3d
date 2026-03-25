"""서울 전역 건축물대장 수집 실행 스크립트.

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

# 이미 수집된 구 확인
engine = create_engine(db_url)
with engine.connect() as conn:
    row = conn.execute(text(
        "SELECT COUNT(*) as cnt, COUNT(DISTINCT LEFT(pnu,5)) as gu_cnt FROM building_ledger"
    )).fetchone()
    logger.info("현재 building_ledger: %d건, %d개 구", row.cnt, row.gu_cnt)
    existing_sgg = conn.execute(text(
        "SELECT DISTINCT LEFT(pnu,5) as sgg FROM building_ledger"
    )).fetchall()
    existing_codes = {r.sgg for r in existing_sgg}

logger.info("이미 수집된 구: %s", existing_codes)

# 미수집 구만 수집 (중복 방지)
remaining = [c for c in SEOUL_SIGUNGU_CODES if c not in existing_codes]
logger.info("수집 대상 구: %d개 %s", len(remaining), remaining)

if not remaining:
    logger.info("모든 구 수집 완료. 종료.")
    sys.exit(0)

# 총괄표제부 수집
logger.info("=== 총괄표제부(recap) 수집 시작 ===")
stats_recap = collect_seoul_ledger(api_key, db_url, ledger_type="recap")
logger.info("총괄표제부 완료: %d건, 실패 %d", stats_recap["total_records"], len(stats_recap["failed"]))

# 표제부 수집
logger.info("=== 표제부(title) 수집 시작 ===")
stats_title = collect_seoul_ledger(api_key, db_url, ledger_type="title")
logger.info("표제부 완료: %d건, 실패 %d", stats_title["total_records"], len(stats_title["failed"]))

# Materialized view 갱신
logger.info("=== buildings_enriched 뷰 갱신 ===")
with engine.connect() as conn:
    conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY buildings_enriched"))
    conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY building_centroids"))
    conn.commit()
    row = conn.execute(text("SELECT COUNT(*) FROM building_ledger")).fetchone()
    logger.info("갱신 완료. building_ledger 총 %d건", row[0])

engine.dispose()
logger.info("=== 수집 완료 ===")
