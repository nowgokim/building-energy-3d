-- 건물 에너지 시계열 모니터링 테이블
-- 실행 순서: init.sql 이후

-- ── 1. 시계열 모니터링 대상 건물 ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS monitored_buildings (
    ts_id       SERIAL PRIMARY KEY,
    pnu         VARCHAR(19),  -- building_footprints.pnu FK 미적용 (pnu 비유니크 — 집합건물 1:N)
    alias       VARCHAR(200),
    meter_types TEXT[]       NOT NULL DEFAULT '{}',
    total_area  REAL,
    usage_type  VARCHAR(100),
    built_year  INTEGER,
    data_source VARCHAR(100),  -- 'KEPCO_AMI' | '가스공사' | '수동입력'
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_monitored_pnu ON monitored_buildings(pnu);

-- building_centroids.pnu B-tree 인덱스 (monitor API의 LEFT JOIN 최적화)
-- monitored_buildings ↔ building_centroids pnu JOIN 시 770K 행 Seq Scan 방지.
-- building_centroids 자체의 기존 인덱스는 GiST(centroid) 뿐이므로 여기서 추가.
CREATE INDEX IF NOT EXISTS idx_bc_pnu ON building_centroids(pnu);

COMMENT ON TABLE monitored_buildings IS
    '시계열 계량 데이터를 보유한 건물 목록. pnu FK는 buildings_enriched와 1:1 매핑.';
COMMENT ON COLUMN monitored_buildings.meter_types IS
    'PostgreSQL 배열. 예: {''electricity'',''gas''}';

-- ── 2. 계량값 (LIST 파티션: 월별) ─────────────────────────────────────────────

-- 파티션 부모 테이블
CREATE TABLE IF NOT EXISTS metered_readings (
    id          BIGSERIAL,
    ts_id       INTEGER     NOT NULL REFERENCES monitored_buildings(ts_id),
    meter_type  VARCHAR(20) NOT NULL CHECK (meter_type IN ('electricity','gas','heat','water')),
    recorded_at TIMESTAMP   NOT NULL,
    value       REAL        NOT NULL,
    unit        VARCHAR(10) NOT NULL DEFAULT 'kWh',
    PRIMARY KEY (id, recorded_at)
) PARTITION BY RANGE (recorded_at);

-- 복합 UNIQUE (중복 삽입 방지 — ON CONFLICT DO NOTHING 의존)
-- 파티션 테이블에서는 UNIQUE는 파티션 키를 포함해야 함
-- NOTE: UNIQUE INDEX가 동일 컬럼셋 (ts_id, meter_type, recorded_at)에 대해
--       조회 최적화도 겸하므로 별도 non-unique 인덱스는 중복이다 — 제거.
CREATE UNIQUE INDEX IF NOT EXISTS uix_metered_readings_key
    ON metered_readings (ts_id, meter_type, recorded_at);

COMMENT ON TABLE metered_readings IS
    '계량 원본값. 15분~1시간 해상도. RANGE 파티션(monthly) 권장.';
COMMENT ON COLUMN metered_readings.value IS
    '계량값 원본 (단위는 unit 컬럼 참조). 전력: kWh, 가스: m3/kWh, 열: MJ.';

-- 파티션 자동 생성 함수 (psql에서 주기적으로 실행하거나 pg_cron 연동)
-- 아래는 2026년 월별 파티션 예시 — 실제 운영 시 pg_partman 도입 권장
CREATE TABLE IF NOT EXISTS metered_readings_2026_01
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_02
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_03
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_04
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_05
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_06
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_07
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_08
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_09
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_10
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_11
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');

CREATE TABLE IF NOT EXISTS metered_readings_2026_12
    PARTITION OF metered_readings
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- ── 3. 이상치 로그 ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS anomaly_log (
    id              BIGSERIAL PRIMARY KEY,
    ts_id           INTEGER     NOT NULL REFERENCES monitored_buildings(ts_id),
    meter_type      VARCHAR(20) NOT NULL,
    detected_at     TIMESTAMP   NOT NULL,
    window_mean     REAL,
    window_std      REAL,
    offending_value REAL,
    z_score         REAL,
    UNIQUE (ts_id, meter_type, detected_at)
);

CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at  ON anomaly_log (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_ts_time      ON anomaly_log (ts_id, detected_at DESC);

COMMENT ON TABLE anomaly_log IS
    '이상치 감지 결과. Celery beat 1시간 주기로 갱신. |z-score| > 2 기준.';
COMMENT ON COLUMN anomaly_log.window_mean IS
    'rolling 24h window 평균 (이상 시점 이전 기준)';
COMMENT ON COLUMN anomaly_log.z_score IS
    '|offending_value - window_mean| / window_std. 2 초과 = 이상치 판정';

-- ── 4. 뷰: 최신 EUI 요약 ──────────────────────────────────────────────────────

CREATE OR REPLACE VIEW monitor_eui_summary AS
SELECT
    mb.ts_id,
    mb.pnu,
    mb.alias,
    mb.usage_type,
    mb.built_year,
    mb.total_area,
    ROUND(
        (SUM(mr.value) FILTER (WHERE mr.meter_type IN ('electricity', 'gas'))
        / NULLIF(mb.total_area, 0))::NUMERIC,
        1
    ) AS eui_kwh_m2_1yr,
    MIN(mr.recorded_at) AS data_start,
    MAX(mr.recorded_at) AS data_end,
    COUNT(DISTINCT mr.meter_type) AS meter_count
FROM monitored_buildings mb
LEFT JOIN metered_readings mr
    ON mb.ts_id = mr.ts_id
    AND mr.recorded_at >= NOW() - INTERVAL '365 days'
GROUP BY mb.ts_id, mb.pnu, mb.alias, mb.usage_type, mb.built_year, mb.total_area;

COMMENT ON VIEW monitor_eui_summary IS
    '최근 365일 EUI 요약. API /monitor/buildings 캐시 만료 시 조회 대상.';

-- ── 5. metered_readings_daily — 일 단위 집계 중간 계층 ──────────────────────────
--
-- 설계 근거: 15분 원본 데이터에서 직접 월별/연별 쿼리 시
--   (365일 × 96개 = 35,040행 per building per year) × 수백 건 = 수천만 행 반복 집계.
--   일 집계 중간 계층을 두면 대시보드 30일 조회: 30행 × 건물 수.
--   Celery daily task가 채운다 (트리거 미사용 — 15분 INSERT 고빈도 오버헤드 회피).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS metered_readings_daily (
    ts_id           INTEGER         NOT NULL REFERENCES monitored_buildings(ts_id) ON DELETE CASCADE,
    meter_type      VARCHAR(20)     NOT NULL,
    day             DATE            NOT NULL,
    total_kwh       DOUBLE PRECISION,
    ok_count        INTEGER         NOT NULL DEFAULT 0,
    missing_count   INTEGER         NOT NULL DEFAULT 0,
    coverage_pct    REAL,
    computed_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ts_id, meter_type, day)
);

CREATE INDEX IF NOT EXISTS idx_mrd_ts_meter_day
    ON metered_readings_daily (ts_id, meter_type, day);

COMMENT ON TABLE metered_readings_daily IS
    '일 단위 집계 중간 계층. metered_readings 에서 Celery daily task가 채운다. '
    'coverage_pct < 50% 일 집계는 연간 EUI 계산에서 제외 권장.';
COMMENT ON COLUMN metered_readings_daily.coverage_pct IS
    '데이터 완전성 (%). 15분 해상도 기준 96 인터벌 중 유효값 비율. '
    'NULL = 해상도 혼재로 계산 불가.';

-- ── 6. fn_sync_tier_c_to_energy_results ────────────────────────────────────────
--
-- metered_readings_daily의 연간 집계를 energy_results에 upsert.
-- 호출 시점: Celery 연간 배치 (매년 1월 또는 수동)
--   SELECT * FROM fn_sync_tier_c_to_energy_results(2025);
--
-- upsert guard: data_tier 숫자 클수록 낮은 품질 (1=실측, 2=인증, 3=추정, 4=아키타입).
--   기존 tier > 신규 tier → 고품질로 교체.
--   동일 tier → 더 최신 reference_year로만 갱신.
--   기존 실측(1)을 저품질(3,4)로 overwrite하지 않음.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_sync_tier_c_to_energy_results(
    p_reference_year INTEGER DEFAULT EXTRACT(YEAR FROM CURRENT_DATE)::INTEGER - 1
)
RETURNS TABLE (
    out_pnu         VARCHAR(19),
    out_action      TEXT,
    out_total_kwh   DOUBLE PRECISION
)
LANGUAGE plpgsql AS
$$
DECLARE
    v_source_id     INTEGER;
    v_run_id        UUID;
BEGIN
    SELECT id INTO v_source_id
    FROM data_sources
    WHERE source_key = 'tier_c_metered'
    LIMIT 1;

    IF v_source_id IS NOT NULL THEN
        INSERT INTO pipeline_runs (run_type, source_id, params, status)
        VALUES (
            'sync_tier_c_annual',
            v_source_id,
            jsonb_build_object('reference_year', p_reference_year),
            'running'
        )
        RETURNING id INTO v_run_id;
    END IF;

    RETURN QUERY
    WITH annual_agg AS (
        SELECT
            mb.pnu,
            mb.ts_id,
            SUM(CASE WHEN d.meter_type = 'electricity'  THEN d.total_kwh ELSE 0 END) AS elec_kwh,
            SUM(CASE WHEN d.meter_type = 'gas'           THEN d.total_kwh ELSE 0 END) AS gas_kwh,
            SUM(CASE WHEN d.meter_type = 'heat'          THEN d.total_kwh ELSE 0 END) AS dh_kwh,
            SUM(CASE WHEN d.meter_type NOT IN ('water')  THEN d.total_kwh ELSE 0 END) AS total_kwh,
            COUNT(DISTINCT d.meter_type)                                               AS meter_count,
            AVG(d.coverage_pct)                                                        AS avg_coverage
        FROM metered_readings_daily d
        JOIN monitored_buildings mb ON mb.ts_id = d.ts_id
        WHERE EXTRACT(YEAR FROM d.day)::INTEGER = p_reference_year
          AND d.total_kwh IS NOT NULL
          AND (d.coverage_pct IS NULL OR d.coverage_pct >= 50.0)
          AND mb.pnu IS NOT NULL
        GROUP BY mb.pnu, mb.ts_id
        HAVING SUM(CASE WHEN d.meter_type NOT IN ('water') THEN d.total_kwh ELSE 0 END) > 0
    ),
    upserted AS (
        INSERT INTO energy_results (
            pnu, total_energy, heating, cooling,
            data_tier, source_id, pipeline_run_id, reference_year,
            is_current, simulation_type, source_metadata
        )
        SELECT
            aa.pnu,
            aa.total_kwh,
            aa.gas_kwh + aa.dh_kwh,
            0,
            1,
            v_source_id,
            v_run_id,
            p_reference_year::SMALLINT,
            TRUE,
            'tier_c_metered',
            jsonb_build_object(
                'elec_kwh', aa.elec_kwh, 'gas_kwh', aa.gas_kwh,
                'dh_kwh', aa.dh_kwh, 'meter_count', aa.meter_count,
                'avg_coverage', ROUND(aa.avg_coverage::NUMERIC, 1),
                'reference_year', p_reference_year
            )
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
                source_metadata = EXCLUDED.source_metadata
            WHERE energy_results.data_tier > EXCLUDED.data_tier
               OR (
                    energy_results.data_tier = EXCLUDED.data_tier
                    AND (
                        energy_results.reference_year IS NULL
                        OR energy_results.reference_year < EXCLUDED.reference_year
                    )
                  )
        RETURNING pnu, (xmax = 0) AS is_insert, total_energy
    )
    SELECT u.pnu::VARCHAR(19),
           CASE WHEN u.is_insert THEN 'inserted' ELSE 'updated' END,
           u.total_energy
    FROM upserted u;

    IF v_run_id IS NOT NULL THEN
        UPDATE pipeline_runs SET status = 'success', ended_at = NOW() WHERE id = v_run_id;
    END IF;

    EXCEPTION WHEN OTHERS THEN
        IF v_run_id IS NOT NULL THEN
            UPDATE pipeline_runs SET status = 'failed', ended_at = NOW(), error_message = SQLERRM
            WHERE id = v_run_id;
        END IF;
        RAISE;
END;
$$;

COMMENT ON FUNCTION fn_sync_tier_c_to_energy_results(INTEGER) IS
    'Tier C 건물의 연간 계량 집계를 energy_results에 upsert. '
    'p_reference_year: 집계 기준 연도 (DEFAULT: 전년도). '
    '호출: SELECT * FROM fn_sync_tier_c_to_energy_results(2025);';

-- ── 7. data_sources에 tier_c_metered 등록 ───────────────────────────────────────

INSERT INTO data_sources
    (source_key, display_name, data_tier, source_type, update_cadence, coverage_note)
VALUES (
    'tier_c_metered',
    'Tier C 실계량기 시계열 (직접 수집)',
    1,
    'measured',
    'continuous',
    '서울시 공공건물·대형 업무용건물 실계량기. 전력/가스/지역난방 15분~일 단위. monitored_buildings 레지스트리 등록 건물만.'
)
ON CONFLICT (source_key) DO UPDATE
    SET display_name  = EXCLUDED.display_name,
        coverage_note = EXCLUDED.coverage_note;
