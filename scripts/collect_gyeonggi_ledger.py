"""경기도·인천 건축물대장 수집 스크립트.

우리 DB의 building_footprints에 이미 있는 경기도·인천 건물에 대해
건축물대장(총괄표제부+표제부)을 수집한다.

서울 수집 파이프라인과 동일한 collect_seoul_ledger()를 재사용.
수집 후 buildings_enriched / building_centroids 뷰 자동 갱신.

Usage (Docker 컨테이너 안에서):
    python scripts/collect_gyeonggi_ledger.py

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

from src.data_ingestion.collect_ledger import collect_seoul_ledger
from sqlalchemy import create_engine, text

# ── 수집 대상 시군구코드 ─────────────────────────────────────────────────────
# building_footprints에 실제로 존재하는 코드만 포함
# (서울 bounding box 수집 시 경계 건물로 포함된 인접 시군구)

GYEONGGI_SIGUNGU_CODES = [
    # ── 고양시 ──────────────
    "41281",  # 고양시 덕양구  (약 19,289동)
    "41285",  # 고양시 일산동구 (약 9,968동)
    "41287",  # 고양시 일산서구 (약 1,115동)
    # ── 성남시 ──────────────
    "41131",  # 성남시 수정구  (약 19,318동)
    "41133",  # 성남시 중원구  (약 10,864동)
    # ── 하남시 ──────────────
    "41195",  # 하남시         (약 14,189동)
    # ── 남양주시 ────────────
    "41360",  # 남양주시       (약 9,545동)
    # ── 구리시 ──────────────
    "41310",  # 구리시         (약 8,424동)
    # ── 의왕시·군포·안양 ──────
    "41199",  # 의왕시 등      (약 9,377동)
    "41197",  # 군포시 등      (약 8,388동)
    "41210",  # 평택시 등      (약 7,791동)
    "41390",  # 시흥시         (약 7,202동)
    # ── 광명시 ──────────────
    "41190",  # 광명시         (약 2,679동)
    "41192",  # 광명시 인접    (약 63동)
    "41194",  # 소규모         (약 61동)
    "41196",  # 소규모         (약 82동)
    # ── 과천시 ──────────────
    "41290",  # 과천시         (약 2,079동)
    # ── 안양시 ──────────────
    "41171",  # 안양시 만안구  (약 108동)
    "41173",  # 안양시 동안구  (약 1동)
    # ── 하남·김포 ────────────
    "41450",  # 하남시(구코드) (약 3,041동)
    "41570",  # 김포시         (약 1,248동)
    # ── 광주·양주 ────────────
    "41610",  # 광주시         (약 7동)
    "41630",  # 양주시         (약 399동)
    # ── 의정부시 ────────────
    "41150",  # 의정부시       (약 14동)
]

INCHEON_SIGUNGU_CODES = [
    "28200",  # 인천 남동구    (약 398동)
    "28237",  # 인천 부평구    (약 3동)
    "28245",  # 인천 계양구    (약 504동)
]

ALL_CODES = GYEONGGI_SIGUNGU_CODES + INCHEON_SIGUNGU_CODES

engine = create_engine(db_url)

# 현재 상태 확인
with engine.connect() as conn:
    row = conn.execute(text(
        "SELECT COUNT(*) as cnt, COUNT(DISTINCT LEFT(pnu,5)) as sgg_cnt "
        "FROM building_ledger WHERE LEFT(pnu,2) IN ('41','28')"
    )).fetchone()
    logger.info("경기도·인천 building_ledger 현황: %d건, %d개 시군구", row.cnt, row.sgg_cnt)

    # 기존 데이터 있는 코드 확인
    existing = conn.execute(text(
        "SELECT DISTINCT LEFT(pnu,5) FROM building_ledger WHERE LEFT(pnu,2) IN ('41','28')"
    )).fetchall()
    existing_codes = {r[0] for r in existing}

# 미수집 코드만 대상으로
needs_collect = [c for c in ALL_CODES if c not in existing_codes]
already_done  = [c for c in ALL_CODES if c in existing_codes]

logger.info("이미 수집됨 (%d개): %s", len(already_done), already_done)
logger.info("수집 대상 (%d개): %s", len(needs_collect), needs_collect)

if not needs_collect:
    logger.info("모든 시군구 이미 수집 완료. 종료.")
    sys.exit(0)

# ── 총괄표제부 수집 ──────────────────────────────────────────────────────────
logger.info("=== 총괄표제부(recap) 수집 시작 — %d개 시군구 ===", len(needs_collect))
stats_recap = collect_seoul_ledger(api_key, db_url, ledger_type="recap",
                                   sigungu_codes=needs_collect)
logger.info("총괄표제부 완료: %d건, 실패 %d", stats_recap["total_records"],
            len(stats_recap.get("failed", [])))

# ── 표제부 수집 ─────────────────────────────────────────────────────────────
logger.info("=== 표제부(title) 수집 시작 — %d개 시군구 ===", len(needs_collect))
stats_title = collect_seoul_ledger(api_key, db_url, ledger_type="title",
                                   sigungu_codes=needs_collect)
logger.info("표제부 완료: %d건, 실패 %d", stats_title["total_records"],
            len(stats_title.get("failed", [])))

# ── Materialized view 갱신 ──────────────────────────────────────────────────
logger.info("=== buildings_enriched / building_centroids 뷰 갱신 ===")
with engine.connect() as conn:
    conn.execute(text("REFRESH MATERIALIZED VIEW buildings_enriched"))
    conn.execute(text("REFRESH MATERIALIZED VIEW building_centroids"))
    conn.commit()
    row = conn.execute(text(
        "SELECT COUNT(*) FROM building_ledger WHERE LEFT(pnu,2) IN ('41','28')"
    )).fetchone()
    logger.info("갱신 완료. 경기도·인천 building_ledger 총 %d건", row[0])

engine.dispose()
logger.info("=== 수집 완료 ===")
