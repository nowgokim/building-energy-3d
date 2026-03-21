"""Coordinate transformation and geometry utilities.

Provides CRS detection and reprojection for Korean geospatial data,
along with building height estimation from available attributes.
"""

import logging
from typing import Any

import geopandas as gpd

logger = logging.getLogger(__name__)

# Common Korean EPSG codes
KOREAN_EPSG_CODES = {
    5174,  # Korea 2000 / Central Belt
    5175,  # Korea 2000 / Central Belt (Jeju)
    5176,  # Korea 2000 / East Belt
    5177,  # Korea 2000 / East Sea Belt
    5178,  # Korea 2000 / West Belt
    5179,  # Korea 2000 / Unified CS
    5186,  # Korea 2000 / Central Belt 2010
    5187,  # Korea 2000 / West Belt 2010
    5188,  # Korea 2000 / East Belt 2010
    2097,  # Korean 1985 / Central Belt
}

# Default floor height in meters (Korean standard)
DEFAULT_FLOOR_HEIGHT_M = 3.3

# Fallback building height when no data is available
DEFAULT_BUILDING_HEIGHT_M = 10.0

# Column names that may contain height information
HEIGHT_COLUMNS = ["height", "HEIGHT", "높이", "bld_height", "BLD_HEIGHT"]

# Column names that may contain floor count
FLOOR_COLUMNS = [
    "num_floors", "grnd_flrs", "층수", "지상층수",
    "GRND_FLR", "FLOOR_CNT", "floors",
]


def ensure_epsg4326(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ensure a GeoDataFrame is in EPSG:4326 (WGS84).

    Detects the current CRS and reprojects if necessary. Handles common
    Korean coordinate reference systems (EPSG:5174, 5186, etc.). If no
    CRS is set, assumes EPSG:5174 as the most common Korean building
    data CRS.

    Args:
        gdf: Input GeoDataFrame, potentially in a Korean CRS.

    Returns:
        GeoDataFrame in EPSG:4326.

    Raises:
        ValueError: If the GeoDataFrame has no geometry column.

    Examples:
        >>> gdf_wgs84 = ensure_epsg4326(gdf_korean)
        >>> gdf_wgs84.crs.to_epsg()
        4326
    """
    if gdf.geometry is None or gdf.geometry.name not in gdf.columns:
        raise ValueError("GeoDataFrame has no geometry column")

    if gdf.crs is None:
        logger.warning(
            "No CRS detected on GeoDataFrame; assuming EPSG:5174"
        )
        gdf = gdf.set_crs(epsg=5174)

    current_epsg = None
    try:
        current_epsg = gdf.crs.to_epsg()
    except Exception:
        logger.warning("Could not determine EPSG code from CRS: %s", gdf.crs)

    if current_epsg == 4326:
        logger.debug("GeoDataFrame is already in EPSG:4326")
        return gdf

    if current_epsg in KOREAN_EPSG_CODES:
        logger.info("Reprojecting from EPSG:%d to EPSG:4326", current_epsg)
    else:
        logger.info("Reprojecting from %s to EPSG:4326", gdf.crs)

    return gdf.to_crs(epsg=4326)


def calculate_height(row: Any) -> float:
    """Calculate building height with priority-based fallback.

    Determines building height using the following priority order:
        1. Explicit height field (if present and valid)
        2. Number of floors * 3.3m per floor
        3. Default height of 10.0m

    Args:
        row: A pandas Series, dict, or object with attribute access
             containing building attribute fields.

    Returns:
        Estimated building height in meters.

    Examples:
        >>> calculate_height({"height": 25.0, "grnd_flrs": 8})
        25.0
        >>> calculate_height({"grnd_flrs": 5})
        16.5
        >>> calculate_height({})
        10.0
    """
    # Priority 1: Direct height value
    for col in HEIGHT_COLUMNS:
        height = _get_field(row, col)
        if height is not None:
            try:
                h = float(height)
                if h > 0:
                    return h
            except (ValueError, TypeError):
                continue

    # Priority 2: Calculate from floor count
    for col in FLOOR_COLUMNS:
        floors = _get_field(row, col)
        if floors is not None:
            try:
                f = int(float(floors))
                if f > 0:
                    return f * DEFAULT_FLOOR_HEIGHT_M
            except (ValueError, TypeError):
                continue

    # Priority 3: Default
    return DEFAULT_BUILDING_HEIGHT_M


def _get_field(row: Any, field_name: str) -> Any:
    """Safely retrieve a field value from a row-like object.

    Supports pandas Series (bracket access), dicts, and objects with
    attribute access. Returns None if the field does not exist or
    the value is null/NaN.

    Args:
        row: Row-like object (Series, dict, or namespace).
        field_name: Name of the field to retrieve.

    Returns:
        Field value or None if not found/null.
    """
    value = None

    try:
        if hasattr(row, "__getitem__"):
            if field_name in row:
                value = row[field_name]
        elif hasattr(row, field_name):
            value = getattr(row, field_name)
    except (KeyError, IndexError, TypeError):
        return None

    # Check for pandas NaN / None
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    return value
