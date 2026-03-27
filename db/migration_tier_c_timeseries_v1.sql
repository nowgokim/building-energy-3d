-- =============================================================================
-- ⚠️  DEPRECATED — 이 파일은 monitor_timeseries.sql 로 통합되었습니다.
--
-- 충돌 경위:
--   이 파일(migration_tier_c_timeseries_v1.sql)과 monitor_timeseries.sql 이
--   동일한 목적(Tier C 시계열 스키마)에 서로 다른 테이블명을 사용해 충돌 발생.
--     - 이 파일: time_series_buildings, building_meter_readings, building_id
--     - monitor.py가 참조하는 실제 스키마: monitored_buildings, metered_readings, ts_id
--
-- 해결책 (2026-03-27):
--   monitor_timeseries.sql 이 canonical SSOT로 확정되었습니다.
--   이 파일의 유용한 기능(metered_readings_daily, fn_sync_tier_c_to_energy_results)은
--   monitor_timeseries.sql 로 이전·통합(테이블명 통일)되었습니다.
--
-- ⚠️  이 파일을 DB에 직접 실행하지 마십시오. monitor_timeseries.sql 을 사용하세요.
--
-- 참조 파일: db/monitor_timeseries.sql (canonical)
-- =============================================================================

-- =============================================================================
-- migration_tier_c_timeseries_v1.sql
-- Tier C 건물 실계량기 시계열 데이터 스키마
--
-- Tier C = 실제 계량기(전력/가스/지역난방) 시계열 데이터 보유 건물
-- 규모: 수십~수백 건 (서울시 공공건물, 대형 업무용건물, 연구 목적 모니터링)
--
-- 실행 순서:
--   1. ENUM 타입 추가
--   2. time_series_buildings — Tier C 건물 레지스트리
--   3. building_meter_readings — 계량값 원본 (파티션 테이블)
--   4. building_ts_daily — 일 집계 (파티션 테이블, 쿼리 최적화 계층)
--   5. building_ts_summary — 대시보드용 MV
--   6. energy_results upsert 함수 (fn_sync_tier_c_to_energy_results)
--   7. 트리거 (선택적)
--   8. 인덱스
--   9. IMPACT 등록 안내 주석
--
-- 적용:
--   docker compose exec db psql -U postgres -d buildings \
--     -f /docker-entrypoint-initdb.d/migration_tier_c_timeseries_v1.sql
--
-- 선행 조건:
--   - migration_energy_unique.sql (energy_results.pnu UNIQUE)
--   - migration_provenance_v1.sql (data_sources, pipeline_runs, data_tier)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. 선행 조건 검증
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    -- energy_results.pnu UNIQUE constraint 확인
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'energy_results'
          AND constraint_type = 'UNIQUE'
          AND constraint_name = 'uq_energy_results_pnu'
    ) THEN
        RAISE EXCEPTION
            'energy_results.pnu UNIQUE constraint 없음. '
            'migration_energy_unique.sql 을 먼저 실행하라.';
    END IF;

    -- data_tier 컬럼 확인
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'energy_results'
          AND column_name = 'data_tier'
    ) THEN
        RAISE EXCEPTION
            'energy_results.data_tier 컬럼 없음. '
            'migration_provenance_v1.sql 을 먼저 실행하라.';
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 1. ENUM 타입
-- ---------------------------------------------------------------------------

-- 계량기 에너지 종류
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meter_energy_type') THEN
        CREATE TYPE meter_energy_type AS ENUM (
            'electricity',       -- 전력 (kWh)
            'gas',               -- 도시가스 (kWh 또는 MJ → 저장 단위는 kWh 통일)
            'district_heating',  -- 지역난방 (kWh)
            'district_cooling',  -- 지역냉방 (kWh)
            'solar_gen',         -- 태양광 발전 (kWh, 생산량)
            'water',             -- 수도 (m³ — 향후 확장용)
            'other'              -- 기타
        );
    END IF;
END$$;

-- 계량기 원시 데이터 시간 해상도
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meter_resolution') THEN
        CREATE TYPE meter_resolution AS ENUM (
            'min15',   -- 15분 (AMI 스마트미터, 전력 수요관리)
            'hourly',  -- 1시간 (건물 BMS, EMS)
            'daily',   -- 1일 (수기 검침, 일부 가스미터)
            'monthly'  -- 월별 (건축HUB 원천과 동일 해상도 예외 허용)
        );
    END IF;
END$$;

-- 계량기 데이터 품질 플래그
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meter_quality_flag') THEN
        CREATE TYPE meter_quality_flag AS ENUM (
            'ok',           -- 정상
            'estimated',    -- 추정값 (검침 불가 구간 보간)
            'suspect',      -- 의심값 (이상치 탐지 결과 플래그)
            'invalid',      -- 무효 (음수, 리셋 후 과도값 등)
            'missing'       -- 결측 (NULL 아닌 명시적 결측 마킹)
        );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 2. time_series_buildings — Tier C 건물 레지스트리
--
-- 설계 결정:
--   - PK를 building_id SERIAL로 분리하고 pnu를 FK로 걸었다.
--     meter_readings에서 (building_id, ts) 복합 인덱스를 B-tree로
--     유지할 때, pnu VARCHAR(19)보다 INTEGER 4바이트가 인덱스 크기와
--     JOIN 속도 모두 유리하다.
--   - pnu UNIQUE: 1건물 1레지스트리 원칙 (동일 건물 중복 등록 방지)
--   - meter_types TEXT[]: 실제 계량기 종류를 배열로 저장.
--     정규화(junction table)보다 단순성 우선.
--     조회 패턴이 "이 건물의 계량기 종류가 무엇인가?" 이지
--     "electricity를 보유한 건물 목록"이 아니기 때문.
--     후자가 필요해지면 GIN 인덱스로 대응 가능.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS time_series_buildings (
    building_id         SERIAL          PRIMARY KEY,
    pnu                 VARCHAR(19)     NOT NULL UNIQUE
                            REFERENCES building_footprints(pnu)
                            ON DELETE RESTRICT,
    -- 건물 식별자: 공공기관명, 연구 코드명 등
    alias               VARCHAR(200)    NOT NULL,
    -- 데이터 출처 설명 (자유 텍스트)
    data_provider       VARCHAR(200),   -- 예: '서울시 공공건물 에너지정보시스템'
    -- data_sources FK (수집 채널)
    source_id           INTEGER
                            REFERENCES data_sources(id)
                            ON DELETE SET NULL,
    -- 보유 계량기 종류 배열
    -- 예: ARRAY['electricity','gas']::meter_energy_type[]
    meter_types         meter_energy_type[]     NOT NULL DEFAULT '{}',
    -- 수집 기간
    collection_start    DATE,
    collection_end      DATE,           -- NULL = 현재 진행 중
    -- 대표 좌표 (지도 jump용, 뷰어에서 직접 사용)
    -- building_footprints.geom의 centroid이거나 수동 보정값
    lon                 DOUBLE PRECISION,
    lat                 DOUBLE PRECISION,
    -- 건물 메타 (빠른 조회용 비정규화, SSOT는 building_ledger)
    -- 동기화는 수집 파이프라인에서 담당 (trigger 없음, 의도적)
    building_name_cache VARCHAR(200),
    usage_type_cache    VARCHAR(100),
    total_area_cache    REAL,
    -- 비고 및 부가 정보 (JSONB — 추후 확장 유연성)
    -- 예: {"bms_type": "Siemens DESIGO", "contract_demand_kw": 500}
    extra_metadata      JSONB,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    registered_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  time_series_buildings IS
    'Tier C 건물 레지스트리. 실계량기 시계열 데이터 보유 건물 목록. '
    '수십~수백 건 규모. building_id INTEGER가 meter_readings 파티션 테이블의 FK로 사용된다.';
COMMENT ON COLUMN time_series_buildings.meter_types IS
    '보유 계량기 종류 배열. GIN 인덱스(idx_tsb_meter_types)로 조회 지원.';
COMMENT ON COLUMN time_series_buildings.collection_end IS
    'NULL = 현재 수집 진행 중. 모니터링 종료 시 날짜 기입.';
COMMENT ON COLUMN time_series_buildings.building_name_cache IS
    '빠른 조회를 위한 building_ledger 비정규화. '
    'SSOT는 building_ledger. 수집 파이프라인에서 동기화.';
COMMENT ON COLUMN time_series_buildings.extra_metadata IS
    'BMS 종류, 계약 전력, 모니터링 목적 등 구조화되지 않은 부가 정보.';

-- updated_at 자동 갱신 (fn_update_updated_at는 provenance v1에서 이미 생성됨)
DROP TRIGGER IF EXISTS trg_tsbuildings_updated_at ON time_series_buildings;
CREATE TRIGGER trg_tsbuildings_updated_at
    BEFORE UPDATE ON time_series_buildings
    FOR EACH ROW EXECUTE FUNCTION fn_update_updated_at();

-- ---------------------------------------------------------------------------
-- 3. building_meter_readings — 계량값 원본 (파티션 테이블)
--
-- 파티셔닝 전략 결정:
--   PARTITION BY RANGE (ts) — 연도별 파티션
--
--   선택 근거:
--   (A) 건물별 파티션: 건물 수가 수십~수백으로 적어 파티션 pruning 효과
--       미미하고, 신규 건물 등록 시마다 DDL이 필요하다. 부적합.
--   (B) 시간 범위 파티션 (채택): 가장 자주 발생하는 쿼리 패턴이
--       "최근 N일 데이터 조회"이므로 ts 기준 pruning이 효과적이다.
--       오래된 파티션은 DETACH → 별도 아카이브 테이블로 이동 가능.
--   (C) TimescaleDB hypertable: 현재 PostGIS + pgrouting만 설치된 상태.
--       TimescaleDB 설치 시 Docker 이미지 교체 및 재초기화가 필요하다.
--       현 규모(수백 건 × 최대 15분 × 수년)에서는 일반 PostgreSQL
--       파티션으로 충분히 대응 가능하므로 채택하지 않는다.
--       규모가 수천 건 × 1분 해상도로 확장될 경우 재검토한다.
--
--   저장 단위 통일 원칙:
--   - 모든 에너지 값은 kWh로 저장
--   - 가스: Nm³ → kWh 변환 (저위발열량 기준: 10.55 kWh/Nm³)
--   - 지역난방: Gcal → kWh 변환 (1 Gcal = 1163 kWh)
--   - 변환 계수는 ingest 파이프라인 책임. DB는 kWh만 저장.
--
--   값 의미:
--   - value: 해당 기간 소비량(구간값, delta). 누적값(pulse)이 아님.
--     예: 15분 간격 → 해당 15분 동안 소비한 kWh
--   - ts: 구간 시작 시각 (interval의 left endpoint, closed)
--
--   PRIMARY KEY 구조:
--   파티션 테이블은 파티션 키(ts)가 PK에 포함되어야 한다.
--   (building_id, energy_type, ts) 복합 PK로 자연키 설계.
--   BIGSERIAL surrogate key는 파티션 테이블에서 UNIQUE 보장이
--   복잡해지므로 사용하지 않는다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS building_meter_readings (
    building_id     INTEGER         NOT NULL
                        REFERENCES time_series_buildings(building_id)
                        ON DELETE CASCADE,
    energy_type     meter_energy_type   NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    -- 구간 시작 시각 (UTC 저장, 한국시간=UTC+9 변환은 애플리케이션 책임)
    resolution      meter_resolution    NOT NULL,
    -- 원본 데이터 해상도. 집계 쿼리에서 해상도 혼재 건 필터링에 사용.
    value_kwh       DOUBLE PRECISION,
    -- NULL = 결측. quality_flag='missing'과 병행 사용.
    -- DOUBLE PRECISION: REAL(4바이트)보다 정밀도 필요.
    -- 15분 분해능에서 0.001 kWh 오차가 연간 35 kWh 편차로 증폭되므로.
    quality_flag    meter_quality_flag  NOT NULL DEFAULT 'ok',
    -- 원천 원시값 (단위 변환 전). NULL이면 kWh가 원천 단위.
    raw_value       DOUBLE PRECISION,
    raw_unit        VARCHAR(20),    -- 예: 'Nm3', 'Gcal', 'MJ'
    -- 수집 파이프라인 추적
    pipeline_run_id UUID
                        REFERENCES pipeline_runs(id)
                        ON DELETE SET NULL,
    PRIMARY KEY (building_id, energy_type, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE  building_meter_readings IS
    'Tier C 건물 계량기 원본 시계열. ts 기준 연도별 파티션. '
    '15분/1시간/1일 해상도 혼재 가능. 에너지 값은 kWh 통일.';
COMMENT ON COLUMN building_meter_readings.ts IS
    '구간 시작 시각 (UTC). 파티션 키. '
    '한국시간 조회 시: ts AT TIME ZONE ''Asia/Seoul''';
COMMENT ON COLUMN building_meter_readings.value_kwh IS
    '해당 resolution 구간의 에너지 소비량(구간값, delta). kWh 단위. '
    'NULL = 결측 (quality_flag=missing과 병행).';
COMMENT ON COLUMN building_meter_readings.resolution IS
    '원본 해상도. min15/hourly/daily/monthly 혼재 가능. '
    'building_ts_daily 집계 시 중복 계산 방지를 위해 필터로 사용.';
COMMENT ON COLUMN building_meter_readings.raw_value IS
    '단위 변환 전 원천값. 가스(Nm³), 지역난방(Gcal) 등. '
    'NULL이면 kWh가 원천 단위이거나 변환 이전 값 미보존.';

-- 연도별 파티션 (현재 2026-03-27 기준: 과거 2년 + 현재 + 미래 1년)
CREATE TABLE IF NOT EXISTS building_meter_readings_2023
    PARTITION OF building_meter_readings
    FOR VALUES FROM ('2023-01-01 00:00:00+00') TO ('2024-01-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS building_meter_readings_2024
    PARTITION OF building_meter_readings
    FOR VALUES FROM ('2024-01-01 00:00:00+00') TO ('2025-01-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS building_meter_readings_2025
    PARTITION OF building_meter_readings
    FOR VALUES FROM ('2025-01-01 00:00:00+00') TO ('2026-01-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS building_meter_readings_2026
    PARTITION OF building_meter_readings
    FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');

CREATE TABLE IF NOT EXISTS building_meter_readings_2027
    PARTITION OF building_meter_readings
    FOR VALUES FROM ('2027-01-01 00:00:00+00') TO ('2028-01-01 00:00:00+00');

-- 범위 외 행 보호
CREATE TABLE IF NOT EXISTS building_meter_readings_default
    PARTITION OF building_meter_readings
    DEFAULT;

COMMENT ON TABLE building_meter_readings_2023 IS '2023년 계량값 파티션';
COMMENT ON TABLE building_meter_readings_2024 IS '2024년 계량값 파티션';
COMMENT ON TABLE building_meter_readings_2025 IS '2025년 계량값 파티션';
COMMENT ON TABLE building_meter_readings_2026 IS '2026년 계량값 파티션 (현재)';
COMMENT ON TABLE building_meter_readings_2027 IS '2027년 계량값 파티션 (사전 생성)';

-- ---------------------------------------------------------------------------
-- 4. building_ts_daily — 일 단위 집계 (파티션 테이블, 중간 집계 계층)
--
-- 설계 근거:
--   15분 원본 데이터에서 직접 월별/연별 쿼리를 실행하면
--   (365일 × 96개 = 35,040행 per building per year) × 수백 건
--   = 수천만 행 집계가 반복 발생한다.
--   일 집계 테이블을 중간 계층으로 두면:
--   - 대시보드 30일 조회: 30행 × 건물 수 (vs 2,880행)
--   - 연간 총량: 365행 SUM (vs 35,040행)
--   이 테이블은 building_meter_readings에서 파이프라인이 채운다.
--   트리거로 실시간 집계하지 않고 배치(Celery daily task)로 갱신한다.
--   이유: 15분 데이터 INSERT가 고빈도일 때 트리거 오버헤드가 크고,
--   대시보드는 D-1 데이터로도 충분하기 때문이다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS building_ts_daily (
    building_id     INTEGER         NOT NULL
                        REFERENCES time_series_buildings(building_id)
                        ON DELETE CASCADE,
    energy_type     meter_energy_type   NOT NULL,
    day             DATE            NOT NULL,
    -- 일별 소비량 집계
    total_kwh       DOUBLE PRECISION,           -- 일일 총 소비량
    -- 품질 지표 (원본 행 품질 기반)
    ok_count        INTEGER         NOT NULL DEFAULT 0,
    -- quality_flag='ok' 행 수
    missing_count   INTEGER         NOT NULL DEFAULT 0,
    -- quality_flag='missing' 또는 value_kwh IS NULL 행 수
    suspect_count   INTEGER         NOT NULL DEFAULT 0,
    -- quality_flag='suspect' 행 수
    coverage_pct    REAL,
    -- ok_count / 기대 행 수 × 100 (15분: 96, 1시간: 24)
    -- NULL = 해상도 혼재로 계산 불가
    -- 집계 기준 해상도 (가장 세밀한 해상도 기준)
    source_resolution meter_resolution,
    computed_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (building_id, energy_type, day)
) PARTITION BY RANGE (day);

COMMENT ON TABLE  building_ts_daily IS
    '일 단위 집계 중간 계층. building_meter_readings에서 Celery daily task가 채운다. '
    'building_ts_summary MV와 대시보드 API의 주요 소스.';
COMMENT ON COLUMN building_ts_daily.coverage_pct IS
    '데이터 완전성 (%). ok_count / expected_intervals × 100. '
    '70% 미만이면 이상치 탐지 및 보고 대상.';
COMMENT ON COLUMN building_ts_daily.computed_at IS
    '이 집계 행이 마지막으로 갱신된 시각. 재처리 추적용.';

-- 연도별 파티션
CREATE TABLE IF NOT EXISTS building_ts_daily_2023
    PARTITION OF building_ts_daily FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');

CREATE TABLE IF NOT EXISTS building_ts_daily_2024
    PARTITION OF building_ts_daily FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

CREATE TABLE IF NOT EXISTS building_ts_daily_2025
    PARTITION OF building_ts_daily FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

CREATE TABLE IF NOT EXISTS building_ts_daily_2026
    PARTITION OF building_ts_daily FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

CREATE TABLE IF NOT EXISTS building_ts_daily_2027
    PARTITION OF building_ts_daily FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');

CREATE TABLE IF NOT EXISTS building_ts_daily_default
    PARTITION OF building_ts_daily DEFAULT;

-- ---------------------------------------------------------------------------
-- 5. building_ts_summary — 대시보드용 Materialized View
--
-- 갱신 주기: 일 1회 Celery beat (building_ts_daily 배치 갱신 직후)
-- CONCURRENTLY 갱신을 위해 UNIQUE INDEX 필수.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS building_ts_summary;

CREATE MATERIALIZED VIEW building_ts_summary AS
WITH

-- 최근 30일 집계 (대시보드 핵심 지표)
recent_30 AS (
    SELECT
        d.building_id,
        d.energy_type,
        COUNT(d.day)                            AS days_with_data,
        SUM(d.total_kwh)                        AS total_kwh_30d,
        AVG(d.total_kwh)                        AS avg_daily_kwh_30d,
        MAX(d.total_kwh)                        AS peak_daily_kwh_30d,
        AVG(d.coverage_pct)                     AS avg_coverage_pct_30d,
        SUM(d.missing_count)                    AS total_missing_intervals_30d,
        SUM(d.suspect_count)                    AS total_suspect_intervals_30d
    FROM building_ts_daily d
    WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'
      AND d.total_kwh IS NOT NULL
    GROUP BY d.building_id, d.energy_type
),

-- 연간 집계 (현재 연도)
current_year AS (
    SELECT
        d.building_id,
        d.energy_type,
        DATE_TRUNC('year', d.day::TIMESTAMPTZ) AS year_start,
        SUM(d.total_kwh)                        AS total_kwh_ytd,
        COUNT(d.day)                            AS days_with_data_ytd
    FROM building_ts_daily d
    WHERE d.day >= DATE_TRUNC('year', CURRENT_DATE)
      AND d.total_kwh IS NOT NULL
    GROUP BY d.building_id, d.energy_type, DATE_TRUNC('year', d.day::TIMESTAMPTZ)
),

-- 이상치 비율 (최근 90일, suspect + invalid 비율)
anomaly_90 AS (
    SELECT
        d.building_id,
        d.energy_type,
        ROUND(
            100.0 * SUM(d.suspect_count + d.missing_count)
            / NULLIF(SUM(d.ok_count + d.suspect_count + d.missing_count), 0),
            2
        ) AS anomaly_pct_90d
    FROM building_ts_daily d
    WHERE d.day >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY d.building_id, d.energy_type
),

-- 최신 데이터 시각
latest_ts AS (
    SELECT
        building_id,
        energy_type,
        MAX(day) AS latest_day
    FROM building_ts_daily
    WHERE total_kwh IS NOT NULL
    GROUP BY building_id, energy_type
)

SELECT
    tsb.building_id,
    tsb.pnu,
    tsb.alias,
    tsb.building_name_cache,
    tsb.usage_type_cache,
    tsb.total_area_cache,
    tsb.lon,
    tsb.lat,
    tsb.meter_types,
    tsb.collection_start,
    tsb.collection_end,
    tsb.is_active,

    r.energy_type,

    -- 30일 지표
    r.days_with_data                            AS days_with_data_30d,
    ROUND(r.total_kwh_30d::NUMERIC, 2)          AS total_kwh_30d,
    ROUND(r.avg_daily_kwh_30d::NUMERIC, 2)      AS avg_daily_kwh_30d,
    ROUND(r.peak_daily_kwh_30d::NUMERIC, 2)     AS peak_daily_kwh_30d,
    ROUND(r.avg_coverage_pct_30d::NUMERIC, 1)   AS avg_coverage_pct_30d,
    r.total_missing_intervals_30d,
    r.total_suspect_intervals_30d,

    -- 연간 YTD 지표
    ROUND(cy.total_kwh_ytd::NUMERIC, 2)         AS total_kwh_ytd,
    cy.days_with_data_ytd,

    -- EUI (kWh/m²) — total_area_cache가 있고 > 0인 경우만
    ROUND(
        CASE
            WHEN tsb.total_area_cache > 0
            THEN r.total_kwh_30d / tsb.total_area_cache
            ELSE NULL
        END::NUMERIC, 3
    )                                           AS eui_kwh_m2_30d,

    -- 이상치 비율
    COALESCE(a.anomaly_pct_90d, 0)              AS anomaly_pct_90d,

    -- 최신 데이터 날짜
    lt.latest_day,

    -- 데이터 신선도: 마지막 데이터로부터 경과 일수
    (CURRENT_DATE - lt.latest_day)              AS days_since_latest,

    NOW()                                       AS refreshed_at

FROM time_series_buildings tsb
JOIN recent_30 r        ON r.building_id = tsb.building_id
LEFT JOIN current_year cy
    ON cy.building_id = tsb.building_id
    AND cy.energy_type = r.energy_type
LEFT JOIN anomaly_90 a
    ON a.building_id = tsb.building_id
    AND a.energy_type = r.energy_type
LEFT JOIN latest_ts lt
    ON lt.building_id = tsb.building_id
    AND lt.energy_type = r.energy_type
WHERE tsb.is_active = TRUE;

COMMENT ON MATERIALIZED VIEW building_ts_summary IS
    '대시보드용 Tier C 건물 시계열 요약. 건물×에너지종류별 1행. '
    'Celery daily task에서 building_ts_daily 갱신 직후 REFRESH CONCURRENTLY 실행.';

-- CONCURRENTLY 갱신을 위한 UNIQUE INDEX
CREATE UNIQUE INDEX IF NOT EXISTS idx_ts_summary_uniq
    ON building_ts_summary (building_id, energy_type);

-- ---------------------------------------------------------------------------
-- 6. fn_sync_tier_c_to_energy_results
--    Tier C 건물의 연간 집계를 energy_results에 upsert하는 함수
--
-- 호출 시점: Celery 연간 배치 (매년 1월 또는 수동)
--   celery task: tasks.sync_tier_c_annual(reference_year=2025)
--
-- 설계 결정: 트리거 vs 함수
--   트리거(INSERT/UPDATE on building_ts_daily → upsert energy_results)는
--   일별 집계 갱신마다 energy_results를 건드려 lock 경합을 유발한다.
--   연간 총량은 연 1회 업데이트로 충분하므로 명시적 함수 호출 채택.
--   upsert guard: WHERE data_tier >= EXCLUDED.data_tier 유지.
--   (기존 Tier 1 실측이 있으면 overwrite하지 않는다는 원칙과 일치)
--   단, Tier C 건물은 data_tier=1이므로 동일 tier끼리의 연도별 갱신이
--   발생한다. 이 경우 reference_year가 더 최신인 것을 우선한다.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_sync_tier_c_to_energy_results(
    p_reference_year INTEGER DEFAULT EXTRACT(YEAR FROM CURRENT_DATE)::INTEGER - 1
)
RETURNS TABLE (
    pnu             VARCHAR(19),
    action          TEXT,       -- 'inserted' | 'updated' | 'skipped'
    total_kwh       DOUBLE PRECISION
)
LANGUAGE plpgsql AS
$$
DECLARE
    v_source_id     INTEGER;
    v_run_id        UUID;
BEGIN
    -- data_sources에서 'tier_c_metered' source_id 조회
    -- (data_sources에 없으면 NULL, upsert는 진행)
    SELECT id INTO v_source_id
    FROM data_sources
    WHERE source_key = 'tier_c_metered'
    LIMIT 1;

    -- pipeline_runs 기록 (선택적 — 없으면 생략)
    IF v_source_id IS NOT NULL THEN
        INSERT INTO pipeline_runs (
            run_type, source_id, params, status
        ) VALUES (
            'sync_tier_c_annual',
            v_source_id,
            jsonb_build_object('reference_year', p_reference_year),
            'running'
        )
        RETURNING id INTO v_run_id;
    END IF;

    -- 연간 집계 → energy_results upsert
    RETURN QUERY
    WITH annual_agg AS (
        -- electricity + gas + district_heating 합산 → total_energy
        -- 각 에너지 종류별 합계도 개별 컬럼으로
        SELECT
            tsb.pnu,
            tsb.building_id,
            SUM(CASE WHEN d.energy_type = 'electricity'
                     THEN d.total_kwh ELSE 0 END)      AS elec_kwh,
            SUM(CASE WHEN d.energy_type = 'gas'
                     THEN d.total_kwh ELSE 0 END)      AS gas_kwh,
            SUM(CASE WHEN d.energy_type = 'district_heating'
                     THEN d.total_kwh ELSE 0 END)      AS dh_kwh,
            SUM(CASE WHEN d.energy_type NOT IN
                          ('solar_gen','water','other')
                     THEN d.total_kwh ELSE 0 END)      AS total_kwh,
            COUNT(DISTINCT d.energy_type)               AS meter_count,
            AVG(d.coverage_pct)                         AS avg_coverage
        FROM building_ts_daily d
        JOIN time_series_buildings tsb
            ON tsb.building_id = d.building_id
        WHERE EXTRACT(YEAR FROM d.day)::INTEGER = p_reference_year
          AND d.total_kwh IS NOT NULL
          -- building_ts_daily에는 quality_flag 컬럼이 없다.
          -- 품질 필터는 coverage_pct 기준으로만 적용한다.
          -- (원본 품질은 building_meter_readings.quality_flag에서 관리되며,
          --  일 집계 시 suspect_count/missing_count로 요약됨)
          -- coverage 50% 미만 일 집계는 연산에서 제외
          AND (d.coverage_pct IS NULL OR d.coverage_pct >= 50.0)
        GROUP BY tsb.pnu, tsb.building_id
        HAVING SUM(CASE WHEN d.energy_type NOT IN
                             ('solar_gen','water','other')
                        THEN d.total_kwh ELSE 0 END) > 0
    ),
    upserted AS (
        INSERT INTO energy_results (
            pnu,
            total_energy,
            heating,     -- 여기서는 district_heating + 일부 가스 추정 불가 → 가스 전체
            cooling,     -- 냉방 분리 불가 시 0 기록
            data_tier,
            source_id,
            pipeline_run_id,
            reference_year,
            is_current,
            simulation_type,
            source_metadata,
            simulated_at
        )
        SELECT
            aa.pnu,
            aa.total_kwh,
            aa.gas_kwh + aa.dh_kwh,  -- 난방 추정 (가스 + 지역난방)
            0,                        -- 냉방 분리 불가 (전력 안에 포함)
            1,                        -- data_tier = 1 (실측)
            v_source_id,
            v_run_id,
            p_reference_year,
            TRUE,
            'tier_c_metered',
            jsonb_build_object(
                'elec_kwh',       aa.elec_kwh,
                'gas_kwh',        aa.gas_kwh,
                'dh_kwh',         aa.dh_kwh,
                'meter_count',    aa.meter_count,
                'avg_coverage',   ROUND(aa.avg_coverage::NUMERIC, 1),
                'reference_year', p_reference_year
            ),
            NOW()
        FROM annual_agg aa
        ON CONFLICT (pnu) DO UPDATE
            SET total_energy    = EXCLUDED.total_energy,
                heating         = EXCLUDED.heating,
                cooling         = EXCLUDED.cooling,
                data_tier       = EXCLUDED.data_tier,
                source_id       = EXCLUDED.source_id,
                pipeline_run_id = EXCLUDED.pipeline_run_id,
                reference_year  = EXCLUDED.reference_year,
                is_current      = TRUE,
                simulation_type = EXCLUDED.simulation_type,
                source_metadata = EXCLUDED.source_metadata,
                simulated_at    = NOW()
            -- upsert guard:
            --   data_tier: 숫자가 클수록 낮은 품질 (1=실측, 2=인증, 3=추정, 4=아키타입).
            --
            --   [조건 A] energy_results.data_tier > EXCLUDED.data_tier
            --     기존 tier가 더 높은 숫자(낮은 품질)이면 → 고품질 신규 데이터로 교체.
            --     예: 기존=3(추정), 신규=1(실측) → 3 > 1 = TRUE → 갱신됨 (올바름)
            --     예: 기존=1(실측), 신규=3(추정) → 1 > 3 = FALSE → 다음 조건으로
            --
            --   [조건 B] 동일 tier에서 reference_year 비교
            --     기존과 신규 tier가 같고(모두 1=실측),
            --     기존 reference_year가 NULL이거나 신규보다 이전/동일 연도이면 갱신.
            --     즉, 동일 tier에서 더 최신 연도 실측 데이터만 갱신 허용.
            --
            --   [보호] 기존=1(실측), 신규=3(추정) → 조건A FALSE, 조건B FALSE → 갱신 안 됨.
            --   고품질 실측 데이터를 저품질 데이터로 overwrite하지 않는다는 원칙 준수.
            WHERE energy_results.data_tier > EXCLUDED.data_tier
               OR (
                    energy_results.data_tier = EXCLUDED.data_tier
                    AND (
                        energy_results.reference_year IS NULL
                        OR energy_results.reference_year <= EXCLUDED.reference_year
                    )
                  )
        RETURNING
            pnu,
            xmax = 0 AS is_insert,  -- xmax=0이면 INSERT, 아니면 UPDATE
            total_energy
    )
    SELECT
        u.pnu,
        CASE WHEN u.is_insert THEN 'inserted' ELSE 'updated' END AS action,
        u.total_energy
    FROM upserted u;

    -- pipeline_runs 완료 기록
    IF v_run_id IS NOT NULL THEN
        UPDATE pipeline_runs
        SET status    = 'success',
            ended_at  = NOW()
        WHERE id = v_run_id;
    END IF;

    EXCEPTION WHEN OTHERS THEN
        IF v_run_id IS NOT NULL THEN
            UPDATE pipeline_runs
            SET status        = 'failed',
                ended_at      = NOW(),
                error_message = SQLERRM
            WHERE id = v_run_id;
        END IF;
        RAISE;
END;
$$;

COMMENT ON FUNCTION fn_sync_tier_c_to_energy_results(INTEGER) IS
    'Tier C 건물의 연간 계량 집계를 energy_results에 upsert한다. '
    'p_reference_year: 집계 기준 연도 (DEFAULT: 전년도). '
    'upsert guard: 동일 tier에서 더 최신 reference_year로만 갱신. '
    '더 높은 tier(실측=1)가 이미 있으면 overwrite하지 않음. '
    '호출: SELECT * FROM fn_sync_tier_c_to_energy_results(2025);';

-- ---------------------------------------------------------------------------
-- 7. data_sources에 tier_c_metered 추가
--    provenance v1 마이그레이션 이후 실행하므로 ON CONFLICT DO UPDATE 사용
-- ---------------------------------------------------------------------------

INSERT INTO data_sources
    (source_key, display_name, data_tier, source_type,
     update_cadence, coverage_note)
VALUES
    (
        'tier_c_metered',
        'Tier C 실계량기 시계열 (직접 수집)',
        1,
        'measured',
        'continuous',
        '서울시 공공건물·대형 업무용건물 실계량기. '
        '전력/가스/지역난방 15분~일 단위. '
        'time_series_buildings 레지스트리 등록 건물만.'
    )
ON CONFLICT (source_key) DO UPDATE
    SET display_name   = EXCLUDED.display_name,
        coverage_note  = EXCLUDED.coverage_note;

-- ---------------------------------------------------------------------------
-- 8. 인덱스
-- ---------------------------------------------------------------------------

-- time_series_buildings
CREATE INDEX IF NOT EXISTS idx_tsb_pnu
    ON time_series_buildings (pnu);

CREATE INDEX IF NOT EXISTS idx_tsb_active
    ON time_series_buildings (is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_tsb_meter_types
    ON time_series_buildings USING GIN (meter_types);
    -- GIN: meter_types @> ARRAY['electricity']::meter_energy_type[] 쿼리 지원

-- building_meter_readings (파티션 부모 테이블 인덱스 → 각 파티션 상속)
--
-- 인덱스 타입 결정:
--   (A) ts 단독 인덱스: BRIN vs B-tree
--       BRIN: 삽입 순서와 ts 순서가 일치할 때 (배치 ingest) 효율적.
--             단, 범위 검색에서 B-tree보다 false positive가 많아
--             heap fetch 비용이 증가한다. 수백 건 규모에서는
--             B-tree의 크기 부담이 없으므로 B-tree 채택.
--       → 규모가 수천 건 × 1분 해상도로 증가하면 BRIN 재검토.
--
--   (B) (building_id, energy_type, ts) 복합 인덱스:
--       이미 PRIMARY KEY로 정의되어 있으므로 추가 인덱스 불필요.
--       PK = 복합 B-tree 인덱스 역할.
--
--   (C) ts 단독 B-tree: "최근 N일 전체 건물 조회" 패턴 지원
--       단, 이 패턴은 building_ts_daily 계층으로 흡수하므로 우선순위 낮음.
--       아래 partial index (quality_flag = 'ok') 우선.
--
--   (D) (building_id, ts) partial index — quality='ok' 건만:
--       품질 필터 + 시간 범위 복합 쿼리에 최적화.

CREATE INDEX IF NOT EXISTS idx_bmr_building_ts
    ON building_meter_readings (building_id, ts DESC)
    WHERE quality_flag = 'ok';
    -- ok 데이터만 고속 조회 (대시보드 기본 뷰)

CREATE INDEX IF NOT EXISTS idx_bmr_ts_brin
    ON building_meter_readings USING BRIN (ts)
    WITH (pages_per_range = 128);
    -- 배치 ingest 후 전체 기간 범위 스캔 보조용 (크기 작고 유지비 낮음)
    -- B-tree PK와 병행 존재. 옵티마이저가 상황에 따라 선택.

CREATE INDEX IF NOT EXISTS idx_bmr_pipeline_run
    ON building_meter_readings (pipeline_run_id)
    WHERE pipeline_run_id IS NOT NULL;
    -- 특정 ingest 실행 결과 검증/롤백용

-- building_ts_daily
CREATE INDEX IF NOT EXISTS idx_btsd_building_day
    ON building_ts_daily (building_id, day DESC);
    -- "건물 X의 최근 N일" 기본 쿼리 패턴

CREATE INDEX IF NOT EXISTS idx_btsd_day_coverage
    ON building_ts_daily (day, coverage_pct)
    WHERE coverage_pct < 70;
    -- 데이터 품질 모니터링: coverage 낮은 날 빠른 탐색

-- ---------------------------------------------------------------------------
-- 9. IMPACT 등록 안내 (주석 — 수동으로 docs/IMPACT.md에 추가 필요)
-- ---------------------------------------------------------------------------
-- 이 마이그레이션 적용 후 docs/IMPACT.md에 아래 행을 추가한다:
--
-- | `time_series_buildings` 스키마 변경 | `db/migration_tier_c_timeseries_v1.sql` |
-- |   `docs/ARCHITECTURE.md §3.2.x` · `src/data_ingestion/collect_tier_c.py` |
-- | `building_meter_readings` 파티션 추가 | `db/migration_tier_c_timeseries_v1.sql` |
-- |   연도별 파티션 CREATE TABLE 추가 필요 (매년 1월 전) |
-- | `building_ts_summary` MV 갱신 주기 | `db/migration_tier_c_timeseries_v1.sql` |
-- |   `Celery beat 스케줄` (daily) |
-- | `fn_sync_tier_c_to_energy_results` 호출 | 해당 Celery task |
-- |   `energy_results` 갱신 → `buildings_enriched` REFRESH 연동 확인 |

-- ---------------------------------------------------------------------------
-- 10. 마이그레이션 완료 검증 쿼리
-- ---------------------------------------------------------------------------
--
-- (1) 테이블 생성 확인
-- SELECT tablename FROM pg_tables
-- WHERE tablename IN (
--     'time_series_buildings',
--     'building_meter_readings',
--     'building_ts_daily'
-- );
-- → 3행 반환
--
-- (2) 파티션 확인
-- SELECT parent.relname, child.relname
-- FROM pg_inherits
-- JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
-- JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
-- WHERE parent.relname IN ('building_meter_readings', 'building_ts_daily')
-- ORDER BY parent.relname, child.relname;
-- → building_meter_readings: 6행, building_ts_daily: 6행
--
-- (3) data_sources 확인
-- SELECT source_key, data_tier, source_type
-- FROM data_sources
-- WHERE source_key = 'tier_c_metered';
-- → 1행 (data_tier=1, source_type='measured')
--
-- (4) MV 확인
-- SELECT COUNT(*) FROM building_ts_summary;
-- → 0 (아직 데이터 없음, 오류 없이 반환되면 정상)
--
-- (5) 함수 확인
-- SELECT routine_name FROM information_schema.routines
-- WHERE routine_name = 'fn_sync_tier_c_to_energy_results';
-- → 1행
