"""
Phase F1/F2 — building_adjacency 테이블 생성기

서울 전역 건물 간 25m 이내 인접 관계를 계산하여 building_adjacency 테이블에 적재한다.

성능 설계:
  - building_centroids (Point GiST) 로 공간 조인 → 구당 ~300ms
  - 도(degree) 단위 threshold로 인덱스 활용, ST_Distance::geography로 정확한 거리 계산
  - 양방향 저장 (A→B, B→A): BFS에서 임의 시작점에서 이웃 탐색 가능

spread_weight 공식:
  (1 - distance_m / radius) × structure_multiplier(source)
  structure_multiplier: masonry=1.4, steel=1.0, RC/기타=0.8

실행 예시:
  python -m src.data_ingestion.build_adjacency              # 서울 전체
  python -m src.data_ingestion.build_adjacency --district 11440  # 마포구만
  python -m src.data_ingestion.build_adjacency --dry-run    # 건수만 출력
  python -m src.data_ingestion.build_adjacency --radius 30  # 30m 기준
"""

import argparse
import logging
import time
from datetime import datetime

from sqlalchemy import text

from src.shared.database import engine as _engine

logger = logging.getLogger(__name__)

# 서울 25개 자치구 시군구코드 (PNU 앞 5자리)
SEOUL_SGG_CODES = [
    "11110",  # 종로구
    "11140",  # 중구
    "11170",  # 용산구
    "11200",  # 성동구
    "11215",  # 광진구
    "11230",  # 동대문구
    "11260",  # 중랑구
    "11290",  # 성북구
    "11305",  # 강북구
    "11320",  # 도봉구
    "11350",  # 노원구
    "11380",  # 은평구
    "11410",  # 서대문구
    "11440",  # 마포구
    "11470",  # 양천구
    "11500",  # 강서구
    "11530",  # 구로구
    "11545",  # 금천구
    "11560",  # 영등포구
    "11590",  # 동작구
    "11620",  # 관악구
    "11650",  # 서초구
    "11680",  # 강남구
    "11710",  # 송파구
    "11740",  # 강동구
]

# 25m를 도(degree) 단위로 변환 — 서울(37.5°N) 기준 안전 여유 포함
# 위도 방향: 25m / 111,320m ≈ 0.000225°
# 경도 방향: 25m / (111,320 × cos(37.5°)) ≈ 0.000284°
# → 0.0003° 사용 (약간 크게 설정, geography distance로 최종 필터링)
_DEGREE_THRESHOLD = 0.0003  # ST_DWithin geometry용 (인덱스 활용)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_SQL = text("""
WITH
/* 1. PNU당 centroid 1개 (building_centroids에 중복 PNU 존재) */
dedup_centroid AS (
    SELECT DISTINCT ON (pnu) pnu, centroid
    FROM building_centroids
    WHERE LEFT(pnu, 5) = :sgg_code
    ORDER BY pnu
),
/* 2. PNU당 structure_class 1개 (buildings_enriched에도 중복 PNU 존재) */
dedup_structure AS (
    SELECT DISTINCT ON (pnu) pnu, COALESCE(structure_class, 'RC') AS structure_class
    FROM buildings_enriched
    WHERE LEFT(pnu, 5) = :sgg_code
    ORDER BY pnu
),
/* 3. 정방향 이웃 쌍 (a.pnu < b.pnu, 중복 없음) */
raw_pairs AS (
    SELECT
        a.pnu AS pnu_a,
        b.pnu AS pnu_b,
        ST_Distance(a.centroid::geography, b.centroid::geography)::real AS dist_m
    FROM dedup_centroid a
    JOIN dedup_centroid b
        ON  a.pnu < b.pnu
        AND ST_DWithin(a.centroid, b.centroid, :deg_threshold)
    WHERE ST_Distance(a.centroid::geography, b.centroid::geography) <= :radius
),
/* 4. 구조계수 조인 (DISTINCT ON으로 이미 중복 없음) */
with_structure AS (
    SELECT
        p.pnu_a, p.pnu_b, p.dist_m,
        COALESCE(sa.structure_class, 'RC') AS sc_a,
        COALESCE(sb.structure_class, 'RC') AS sc_b
    FROM raw_pairs p
    LEFT JOIN dedup_structure sa ON sa.pnu = p.pnu_a
    LEFT JOIN dedup_structure sb ON sb.pnu = p.pnu_b
)
/* 5. 양방향 삽입 */
INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m, spread_weight)
SELECT src, tgt, dist_m, weight FROM (
    SELECT pnu_a AS src, pnu_b AS tgt, dist_m,
        GREATEST((1.0 - dist_m / :radius) *
            CASE sc_a WHEN 'masonry' THEN 1.4 WHEN 'steel' THEN 1.0 ELSE 0.8 END, 0.0
        )::real AS weight
    FROM with_structure
    UNION ALL
    SELECT pnu_b AS src, pnu_a AS tgt, dist_m,
        GREATEST((1.0 - dist_m / :radius) *
            CASE sc_b WHEN 'masonry' THEN 1.4 WHEN 'steel' THEN 1.0 ELSE 0.8 END, 0.0
        )::real AS weight
    FROM with_structure
) bidir
ON CONFLICT (source_pnu, target_pnu) DO UPDATE
    SET distance_m    = EXCLUDED.distance_m,
        spread_weight = EXCLUDED.spread_weight
""")

_COUNT_SQL = text("""
WITH dedup AS (
    SELECT DISTINCT ON (pnu) pnu, centroid
    FROM building_centroids
    WHERE LEFT(pnu, 5) = :sgg_code
    ORDER BY pnu
)
SELECT COUNT(*) * 2 AS bidirectional_pairs
FROM dedup a
JOIN dedup b
    ON  a.pnu < b.pnu
    AND ST_DWithin(a.centroid, b.centroid, :deg_threshold)
WHERE ST_Distance(a.centroid::geography, b.centroid::geography) <= :radius
""")

_DELETE_SQL = text("""
DELETE FROM building_adjacency
WHERE LEFT(source_pnu, 5) = :sgg_code
""")


# ---------------------------------------------------------------------------
# 내부 함수
# ---------------------------------------------------------------------------

def _ensure_spread_weight_column() -> None:
    with _engine.connect() as conn:
        conn.execute(text("""
            ALTER TABLE building_adjacency
            ADD COLUMN IF NOT EXISTS spread_weight REAL DEFAULT 0.0
        """))
        conn.commit()


def _process_district(sgg_code: str, radius: float, dry_run: bool) -> dict:
    params = {
        "sgg_code": sgg_code,
        "radius": float(radius),
        "deg_threshold": _DEGREE_THRESHOLD,
    }
    t0 = time.time()

    with _engine.connect() as conn:
        if dry_run:
            row = conn.execute(_COUNT_SQL, params).fetchone()
            pairs = row.bidirectional_pairs or 0
            return {"sgg_code": sgg_code, "pairs": pairs, "elapsed": time.time() - t0}

        conn.execute(_DELETE_SQL, {"sgg_code": sgg_code})
        result = conn.execute(_INSERT_SQL, params)
        inserted = result.rowcount
        conn.commit()

    return {"sgg_code": sgg_code, "inserted": inserted, "elapsed": time.time() - t0}


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def build_adjacency(
    districts: list[str] | None = None,
    radius: float = 25.0,
    dry_run: bool = False,
) -> int:
    """
    building_adjacency 테이블을 생성(또는 갱신)한다.

    Returns:
        삽입된 총 행 수 (dry_run이면 0)
    """
    targets = districts or SEOUL_SGG_CODES

    if not dry_run:
        _ensure_spread_weight_column()

    total = 0
    t_start = time.time()

    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  building_adjacency 생성")
    print(f"  반경: {radius}m | 구 수: {len(targets)} | dry_run: {dry_run}")
    print(f"  시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{sep}\n")

    for i, sgg_code in enumerate(targets, 1):
        label = f"[{i:2d}/{len(targets)}] {sgg_code}"
        print(f"{label} 처리 중...", end=" ", flush=True)
        try:
            stats = _process_district(sgg_code, radius, dry_run)
            if dry_run:
                n = stats["pairs"]
                print(f"예상 {n:>8,} 쌍 (양방향)  {stats['elapsed']:.1f}s")
            else:
                n = stats["inserted"]
                total += n
                print(f"삽입 {n:>8,} 행           {stats['elapsed']:.1f}s")
        except Exception as exc:
            print(f"ERROR: {exc}")
            logger.exception("구 처리 실패 sgg_code=%s", sgg_code)

    elapsed = time.time() - t_start
    print(f"\n{sep}")
    if dry_run:
        print(f"  [DRY-RUN] 완료  소요: {elapsed:.0f}s")
    else:
        with _engine.connect() as conn:
            db_total = conn.execute(
                text("SELECT COUNT(*) FROM building_adjacency")
            ).scalar()
        print(f"  완료: 이번 삽입 {total:,}행  |  DB 합계: {db_total:,}행  |  소요: {elapsed:.0f}s")
    print(f"{sep}\n")
    return total if not dry_run else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="서울 전역 건물 인접 관계 그래프 생성 (Phase F1/F2)",
    )
    parser.add_argument(
        "--district", metavar="SGG_CODE",
        help="특정 시군구코드만 처리 (예: 11440). 미지정 시 서울 전역.",
    )
    parser.add_argument(
        "--radius", type=float, default=25.0,
        help="인접 판단 거리 기준(m). 기본 25m.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="건수만 출력, DB 수정 없음.",
    )
    args = parser.parse_args()

    districts = [args.district] if args.district else None
    build_adjacency(districts=districts, radius=args.radius, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
