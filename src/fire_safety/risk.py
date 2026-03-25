"""
화재 위험도 API 라우터 (Phase F0+F1).

Phase F0: 건물별 위험 점수 (구조/연령/용도/층수)
Phase F1: 인접 그래프, 소방서 위치, 클러스터, 대응 커버리지
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.database import get_db_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fire", tags=["fire"])


@router.get("/risk/{pnu}")
def get_fire_risk(
    pnu: str,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """단일 건물 화재 위험도 조회."""
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            r.pnu,
            r.structure_score,
            r.age_score,
            r.usage_score,
            r.height_score,
            r.total_score,
            r.risk_grade,
            b.structure_type,
            b.built_year,
            b.usage_type,
            b.floors_above
        FROM building_fire_risk r
        JOIN buildings_enriched b ON r.pnu = b.pnu
        WHERE r.pnu = :pnu
        LIMIT 1
    """)
    row = db.execute(sql, {"pnu": pnu}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Building {pnu} not found")

    return {
        "pnu": row.pnu,
        "total_score": int(row.total_score),
        "risk_grade": row.risk_grade,
        "breakdown": {
            "structure": {"score": int(row.structure_score), "value": row.structure_type},
            "age":       {"score": int(row.age_score),       "value": row.built_year},
            "usage":     {"score": int(row.usage_score),     "value": row.usage_type},
            "height":    {"score": int(row.height_score),    "value": row.floors_above},
        },
    }


@router.get("/risk")
def list_fire_risk(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    grade: Optional[str]   = Query(None, description="HIGH | MEDIUM | LOW"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 건물 화재 위험도 목록 (히트맵용, 최대 5000건)."""
    conditions: list[str] = []
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    if grade:
        conditions.append("r.risk_grade = :grade")
        params["grade"] = grade.upper()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = text(f"""
        SELECT
            r.pnu,
            r.total_score,
            r.risk_grade,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM building_fire_risk r
        JOIN buildings_enriched b ON r.pnu = b.pnu
        {where}
        ORDER BY r.total_score DESC
        LIMIT 5000
    """)
    rows = db.execute(sql, params).fetchall()
    logger.info("Fire risk list: %d buildings", len(rows))

    return {
        "count": len(rows),
        "features": [
            {
                "pnu":   r.pnu,
                "score": int(r.total_score),
                "grade": r.risk_grade,
                "lng":   float(r.lng),
                "lat":   float(r.lat),
            }
            for r in rows
        ],
    }


@router.get("/stats")
def get_fire_stats(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 화재 위험도 등급별 통계."""
    params: dict = {}
    join_clause = ""

    if all(v is not None for v in [west, south, east, north]):
        join_clause = (
            "JOIN buildings_enriched b ON r.pnu = b.pnu "
            "WHERE ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params = {"west": west, "south": south, "east": east, "north": north}

    sql = text(f"""
        SELECT
            r.risk_grade,
            COUNT(*) AS cnt,
            ROUND(AVG(r.total_score)::numeric, 1) AS avg_score
        FROM building_fire_risk r
        {join_clause}
        GROUP BY r.risk_grade
        ORDER BY r.risk_grade
    """)
    rows = db.execute(sql, params).fetchall()

    return {
        "grade_distribution": {
            r.risk_grade: {"count": int(r.cnt), "avg_score": float(r.avg_score)}
            for r in rows
        }
    }


# ── Phase F1 엔드포인트 ──────────────────────────────────────────────────────


@router.get("/stations")
def list_fire_stations(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """소방서 / 119안전센터 위치 목록."""
    conditions: list[str] = []
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = text(f"""
        SELECT id, name, station_type, district,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM fire_stations
        {where}
        ORDER BY district
    """)
    rows = db.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "stations": [
            {
                "id": r.id,
                "name": r.name,
                "type": r.station_type,
                "district": r.district,
                "lng": float(r.lng),
                "lat": float(r.lat),
            }
            for r in rows
        ],
    }


@router.get("/clusters")
def list_clusters(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    risk_level: Optional[str] = Query(None, description="HIGH | MEDIUM"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """고위험 클러스터 목록 (GeoJSON Polygon)."""
    conditions: list[str] = []
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    if risk_level:
        conditions.append("risk_level = :risk_level")
        params["risk_level"] = risk_level.upper()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = text(f"""
        SELECT
            cluster_id,
            risk_level,
            building_count,
            avg_risk_score,
            ST_AsGeoJSON(geom) AS geojson
        FROM fire_risk_clusters
        {where}
        ORDER BY building_count DESC
        LIMIT 2000
    """)
    rows = db.execute(sql, params).fetchall()
    import json
    return {
        "count": len(rows),
        "clusters": [
            {
                "cluster_id": r.cluster_id,
                "risk_level": r.risk_level,
                "building_count": r.building_count,
                "avg_score": round(float(r.avg_risk_score), 1),
                "geometry": json.loads(r.geojson) if r.geojson else None,
            }
            for r in rows
        ],
    }


@router.get("/coverage/{pnu}")
def get_station_coverage(
    pnu: str,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """건물 PNU로부터 가장 가까운 소방서 응답시간 추정."""
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        WITH bldg AS (
            SELECT ST_Centroid(geom) AS pt
            FROM buildings_enriched
            WHERE pnu = :pnu AND geom IS NOT NULL
            LIMIT 1
        )
        SELECT
            fs.id,
            fs.name,
            fs.district,
            fs.station_type,
            ST_Distance(b.pt::geography, fs.geom::geography) AS dist_m,
            ST_X(fs.geom) AS slng,
            ST_Y(fs.geom) AS slat
        FROM bldg b
        CROSS JOIN LATERAL (
            SELECT * FROM fire_stations
            ORDER BY geom <-> b.pt
            LIMIT 3
        ) fs
        ORDER BY dist_m
    """)
    rows = db.execute(sql, {"pnu": pnu}).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Building {pnu} not found")

    def est_time(dist_m: float) -> float:
        """소방차 평균 속도 30km/h (도심 기준) 응답시간 추정(분)."""
        return round(dist_m / (30000 / 60), 1)

    return {
        "pnu": pnu,
        "nearest_stations": [
            {
                "id": r.id,
                "name": r.name,
                "district": r.district,
                "type": r.station_type,
                "distance_m": round(float(r.dist_m), 0),
                "est_response_min": est_time(float(r.dist_m)),
                "lng": float(r.slng),
                "lat": float(r.slat),
            }
            for r in rows
        ],
    }


@router.post("/adjacency/build")
def trigger_adjacency_build(
    background_tasks: BackgroundTasks,
    sgg_code: Optional[str] = Query(None, description="자치구 SGG 코드 (없으면 전체)"),
    dist_m: float = Query(25.0, description="인접 판단 거리(m)"),
) -> dict:
    """인접 건물 그래프 계산 Celery 태스크 트리거."""
    from src.fire_safety.tasks import build_adjacency_district, build_all_adjacency

    if sgg_code:
        job = build_adjacency_district.delay(sgg_code, dist_m)
        return {"status": "queued", "task_id": job.id, "sgg_code": sgg_code}
    else:
        jobs = build_all_adjacency.delay(dist_m)
        return {"status": "queued", "task_id": jobs.id, "sgg_codes": "all"}


@router.post("/clusters/compute")
def trigger_cluster_compute(
    eps_m: float = Query(100.0, description="DBSCAN epsilon (m)"),
    min_points: int = Query(5, description="최소 클러스터 건물 수"),
    min_score: float = Query(60.0, description="포함 최소 위험 점수"),
) -> dict:
    """고위험 클러스터 ST_ClusterDBSCAN 계산 트리거."""
    from src.fire_safety.tasks import compute_fire_clusters

    job = compute_fire_clusters.delay(eps_m, min_points, min_score)
    return {"status": "queued", "task_id": job.id}


@router.get("/adjacency/{pnu}")
def get_neighbors(
    pnu: str,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """특정 건물의 인접 건물 목록 (화재 확산 시뮬레이션용)."""
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            CASE WHEN source_pnu = :pnu THEN target_pnu ELSE source_pnu END AS neighbor_pnu,
            distance_m
        FROM building_adjacency
        WHERE source_pnu = :pnu OR target_pnu = :pnu
        ORDER BY distance_m
        LIMIT 50
    """)
    rows = db.execute(sql, {"pnu": pnu}).fetchall()
    return {
        "pnu": pnu,
        "neighbor_count": len(rows),
        "neighbors": [
            {"pnu": r.neighbor_pnu, "distance_m": round(float(r.distance_m), 1)}
            for r in rows
        ],
    }
