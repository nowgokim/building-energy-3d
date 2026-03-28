"""Phase A/A' 적용: Korean_BB 보정 EUI → energy_results 대량 업데이트

buildings_enriched의 모든 건물(~770K)에 대해:
1. get_korean_bb_eui()로 보정된 EUI 계산
2. energy_results의 data_tier=4 레코드를 UPSERT

기존 Tier 1(실측), Tier 2(인증) 데이터는 건드리지 않는다.

Usage:
    python -m src.data_ingestion.populate_korean_bb_eui
    python -m src.data_ingestion.populate_korean_bb_eui --dry-run       # 상위 1000건만 계산
    python -m src.data_ingestion.populate_korean_bb_eui --batch-size 5000
"""
import sys
import os
import argparse
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 경로 ───────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── 에너지 엔드유즈 비율 (usage key별 breakdown) ──────────────────────────
# archetypes.py _END_USE_RATIOS와 동일값 유지
_END_USE_RATIOS: dict[str, dict[str, float]] = {
    "apartment":          {"heating": 0.42, "cooling": 0.10, "hot_water": 0.28, "lighting": 0.10, "ventilation": 0.10},
    "residential_single": {"heating": 0.45, "cooling": 0.08, "hot_water": 0.30, "lighting": 0.08, "ventilation": 0.09},
    "office":             {"heating": 0.30, "cooling": 0.25, "hot_water": 0.10, "lighting": 0.20, "ventilation": 0.15},
    "retail":             {"heating": 0.22, "cooling": 0.30, "hot_water": 0.05, "lighting": 0.25, "ventilation": 0.18},
    "education":          {"heating": 0.38, "cooling": 0.15, "hot_water": 0.15, "lighting": 0.18, "ventilation": 0.14},
    "hospital":           {"heating": 0.32, "cooling": 0.22, "hot_water": 0.20, "lighting": 0.14, "ventilation": 0.12},
    "warehouse":          {"heating": 0.35, "cooling": 0.12, "hot_water": 0.05, "lighting": 0.30, "ventilation": 0.18},
    "cultural":           {"heating": 0.35, "cooling": 0.18, "hot_water": 0.08, "lighting": 0.22, "ventilation": 0.17},
    "mixed_use":          {"heating": 0.28, "cooling": 0.22, "hot_water": 0.25, "lighting": 0.14, "ventilation": 0.11},
}
_DEFAULT_END_USE: dict[str, float] = {
    "heating": 0.35, "cooling": 0.18, "hot_water": 0.18, "lighting": 0.16, "ventilation": 0.13,
}

# ── 도시 매핑 (서울 외 도시는 현재 서울 보정계수 적용) ──────────────────
CITY_DEFAULT = "seoul"


def compute_eui_batch(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame(pnu, usage_type, built_year, floors_above, ...) → EUI 컬럼 추가.

    벡터화 없이 건물별 루프 (Korean_BB 룩업이 dict 기반이므로 충분히 빠름).
    ~770K 건물 기준 약 60-90초 소요 예상.
    """
    from src.simulation.archetypes import (
        get_korean_bb_eui,
        _normalize_usage,
        _END_USE_RATIOS as _AR_RATIOS,
    )

    results = []
    fallback_count = 0
    lookup_count = 0

    for _, row in df.iterrows():
        usage_type = row.get("usage_type") or ""
        built_year = row.get("built_year")
        floors_above = row.get("floors_above")
        pnu = row["pnu"]

        eui = get_korean_bb_eui(usage_type, built_year, floors_above, CITY_DEFAULT)

        if eui is None or eui <= 0:
            # fallback: archetypes.py ARCHETYPE_PARAMS 기반 추정
            from src.simulation.archetypes import match_archetype, estimate_energy
            arch = match_archetype(
                usage_type,
                built_year,
                row.get("total_area", 0),
                row.get("structure_class", "RC"),
            )
            energy = estimate_energy(arch)
            eui = energy["total_energy"]
            end_use = energy
            fallback_count += 1
            sim_type = "archetype_fallback"
        else:
            # EUI breakdown using end-use ratios
            usage_key = _normalize_usage(usage_type)
            ratios = _END_USE_RATIOS.get(usage_key, _DEFAULT_END_USE)
            end_use = {
                "heating":     round(eui * ratios["heating"], 2),
                "cooling":     round(eui * ratios["cooling"], 2),
                "hot_water":   round(eui * ratios["hot_water"], 2),
                "lighting":    round(eui * ratios["lighting"], 2),
                "ventilation": round(eui * ratios["ventilation"], 2),
                "total_energy": eui,
            }
            lookup_count += 1
            sim_type = "korean_bb_calibrated"

        results.append({
            "pnu":          pnu,
            "total_energy": end_use.get("total_energy", eui),
            "heating":      end_use.get("heating", 0),
            "cooling":      end_use.get("cooling", 0),
            "hot_water":    end_use.get("hot_water", 0),
            "lighting":     end_use.get("lighting", 0),
            "ventilation":  end_use.get("ventilation", 0),
            "simulation_type": sim_type,
        })

    logger.info(
        "  룩업 성공: %d건 / fallback: %d건",
        lookup_count, fallback_count,
    )
    return pd.DataFrame(results)


def _get_pg_conn(db_url: str):
    """Parse DATABASE_URL → psycopg2 connection."""
    import psycopg2
    from urllib.parse import urlparse
    p = urlparse(db_url)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
    )


def upsert_energy_results(result_df: pd.DataFrame, db_url: str, dry_run: bool = False) -> int:
    """Tier 4 에너지 결과를 energy_results에 bulk UPSERT.

    임시 테이블(temp_kbb_eui)에 배치 데이터를 COPY로 적재한 후:
    - 기존 data_tier=4 레코드 → UPDATE
    - 없는 pnu → INSERT
    Tier 1 / Tier 2 레코드는 절대 수정하지 않는다.
    """
    if dry_run:
        logger.info("  [DRY RUN] %d건 업데이트 대상 (DB 변경 없음)", len(result_df))
        return len(result_df)

    import io
    conn = _get_pg_conn(db_url)
    cur = conn.cursor()

    # 임시 테이블 생성
    cur.execute("""
        CREATE TEMP TABLE temp_kbb_eui (
            pnu            text,
            total_energy   double precision,
            heating        double precision,
            cooling        double precision,
            hot_water      double precision,
            lighting       double precision,
            ventilation    double precision,
            simulation_type text
        ) ON COMMIT DROP
    """)

    # Tier 1/2 PNU 목록 조회 → 덮어쓰기 방지
    cur.execute("SELECT pnu FROM energy_results WHERE data_tier IN (1,2)")
    protected_pnus = {row[0] for row in cur.fetchall()}
    protected_count = len(protected_pnus)

    # 보호 PNU 제외
    filtered_df = result_df[~result_df["pnu"].isin(protected_pnus)].copy()
    logger.info(
        "  Tier 1/2 보호: %d건 제외, %d건 처리",
        len(result_df) - len(filtered_df), len(filtered_df),
    )

    if filtered_df.empty:
        logger.info("  처리 대상 없음")
        conn.commit()
        cur.close()
        conn.close()
        return 0

    # COPY로 bulk 적재
    buf = io.StringIO()
    filtered_df[["pnu","total_energy","heating","cooling",
                 "hot_water","lighting","ventilation","simulation_type"]].to_csv(
        buf, index=False, header=False, sep="\t", na_rep="\\N"
    )
    buf.seek(0)
    cur.copy_from(buf, "temp_kbb_eui", sep="\t",
                  columns=["pnu","total_energy","heating","cooling",
                            "hot_water","lighting","ventilation","simulation_type"])

    # UPDATE 기존 tier4 레코드
    cur.execute("""
        UPDATE energy_results er
        SET total_energy    = t.total_energy,
            heating         = t.heating,
            cooling         = t.cooling,
            hot_water       = t.hot_water,
            lighting        = t.lighting,
            ventilation     = t.ventilation,
            simulation_type = t.simulation_type,
            is_current      = true
        FROM temp_kbb_eui t
        WHERE er.pnu = t.pnu
          AND er.data_tier = 4
    """)
    updated = cur.rowcount

    # INSERT 없는 pnu (기존 레코드 자체가 없는 건물)
    cur.execute("""
        SELECT id FROM data_sources WHERE data_tier = 4 LIMIT 1
    """)
    row = cur.fetchone()
    source_id = row[0] if row else None

    cur.execute("""
        INSERT INTO energy_results (
            pnu, simulation_type,
            heating, cooling, hot_water, lighting, ventilation, total_energy,
            source_id, data_tier, is_current
        )
        SELECT
            t.pnu, t.simulation_type,
            t.heating, t.cooling, t.hot_water, t.lighting, t.ventilation, t.total_energy,
            %(source_id)s, 4, true
        FROM temp_kbb_eui t
        WHERE NOT EXISTS (
            SELECT 1 FROM energy_results er WHERE er.pnu = t.pnu
        )
    """, {"source_id": source_id})
    inserted = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    logger.info("  DB: %d updated + %d inserted", updated, inserted)
    return updated + inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:password@localhost:5434/buildings",
    ))
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true",
                        help="EUI 계산만 수행, DB 변경 없음")
    parser.add_argument("--limit", type=int, default=0,
                        help="처리 건물 수 제한 (테스트용, 0=전체)")
    args = parser.parse_args()

    logger.info("=== Korean_BB 보정 EUI → energy_results 업데이트 ===")
    logger.info("  DB: %s", args.db_url.split("@")[-1] if "@" in args.db_url else args.db_url)
    logger.info("  배치 크기: %d | dry_run: %s", args.batch_size, args.dry_run)

    # buildings_enriched 로드
    try:
        import psycopg2
        from urllib.parse import urlparse
        p = urlparse(args.db_url)
        conn = psycopg2.connect(
            host=p.hostname, port=p.port or 5432,
            dbname=p.path.lstrip("/"), user=p.username, password=p.password,
        )
        limit_clause = f"LIMIT {args.limit}" if args.limit else ""
        query = f"""
            SELECT pnu, usage_type, built_year, floors_above, total_area, structure_class
            FROM buildings_enriched
            WHERE pnu IS NOT NULL
            {limit_clause}
        """
        logger.info("  buildings_enriched 조회 중...")
        buildings_df = pd.read_sql_query(query, conn)
        conn.close()
    except Exception as e:
        logger.error("DB 연결 실패: %s", e)
        logger.error("Docker가 실행 중인지 확인하세요: docker compose up -d db")
        sys.exit(1)

    total = len(buildings_df)
    logger.info("  총 %d건 처리 예정", total)

    # 배치 처리
    t_start = time.time()
    total_processed = 0
    n_batches = (total + args.batch_size - 1) // args.batch_size

    for batch_idx in range(n_batches):
        start = batch_idx * args.batch_size
        end = min(start + args.batch_size, total)
        batch = buildings_df.iloc[start:end].copy()

        logger.info("[%d/%d] 배치 처리 중 (건물 %d~%d)...", batch_idx+1, n_batches, start, end-1)
        t_batch = time.time()

        result_df = compute_eui_batch(batch)
        n_done = upsert_energy_results(result_df, args.db_url, dry_run=args.dry_run)
        total_processed += n_done

        elapsed = time.time() - t_batch
        logger.info("  배치 완료: %.1fs (누적 %d건)", elapsed, total_processed)

    total_elapsed = time.time() - t_start
    logger.info("=== 완료: %d건 처리, %.1f초 소요 ===", total_processed, total_elapsed)

    # 요약 통계 출력
    if not args.dry_run:
        conn = _get_pg_conn(args.db_url)
        sample_df = pd.read_sql_query(
            "SELECT pnu, ROUND(total_energy::numeric,1) AS eui_kwh_m2, simulation_type "
            "FROM energy_results WHERE data_tier=4 ORDER BY RANDOM() LIMIT 10",
            conn,
        )
        conn.close()
        logger.info("\n[샘플 결과 10건]\n%s", sample_df.to_string(index=False))


if __name__ == "__main__":
    main()
