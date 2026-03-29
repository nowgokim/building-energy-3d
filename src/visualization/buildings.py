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
from src.simulation.retrofit import ALL_MEASURE_IDS, simulate_retrofit

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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: 분석 API  ← /{pnu} 보다 먼저 등록해야 라우트 우선순위 정상 작동
# ─────────────────────────────────────────────────────────────────────────────

# sigungu_cd(PNU 앞 5자리) → 지역명 (행정안전부 표준코드, 불변 메타데이터)
_SIGUNGU_NAMES: dict[str, str] = {
    # 서울특별시 25구
    "11110": "종로구",    "11140": "중구",      "11170": "용산구",    "11200": "성동구",
    "11215": "광진구",    "11230": "동대문구",  "11260": "중랑구",    "11290": "성북구",
    "11305": "강북구",    "11320": "도봉구",    "11350": "노원구",    "11380": "은평구",
    "11410": "서대문구",  "11440": "마포구",    "11470": "양천구",    "11500": "강서구",
    "11530": "구로구",    "11545": "금천구",    "11560": "영등포구",  "11590": "동작구",
    "11620": "관악구",    "11650": "서초구",    "11680": "강남구",    "11710": "송파구",
    "11740": "강동구",
    # 인천광역시
    "28110": "중구(인천)","28140": "동구(인천)","28177": "미추홀구",  "28185": "연수구",
    "28200": "남동구",    "28237": "부평구",    "28245": "계양구",    "28260": "서구(인천)",
    "28710": "강화군",    "28720": "옹진군",
    # 경기도 주요 시군구
    "41110": "수원시",    "41111": "수원시 장안구","41113": "수원시 권선구",
    "41115": "수원시 팔달구","41117": "수원시 영통구",
    "41130": "성남시",    "41131": "성남시 수정구","41133": "성남시 중원구","41135": "성남시 분당구",
    "41150": "의정부시",  "41170": "안양시",
    "41171": "안양시 만안구","41173": "안양시 동안구",
    "41190": "부천시",    "41192": "부천시 오정구","41194": "부천시 원미구","41195": "부천시",
    "41196": "부천시 소사구","41197": "부천시 여월동","41199": "화성시",
    "41210": "광명시",    "41220": "평택시",    "41250": "동두천시",  "41270": "안산시",
    "41271": "안산시 단원구","41273": "안산시 상록구",
    "41280": "고양시",    "41281": "고양시 덕양구","41285": "고양시 일산동구","41287": "고양시 일산서구",
    "41290": "과천시",    "41310": "구리시",    "41360": "남양주시",  "41370": "오산시",
    "41390": "시흥시",    "41410": "군포시",    "41430": "의왕시",    "41450": "하남시",
    "41461": "용인시 처인구","41463": "용인시 기흥구","41465": "용인시 수지구",
    "41570": "파주시",    "41590": "이천시",    "41610": "안성시",    "41630": "김포시",
    "41650": "화성시",    "41670": "광주시",    "41690": "양주시",    "41720": "포천시",
    "41730": "여주시",    "41820": "연천군",    "41830": "가평군",    "41840": "양평군",
}
_ZEB_THRESHOLD = 150.0  # kWh/m²/yr (2025년 녹색건축법 1차에너지 기준)


@router.get("/district-stats")
def get_district_stats(db: Session = Depends(get_db_dependency)) -> dict:
    """행정구역(시군구)별 에너지 통계. SSOT: energy_results(is_current=TRUE)."""
    sql = text("""
        SELECT
            LEFT(b.pnu, 5)                         AS sigungu_cd,
            COUNT(*)                                AS building_count,
            ROUND(AVG(er.total_energy)::numeric, 1) AS avg_eui,
            ROUND(MIN(er.total_energy)::numeric, 1) AS min_eui,
            ROUND(MAX(er.total_energy)::numeric, 1) AS max_eui,
            COUNT(CASE WHEN er.total_energy <= :zeb THEN 1 END) AS zeb_count,
            COUNT(CASE WHEN er.total_energy >  :zeb THEN 1 END) AS non_zeb_count,
            AVG(ST_X(ST_Centroid(b.geom)))          AS center_lng,
            AVG(ST_Y(ST_Centroid(b.geom)))          AS center_lat
        FROM buildings_enriched b
        JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        WHERE er.total_energy IS NOT NULL AND er.total_energy > 0
        GROUP BY LEFT(b.pnu, 5)
        ORDER BY avg_eui DESC
    """)
    rows = db.execute(sql, {"zeb": _ZEB_THRESHOLD}).fetchall()
    districts = []
    for r in rows:
        total = (r.zeb_count or 0) + (r.non_zeb_count or 0)
        districts.append({
            "sigungu_cd":     r.sigungu_cd,
            "name":           _SIGUNGU_NAMES.get(r.sigungu_cd, r.sigungu_cd),
            "building_count": r.building_count,
            "avg_eui":        float(r.avg_eui) if r.avg_eui else None,
            "min_eui":        float(r.min_eui) if r.min_eui else None,
            "max_eui":        float(r.max_eui) if r.max_eui else None,
            "zeb_count":      r.zeb_count or 0,
            "non_zeb_count":  r.non_zeb_count or 0,
            "zeb_pct":        round((r.zeb_count or 0) / total * 100, 1) if total else 0,
            "center_lng":     float(r.center_lng) if r.center_lng else None,
            "center_lat":     float(r.center_lat) if r.center_lat else None,
        })
    return {"zeb_threshold": _ZEB_THRESHOLD, "districts": districts}


@router.get("/compare")
def compare_buildings(
    pnu1: str = Query(..., description="첫 번째 건물 PNU"),
    pnu2: str = Query(..., description="두 번째 건물 PNU"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """두 건물 에너지 데이터 비교. SSOT: energy_results(is_current=TRUE)."""
    for pnu in (pnu1, pnu2):
        if not re.match(r"^\d{19,25}$", pnu):
            raise HTTPException(status_code=400, detail=f"Invalid PNU: {pnu}")

    sql = text("""
        SELECT
            b.pnu, b.building_name, b.usage_type, b.vintage_class,
            b.built_year, b.total_area, b.floors_above, b.height,
            b.structure_type, b.energy_grade,
            er.total_energy, er.heating, er.cooling, er.hot_water,
            er.lighting, er.ventilation, er.co2_kg_m2,
            er.primary_energy_kwh_m2, er.data_tier, er.simulation_type,
            ST_X(bc.centroid) AS lng, ST_Y(bc.centroid) AS lat
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        LEFT JOIN building_centroids bc ON bc.pnu = b.pnu
        WHERE b.pnu = ANY(:pnus)
    """)
    rows = {r.pnu: r for r in db.execute(sql, {"pnus": [pnu1, pnu2]}).fetchall()}

    def _serialize(pnu: str) -> dict:
        r = rows.get(pnu)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Building {pnu} not found")
        area = float(r.total_area) if r.total_area else None
        is_tier_c = r.simulation_type == "tier_c_metered"
        total_e = float(r.total_energy) if r.total_energy else None
        eui = (round(total_e / area, 1) if area else None) if (is_tier_c and total_e) else total_e
        return {
            "pnu": r.pnu, "building_name": r.building_name,
            "usage_type": r.usage_type, "vintage_class": r.vintage_class,
            "built_year": r.built_year, "total_area": area,
            "floors_above": r.floors_above,
            "height": float(r.height) if r.height else None,
            "structure_type": r.structure_type, "energy_grade": r.energy_grade,
            "eui_kwh_m2": eui,
            "heating":     float(r.heating)     if r.heating     else None,
            "cooling":     float(r.cooling)     if r.cooling     else None,
            "hot_water":   float(r.hot_water)   if r.hot_water   else None,
            "lighting":    float(r.lighting)    if r.lighting    else None,
            "ventilation": float(r.ventilation) if r.ventilation else None,
            "co2_kg_m2":   float(r.co2_kg_m2)  if r.co2_kg_m2 is not None else None,
            "primary_energy_kwh_m2": float(r.primary_energy_kwh_m2) if r.primary_energy_kwh_m2 else None,
            "data_tier": r.data_tier, "simulation_type": r.simulation_type,
            "zeb_gap": round(eui - _ZEB_THRESHOLD, 1) if eui else None,
            "lng": float(r.lng) if r.lng else None,
            "lat": float(r.lat) if r.lat else None,
        }

    b1, b2 = _serialize(pnu1), _serialize(pnu2)

    def _diff(key: str) -> float | None:
        v1, v2 = b1.get(key), b2.get(key)
        return round(v2 - v1, 1) if v1 is not None and v2 is not None else None

    return {
        "building1": b1, "building2": b2,
        "diff": {
            "eui_kwh_m2": _diff("eui_kwh_m2"), "co2_kg_m2": _diff("co2_kg_m2"),
            "total_area": _diff("total_area"),  "built_year": _diff("built_year"),
        },
        "zeb_threshold": _ZEB_THRESHOLD,
    }


@router.get("/retrofit-priority")
def get_retrofit_priority(
    limit: int = Query(default=20, ge=1, le=100),
    sigungu_cd: Optional[str] = Query(default=None),
    vintage: Optional[str] = Query(default=None),
    usage_type: Optional[str] = Query(default=None),
    min_eui: float = Query(default=150.0),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """그린리모델링 우선순위. 기준: EUI × 면적 (총 에너지 소비량 kWh/yr). SSOT: energy_results."""
    conditions = [
        "er.total_energy > :min_eui",
        "b.total_area IS NOT NULL AND b.total_area > 0",
    ]
    params: dict = {"min_eui": min_eui, "limit": limit}
    if sigungu_cd:
        conditions.append("LEFT(b.pnu, 5) = :sigungu_cd")
        params["sigungu_cd"] = sigungu_cd
    if vintage:
        conditions.append("b.vintage_class = :vintage")
        params["vintage"] = vintage
    if usage_type:
        conditions.append("b.usage_type = :usage_type")
        params["usage_type"] = usage_type

    where_clause = " AND ".join(conditions)
    sql = text(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (b.pnu)
                b.pnu,
                COALESCE(NULLIF(TRIM(b.building_name),''), NULLIF(TRIM(b.usage_type),'') || ' 건물', '미분류 건물') AS building_name,
                b.usage_type, b.vintage_class, b.built_year,
                ROUND(b.total_area::numeric, 0)                           AS total_area,
                b.floors_above, b.energy_grade, LEFT(b.pnu, 5) AS sigungu_cd,
                ROUND(er.total_energy::numeric, 1)                        AS eui,
                ROUND((er.total_energy * b.total_area)::numeric, 0)       AS total_kwh_yr,
                ROUND((er.total_energy - :min_eui)::numeric, 1)           AS zeb_gap,
                er.data_tier,
                ST_X(ST_Centroid(b.geom)) AS lng, ST_Y(ST_Centroid(b.geom)) AS lat
            FROM buildings_enriched b
            JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
            WHERE {where_clause}
            ORDER BY b.pnu, (er.total_energy * b.total_area) DESC
        ) sub
        ORDER BY total_kwh_yr DESC
        LIMIT :limit
    """)
    rows = db.execute(sql, params).fetchall()
    return {
        "zeb_threshold": min_eui,
        "total_returned": len(rows),
        "buildings": [
            {
                "rank": i, "pnu": r.pnu, "building_name": r.building_name,
                "usage_type": r.usage_type, "vintage_class": r.vintage_class,
                "built_year": r.built_year, "total_area": float(r.total_area),
                "floors_above": r.floors_above, "energy_grade": r.energy_grade,
                "sigungu_cd": r.sigungu_cd,
                "district_name": _SIGUNGU_NAMES.get(r.sigungu_cd, r.sigungu_cd),
                "eui_kwh_m2": float(r.eui), "total_kwh_yr": float(r.total_kwh_yr),
                "zeb_gap": float(r.zeb_gap), "data_tier": r.data_tier,
                "lng": float(r.lng) if r.lng else None,
                "lat": float(r.lat) if r.lat else None,
            }
            for i, r in enumerate(rows, 1)
        ],
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
        # Tier C: total_energy = 절대값(kWh/yr) → area로 나눠 EUI 계산
        # 나머지: total_energy = EUI(kWh/m²/yr) 직접 사용
        # Tier C인데 area 없으면 EUI 계산 불가 → None
        if is_tier_c:
            eui = round(total_e / area, 1) if area else None
        else:
            eui = total_e
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


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/buildings/{pnu}/retrofit
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{pnu}/retrofit")
def get_retrofit_simulation(
    pnu: str,
    measures: str = Query(
        default="",
        description="쉼표 구분 조치 ID. 비어있으면 전체 적용. "
                    f"선택 가능: {','.join(ALL_MEASURE_IDS)}",
    ),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """건물 리트로핏 비용·효과 추정.

    현재 EUI·CO2 기준으로 각 개선 조치(창호 교체, 단열 강화, LED, HVAC)의
    에너지 절감량·CO2 저감량·시공비·단순회수기간을 반환.

    - **measures**: 쉼표 구분 조치 ID (예: `window,led_lighting`). 생략 시 전체.
    - EUI는 energy_results 실측/추정 값을 사용.
    - 면적 없는 건물은 총비용·회수기간 = null.
    """
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            b.pnu, b.usage_type, b.vintage_class, b.total_area,
            er.total_energy, er.co2_kg_m2, er.simulation_type
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu AND er.is_current = TRUE
        WHERE b.pnu = :pnu
    """)
    row = db.execute(sql, {"pnu": pnu}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Building {pnu} not found")
    if row.total_energy is None:
        raise HTTPException(
            status_code=422,
            detail="Energy data not available for this building",
        )

    # EUI 결정 (Tier C는 절대값 → area로 나눔, 나머지는 EUI 직접)
    is_tier_c = row.simulation_type == "tier_c_metered"
    area = float(row.total_area) if row.total_area else None
    total_e = float(row.total_energy)
    if is_tier_c:
        if not area:
            raise HTTPException(status_code=422, detail="Tier C building has no area — EUI cannot be computed")
        eui = round(total_e / area, 1)
    else:
        eui = total_e
    if eui <= 0:
        raise HTTPException(status_code=422, detail="EUI must be positive")

    selected = [m.strip() for m in measures.split(",") if m.strip()] or None

    result = simulate_retrofit(
        pnu=pnu,
        eui_kwh_m2=eui,
        co2_kg_m2=float(row.co2_kg_m2) if row.co2_kg_m2 is not None else None,
        total_area_m2=area,
        vintage_class=row.vintage_class,
        usage_type=row.usage_type,
        selected_measures=selected,
    )

    return {
        "pnu": result.pnu,
        "building": {
            "eui_kwh_m2": result.eui_before,
            "co2_kg_m2": result.co2_before_kg_m2,
            "total_area_m2": result.total_area_m2,
            "vintage_class": result.vintage_class,
            "usage_type": row.usage_type,
        },
        "measures": [
            {
                "id": m.id,
                "label": m.label,
                "description": m.description,
                "eui_saving_kwh_m2": m.eui_saving_kwh_m2,
                "saving_pct": round(m.saving_pct * 100, 1),
                "co2_saving_kg_m2": m.co2_saving_kg_m2,
                "cost_per_m2": m.cost_per_m2,
                "cost_total_krw": m.cost_total_krw,
                "annual_saving_krw": m.annual_saving_krw,
                "payback_years": m.payback_years,
            }
            for m in result.measures
        ],
        "combined": {
            "eui_before": result.eui_before,
            "eui_after": result.eui_after,
            "eui_saving_kwh_m2": result.eui_saving_kwh_m2,
            "saving_pct": round(result.saving_pct * 100, 1),
            "co2_before_kg_m2": result.co2_before_kg_m2,
            "co2_after_kg_m2": result.co2_after_kg_m2,
            "co2_saving_kg_m2": result.co2_saving_kg_m2,
            "cost_total_krw": result.cost_total_krw,
            "annual_saving_krw": result.annual_saving_krw,
            "payback_years": result.payback_years,
        },
    }
