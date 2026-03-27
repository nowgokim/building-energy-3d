-- Phase F3: 대피 관련 테이블 + pgRouting 함수
-- 실행: docker compose exec db psql -U postgres -d buildings -f /app/db/f3_evacuation.sql

-- ── 1. pgRouting 도로 네트워크 테이블 ─────────────────────────────────────────
-- osm2pgrouting이 자동 생성하는 테이블 (ways, ways_vertices_pgr)
-- 직접 CREATE 불필요; 여기서는 후처리 인덱스만 추가

-- ── 2. 피난 집결지 테이블 ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evacuation_points (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT,            -- '지정집결지' | '공원' | '학교' | '체육관'
    capacity    INT,             -- 수용 인원 (명)
    address     TEXT,
    geom        GEOMETRY(Point, 4326) NOT NULL,
    source      TEXT             -- 데이터 출처
);

CREATE INDEX IF NOT EXISTS idx_evac_geom ON evacuation_points USING gist(geom);

-- ── 3. 대피 경로 계산 함수 ─────────────────────────────────────────────────────
-- 인자: 발화 건물 PNU, 반환 경로 수 (기본 3개)
-- 반환: target_id, target_name, target_category, cost_m, geom (LineString)
CREATE OR REPLACE FUNCTION get_evacuation_routes(
    p_origin_pnu    TEXT,
    p_max_routes    INT DEFAULT 3
)
RETURNS TABLE (
    target_id       INT,
    target_name     TEXT,
    target_category TEXT,
    distance_m      FLOAT,
    route_geom      GEOMETRY(LineString, 4326)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_origin_geom   GEOMETRY;
    v_start_vertex  BIGINT;
BEGIN
    -- 발화 건물 centroid 조회
    SELECT centroid INTO v_origin_geom
    FROM building_centroids
    WHERE pnu = p_origin_pnu
    LIMIT 1;

    IF v_origin_geom IS NULL THEN
        RETURN;
    END IF;

    -- 가장 가까운 도로 노드 탐색
    SELECT id INTO v_start_vertex
    FROM osm_ways_vertices_pgr
    ORDER BY the_geom <-> v_origin_geom
    LIMIT 1;

    IF v_start_vertex IS NULL THEN
        RETURN;
    END IF;

    -- 가장 가까운 피난 집결지 p_max_routes개 → Dijkstra 최단 경로
    RETURN QUERY
    WITH nearest_targets AS (
        SELECT ep.id, ep.name, ep.category, ep.geom
        FROM evacuation_points ep
        ORDER BY ep.geom <-> v_origin_geom
        LIMIT p_max_routes
    ),
    target_vertices AS (
        SELECT nt.id AS ep_id, nt.name, nt.category, nt.geom AS ep_geom,
               (SELECT wv.id FROM osm_ways_vertices_pgr wv
                ORDER BY wv.the_geom <-> nt.geom LIMIT 1) AS vertex_id
        FROM nearest_targets nt
    ),
    routes AS (
        SELECT
            tv.ep_id,
            tv.name,
            tv.category,
            pgr.cost,
            pgr.path_seq,
            pgr.edge
        FROM target_vertices tv
        CROSS JOIN LATERAL (
            SELECT * FROM pgr_dijkstra(
                'SELECT gid AS id, source, target, length_m AS cost FROM osm_ways',
                v_start_vertex,
                tv.vertex_id,
                directed := false
            )
        ) pgr
        WHERE pgr.edge <> -1
    )
    SELECT
        r.ep_id                                         AS target_id,
        r.name                                          AS target_name,
        r.category                                      AS target_category,
        SUM(r.cost)                                     AS distance_m,
        ST_LineMerge(ST_Collect(w.the_geom))            AS route_geom
    FROM routes r
    JOIN osm_ways w ON w.gid = r.edge
    GROUP BY r.ep_id, r.name, r.category
    ORDER BY distance_m;
END;
$$;
