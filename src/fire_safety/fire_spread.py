"""
Phase F2 — 화재 확산 BFS 시뮬레이션 엔진

SQL-backed BFS: building_adjacency의 spread_weight + 바람 방향 보정으로 단계별 확산.
networkx 전체 그래프 로드 없이 온디맨드 DB 쿼리 방식 — 메모리 효율 우선.

알고리즘:
  1. frontier(현재 발화 건물 집합) → DB에서 인접 엣지 조회
  2. 각 후보 건물: spread_weight × wind_factor >= SPREAD_THRESHOLD 이면 확산
  3. wind_factor: 바람 방향과 확산 방향 일치도 (cos 유사도 기반)
  4. 결과: {step, time_min, new_pnus, cumulative_count}[] 타임라인
"""
import math
import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# ── 시뮬레이션 상수 ──────────────────────────────────────────────────────────

# 이 확률 이상이면 인접 건물로 확산 (0.0 ~ 1.5+ 범위)
SPREAD_THRESHOLD = 0.25

# 각 BFS step = 몇 분 (초기 성장기 기준 5분/step)
TIME_PER_STEP_MIN = 5

# 단일 시나리오 상한 — 이 이상은 중단 (메모리 보호)
MAX_BUILDINGS = 5_000

# 기본 최대 BFS 단계
DEFAULT_MAX_STEPS = 30

# ── SQL ──────────────────────────────────────────────────────────────────────

# 무풍(wind_speed==0) 전용: centroid join 없이 spread_weight만 조회 — 빠름
_STEP_SQL_NOWIND = text("""
    SELECT source_pnu, target_pnu, spread_weight
    FROM building_adjacency
    WHERE source_pnu = ANY(:frontier)
""")

# 바람 있을 때: centroid 배치 조회로 방위각 계산
# LATERAL + LIMIT 1: building_centroids 중복 PNU 대응 (ADR-004 패턴)
_STEP_SQL_WIND = text("""
    SELECT
        a.source_pnu,
        a.target_pnu,
        a.spread_weight,
        ST_X(cs.centroid) AS src_lng,
        ST_Y(cs.centroid) AS src_lat,
        ST_X(ct.centroid) AS tgt_lng,
        ST_Y(ct.centroid) AS tgt_lat
    FROM building_adjacency a
    JOIN LATERAL (
        SELECT centroid FROM building_centroids WHERE pnu = a.source_pnu LIMIT 1
    ) cs ON true
    JOIN LATERAL (
        SELECT centroid FROM building_centroids WHERE pnu = a.target_pnu LIMIT 1
    ) ct ON true
    WHERE a.source_pnu = ANY(:frontier)
""")

# 통계: DISTINCT ON으로 중복 PNU 제거 후 집계 (buildings_enriched는 PNU 중복 가능)
_STATS_SQL = text("""
    SELECT
        COALESCE(SUM(b.building_area), 0)  AS total_area_m2,
        COUNT(*) FILTER (WHERE r.risk_grade = 'HIGH')   AS high_risk_count,
        COUNT(*) FILTER (WHERE r.risk_grade = 'MEDIUM') AS medium_risk_count,
        COUNT(*) FILTER (WHERE b.usage_type LIKE '%주택%'
                             OR b.usage_type LIKE '%아파트%'
                             OR b.usage_type LIKE '%다세대%'
                             OR b.usage_type LIKE '%단독%') AS residential_count,
        COUNT(*) FILTER (WHERE b.usage_type LIKE '%상업%'
                             OR b.usage_type LIKE '%업무%'
                             OR b.usage_type LIKE '%판매%'
                             OR b.usage_type LIKE '%근린%') AS commercial_count
    FROM (
        SELECT DISTINCT ON (pnu) pnu, building_area, usage_type
        FROM buildings_enriched
        WHERE pnu = ANY(:pnus)
        ORDER BY pnu
    ) b
    LEFT JOIN (
        SELECT DISTINCT ON (pnu) pnu, risk_grade
        FROM building_fire_risk
        ORDER BY pnu
    ) r ON r.pnu = b.pnu
""")


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _bearing(src_lng: float, src_lat: float, tgt_lng: float, tgt_lat: float) -> float:
    """두 점 사이 방위각 (0=North, 시계방향, degrees)."""
    lat1 = math.radians(src_lat)
    lat2 = math.radians(tgt_lat)
    dlon = math.radians(tgt_lng - src_lng)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _wind_factor(bearing: float, wind_from_deg: float, wind_speed_ms: float) -> float:
    """
    바람이 화재 확산에 미치는 가중치.

    Args:
        bearing:        source → target 방위각 (degrees)
        wind_from_deg:  바람이 불어오는 방향 (기상학 표기, 0=North)
        wind_speed_ms:  바람 속도 (m/s)

    반환 범위:
        wind_speed=0 → 1.0 (무풍)
        wind_speed=10, 정렬 → 1.5 (50% 강화)
        wind_speed=10, 역방향 → 0.5 (50% 약화)
        최솟값 0.0 (역풍 강풍에서 확산 완전 차단 가능)
    """
    wind_to_deg = (wind_from_deg + 180.0) % 360
    diff = abs(bearing - wind_to_deg) % 360
    if diff > 180:
        diff = 360 - diff
    cos_sim = math.cos(math.radians(diff))  # -1.0 ~ +1.0
    return max(0.0, 1.0 + (wind_speed_ms / 20.0) * cos_sim)


# ── 공개 API ─────────────────────────────────────────────────────────────────

def run_bfs(
    conn: Connection,
    origin_pnu: str,
    wind_direction: float = 0.0,
    wind_speed: float = 0.0,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict:
    """
    BFS 화재 확산 시뮬레이션 실행.

    Args:
        conn:           SQLAlchemy Connection (autocommit 불필요)
        origin_pnu:     발화 건물 PNU
        wind_direction: 바람이 불어오는 방향 (0=North, 90=East, degrees)
        wind_speed:     바람 속도 (m/s). 0이면 무풍.
        max_steps:      최대 BFS 단계 수

    Returns:
        {
            "origin_pnu":       str,
            "wind_direction":   float,
            "wind_speed":       float,
            "spread_timeline":  [{"step", "time_min", "new_pnus", "cumulative_count"}, ...],
            "affected_pnus":    [str, ...],
            "stats":            {"total_buildings", "total_area_m2", ...},
        }
    """
    visited: set[str] = {origin_pnu}
    frontier: list[str] = [origin_pnu]
    halted_due_to_limit = False

    timeline: list[dict] = [
        {
            "step": 0,
            "time_min": 0,
            "new_pnus": [origin_pnu],
            "cumulative_count": 1,
        }
    ]

    logger.info(
        "BFS start: origin=%s wind=%.0f°@%.1fm/s max_steps=%d",
        origin_pnu, wind_direction, wind_speed, max_steps,
    )

    use_wind = wind_speed > 0.0
    step_sql = _STEP_SQL_WIND if use_wind else _STEP_SQL_NOWIND

    for step in range(1, max_steps + 1):
        if not frontier:
            break
        if len(visited) >= MAX_BUILDINGS:
            logger.warning("BFS reached MAX_BUILDINGS=%d — halting", MAX_BUILDINGS)
            halted_due_to_limit = True
            break

        rows = conn.execute(step_sql, {"frontier": frontier}).fetchall()

        # PNU별 최대 확산 확률 집계 (여러 frontier 건물에서 동시 도달 가능)
        candidate_prob: dict[str, float] = {}
        for row in rows:
            tgt = row.target_pnu
            if tgt in visited:
                continue
            base_weight = float(row.spread_weight)
            if base_weight <= 0.0:
                continue  # zero-weight 엣지: 확산 불가 — 계산 스킵
            if use_wind and row.src_lng is not None and row.tgt_lng is not None:
                b = _bearing(
                    float(row.src_lng), float(row.src_lat),
                    float(row.tgt_lng), float(row.tgt_lat),
                )
                wf = _wind_factor(b, wind_direction, wind_speed)
                prob = base_weight * wf
            else:
                prob = base_weight

            if prob > candidate_prob.get(tgt, -1.0):
                candidate_prob[tgt] = prob

        new_frontier = [
            tgt for tgt, prob in candidate_prob.items()
            if prob >= SPREAD_THRESHOLD
        ]

        if not new_frontier:
            logger.info("BFS halted at step %d (no spread)", step)
            break

        for pnu in new_frontier:
            visited.add(pnu)

        timeline.append({
            "step": step,
            "time_min": step * TIME_PER_STEP_MIN,
            "new_pnus": new_frontier,
            "cumulative_count": len(visited),
        })
        frontier = new_frontier

        logger.debug("step=%d new=%d cumulative=%d", step, len(new_frontier), len(visited))

    affected = list(visited)

    # 통계 집계
    stats_row = conn.execute(_STATS_SQL, {"pnus": affected}).fetchone()

    time_to_100: int | None = None
    for entry in timeline:
        if entry["cumulative_count"] >= 100:
            time_to_100 = entry["time_min"]
            break

    stats = {
        "total_buildings":            len(affected),
        "total_area_m2":              float(stats_row.total_area_m2) if stats_row else 0.0,
        "high_risk_count":            int(stats_row.high_risk_count) if stats_row else 0,
        "medium_risk_count":          int(stats_row.medium_risk_count) if stats_row else 0,
        "residential_count":          int(stats_row.residential_count) if stats_row else 0,
        "commercial_count":           int(stats_row.commercial_count) if stats_row else 0,
        "max_step_reached":           len(timeline) - 1,
        "simulation_duration_min":    (len(timeline) - 1) * TIME_PER_STEP_MIN,
        "time_to_100_buildings_min":  time_to_100,
        "halted_due_to_limit":        halted_due_to_limit,
    }

    logger.info(
        "BFS done: total=%d steps=%d area=%.0fm²",
        len(affected), len(timeline) - 1, stats["total_area_m2"],
    )

    return {
        "origin_pnu":      origin_pnu,
        "wind_direction":  wind_direction,
        "wind_speed":      wind_speed,
        "spread_timeline": timeline,
        "affected_pnus":   affected,
        "stats":           stats,
    }
