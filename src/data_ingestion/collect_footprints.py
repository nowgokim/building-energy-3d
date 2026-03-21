"""GIS building footprint loader from SHP files.

Loads Korean building footprint shapefiles, reprojects to WGS84,
normalizes column names, and stores in a PostGIS-enabled PostgreSQL database.

Usage:
    count = load_footprints_from_shp("buildings.shp", "postgresql://...", "11440")
"""

import logging
from typing import Dict

import geopandas as gpd
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# Common Korean column name mappings to English
COLUMN_NAME_MAP: Dict[str, str] = {
    "건물명": "bld_name",
    "건물명칭": "bld_name",
    "BLD_NM": "bld_name",
    "건물용도": "bld_usage",
    "주용도": "bld_usage",
    "USABILITY": "bld_usage",
    "시군구코드": "sigungu_cd",
    "SGG_CD": "sigungu_cd",
    "법정동코드": "bdong_cd",
    "BDONG_CD": "bdong_cd",
    "대지구분": "daeji_gb",
    "번": "bon",
    "지": "ji",
    "층수": "num_floors",
    "지상층수": "grnd_flrs",
    "GRND_FLR": "grnd_flrs",
    "지하층수": "ugrnd_flrs",
    "UGRND_FLR": "ugrnd_flrs",
    "높이": "height",
    "HEIGHT": "height",
    "구조": "structure",
    "STRCT_CD": "structure",
    "연면적": "total_area",
    "TOTL_AREA": "total_area",
    "건축면적": "bld_area",
    "BLD_AREA": "bld_area",
    "사용승인일": "use_apr_dt",
    "USE_DATE": "use_apr_dt",
    "PNU": "pnu",
}

# Korean CRS codes commonly used in building data
KOREAN_CRS_CODES = {5174, 5179, 5186, 2097}


def load_footprints_from_shp(
    shp_path: str,
    db_url: str,
    sigungu_code: str = "11440",
) -> int:
    """Load building footprints from a shapefile into PostGIS.

    Reads a Korean building footprint SHP file, filters by 시군구 code,
    reprojects from the source Korean CRS (EPSG:5174 or EPSG:5186) to
    WGS84 (EPSG:4326), normalizes column names to English, and saves
    to the ``building_footprints`` table in PostGIS.

    Args:
        shp_path: Path to the .shp file.
        db_url: SQLAlchemy-compatible PostgreSQL/PostGIS connection URL.
                e.g. "postgresql://user:pass@host:5432/dbname"
        sigungu_code: 시군구코드 to filter by. Defaults to "11440" (마포구).

    Returns:
        Number of records loaded into the database.

    Raises:
        FileNotFoundError: If the shapefile does not exist.
        ValueError: If the GeoDataFrame has no geometry after loading.
    """
    logger.info("Loading footprints from %s", shp_path)

    # Read shapefile with encoding for Korean characters
    gdf = gpd.read_file(shp_path, encoding="euc-kr")
    logger.info("Read %d features from shapefile", len(gdf))

    if gdf.empty:
        logger.warning("Shapefile is empty: %s", shp_path)
        return 0

    # Filter by sigungu_code if a matching column exists
    sigungu_col = None
    for col_candidate in ["시군구코드", "SGG_CD", "sigungu_cd", "SIGUNGU_CD"]:
        if col_candidate in gdf.columns:
            sigungu_col = col_candidate
            break

    if sigungu_col is not None:
        original_count = len(gdf)
        gdf[sigungu_col] = gdf[sigungu_col].astype(str).str.strip()
        gdf = gdf[gdf[sigungu_col] == sigungu_code].copy()
        logger.info(
            "Filtered by %s=%s: %d -> %d features",
            sigungu_col, sigungu_code, original_count, len(gdf),
        )
        if gdf.empty:
            logger.warning("No features remaining after sigungu filter")
            return 0
    else:
        logger.info("No sigungu code column found; loading all features")

    # Reproject to EPSG:4326
    gdf = _reproject_to_4326(gdf)

    # Rename Korean columns to English
    rename_map = {}
    for col in gdf.columns:
        if col in COLUMN_NAME_MAP:
            rename_map[col] = COLUMN_NAME_MAP[col]
    if rename_map:
        gdf = gdf.rename(columns=rename_map)
        logger.info("Renamed columns: %s", rename_map)

    # Ensure geometry column is named 'geometry'
    if gdf.geometry.name != "geometry":
        gdf = gdf.rename_geometry("geometry")

    # Save to PostGIS
    engine = create_engine(db_url)
    try:
        gdf.to_postgis(
            name="building_footprints",
            con=engine,
            if_exists="append",
            index=False,
            chunksize=1000,
        )
    finally:
        engine.dispose()

    record_count = len(gdf)
    logger.info("Loaded %d footprints into building_footprints table", record_count)
    return record_count


def _reproject_to_4326(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject a GeoDataFrame to EPSG:4326 (WGS84).

    Handles common Korean coordinate reference systems:
        - EPSG:5174 (Korea 2000 / Central Belt)
        - EPSG:5186 (Korea 2000 / Central Belt 2010)
        - Other Korean CRS codes

    If the CRS is already EPSG:4326 or is unset, the data is returned as-is
    (with CRS set to 4326 if missing).

    Args:
        gdf: Input GeoDataFrame with Korean CRS.

    Returns:
        GeoDataFrame reprojected to EPSG:4326.
    """
    if gdf.crs is None:
        logger.warning(
            "No CRS detected; assuming EPSG:5174 (Korea 2000 / Central Belt)"
        )
        gdf = gdf.set_crs(epsg=5174)

    source_epsg = None
    try:
        source_epsg = gdf.crs.to_epsg()
    except Exception:
        pass

    if source_epsg == 4326:
        logger.info("Data is already in EPSG:4326")
        return gdf

    if source_epsg in KOREAN_CRS_CODES:
        logger.info("Reprojecting from EPSG:%d to EPSG:4326", source_epsg)
    else:
        logger.info("Reprojecting from %s to EPSG:4326", gdf.crs)

    gdf = gdf.to_crs(epsg=4326)
    return gdf
