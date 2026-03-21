"""GIS building footprint loader from SHP files or VWorld WFS API.

Loads Korean building footprint shapefiles or fetches from VWorld WFS,
reprojects to WGS84, normalizes column names, and stores in PostGIS.

Usage:
    count = load_footprints_from_shp("buildings.shp", "postgresql://...", "11440")
    count = load_footprints_from_vworld("API_KEY", "postgresql://...")
"""

import logging
import time
from typing import Dict

import geopandas as gpd
import httpx
import pandas as pd
from shapely.geometry import shape
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

    # Deduplicate by PNU if column exists
    if "pnu" in gdf.columns:
        before = len(gdf)
        gdf = gdf.drop_duplicates(subset=["pnu"])
        if before != len(gdf):
            logger.info("Deduplicated by PNU: %d -> %d", before, len(gdf))

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


def load_footprints_from_vworld(
    api_key: str,
    db_url: str,
    domain: str = "jukim.github.io",
) -> int:
    """Fetch building footprints from VWorld data API and store in PostGIS.

    Uses the VWorld LT_C_SPBD (건물) layer. The API limits bbox to 10km,
    so 마포구 is split into smaller tiles.

    Args:
        api_key: VWorld API key.
        db_url: SQLAlchemy PostgreSQL connection URL.
        domain: Registered domain for the API key.

    Returns:
        Number of records loaded into the database.
    """
    # 마포구 bbox를 ~3km 타일로 분할 (10km 제한 준수)
    TILES = [
        (126.890, 37.530, 126.920, 37.555),
        (126.920, 37.530, 126.955, 37.555),
        (126.890, 37.555, 126.920, 37.580),
        (126.920, 37.555, 126.955, 37.580),
    ]

    logger.info("Fetching building footprints from VWorld API (LT_C_SPBD)")

    all_features = []

    for tile_idx, (w, s, e, n) in enumerate(TILES):
        bbox_str = f"{w},{s},{e},{n}"
        page = 1

        while page <= 100:
            params = {
                "service": "data",
                "request": "GetFeature",
                "data": "LT_C_SPBD",
                "key": api_key,
                "domain": domain,
                "format": "json",
                "size": "1000",
                "page": str(page),
                "geomFilter": f"BOX({bbox_str})",
                "crs": "EPSG:4326",
            }

            try:
                resp = httpx.get(
                    "https://api.vworld.kr/req/data",
                    params=params,
                    timeout=30.0,
                )
                data = resp.json()
            except Exception:
                logger.exception("VWorld API failed (tile %d, page %d)", tile_idx, page)
                break

            response_data = data.get("response", {})
            status = response_data.get("status", "")

            if status != "OK":
                if page == 1:
                    logger.warning("Tile %d: status=%s, error=%s", tile_idx, status,
                                   response_data.get("error", {}).get("code", ""))
                break

            features = response_data.get("result", {}).get("featureCollection", {}).get("features", [])
            if not features:
                break

            all_features.extend(features)
            total_count = int(response_data.get("record", {}).get("total", "0"))
            logger.info("Tile %d page %d: %d features (running: %d / tile total: %d)",
                        tile_idx, page, len(features), len(all_features), total_count)

            if len(features) < 1000:
                break

            page += 1
            time.sleep(0.3)

        time.sleep(0.3)

    if not all_features:
        logger.warning("No features returned from VWorld API")
        return 0

    logger.info("Total features fetched: %d", len(all_features))

    # Convert to GeoDataFrame
    geometries = []
    properties_list = []
    for f in all_features:
        try:
            geom = shape(f["geometry"])
            geometries.append(geom)
            properties_list.append(f.get("properties", {}))
        except Exception:
            continue

    gdf = gpd.GeoDataFrame(properties_list, geometry=geometries, crs="EPSG:4326")
    logger.info("GeoDataFrame: %d features, columns: %s", len(gdf), list(gdf.columns))

    # Map VWorld LT_C_SPBD columns to DB schema
    col_map = {
        "buld_nm": "bld_nm",
        "bd_mgt_sn": "bld_mgt_sn",
        "gro_flo_co": "grnd_flr",
        "rd_nm": "dong_nm",
    }
    rename = {k: v for k, v in col_map.items() if k in gdf.columns}
    gdf = gdf.rename(columns=rename)

    # Extract PNU from bld_mgt_sn (first 19 chars)
    if "bld_mgt_sn" in gdf.columns:
        gdf["pnu"] = gdf["bld_mgt_sn"].apply(
            lambda x: str(x)[:19] if pd.notna(x) and len(str(x)) >= 19 else None
        )

    # Convert floor count to integer
    if "grnd_flr" in gdf.columns:
        gdf["grnd_flr"] = pd.to_numeric(gdf["grnd_flr"], errors="coerce").astype("Int64")

    # Ensure required columns exist
    for col in ["pnu", "bld_nm", "dong_nm", "usage_type", "grnd_flr", "ugrnd_flr", "height"]:
        if col not in gdf.columns:
            gdf[col] = None

    # Estimate height from floors if not available
    if gdf["height"].isna().all() and "grnd_flr" in gdf.columns:
        gdf["height"] = gdf["grnd_flr"].apply(
            lambda x: round(float(x) * 3.3, 1) if pd.notna(x) and x > 0 else None
        )

    # Select only columns matching the DB schema
    keep_cols = ["pnu", "bld_mgt_sn", "bld_nm", "dong_nm", "usage_type",
                 "grnd_flr", "ugrnd_flr", "height", "geometry"]
    gdf = gdf[[c for c in keep_cols if c in gdf.columns]]

    # Ensure MultiPolygon geometry
    from shapely.geometry import MultiPolygon, Polygon
    gdf["geometry"] = gdf["geometry"].apply(
        lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
    )
    gdf = gdf.rename_geometry("geom")

    # Deduplicate by geometry centroid (tiles may overlap)
    gdf["_cx"] = gdf.geometry.centroid.x.round(7)
    gdf["_cy"] = gdf.geometry.centroid.y.round(7)
    before = len(gdf)
    gdf = gdf.drop_duplicates(subset=["_cx", "_cy"])
    gdf = gdf.drop(columns=["_cx", "_cy"])
    if before != len(gdf):
        logger.info("Deduplicated: %d -> %d", before, len(gdf))

    # Save to PostGIS
    engine = create_engine(db_url)
    try:
        gdf.to_postgis(
            name="building_footprints",
            con=engine,
            if_exists="append",
            index=False,
            chunksize=500,
        )
    finally:
        engine.dispose()

    logger.info("Loaded %d footprints from VWorld API", len(gdf))
    return len(gdf)


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
