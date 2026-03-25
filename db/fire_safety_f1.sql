-- Phase F1: 밀집도·클러스터·소방서 테이블
-- 실행: docker compose exec db psql -U postgres -d buildings -f /tmp/fire_safety_f1.sql

-- 1. 인접 건물 그래프 (화재 확산 사전계산 + 밀집도 계산 기반)
CREATE TABLE IF NOT EXISTS building_adjacency (
    source_pnu  VARCHAR(25) NOT NULL,
    target_pnu  VARCHAR(25) NOT NULL,
    distance_m  REAL        NOT NULL,
    PRIMARY KEY (source_pnu, target_pnu)
);
CREATE INDEX IF NOT EXISTS idx_adj_source ON building_adjacency(source_pnu);
CREATE INDEX IF NOT EXISTS idx_adj_target ON building_adjacency(target_pnu);

-- 2. 소방서 / 119안전센터 위치
CREATE TABLE IF NOT EXISTS fire_stations (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    station_type    VARCHAR(20)  NOT NULL DEFAULT 'fire_station',
    district        VARCHAR(50),
    address         TEXT,
    geom            GEOMETRY(Point, 4326),
    ladder_height   REAL,
    vehicle_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fire_stations_geom ON fire_stations USING GIST(geom);

-- 3. 고위험 클러스터 (ST_ClusterDBSCAN 결과)
CREATE TABLE IF NOT EXISTS fire_risk_clusters (
    cluster_id      INTEGER,
    risk_level      VARCHAR(10) NOT NULL,
    building_count  INTEGER     NOT NULL,
    avg_risk_score  REAL,
    geom            GEOMETRY(Geometry, 4326),
    computed_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_clusters_geom ON fire_risk_clusters USING GIST(geom);

-- 4. 화재 시나리오 결과 (F2 준비)
CREATE TABLE IF NOT EXISTS fire_scenario_results (
    scenario_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_pnu      VARCHAR(25),
    wind_direction  REAL,
    wind_speed      REAL,
    affected_pnus   TEXT[],
    spread_timeline JSONB,
    stats           JSONB,
    computed_at     TIMESTAMP DEFAULT NOW()
);

-- 소방서 시드 데이터 (서울 25개 자치구 본서)
TRUNCATE fire_stations;
INSERT INTO fire_stations (name, station_type, district, geom) VALUES
  ('종로소방서',   'fire_station', '종로구', ST_SetSRID(ST_MakePoint(126.9784, 37.5760), 4326)),
  ('중부소방서',   'fire_station', '중구',   ST_SetSRID(ST_MakePoint(126.9979, 37.5636), 4326)),
  ('용산소방서',   'fire_station', '용산구', ST_SetSRID(ST_MakePoint(126.9882, 37.5326), 4326)),
  ('성동소방서',   'fire_station', '성동구', ST_SetSRID(ST_MakePoint(127.0422, 37.5637), 4326)),
  ('광진소방서',   'fire_station', '광진구', ST_SetSRID(ST_MakePoint(127.0898, 37.5540), 4326)),
  ('동대문소방서', 'fire_station', '동대문구',ST_SetSRID(ST_MakePoint(127.0400, 37.5741), 4326)),
  ('중랑소방서',   'fire_station', '중랑구', ST_SetSRID(ST_MakePoint(127.0923, 37.6052), 4326)),
  ('성북소방서',   'fire_station', '성북구', ST_SetSRID(ST_MakePoint(127.0167, 37.6052), 4326)),
  ('강북소방서',   'fire_station', '강북구', ST_SetSRID(ST_MakePoint(127.0281, 37.6390), 4326)),
  ('도봉소방서',   'fire_station', '도봉구', ST_SetSRID(ST_MakePoint(127.0474, 37.6693), 4326)),
  ('노원소방서',   'fire_station', '노원구', ST_SetSRID(ST_MakePoint(127.0771, 37.6548), 4326)),
  ('은평소방서',   'fire_station', '은평구', ST_SetSRID(ST_MakePoint(126.9334, 37.6217), 4326)),
  ('서대문소방서', 'fire_station', '서대문구',ST_SetSRID(ST_MakePoint(126.9360, 37.5787), 4326)),
  ('마포소방서',   'fire_station', '마포구', ST_SetSRID(ST_MakePoint(126.9087, 37.5574), 4326)),
  ('양천소방서',   'fire_station', '양천구', ST_SetSRID(ST_MakePoint(126.8686, 37.5145), 4326)),
  ('강서소방서',   'fire_station', '강서구', ST_SetSRID(ST_MakePoint(126.8341, 37.5554), 4326)),
  ('구로소방서',   'fire_station', '구로구', ST_SetSRID(ST_MakePoint(126.8875, 37.4948), 4326)),
  ('금천소방서',   'fire_station', '금천구', ST_SetSRID(ST_MakePoint(126.8942, 37.4568), 4326)),
  ('영등포소방서', 'fire_station', '영등포구',ST_SetSRID(ST_MakePoint(126.9082, 37.5264), 4326)),
  ('동작소방서',   'fire_station', '동작구', ST_SetSRID(ST_MakePoint(126.9440, 37.4987), 4326)),
  ('관악소방서',   'fire_station', '관악구', ST_SetSRID(ST_MakePoint(126.9531, 37.4745), 4326)),
  ('서초소방서',   'fire_station', '서초구', ST_SetSRID(ST_MakePoint(127.0000, 37.4867), 4326)),
  ('강남소방서',   'fire_station', '강남구', ST_SetSRID(ST_MakePoint(127.0551, 37.5104), 4326)),
  ('송파소방서',   'fire_station', '송파구', ST_SetSRID(ST_MakePoint(127.1070, 37.5051), 4326)),
  ('강동소방서',   'fire_station', '강동구', ST_SetSRID(ST_MakePoint(127.1477, 37.5547), 4326));

SELECT 'fire_safety_f1 tables created, ' || COUNT(*) || ' stations seeded' AS result
FROM fire_stations;
