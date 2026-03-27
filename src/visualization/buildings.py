"""
Building data API router.

Provides endpoints for listing, filtering, and retrieving building data
with energy attributes, returned as GeoJSON for map integration.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.database import get_db_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/buildings", tags=["buildings"])


@router.get("/pick")
def pick_building(
    lng: float = Query(..., description="Click longitude"),
    lat: float = Query(..., description="Click latitude"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Find the nearest building to a click position using PostGIS KNN.

    Uses ST_DWithin pre-filter (100m radius) to avoid picking far buildings,
    then orders by KNN distance for the closest match.
    """
    sql = text("""
        SELECT pnu, building_name
        FROM building_centroids
        WHERE ST_DWithin(
            centroid,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            0.001
        )
        ORDER BY centroid <-> ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)
        LIMIT 1
    """)
    row = db.execute(sql, {"lng": lng, "lat": lat}).fetchone()
    if row is None:
        return {"pnu": None, "building_name": None}
    return {"pnu": row.pnu, "building_name": row.building_name}


class CentroidBatchRequest(BaseModel):
    pnus: list[str]


@router.post("/centroids/batch")
def batch_centroids(req: CentroidBatchRequest, db: Session = Depends(get_db_dependency)) -> dict:
    """
    PNU 목록에 대한 centroid 좌표 일괄 조회 (F2 화재 확산 시뮬레이션 애니메이션용).

    Returns {pnu: str, lng: float, lat: float}[] — 최대 10,000건.
    """
    if not req.pnus:
        return {"count": 0, "centroids": []}
    if len(req.pnus) > 10_000:
        raise HTTPException(status_code=400, detail="최대 10,000건까지 조회 가능합니다")

    sql = text("""
        SELECT DISTINCT ON (pnu) pnu,
               ST_X(centroid) AS lng,
               ST_Y(centroid) AS lat
        FROM building_centroids
        WHERE pnu = ANY(:pnus)
        ORDER BY pnu
    """)
    rows = db.execute(sql, {"pnus": req.pnus}).fetchall()
    return {
        "count": len(rows),
        "centroids": [{"pnu": r.pnu, "lng": float(r.lng), "lat": float(r.lat)} for r in rows],
    }


@router.get("/centroids")
def list_centroids(
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Lightweight centroid-only endpoint for click matching."""
    conditions: list[str] = []
    params: dict = {}
    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = text(f"""
        SELECT pnu, ST_X(ST_Centroid(geom)) AS lng, ST_Y(ST_Centroid(geom)) AS lat
        FROM buildings_enriched
        {where_clause}
    """)
    rows = db.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "centroids": [{"pnu": r.pnu, "lng": float(r.lng), "lat": float(r.lat)} for r in rows],
    }


@router.get("/stats")
def get_building_stats(
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Aggregate statistics for the current bounding box.

    Returns total count, average energy consumption, grade distribution,
    and usage type distribution.
    """
    bbox_clause = ""
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        bbox_clause = (
            "WHERE ST_Intersects("
            "  geom,"
            "  ST_MakeEnvelope(:west, :south, :east, :north, 4326)"
            ")"
        )
        params = {"west": west, "south": south, "east": east, "north": north}

    # Total count and average energy
    summary_sql = text(f"""
        SELECT
            COUNT(*) AS total_count,
            AVG(er.total_energy) AS avg_energy
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        {bbox_clause}
    """)
    row = db.execute(summary_sql, params).fetchone()
    total_count = row.total_count if row else 0
    avg_energy = float(row.avg_energy) if row and row.avg_energy else None

    # Grade distribution
    grade_sql = text(f"""
        SELECT
            COALESCE(b.energy_grade, 'unknown') AS grade,
            COUNT(*) AS cnt
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        {bbox_clause}
        GROUP BY COALESCE(b.energy_grade, 'unknown')
        ORDER BY grade
    """)
    grade_rows = db.execute(grade_sql, params).fetchall()
    grade_distribution = {r.grade: r.cnt for r in grade_rows}

    # Usage distribution
    usage_sql = text(f"""
        SELECT
            COALESCE(b.usage_type, 'unknown') AS usage,
            COUNT(*) AS cnt
        FROM buildings_enriched b
        {bbox_clause}
        GROUP BY COALESCE(b.usage_type, 'unknown')
        ORDER BY cnt DESC
    """)
    usage_rows = db.execute(usage_sql, params).fetchall()
    usage_distribution = {r.usage: r.cnt for r in usage_rows}

    logger.info(
        "Stats requested — total_count=%d, bbox=%s",
        total_count,
        [west, south, east, north] if west is not None else "none",
    )

    return {
        "total_count": total_count,
        "avg_energy": avg_energy,
        "grade_distribution": grade_distribution,
        "usage_distribution": usage_distribution,
    }


@router.get("/")
def list_buildings(
    west: Optional[float] = Query(None, description="Bounding box west longitude"),
    south: Optional[float] = Query(None, description="Bounding box south latitude"),
    east: Optional[float] = Query(None, description="Bounding box east longitude"),
    north: Optional[float] = Query(None, description="Bounding box north latitude"),
    energy_grade: Optional[str] = Query(None, description="Filter by energy grade"),
    usage_type: Optional[str] = Query(None, description="Filter by usage type"),
    vintage: Optional[str] = Query(None, description="Filter by vintage class"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """List buildings with optional spatial and attribute filters.

    Returns a GeoJSON FeatureCollection with up to 1000 features.
    """
    conditions: list[str] = []
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    if energy_grade is not None:
        conditions.append("b.energy_grade = :energy_grade")
        params["energy_grade"] = energy_grade

    if usage_type is not None:
        conditions.append("b.usage_type = :usage_type")
        params["usage_type"] = usage_type

    if vintage is not None:
        conditions.append("b.vintage_class = :vintage")
        params["vintage"] = vintage

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    sql = text(f"""
        SELECT
            b.pnu,
            CASE
                WHEN b.building_name IS NOT NULL AND TRIM(b.building_name) != ''
                    THEN b.building_name
                ELSE CONCAT(
                    CAST(CAST(SUBSTRING(b.pnu, 12, 4) AS INTEGER) AS TEXT),
                    CASE WHEN CAST(SUBSTRING(b.pnu, 16, 4) AS INTEGER) > 0
                         THEN CONCAT('-', CAST(CAST(SUBSTRING(b.pnu, 16, 4) AS INTEGER) AS TEXT))
                         ELSE '' END,
                    '번지'
                )
            END AS building_name,
            b.usage_type,
            b.vintage_class,
            b.built_year,
            b.total_area,
            b.floors_above,
            b.floors_below,
            b.height,
            b.structure_type,
            b.energy_grade,
            er.total_energy,
            CASE WHEN b.geom IS NOT NULL THEN ST_AsGeoJSON(b.geom)::json ELSE NULL END AS geometry,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        {where_clause}
        LIMIT 5000
    """)

    rows = db.execute(sql, params).fetchall()
    logger.info("Listed %d buildings with filters: %s", len(rows), params)

    features = []
    for r in rows:
        feature = {
            "type": "Feature",
            "geometry": r.geometry,
            "properties": {
                "pnu": r.pnu,
                "building_name": r.building_name,
                "usage_type": r.usage_type,
                "vintage_class": r.vintage_class,
                "built_year": r.built_year,
                "total_area": float(r.total_area) if r.total_area else None,
                "floors_above": r.floors_above,
                "floors_below": r.floors_below,
                "height": float(r.height) if r.height else None,
                "structure_type": r.structure_type,
                "energy_grade": r.energy_grade,
                "total_energy": float(r.total_energy) if r.total_energy else None,
                "lng": float(r.lng) if r.lng else None,
                "lat": float(r.lat) if r.lat else None,
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/{pnu}")
def get_building_detail(
    pnu: str,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Retrieve a single building with full attributes and energy results."""
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            b.pnu,
            b.building_name,
            b.usage_type,
            b.vintage_class,
            b.built_year,
            b.total_area,
            b.floors_above,
            b.floors_below,
            b.height,
            b.structure_type,
            b.energy_grade,
            er.total_energy,
            er.heating,
            er.cooling,
            er.hot_water,
            er.lighting,
            er.ventilation,
            er.data_tier,
            er.simulation_type,
            er.co2_kg_m2,
            er.primary_energy_kwh_m2,
            ST_AsGeoJSON(b.geom)::json AS geometry,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        WHERE b.pnu = :pnu
    """)

    row = db.execute(sql, {"pnu": pnu}).fetchone()
    if row is None:
        logger.warning("Building not found: pnu=%s", pnu)
        raise HTTPException(status_code=404, detail=f"Building {pnu} not found")

    logger.info("Retrieved building detail: pnu=%s", pnu)

    result: dict = {
        "type": "Feature",
        "geometry": row.geometry,
        "properties": {
            "pnu": row.pnu,
            "building_name": row.building_name,
            "usage_type": row.usage_type,
            "vintage_class": row.vintage_class,
            "built_year": row.built_year,
            "total_area": float(row.total_area) if row.total_area else None,
            "floors_above": row.floors_above,
            "floors_below": row.floors_below,
            "height": float(row.height) if row.height else None,
            "structure_type": row.structure_type,
            "energy_grade": row.energy_grade,
            "lng": float(row.lng) if row.lng else None,
            "lat": float(row.lat) if row.lat else None,
        },
    }

    # Attach energy breakdown + provenance if available
    if row.total_energy is not None:
        total_e = float(row.total_energy)
        area = float(row.total_area) if row.total_area else None
        # total_energy는 archetype/Tier1/2/4에서는 EUI(kWh/m²/yr),
        # Tier C(tier_c_metered)에서는 절대값(kWh/yr)
        is_tier_c = row.simulation_type == "tier_c_metered"
        eui = round(total_e / area, 1) if (is_tier_c and area) else total_e
        result["properties"]["energy"] = {
            "total_energy": total_e,
            "eui_kwh_m2": eui,
            "heating": float(row.heating) if row.heating else None,
            "cooling": float(row.cooling) if row.cooling else None,
            "hot_water": float(row.hot_water) if row.hot_water else None,
            "lighting": float(row.lighting) if row.lighting else None,
            "ventilation": float(row.ventilation) if row.ventilation else None,
            "co2_kg_m2": float(row.co2_kg_m2) if row.co2_kg_m2 is not None else None,
            "primary_energy_kwh_m2": float(row.primary_energy_kwh_m2) if row.primary_energy_kwh_m2 is not None else None,
            "data_tier": row.data_tier,
            "simulation_type": row.simulation_type,
        }
    result["properties"]["data_tier"] = row.data_tier
    result["properties"]["simulation_type"] = row.simulation_type

    return result
