"""
화재 위험도 API 라우터 (Phase F0+F1+F2+F3+F4).

Phase F0: 건물별 위험 점수 (구조/연령/용도/층수)
Phase F1: 인접 그래프, 소방서 위치, 클러스터, 대응 커버리지
Phase F2: BFS 화재 확산 시뮬레이션 (시나리오 POST/GET)
Phase F3: pgRouting 대피 경로 계산
Phase F4: 현재 기상 조회 (기상청 동네예보 기반)
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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
                "lng":   float(r.lng) if r.lng is not None else None,
                "lat":   float(r.lat) if r.lat is not None else None,
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
                "lng": float(r.lng) if r.lng is not None else None,
                "lat": float(r.lat) if r.lat is not None else None,
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


# ── Phase F2: 화재 확산 시뮬레이션 엔드포인트 ────────────────────────────────


class ScenarioRequest(BaseModel):
    origin_pnu: str = Field(..., description="발화 건물 PNU")
    wind_direction: float = Field(
        default=0.0, ge=0.0, lt=360.0,
        description="바람이 불어오는 방향 (0=North, 90=East, degrees)",
    )
    wind_speed: float = Field(
        default=0.0, ge=0.0, le=30.0,
        description="바람 속도 (m/s). 기상청 기준: 약풍 2~5, 강풍 10+",
    )
    max_steps: int = Field(
        default=30, ge=1, le=60,
        description="최대 BFS 단계 수 (1 step = 5분)",
    )


@router.post("/scenario")
def create_fire_scenario(
    req: ScenarioRequest,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    화재 확산 시뮬레이션 시나리오 생성 및 즉시 실행 (동기).

    BFS 시뮬레이션을 API 프로세스에서 직접 실행하고 결과를 반환한다.
    수초 이내 완료되므로 동기 처리가 적합하다.

    Returns:
        {"scenario_id": str, "status": "done", "stats": {...}}
    """
    from src.fire_safety.fire_spread import run_bfs
    from src.shared.database import engine

    if not re.match(r"^\d{19,25}$", req.origin_pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    logger.info(
        "Fire scenario start: origin=%s wind=%.0f°@%.1fm/s steps=%d",
        req.origin_pnu, req.wind_direction, req.wind_speed, req.max_steps,
    )

    try:
        with engine.connect() as conn:
            result = run_bfs(
                conn,
                origin_pnu=req.origin_pnu,
                wind_direction=req.wind_direction,
                wind_speed=req.wind_speed,
                max_steps=req.max_steps,
            )

            insert_sql = text("""
                INSERT INTO fire_scenario_results
                    (origin_pnu, wind_direction, wind_speed,
                     affected_pnus, spread_timeline, stats)
                VALUES
                    (:origin_pnu, :wind_direction, :wind_speed,
                     :affected_pnus,
                     CAST(:spread_timeline AS jsonb),
                     CAST(:stats AS jsonb))
                RETURNING CAST(scenario_id AS text)
            """)
            row = conn.execute(insert_sql, {
                "origin_pnu":      req.origin_pnu,
                "wind_direction":  req.wind_direction,
                "wind_speed":      req.wind_speed,
                "affected_pnus":   result["affected_pnus"],
                "spread_timeline": json.dumps(result["spread_timeline"]),
                "stats":           json.dumps(result["stats"]),
            }).fetchone()
            conn.commit()
            scenario_id = row[0]

    except Exception as exc:
        logger.error("Fire scenario error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        "Fire scenario done: id=%s total=%d",
        scenario_id, result["stats"]["total_buildings"],
    )
    return {
        "scenario_id":   scenario_id,
        "status":        "done",
        "origin_pnu":    req.origin_pnu,
        "wind_direction": req.wind_direction,
        "wind_speed":    req.wind_speed,
        "stats":         result["stats"],
    }


@router.get("/scenario/task/{task_id}")
def poll_scenario_task(task_id: str) -> dict:
    """
    Celery 태스크 상태 폴링.

    Returns:
        status: "queued" | "running" | "done" | "error"
        result: 완료 시 {"scenario_id", "stats"} 포함
    """
    from src.shared.celery_app import celery as _celery

    result = _celery.AsyncResult(task_id)
    state = result.state  # PENDING / STARTED / SUCCESS / FAILURE

    if state == "PENDING":
        return {"task_id": task_id, "status": "queued"}
    elif state == "STARTED":
        return {"task_id": task_id, "status": "running"}
    elif state == "SUCCESS":
        return {"task_id": task_id, "status": "done", "result": result.result}
    elif state == "FAILURE":
        return {"task_id": task_id, "status": "error", "detail": str(result.result)}
    else:
        return {"task_id": task_id, "status": state.lower()}


@router.get("/scenario/{scenario_id}")
def get_scenario(
    scenario_id: str,
    include_timeline: bool = Query(True, description="spread_timeline 포함 여부"),
    include_pnus: bool = Query(False, description="affected_pnus 전체 목록 포함 (대용량)"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    저장된 시나리오 결과 조회.

    - include_timeline=true (기본): CesiumJS 타임라인 애니메이션용 단계별 데이터 포함
    - include_pnus=false (기본): 피해 건물 PNU 전체 목록 제외 (통계만)
    """
    sql = text("""
        SELECT
            CAST(scenario_id AS text) AS scenario_id,
            origin_pnu,
            wind_direction,
            wind_speed,
            affected_pnus,
            spread_timeline,
            stats,
            computed_at
        FROM fire_scenario_results
        WHERE scenario_id = CAST(:sid AS uuid)
        LIMIT 1
    """)
    row = db.execute(sql, {"sid": scenario_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")

    resp: dict = {
        "scenario_id":   row.scenario_id,
        "origin_pnu":    row.origin_pnu,
        "wind_direction": float(row.wind_direction) if row.wind_direction is not None else 0.0,
        "wind_speed":    float(row.wind_speed) if row.wind_speed is not None else 0.0,
        "stats":         row.stats if isinstance(row.stats, dict) else json.loads(row.stats),
        "computed_at":   row.computed_at.isoformat() if row.computed_at else None,
    }
    if include_timeline:
        resp["spread_timeline"] = (
            row.spread_timeline
            if isinstance(row.spread_timeline, list)
            else json.loads(row.spread_timeline)
        )
    if include_pnus:
        resp["affected_pnus"] = row.affected_pnus or []

    return resp


@router.get("/scenarios")
def list_scenarios(
    origin_pnu: Optional[str] = Query(None, description="발화 건물 PNU로 필터"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """최근 시나리오 목록 (통계만, 타임라인 제외)."""
    conditions = []
    params: dict = {"limit": limit}
    if origin_pnu:
        if not re.match(r"^\d{19,25}$", origin_pnu):
            raise HTTPException(status_code=400, detail="Invalid PNU format")
        conditions.append("origin_pnu = :origin_pnu")
        params["origin_pnu"] = origin_pnu

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = text(f"""
        SELECT
            CAST(scenario_id AS text) AS scenario_id,
            origin_pnu,
            wind_direction,
            wind_speed,
            stats,
            computed_at
        FROM fire_scenario_results
        {where}
        ORDER BY computed_at DESC
        LIMIT :limit
    """)
    rows = db.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "scenarios": [
            {
                "scenario_id":   r.scenario_id,
                "origin_pnu":    r.origin_pnu,
                "wind_direction": float(r.wind_direction) if r.wind_direction is not None else 0.0,
                "wind_speed":    float(r.wind_speed) if r.wind_speed is not None else 0.0,
                "stats":         r.stats if isinstance(r.stats, dict) else json.loads(r.stats),
                "computed_at":   r.computed_at.isoformat() if r.computed_at else None,
            }
            for r in rows
        ],
    }


# ── Phase F3: 대피 경로 ───────────────────────────────────────────────────────


@router.get("/evacuation/{pnu}")
def get_evacuation(
    pnu: str,
    max_routes: int = Query(default=3, ge=1, le=5),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    발화 건물 → 가장 가까운 피난 집결지까지 최적 대피 경로.

    반환:
    - routes[]: target_name, target_category, distance_m, coordinates (LineString)
    - origin: 발화 건물 좌표
    """
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            target_id,
            target_name,
            target_category,
            distance_m,
            ST_AsGeoJSON(route_geom)::text AS route_geojson
        FROM get_evacuation_routes(:pnu, :max_routes)
    """)
    rows = db.execute(sql, {"pnu": pnu, "max_routes": max_routes}).fetchall()

    if not rows:
        # 도로 네트워크 범위 밖이거나 PNU 미존재
        raise HTTPException(
            status_code=404,
            detail="No evacuation route found. Building may be outside road network coverage.",
        )

    # origin 좌표
    origin_sql = text("""
        SELECT ST_X(centroid) AS lng, ST_Y(centroid) AS lat
        FROM building_centroids WHERE pnu = :pnu LIMIT 1
    """)
    origin_row = db.execute(origin_sql, {"pnu": pnu}).fetchone()

    routes = []
    for r in rows:
        geojson = json.loads(r.route_geojson) if r.route_geojson else None
        coords = geojson.get("coordinates", []) if geojson else []
        routes.append({
            "target_id":       r.target_id,
            "target_name":     r.target_name,
            "target_category": r.target_category,
            "distance_m":      round(float(r.distance_m), 0),
            "coordinates":     coords,
        })

    return {
        "pnu":    pnu,
        "origin": {
            "lng": float(origin_row.lng),
            "lat": float(origin_row.lat),
        } if origin_row else None,
        "routes": routes,
    }


# ── Phase F4: 현재 기상 ───────────────────────────────────────────────────────


@router.get("/weather/current")
def get_current_weather(
    db: Session = Depends(get_db_dependency),
) -> dict:
    """
    서울 현재 기상 (기상청 동네예보 기반 평균).
    weather_snapshots 테이블 없거나 비어있으면 더미 반환.
    """
    from src.data_ingestion.collect_weather import get_current_seoul_wind
    return get_current_seoul_wind(db)


@router.post("/weather/collect")
def trigger_weather_collect(
    db: Session = Depends(get_db_dependency),
) -> dict:
    """기상 수집 즉시 실행 (Celery beat 외 수동 트리거)."""
    from src.data_ingestion.collect_weather import collect_and_save
    from src.shared.config import get_settings
    settings = get_settings()
    with db.get_bind().begin() as conn:
        saved = collect_and_save(
            conn,
            api_key=settings.DATA_GO_KR_API_KEY,
            kma_hub_key=settings.KMA_API_KEY,
        )
    return {"saved": saved}
