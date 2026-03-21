"""
Address search and filter API router.

Provides keyword search against building ledger, multi-criteria filtering,
and CSV export of filtered results.
"""

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.database import get_db_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class FilterRequest(BaseModel):
    """Multi-criteria filter for buildings."""

    energy_grades: list[str] = []
    vintage_classes: list[str] = []
    usage_types: list[str] = []
    bbox: list[float] = []  # [west, south, east, north]

    @classmethod
    def model_validator_bbox(cls, values: dict) -> dict:
        bbox = values.get("bbox", [])
        if bbox and len(bbox) != 4:
            raise ValueError("bbox must have exactly 4 values: [west, south, east, north]")
        if bbox:
            w, s, e, n = bbox
            if not (-180 <= w <= 180 and -180 <= e <= 180):
                raise ValueError("bbox longitude must be between -180 and 180")
            if not (-90 <= s <= 90 and -90 <= n <= 90):
                raise ValueError("bbox latitude must be between -90 and 90")
        return values


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_filter_query(
    filters: FilterRequest,
) -> tuple[str, dict]:
    """Build a WHERE clause and parameter dict from a FilterRequest."""
    conditions: list[str] = []
    params: dict = {}

    if filters.energy_grades:
        conditions.append("b.energy_grade = ANY(:energy_grades)")
        params["energy_grades"] = filters.energy_grades

    if filters.vintage_classes:
        conditions.append("b.vintage_class = ANY(:vintage_classes)")
        params["vintage_classes"] = filters.vintage_classes

    if filters.usage_types:
        conditions.append("b.usage_type = ANY(:usage_types)")
        params["usage_types"] = filters.usage_types

    if len(filters.bbox) == 4:
        west, south, east, north = filters.bbox
        conditions.append(
            "ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    return where_clause, params


def _filtered_rows(db: Session, filters: FilterRequest) -> list:
    """Execute a filtered query and return row objects."""
    where_clause, params = _build_filter_query(filters)

    sql = text(f"""
        SELECT
            b.pnu,
            b.building_name,
            b.usage_type,
            b.vintage_class,
            b.built_year,
            b.total_area,
            b.floors_above,
            b.height,
            b.structure_type,
            b.energy_grade,
            er.total_energy,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu
        {where_clause}
        LIMIT 1000
    """)

    return db.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/search")
def search_buildings(
    q: str = Query(..., min_length=1, max_length=100, description="Search keyword"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Search buildings by name (case-insensitive partial match).

    Returns the top 10 matches with PNU and centroid coordinates, joined
    against the footprints table to obtain geometry.
    """
    sql = text("""
        SELECT
            b.pnu,
            b.building_name,
            b.usage_type,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM buildings_enriched b
        WHERE b.building_name ILIKE :pattern
        LIMIT 10
    """)

    # Escape LIKE special characters
    safe_q = q.replace("%", r"\%").replace("_", r"\_")
    pattern = f"%{safe_q}%"
    rows = db.execute(sql, {"pattern": pattern}).fetchall()
    logger.info("Search query='%s' returned %d results", q, len(rows))

    results = [
        {
            "pnu": r.pnu,
            "building_name": r.building_name,
            "usage_type": r.usage_type,
            "lng": float(r.lng) if r.lng else None,
            "lat": float(r.lat) if r.lat else None,
        }
        for r in rows
    ]

    return {"query": q, "count": len(results), "results": results}


@router.post("/filter")
def filter_buildings(
    filters: FilterRequest,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """Filter buildings by energy grades, vintage classes, usage types, and bbox."""
    rows = _filtered_rows(db, filters)
    logger.info("Filter returned %d buildings", len(rows))

    features = []
    for r in rows:
        features.append({
            "pnu": r.pnu,
            "building_name": r.building_name,
            "usage_type": r.usage_type,
            "vintage_class": r.vintage_class,
            "built_year": r.built_year,
            "total_area": float(r.total_area) if r.total_area else None,
            "floors_above": r.floors_above,
            "height": float(r.height) if r.height else None,
            "structure_type": r.structure_type,
            "energy_grade": r.energy_grade,
            "total_energy": float(r.total_energy) if r.total_energy else None,
            "lng": float(r.lng) if r.lng else None,
            "lat": float(r.lat) if r.lat else None,
        })

    return {"count": len(features), "buildings": features}


@router.get("/filter/export")
def export_filtered_buildings(
    energy_grades: Optional[str] = Query(None, description="Comma-separated energy grades"),
    vintage_classes: Optional[str] = Query(None, description="Comma-separated vintage classes"),
    usage_types: Optional[str] = Query(None, description="Comma-separated usage types"),
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> StreamingResponse:
    """Export filtered buildings as a CSV download.

    Accepts the same filter criteria as POST /filter but via query params
    so the endpoint can be opened directly in a browser.
    """
    bbox: list[float] = []
    if all(v is not None for v in [west, south, east, north]):
        bbox = [west, south, east, north]  # type: ignore[list-item]

    filters = FilterRequest(
        energy_grades=energy_grades.split(",") if energy_grades else [],
        vintage_classes=vintage_classes.split(",") if vintage_classes else [],
        usage_types=usage_types.split(",") if usage_types else [],
        bbox=bbox,
    )

    rows = _filtered_rows(db, filters)
    logger.info("CSV export: %d buildings", len(rows))

    # Build CSV in memory
    output = io.StringIO()
    fieldnames = [
        "pnu", "building_name", "usage_type", "vintage_class", "built_year",
        "total_area", "floors_above", "height", "structure_type",
        "energy_grade", "total_energy", "lng", "lat",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for r in rows:
        writer.writerow({
            "pnu": r.pnu,
            "building_name": r.building_name,
            "usage_type": r.usage_type,
            "vintage_class": r.vintage_class,
            "built_year": r.built_year,
            "total_area": float(r.total_area) if r.total_area else "",
            "floors_above": r.floors_above,
            "height": float(r.height) if r.height else "",
            "structure_type": r.structure_type,
            "energy_grade": r.energy_grade,
            "total_energy": float(r.total_energy) if r.total_energy else "",
            "lng": float(r.lng) if r.lng else "",
            "lat": float(r.lat) if r.lat else "",
        })

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=buildings_export.csv"},
    )
