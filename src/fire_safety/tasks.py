"""
화재 안전 Celery 태스크 (Phase F1 + F2).

- build_adjacency_district: 구별 인접 건물 그래프 계산
- build_all_adjacency: 서울 전체 인접 그래프 빌드
- compute_fire_clusters: ST_ClusterDBSCAN 고위험 클러스터 계산
- run_fire_scenario: BFS 화재 확산 시뮬레이션 (Phase F2)
"""
import hashlib
import json
import logging
from typing import Optional

from sqlalchemy import text

from src.shared.celery_app import celery
from src.shared.database import engine, get_db

logger = logging.getLogger(__name__)

# 서울 25개 자치구 SGG 코드 접두어
SEOUL_SGG_CODES = [
    "11110", "11140", "11170", "11200", "11215",
    "11230", "11260", "11290", "11305", "11320",
    "11350", "11380", "11410", "11440", "11470",
    "11500", "11530", "11545", "11560", "11590",
    "11620", "11650", "11680", "11710", "11740",
]

ADJACENCY_DIST_M = 25.0  # 인접 판단 거리 (미터)


@celery.task(name="fire_safety.build_adjacency_district", bind=True, max_retries=3)
def build_adjacency_district(self, sgg_code: str, dist_m: float = ADJACENCY_DIST_M):
    """
    특정 자치구(sgg_code 접두어)의 건물 인접 그래프를 계산하여
    building_adjacency 테이블에 upsert한다.

    25m 이내 이웃 쌍만 저장 (양방향, 자기참조 제외).
    PostGIS ST_DWithin(geography) 사용 → 미터 단위 정확.
    """
    db = next(get_db())
    try:
        logger.info("Adjacency build start: sgg=%s dist=%.0fm", sgg_code, dist_m)

        # 양방향(a→b, b→a) 저장: BFS에서 source_pnu로만 이웃 조회하므로 양방향 필수
        sql = text("""
            INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m, spread_weight)
            WITH pairs AS (
                SELECT
                    a.pnu  AS pnu_a,
                    b.pnu  AS pnu_b,
                    ST_Distance(
                        a.geom::geography,
                        b.geom::geography
                    )::REAL AS dist,
                    GREATEST(0.0,
                        (1.0 - ST_Distance(a.geom::geography, b.geom::geography)::REAL / :dist_m)
                        * CASE
                            WHEN a.structure_class = 'masonry'
                              OR b.structure_class = 'masonry'
                            THEN 1.2
                            ELSE 0.8
                          END
                    )::REAL AS weight
                FROM buildings_enriched a
                JOIN buildings_enriched b
                  ON a.pnu < b.pnu
                 AND ST_DWithin(a.geom::geography, b.geom::geography, :dist_m)
                WHERE a.pnu LIKE :prefix
                  AND b.pnu LIKE :prefix
                  AND a.geom IS NOT NULL
                  AND b.geom IS NOT NULL
            )
            SELECT pnu_a, pnu_b, dist, weight FROM pairs
            UNION ALL
            SELECT pnu_b, pnu_a, dist, weight FROM pairs
            ON CONFLICT (source_pnu, target_pnu) DO UPDATE
              SET distance_m    = EXCLUDED.distance_m,
                  spread_weight = EXCLUDED.spread_weight
        """)
        result = db.execute(sql, {
            "dist_m": dist_m,
            "prefix": f"{sgg_code}%",
        })
        db.commit()
        inserted = result.rowcount
        logger.info("Adjacency build done: sgg=%s inserted/updated=%d", sgg_code, inserted)
        return {"sgg_code": sgg_code, "upserted": inserted}

    except Exception as exc:
        db.rollback()
        logger.error("Adjacency build error sgg=%s: %s", sgg_code, exc)
        raise self.retry(exc=exc, countdown=30)
    finally:
        db.close()


@celery.task(name="fire_safety.build_all_adjacency")
def build_all_adjacency(dist_m: float = ADJACENCY_DIST_M):
    """서울 전체 구별 순차 빌드 — 각 구를 별도 태스크로 분산."""
    results = []
    for sgg in SEOUL_SGG_CODES:
        job = build_adjacency_district.delay(sgg, dist_m)
        results.append({"sgg_code": sgg, "task_id": job.id})
        logger.info("Enqueued adjacency task: sgg=%s id=%s", sgg, job.id)
    return results


@celery.task(name="fire_safety.build_cross_sgg_adjacency", bind=True, max_retries=2,
             time_limit=1800, soft_time_limit=1500)
def build_cross_sgg_adjacency(self, dist_m: float = ADJACENCY_DIST_M):
    """
    서로 다른 자치구에 속하지만 dist_m 이내인 건물 쌍의 인접 그래프를 추가.

    구 경계에 위치한 건물은 build_adjacency_district(구 단위 빌드)에서 누락됨.
    이 태스크는 크로스-SGG 경계 쌍만 처리하므로 전체 서울 빌드보다 빠름.
    build_all_adjacency 완료 후 한 번 실행하면 됨.
    """
    db = next(get_db())
    try:
        logger.info("Cross-SGG adjacency build start: dist=%.0fm", dist_m)

        sql = text("""
            INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m, spread_weight)
            WITH pairs AS (
                SELECT DISTINCT ON (LEAST(a.pnu, b.pnu), GREATEST(a.pnu, b.pnu))
                    LEAST(a.pnu, b.pnu)    AS pnu_a,
                    GREATEST(a.pnu, b.pnu) AS pnu_b,
                    ST_Distance(
                        a.geom::geography,
                        b.geom::geography
                    )::REAL AS dist,
                    GREATEST(0.0,
                        (1.0 - ST_Distance(a.geom::geography, b.geom::geography)::REAL / :dist_m)
                        * CASE
                            WHEN a.structure_class = 'masonry'
                              OR b.structure_class = 'masonry'
                            THEN 1.2
                            ELSE 0.8
                          END
                    )::REAL AS weight
                FROM buildings_enriched a
                JOIN buildings_enriched b
                  ON LEFT(a.pnu, 5) <> LEFT(b.pnu, 5)
                 AND ST_DWithin(a.geom::geography, b.geom::geography, :dist_m)
                WHERE a.pnu LIKE '11%'
                  AND b.pnu LIKE '11%'
                  AND a.geom IS NOT NULL
                  AND b.geom IS NOT NULL
            )
            SELECT pnu_a, pnu_b, dist, weight FROM pairs
            UNION ALL
            SELECT pnu_b, pnu_a, dist, weight FROM pairs
            ON CONFLICT (source_pnu, target_pnu) DO UPDATE
              SET distance_m    = EXCLUDED.distance_m,
                  spread_weight = EXCLUDED.spread_weight
        """)
        result = db.execute(sql, {"dist_m": dist_m})
        db.commit()
        inserted = result.rowcount
        logger.info("Cross-SGG adjacency build done: inserted/updated=%d", inserted)
        return {"cross_sgg_upserted": inserted}

    except Exception as exc:
        db.rollback()
        logger.error("Cross-SGG adjacency build error: %s", exc)
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


@celery.task(name="fire_safety.compute_fire_clusters", bind=True)
def compute_fire_clusters(
    self,
    eps_m: float = 100.0,
    min_points: int = 5,
    min_score: float = 60.0,
):
    """
    ST_ClusterDBSCAN으로 고위험(HIGH+MEDIUM) 건물 클러스터를 탐지한다.

    Args:
        eps_m: DBSCAN epsilon (미터, 기본 100m)
        min_points: 클러스터 최소 건물 수
        min_score: 클러스터 포함 최소 위험 점수
    """
    db = next(get_db())
    try:
        logger.info(
            "Cluster compute start: eps=%.0fm min_pts=%d min_score=%.0f",
            eps_m, min_points, min_score,
        )

        # 기존 결과 삭제 후 재계산
        db.execute(text("TRUNCATE fire_risk_clusters"))

        sql = text("""
            INSERT INTO fire_risk_clusters
                (cluster_id, risk_level, building_count, avg_risk_score, geom)
            WITH clustered AS (
                SELECT
                    r.pnu,
                    r.total_score,
                    r.risk_grade,
                    b.geom,
                    ST_ClusterDBSCAN(b.geom, eps := :eps_deg, minpoints := :min_pts)
                        OVER () AS cid
                FROM building_fire_risk r
                JOIN buildings_enriched b ON r.pnu = b.pnu
                WHERE r.total_score >= :min_score
                  AND b.geom IS NOT NULL
            )
            SELECT
                cid               AS cluster_id,
                CASE
                    WHEN AVG(total_score) >= 60 THEN 'HIGH'
                    WHEN AVG(total_score) >= 35 THEN 'MEDIUM'
                    ELSE 'LOW'
                END               AS risk_level,
                COUNT(*)          AS building_count,
                AVG(total_score)::REAL AS avg_risk_score,
                ST_ConvexHull(ST_Collect(geom)) AS geom
            FROM clustered
            WHERE cid IS NOT NULL
            GROUP BY cid
            HAVING COUNT(*) >= :min_pts
        """)

        # eps를 미터에서 도(degree)로 변환 (서울 위도 ~37.5°)
        import math
        eps_deg = eps_m / (111000 * math.cos(37.5 * math.pi / 180))

        result = db.execute(sql, {
            "eps_deg": eps_deg,
            "min_pts": min_points,
            "min_score": min_score,
        })
        db.commit()
        count = result.rowcount
        logger.info("Cluster compute done: %d clusters inserted", count)
        return {"clusters_inserted": count}

    except Exception as exc:
        db.rollback()
        logger.error("Cluster compute error: %s", exc)
        raise self.retry(exc=exc, countdown=10)
    finally:
        db.close()


# ── Phase F2: 화재 확산 시뮬레이션 ───────────────────────────────────────────

def _scenario_cache_key(origin_pnu: str, wind_direction: float, wind_speed: float) -> str:
    """동일 파라미터 시나리오를 Redis에서 찾기 위한 캐시 키."""
    raw = f"{origin_pnu}:{wind_direction:.1f}:{wind_speed:.1f}"
    return "fire_scenario:" + hashlib.md5(raw.encode()).hexdigest()


@celery.task(
    name="fire_safety.run_fire_scenario",
    bind=True,
    max_retries=2,
    time_limit=120,
    soft_time_limit=90,
)
def run_fire_scenario(
    self,
    origin_pnu: str,
    wind_direction: float = 0.0,
    wind_speed: float = 0.0,
    max_steps: int = 30,
) -> dict:
    """
    Phase F2: BFS 화재 확산 시뮬레이션 실행 후 DB 저장.

    Args:
        origin_pnu:     발화 건물 PNU
        wind_direction: 바람이 불어오는 방향 (0=North, 90=East, degrees)
        wind_speed:     바람 속도 (m/s)
        max_steps:      최대 BFS 단계 (기본 30, = 최대 150분)

    Returns:
        {"scenario_id": str, "status": "done", "stats": {...}}
    """
    from src.fire_safety.fire_spread import run_bfs

    logger.info(
        "Fire scenario start: origin=%s wind=%.0f°@%.1fm/s",
        origin_pnu, wind_direction, wind_speed,
    )

    try:
        with engine.connect() as conn:
            result = run_bfs(
                conn,
                origin_pnu=origin_pnu,
                wind_direction=wind_direction,
                wind_speed=wind_speed,
                max_steps=max_steps,
            )

            # fire_scenario_results 저장
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
                "origin_pnu":      origin_pnu,
                "wind_direction":  wind_direction,
                "wind_speed":      wind_speed,
                "affected_pnus":   result["affected_pnus"],
                "spread_timeline": json.dumps(result["spread_timeline"]),
                "stats":           json.dumps(result["stats"]),
            }).fetchone()
            conn.commit()
            scenario_id = row[0]

        logger.info(
            "Fire scenario done: id=%s total_buildings=%d",
            scenario_id, result["stats"]["total_buildings"],
        )
        return {
            "scenario_id": scenario_id,
            "status":       "done",
            "stats":        result["stats"],
        }

    except Exception as exc:
        logger.error("Fire scenario error origin=%s: %s", origin_pnu, exc)
        raise self.retry(exc=exc, countdown=5)


# ── Phase F4: 기상 수집 태스크 ────────────────────────────────────────────────

@celery.task(name="src.fire_safety.tasks.collect_weather_task", bind=True)
def collect_weather_task(self):
    """1시간마다 기상청 동네예보 수집 → weather_snapshots 저장."""
    from src.data_ingestion.collect_weather import collect_and_save
    from src.shared.config import get_settings
    settings = get_settings()

    with engine.begin() as conn:
        saved = collect_and_save(
            conn,
            api_key=settings.DATA_GO_KR_API_KEY,
            kma_hub_key=settings.KMA_API_KEY,
        )

    logger.info("collect_weather_task: saved=%d", saved)
    return {"saved": saved}


# ── Phase 4-A: 에너지 데이터 수집 태스크 ─────────────────────────────────────

@celery.task(name="simulation.collect_energy_data", bind=True, time_limit=600)
def collect_energy_data_task(self):
    """건축물대장 에너지효율등급 → energy_results 업데이트 (주 1회 수준)."""
    from src.data_ingestion.collect_energy import collect_all, get_coverage_stats
    from src.shared.config import get_settings
    settings = get_settings()

    with engine.begin() as conn:
        result = collect_all(
            conn,
            data_go_kr_key=settings.DATA_GO_KR_API_KEY,
            seoul_data_key=settings.SEOUL_DATA_API_KEY,
            hub_limit=3000,
        )
        stats = get_coverage_stats(conn)

    logger.info(
        "collect_energy_data_task: grade=%d seoul=%d coverage=%.1f%%",
        result["grade_upserted"],
        result["seoul_api_upserted"],
        stats.get("with_energy", 0) / max(stats.get("total_buildings", 1), 1) * 100,
    )
    return {**result, "coverage_stats": stats}
