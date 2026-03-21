"""
데이터 파이프라인 오케스트레이션 - MVP 전체 실행 스크립트

사용법:
    python -m src.data_ingestion.pipeline --shp-path ./data/raw/mapo.shp
    python -m src.data_ingestion.pipeline --ledger-only
    python -m src.data_ingestion.pipeline --refresh-view
"""
import argparse
import logging
import os
import sys

from src.shared.config import get_settings
from src.shared.database import execute_sql

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def step_load_footprints(shp_path: str = None):
    """Step 1: GIS건물통합정보 SHP 또는 VWorld API → PostGIS"""
    settings = get_settings()

    if shp_path:
        from src.data_ingestion.collect_footprints import load_footprints_from_shp
        logger.info(f"Loading footprints from {shp_path}")
        count = load_footprints_from_shp(shp_path, settings.DATABASE_URL)
    else:
        from src.data_ingestion.collect_footprints import load_footprints_from_vworld
        if not settings.VWORLD_API_KEY:
            logger.error("VWORLD_API_KEY not set and no SHP path provided")
            return 0
        logger.info("Loading footprints from VWorld API")
        count = load_footprints_from_vworld(settings.VWORLD_API_KEY, settings.DATABASE_URL)

    logger.info(f"Loaded {count} building footprints")
    return count


def step_collect_ledger():
    """Step 2: 건축물대장 API → PostgreSQL"""
    from src.data_ingestion.collect_ledger import collect_mapo_ledger

    settings = get_settings()
    if not settings.DATA_GO_KR_API_KEY:
        logger.error("DATA_GO_KR_API_KEY not set in .env")
        return None
    logger.info("Collecting building ledger data for 마포구...")
    stats = collect_mapo_ledger(settings.DATA_GO_KR_API_KEY, settings.DATABASE_URL)
    logger.info(f"Ledger collection complete: {stats}")
    return stats


def step_refresh_view():
    """Step 3: buildings_enriched Materialized View 생성/갱신"""
    logger.info("Refreshing buildings_enriched materialized view...")
    views_sql_path = os.path.join(os.path.dirname(__file__), "../../db/views.sql")
    with open(views_sql_path, encoding="utf-8") as f:
        sql = f.read()
    execute_sql(sql)
    # 건물 수 확인
    result = execute_sql("SELECT COUNT(*) FROM buildings_enriched")
    count = result.fetchone()[0]
    logger.info(f"buildings_enriched view: {count} records")
    return count


def step_match_energy():
    """Step 4: 원형 매칭 → 에너지 추정 → energy_results 저장"""
    from src.simulation.archetypes import match_archetype, estimate_energy
    from src.shared.database import engine
    import pandas as pd

    logger.info("Matching archetypes and estimating energy...")
    buildings = pd.read_sql(
        "SELECT pnu, usage_type, built_year, total_area, structure_class FROM buildings_enriched",
        engine,
    )

    results = []
    for _, row in buildings.iterrows():
        arch = match_archetype(
            row.get("usage_type", ""),
            row.get("built_year"),
            row.get("total_area", 0),
            row.get("structure_class", "RC"),
        )
        energy = estimate_energy(arch)
        results.append({
            "pnu": row["pnu"],
            "archetype_id": None,
            "wall_uvalue": arch.get("wall_uvalue"),
            "roof_uvalue": arch.get("roof_uvalue"),
            "window_uvalue": arch.get("window_uvalue"),
            "wwr": arch.get("wwr"),
            "simulation_type": "archetype",
            **energy,
        })

    if results:
        df = pd.DataFrame(results)
        df.to_sql("energy_results", engine, if_exists="replace", index=False)
        logger.info(f"Saved {len(df)} energy results")
    return len(results)


def step_generate_tiles():
    """Step 5: 3D Tiles (GLB) 생성"""
    from src.tile_generation.generate import generate_buildings_glb

    settings = get_settings()
    output_dir = settings.TILES_LOCAL_DIR
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Generating 3D tiles to {output_dir}")
    count = generate_buildings_glb(settings.DATABASE_URL, output_dir)
    logger.info(f"Generated GLB with {count} buildings")
    return count


def main():
    parser = argparse.ArgumentParser(description="Building Energy Data Pipeline")
    parser.add_argument("--shp-path", help="GIS건물통합정보 SHP 파일 경로")
    parser.add_argument("--ledger-only", action="store_true", help="건축물대장 API만 수집")
    parser.add_argument("--refresh-view", action="store_true", help="Materialized View만 갱신")
    parser.add_argument("--energy-only", action="store_true", help="에너지 추정만 실행")
    parser.add_argument("--tiles-only", action="store_true", help="3D Tiles만 생성")
    parser.add_argument("--full", action="store_true", help="전체 파이프라인 실행")
    args = parser.parse_args()

    if args.ledger_only:
        step_collect_ledger()
    elif args.refresh_view:
        step_refresh_view()
    elif args.energy_only:
        step_match_energy()
    elif args.tiles_only:
        step_generate_tiles()
    elif args.full or args.shp_path:
        step_load_footprints(args.shp_path)
        step_collect_ledger()
        step_refresh_view()
        step_match_energy()
        step_generate_tiles()
        logger.info("=== Full pipeline complete ===")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
