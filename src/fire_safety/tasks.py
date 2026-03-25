"""
화재 안전 Celery 태스크 (Phase F1).

- build_adjacency_district: 구별 인접 건물 그래프 계산
- build_all_adjacency: 서울 전체 인접 그래프 빌드
- compute_fire_clusters: ST_ClusterDBSCAN 고위험 클러스터 계산
"""
import logging
from typing import Optional

from src.shared.celery_app import celery
from src.shared.database import get_db

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
    from sqlalchemy import text
    db = next(get_db())
    try:
        logger.info("Adjacency build start: sgg=%s dist=%.0fm", sgg_code, dist_m)

        sql = text("""
            INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m)
            SELECT
                a.pnu  AS source_pnu,
                b.pnu  AS target_pnu,
                ST_Distance(
                    a.geom::geography,
                    b.geom::geography
                )::REAL AS distance_m
            FROM buildings_enriched a
            JOIN buildings_enriched b
              ON a.pnu < b.pnu
             AND ST_DWithin(a.geom::geography, b.geom::geography, :dist_m)
            WHERE a.pnu LIKE :prefix
              AND b.pnu LIKE :prefix
              AND a.geom IS NOT NULL
              AND b.geom IS NOT NULL
            ON CONFLICT (source_pnu, target_pnu) DO UPDATE
              SET distance_m = EXCLUDED.distance_m
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
    from sqlalchemy import text
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
