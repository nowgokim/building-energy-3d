-- Phase F4: 기상 스냅샷 테이블
-- 기상청 동네예보 격자 기반 서울 바람 데이터
-- 실행: docker compose exec -T db psql -U postgres -d buildings < db/f4_weather.sql

CREATE TABLE IF NOT EXISTS weather_snapshots (
    id              SERIAL PRIMARY KEY,
    grid_x          SMALLINT NOT NULL,      -- 기상청 격자 X
    grid_y          SMALLINT NOT NULL,      -- 기상청 격자 Y
    lng             REAL NOT NULL,
    lat             REAL NOT NULL,
    wind_direction  REAL NOT NULL,          -- 바람이 불어오는 방향 (도, 0=North)
    wind_speed      REAL NOT NULL,          -- 풍속 (m/s)
    measured_at     TIMESTAMPTZ NOT NULL,   -- 예보 기준 시각
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weather_measured_at ON weather_snapshots(measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_grid ON weather_snapshots(grid_x, grid_y, measured_at DESC);

-- 최신 서울 평균 바람 뷰 (현재 시간 기준 가장 최근 스냅샷)
CREATE OR REPLACE VIEW current_seoul_wind AS
SELECT
    AVG(wind_direction) AS wind_direction,
    AVG(wind_speed)     AS wind_speed,
    MAX(measured_at)    AS measured_at,
    COUNT(*)            AS grid_count
FROM weather_snapshots
WHERE measured_at = (SELECT MAX(measured_at) FROM weather_snapshots);
