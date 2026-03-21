"""마포구 테스트 건물 데이터 시드 — 파이프라인 검증용.

실제 API 연동 전까지 파이프라인 전체를 테스트하기 위한 합성 데이터.
마포구 주요 지역(합정, 상수, 연남, 홍대) 좌표 기반.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
import random

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 마포구 주요 건물 위치 (lng, lat) — 합정/상수/연남/홍대/공덕
BUILDING_SEEDS = [
    # (lng, lat, name, usage, floors, built_year, structure, area)
    (126.9138, 37.5496, "합정역 오피스", "업무시설", 12, 2015, "RC", 8500),
    (126.9118, 37.5478, "합정동 아파트", "공동주택", 18, 2008, "RC", 12000),
    (126.9230, 37.5479, "상수동 카페거리", "제2종근린생활시설", 3, 1995, "masonry", 450),
    (126.9250, 37.5502, "상수동 빌라", "공동주택", 5, 2002, "RC", 2000),
    (126.9285, 37.5595, "연남동 주택", "공동주택", 4, 1998, "masonry", 800),
    (126.9265, 37.5578, "연남동 상가", "제1종근린생활시설", 4, 2010, "RC", 1200),
    (126.9245, 37.5555, "연남동 오피스텔", "업무시설", 15, 2018, "RC", 6000),
    (126.9262, 37.5565, "연남동 학원", "교육연구시설", 6, 2005, "RC", 3000),
    (126.9232, 37.5573, "연남동 병원", "의료시설", 8, 2012, "RC", 4500),
    (126.9225, 37.5520, "홍대입구역 상가", "판매시설", 5, 2000, "RC", 3500),
    (126.9210, 37.5510, "홍대 아파트", "공동주택", 22, 2019, "RC", 18000),
    (126.9195, 37.5530, "홍대 오피스", "업무시설", 10, 2007, "steel", 5000),
    (126.9508, 37.5440, "공덕역 오피스", "업무시설", 20, 2016, "RC", 15000),
    (126.9525, 37.5455, "공덕동 아파트", "공동주택", 25, 2020, "RC", 22000),
    (126.9540, 37.5468, "공덕동 학교", "교육연구시설", 4, 1988, "RC", 5500),
    (126.9320, 37.5470, "마포대교 북단 상가", "제2종근린생활시설", 3, 1992, "masonry", 600),
    (126.9350, 37.5485, "마포역 오피스", "업무시설", 14, 2013, "RC", 7000),
    (126.9375, 37.5500, "마포동 빌라", "공동주택", 5, 2001, "RC", 1500),
    (126.9395, 37.5515, "아현동 아파트", "공동주택", 20, 2017, "RC", 16000),
    (126.9180, 37.5550, "망원동 주택", "공동주택", 3, 1985, "masonry", 500),
    (126.9160, 37.5560, "망원동 상가", "제1종근린생활시설", 4, 1997, "RC", 900),
    (126.9145, 37.5545, "망원동 병원", "의료시설", 6, 2009, "RC", 3200),
    (126.9430, 37.5490, "대흥동 오피스", "업무시설", 8, 2003, "RC", 4000),
    (126.9450, 37.5505, "대흥동 아파트", "공동주택", 15, 2011, "RC", 9000),
    (126.9470, 37.5520, "신수동 학교", "교육연구시설", 3, 1976, "RC", 4200),
]


def _make_building_polygon(lng: float, lat: float) -> MultiPolygon:
    """Create a small rectangular polygon around a point."""
    w = random.uniform(0.0002, 0.0005)
    h = random.uniform(0.0002, 0.0004)
    dx = random.uniform(-0.0001, 0.0001)
    dy = random.uniform(-0.0001, 0.0001)
    poly = Polygon([
        (lng - w / 2 + dx, lat - h / 2 + dy),
        (lng + w / 2 + dx, lat - h / 2 + dy),
        (lng + w / 2 + dx, lat + h / 2 + dy),
        (lng - w / 2 + dx, lat + h / 2 + dy),
        (lng - w / 2 + dx, lat - h / 2 + dy),
    ])
    return MultiPolygon([poly])


def seed_footprints(db_url: str) -> int:
    """Insert test building footprints."""
    random.seed(42)
    records = []
    for i, (lng, lat, name, usage, floors, year, struct, area) in enumerate(BUILDING_SEEDS):
        pnu = f"11440{10100 + i * 100:05d}1{random.randint(1, 999):04d}{random.randint(0, 99):04d}"
        records.append({
            "pnu": pnu,
            "bld_nm": name,
            "dong_nm": name.split()[0],
            "usage_type": usage,
            "grnd_flr": floors,
            "ugrnd_flr": random.randint(0, 3),
            "height": round(floors * 3.3, 1),
            "approval_date": f"{year}0601",
            "geometry": _make_building_polygon(lng, lat),
        })

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    gdf = gdf.rename_geometry("geom")

    engine = create_engine(db_url)
    try:
        # Drop existing rows for clean seed
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM building_footprints"))
            conn.commit()
        gdf.to_postgis("building_footprints", con=engine, if_exists="append", index=False)
    finally:
        engine.dispose()

    logger.info("Seeded %d building footprints", len(gdf))
    return len(gdf)


def seed_ledger(db_url: str) -> int:
    """Insert test building ledger data matching the footprints."""
    random.seed(42)
    records = []
    for i, (lng, lat, name, usage, floors, year, struct, area) in enumerate(BUILDING_SEEDS):
        pnu = f"11440{10100 + i * 100:05d}1{random.randint(1, 999):04d}{random.randint(0, 99):04d}"

        # 구조코드 매핑
        strct_map = {"RC": "철근콘크리트구조", "steel": "철골구조", "masonry": "조적구조"}

        records.append({
            "pnu": pnu,
            "bld_mgt_sn": f"11440-{i:06d}",
            "bld_nm": name,
            "dong_nm": name.split()[0],
            "main_purps_cd": f"0{i % 5 + 1}000",
            "main_purps_nm": usage,
            "strct_cd": "21" if struct == "RC" else ("22" if struct == "steel" else "11"),
            "strct_nm": strct_map.get(struct, "철근콘크리트구조"),
            "grnd_flr_cnt": floors,
            "ugrnd_flr_cnt": random.randint(0, 3),
            "bld_ht": round(floors * 3.3, 1),
            "tot_area": float(area),
            "bld_area": float(area) / floors,
            "use_apr_day": f"{year}0601",
            "enrgy_eff_rate": random.choice(["1+", "1", "2", "3", "4", "5", None]),
            "epi_score": round(random.uniform(80, 250), 1),
        })

    df = pd.DataFrame(records)
    engine = create_engine(db_url)
    try:
        df.to_sql("building_ledger", con=engine, if_exists="append", index=False)
    finally:
        engine.dispose()

    logger.info("Seeded %d building ledger records", len(df))
    return len(df)


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from src.shared.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    db_url = settings.DATABASE_URL
    logger.info("Database URL: %s", db_url)

    fp_count = seed_footprints(db_url)
    ledger_count = seed_ledger(db_url)

    # Verify
    engine = create_engine(db_url)
    with engine.connect() as conn:
        r = conn.execute(text("SELECT COUNT(*) FROM building_footprints"))
        print(f"\nbuilding_footprints: {r.fetchone()[0]} rows")
        r = conn.execute(text("SELECT COUNT(*) FROM building_ledger"))
        print(f"building_ledger: {r.fetchone()[0]} rows")
    engine.dispose()

    print(f"\nSeed complete: {fp_count} footprints, {ledger_count} ledger records")
