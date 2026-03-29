"""
Address search and filter API router.

Provides keyword search against building ledger, multi-criteria filtering,
and CSV export of filtered results.
"""

import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.cache import get_redis as _get_redis
from src.shared.database import get_db_dependency
from src.shared.limiter import limiter

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
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=1000, ge=1, le=1000)  # 최대 1000건 (지도 오버레이 상한)

    @model_validator(mode="before")
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
            if w >= e:
                raise ValueError("bbox west must be less than east")
            if s >= n:
                raise ValueError("bbox south must be less than north")
        return values


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_filter_query(
    filters: FilterRequest,
) -> tuple[str, dict]:
    """Build a WHERE clause and parameter dict from a FilterRequest.

    Uses numbered bind parameters for list values (e.g. :eg_0, :eg_1) to
    avoid psycopg2's inability to adapt Python list objects with ANY().
    """
    conditions: list[str] = []
    params: dict = {}

    if filters.energy_grades:
        keys = [f"eg_{i}" for i in range(len(filters.energy_grades))]
        conditions.append(f"b.energy_grade IN ({', '.join(':' + k for k in keys)})")
        params.update(dict(zip(keys, filters.energy_grades)))

    if filters.vintage_classes:
        keys = [f"vc_{i}" for i in range(len(filters.vintage_classes))]
        conditions.append(f"b.vintage_class IN ({', '.join(':' + k for k in keys)})")
        params.update(dict(zip(keys, filters.vintage_classes)))

    if filters.usage_types:
        keys = [f"ut_{i}" for i in range(len(filters.usage_types))]
        conditions.append(f"b.usage_type IN ({', '.join(':' + k for k in keys)})")
        params.update(dict(zip(keys, filters.usage_types)))

    if len(filters.bbox) == 4:
        west, south, east, north = filters.bbox
        # building_centroids(GiST 인덱스)를 JOIN하여 bbox 필터 — ST_Centroid(geom) 직접 계산 대비 5~10x 빠름
        conditions.append(
            "EXISTS (SELECT 1 FROM building_centroids bc WHERE bc.pnu = b.pnu"
            " AND ST_Within(bc.centroid, ST_MakeEnvelope(:west, :south, :east, :north, 4326)))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    return where_clause, params


def _filtered_rows(db: Session, filters: FilterRequest) -> tuple[list, int]:
    """Execute a filtered query. Returns (rows, total_count)."""
    where_clause, params = _build_filter_query(filters)

    offset = (filters.page - 1) * filters.page_size
    params["_limit"] = filters.page_size
    params["_offset"] = offset

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
            er.co2_kg_m2,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat
        FROM buildings_enriched b
        LEFT JOIN building_centroids bc ON bc.pnu = b.pnu
        LEFT JOIN LATERAL (
            SELECT total_energy, co2_kg_m2
            FROM energy_results WHERE pnu = b.pnu AND is_current = TRUE LIMIT 1
        ) er ON true
        {where_clause}
        LIMIT :_limit OFFSET :_offset
    """)
    rows = db.execute(sql, params).fetchall()

    # 전체 건수: 첫 페이지에서 page_size 미만이면 COUNT 불필요
    if offset == 0 and len(rows) < filters.page_size:
        total = len(rows)
    else:
        count_sql = text(f"SELECT COUNT(*) FROM buildings_enriched b {where_clause}")
        count_params = {k: v for k, v in params.items() if not k.startswith("_")}
        total = db.execute(count_sql, count_params).scalar() or 0

    return rows, total


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_FILTER_OPTIONS_KEY = "filter_options"
_FILTER_OPTIONS_TTL = 86400  # 24시간 (용도/연대/등급 옵션은 거의 변하지 않음)

@router.get("/filter/options")
def get_filter_options(db: Session = Depends(get_db_dependency)) -> dict:
    """Return distinct filter values available in the database.

    Results are cached in Redis for 1 hour to avoid a full seq scan on 770K rows.
    """
    rc = _get_redis()
    if rc is not None:
        try:
            cached = rc.get(_FILTER_OPTIONS_KEY)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    sql = text("""
        SELECT
            array_agg(DISTINCT usage_type   ORDER BY usage_type)   FILTER (WHERE usage_type   IS NOT NULL AND usage_type   != '') AS usage_types,
            array_agg(DISTINCT vintage_class ORDER BY vintage_class) FILTER (WHERE vintage_class IS NOT NULL AND vintage_class != '') AS vintage_classes,
            array_agg(DISTINCT energy_grade  ORDER BY energy_grade)  FILTER (WHERE energy_grade  IS NOT NULL AND energy_grade  != '') AS energy_grades
        FROM buildings_enriched
    """)
    row = db.execute(sql).fetchone()
    result = {
        "usage_types":    row.usage_types    or [],
        "vintage_classes": row.vintage_classes or [],
        "energy_grades":   row.energy_grades  or [],
    }

    if rc is not None:
        try:
            rc.setex(_FILTER_OPTIONS_KEY, _FILTER_OPTIONS_TTL, json.dumps(result))
        except Exception:
            pass

    return result


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

    # LIKE 특수문자 이스케이프 (백슬래시 → %% → ___ 순서 중요)
    safe_q = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
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
    rows, total_count = _filtered_rows(db, filters)
    logger.info("Filter returned %d / %d buildings", len(rows), total_count)

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
            "co2_kg_m2": float(r.co2_kg_m2) if r.co2_kg_m2 is not None else None,
            "lng": float(r.lng) if r.lng else None,
            "lat": float(r.lat) if r.lat else None,
        })

    return {"count": len(features), "total_count": total_count, "buildings": features}


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/overlay/co2  — 뷰포트 CO2 오버레이 (최대 5,000건)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overlay/co2")
@limiter.limit("30/minute")
def co2_overlay(
    request: Request,
    west:  float = Query(...),
    south: float = Query(...),
    east:  float = Query(...),
    north: float = Query(...),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 CO2 강도 오버레이용 건물 포인트 반환.

    - co2_kg_m2: kgCO₂eq/m²/yr (배출계수 전력 0.4781 · 가스 0.2036 · 지역난방 0.1218)
    - pe_kwh_m2: 1차에너지 강도 kWh/m²/yr (ZEB 지표)
    - 최대 5,000건. 고도별 축소 표시 권장.
    """
    sql = text("""
        SELECT
            bc.pnu,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat,
            er.co2_kg_m2,
            er.primary_energy_kwh_m2  AS pe_kwh_m2
        FROM building_centroids bc
        JOIN energy_results er ON er.pnu = bc.pnu AND er.is_current = TRUE
        WHERE er.co2_kg_m2 IS NOT NULL
          AND ST_Within(
              bc.centroid,
              ST_MakeEnvelope(:west, :south, :east, :north, 4326)
          )
        ORDER BY RANDOM()
        LIMIT 5000
    """)
    rows = db.execute(sql, {"west": west, "south": south, "east": east, "north": north}).fetchall()
    return {
        "count": len(rows),
        "features": [
            {
                "pnu": r.pnu,
                "lng": float(r.lng),
                "lat": float(r.lat),
                "co2": round(float(r.co2_kg_m2), 1),
                "pe":  round(float(r.pe_kwh_m2), 1) if r.pe_kwh_m2 else None,
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/overlay/zeb  — ZEB 달성 여부 오버레이 (최대 5,000건)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overlay/zeb")
@limiter.limit("30/minute")
def zeb_overlay(
    request: Request,
    west:  float = Query(...),
    south: float = Query(...),
    east:  float = Query(...),
    north: float = Query(...),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 ZEB 달성 여부 오버레이.

    - zeb: true = EUI ≤ 150 (ZEB 달성), false = 초과
    - eui: total_energy kWh/m²/yr (SSOT: energy_results)
    """
    ZEB_THRESHOLD = 150.0
    sql = text("""
        SELECT
            bc.pnu,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat,
            er.total_energy   AS eui
        FROM building_centroids bc
        JOIN energy_results er ON er.pnu = bc.pnu AND er.is_current = TRUE
        WHERE er.total_energy IS NOT NULL
          AND er.total_energy > 0
          AND ST_Within(
              bc.centroid,
              ST_MakeEnvelope(:west, :south, :east, :north, 4326)
          )
        ORDER BY RANDOM()
        LIMIT 5000
    """)
    rows = db.execute(sql, {"west": west, "south": south, "east": east, "north": north}).fetchall()
    zeb_count = sum(1 for r in rows if float(r.eui) <= ZEB_THRESHOLD)
    return {
        "count": len(rows),
        "zeb_count": zeb_count,
        "non_zeb_count": len(rows) - zeb_count,
        "zeb_threshold": ZEB_THRESHOLD,
        "features": [
            {
                "pnu": r.pnu,
                "lng": float(r.lng),
                "lat": float(r.lat),
                "eui": round(float(r.eui), 1),
                "zeb": float(r.eui) <= ZEB_THRESHOLD,
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/overlay/hourly  — 시간대별 에너지 부하 오버레이 (최대 5,000건)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overlay/hourly")
@limiter.limit("30/minute")
def hourly_overlay(
    request: Request,
    west:  float = Query(...),
    south: float = Query(...),
    east:  float = Query(...),
    north: float = Query(...),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 건물별 일일 EUI + 용도 유형 반환.

    클라이언트가 시간대별 점유율(hourly_profile)을 적용하여
    0~23시 슬라이더 애니메이션을 구현한다.
    - eui_daily: total_energy / 365 (kWh/m²/일)
    - usage_type: 점유율 프로파일 선택용 (apartment/office/retail 등)
    """
    sql = text("""
        SELECT
            bc.pnu,
            ST_X(bc.centroid) AS lng,
            ST_Y(bc.centroid) AS lat,
            er.total_energy / 365.0 AS eui_daily,
            b.usage_type
        FROM building_centroids bc
        JOIN energy_results er ON er.pnu = bc.pnu AND er.is_current = TRUE
        JOIN buildings_enriched b ON b.pnu = bc.pnu
        WHERE er.total_energy IS NOT NULL
          AND er.total_energy > 0
          AND ST_Within(
              bc.centroid,
              ST_MakeEnvelope(:west, :south, :east, :north, 4326)
          )
        ORDER BY RANDOM()
        LIMIT 5000
    """)
    rows = db.execute(sql, {"west": west, "south": south, "east": east, "north": north}).fetchall()
    return {
        "count": len(rows),
        "features": [
            {
                "pnu": r.pnu,
                "lng": float(r.lng),
                "lat": float(r.lat),
                "eui_daily": round(float(r.eui_daily), 3),
                "usage_type": r.usage_type or "기타",
            }
            for r in rows
        ],
    }


@router.get("/filter/export")
@limiter.limit("10/minute")  # CSV 다운로드는 더 엄격하게 제한
def export_filtered_buildings(
    request: Request,
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

    rows, total_count = _filtered_rows(db, filters)
    logger.info("CSV export: %d buildings", len(rows))

    # Build CSV in memory
    output = io.StringIO()
    fieldnames = [
        "pnu", "building_name", "usage_type", "vintage_class", "built_year",
        "total_area", "floors_above", "height", "structure_type",
        "energy_grade", "total_energy", "co2_kg_m2", "lng", "lat",
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
            "co2_kg_m2": float(r.co2_kg_m2) if r.co2_kg_m2 is not None else "",
            "lng": float(r.lng) if r.lng else "",
            "lat": float(r.lat) if r.lat else "",
        })

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=buildings_export.csv"},
    )
