"""Phase 4-D D-3: EnergyPlus 파일럿 테스트 (Tier 1 실측 10건 교차검증)

Usage:
    python scratch/pilot_energyplus.py                    # Tier 1 건물 10건
    python scratch/pilot_energyplus.py --pnu <PNU>        # 특정 건물 1건
    python scratch/pilot_energyplus.py --dry-run          # DB 저장 없이 결과만 출력
"""
import argparse
import logging
import os
import sys
from pathlib import Path

# scratch/ 폴더 — 중간 파일, 작업 완료 후 삭제 가능
SCRATCH_DIR = Path(__file__).parent

sys.path.insert(0, str(SCRATCH_DIR.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def run_pilot(db_url: str, limit: int = 10, pnu: str | None = None, dry_run: bool = False):
    import pandas as pd
    import psycopg2
    from urllib.parse import urlparse

    p = urlparse(db_url)
    conn = psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username, password=p.password,
    )

    if pnu:
        query = f"SELECT pnu, total_energy AS tier1_eui FROM energy_results WHERE pnu = '{pnu}' AND data_tier = 1"
    else:
        query = f"""
            SELECT pnu, total_energy AS tier1_eui
            FROM energy_results
            WHERE data_tier = 1
              AND total_energy > 0
            LIMIT {limit}
        """

    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        logger.warning("Tier 1 건물을 찾을 수 없습니다.")
        return

    logger.info("파일럿 대상: %d건", len(df))

    from src.simulation.energyplus_runner import simulate_building, save_tier3

    results = []
    for _, row in df.iterrows():
        p = row["pnu"]
        tier1_eui = row["tier1_eui"]
        logger.info("  시뮬: pnu=%s tier1_eui=%.1f", p, tier1_eui)

        result = simulate_building(p, db_url)

        if "error" in result:
            logger.warning("  실패: %s", result["error"])
            results.append({
                "pnu": p,
                "tier1_eui": tier1_eui,
                "ep_eui": None,
                "error": result["error"],
                "ratio": None,
            })
            continue

        ep_eui = result.get("eui_total", 0)
        ratio = ep_eui / tier1_eui if tier1_eui > 0 else None
        logger.info("  EP EUI=%.1f  비율=%.2f", ep_eui, ratio or 0)

        if not dry_run:
            save_tier3(result, db_url)

        results.append({
            "pnu": p,
            "tier1_eui": tier1_eui,
            "ep_eui": ep_eui,
            "ratio": ratio,
            "error": None,
        })

    # 결과 요약
    import pandas as pd
    result_df = pd.DataFrame(results)
    success = result_df[result_df["error"].isna()]

    print("\n=== 파일럿 결과 ===")
    print(result_df.to_string(index=False))

    if not success.empty:
        print(f"\n성공: {len(success)}/{len(result_df)}건")
        print(f"EP/Tier1 비율 — 중앙값: {success['ratio'].median():.3f}  "
              f"평균: {success['ratio'].mean():.3f}  "
              f"범위: {success['ratio'].min():.3f}~{success['ratio'].max():.3f}")

    if dry_run:
        print("\n[DRY RUN] DB 저장 생략됨")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:password@localhost:5434/buildings",
    ))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--pnu", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_pilot(args.db_url, args.limit, args.pnu, args.dry_run)


if __name__ == "__main__":
    main()
