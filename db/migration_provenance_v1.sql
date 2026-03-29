-- =============================================================================
-- migration_provenance_v1.sql
-- Phase 4-B: 에너지 데이터 출처 추적 (Provenance) 스키마
--
-- 실행 순서:
--   1. ENUM 타입 생성
--   2. data_sources 테이블
--   3. pipeline_runs 테이블
--   4. energy_results ALTER (새 컬럼 추가)
--   5. model_registry 테이블
--   6. model_versions 테이블
--   7. energy_predictions 파티셔닝 테이블
--   8. model_accuracy_summary Materialized View
--   9. 인덱스
--  10. 초기 데이터 (data_sources 7건)
--  11. 파티션 사전 생성 (현재 연도 + 다음 연도)
--
-- 적용:
--   docker compose exec db psql -U postgres -d buildings \
--     -f /docker-entrypoint-initdb.d/migration_provenance_v1.sql
--
-- 롤백:
--   이 마이그레이션은 ADD COLUMN / CREATE TABLE 만 수행하므로
--   롤백 시 DROP TABLE 및 ALTER TABLE DROP COLUMN 으로 되돌린다.
--   energy_results 의 기존 행은 data_tier DEFAULT 4 로 자동 보정되어
--   기존 인서트 코드에 영향 없음.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. pgcrypto (gen_random_uuid 지원)
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. ENUM 타입
-- ---------------------------------------------------------------------------

-- 파이프라인 실행 상태
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'pipeline_run_status') THEN
        CREATE TYPE pipeline_run_status AS ENUM (
            'running',
            'success',
            'partial',   -- 일부 행 실패 후 완료
            'failed',
            'cancelled'
        );
    END IF;
END$$;

-- 모델 종류
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'model_type_enum') THEN
        CREATE TYPE model_type_enum AS ENUM (
            'xgboost',
            'lightgbm',
            'lstm',
            'transformer',
            'patchtst',
            'dl_generic',
            'energyplus',
            'archetype_lookup'
        );
    END IF;
END$$;

-- 모델 배포 단계
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'model_stage_enum') THEN
        CREATE TYPE model_stage_enum AS ENUM (
            'dev',
            'staging',
            'production',
            'archived'
        );
    END IF;
END$$;

-- 예측 시간 해상도
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'temporal_scale_enum') THEN
        CREATE TYPE temporal_scale_enum AS ENUM (
            'annual',
            'daily',
            'hourly'
        );
    END IF;
END$$;

-- 에너지 예측 대상 변수
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'target_variable_enum') THEN
        CREATE TYPE target_variable_enum AS ENUM (
            'eui',       -- 에너지사용원단위 (kWh/m²·년)
            'elec_kwh',  -- 전기 소비량 (kWh)
            'gas_kwh'    -- 가스 소비량 (kWh)
        );
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 2. data_sources 테이블
--    에너지 데이터 원천 레지스트리. 행이 추가만 되는 참조 테이블이다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS data_sources (
    id              SERIAL PRIMARY KEY,
    source_key      VARCHAR(50)  NOT NULL UNIQUE,
    display_name    VARCHAR(200) NOT NULL,
    data_tier       SMALLINT     NOT NULL CHECK (data_tier BETWEEN 1 AND 4),
    -- Tier 1: 실측, Tier 2: 인증/공인, Tier 3: 시뮬/예측, Tier 4: 폴백(아키타입)
    source_type     VARCHAR(30)  NOT NULL,
    -- 'measured' | 'certified' | 'simulation' | 'prediction' | 'lookup'
    api_endpoint    TEXT,        -- 공공API URL (있을 경우)
    data_go_kr_id   VARCHAR(20), -- 공공데이터포털 서비스 ID
    update_cadence  VARCHAR(50), -- 예: 'monthly', 'annual', 'on_demand'
    coverage_note   TEXT,        -- 커버리지 설명
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  data_sources IS
    '에너지 데이터 원천 레지스트리. Tier 1(실측) ~ Tier 4(폴백) 우선순위 정의.';
COMMENT ON COLUMN data_sources.source_key IS
    '코드 내 참조 키. collect_energy.py의 simulation_type 값과 일치시킨다.';
COMMENT ON COLUMN data_sources.data_tier IS
    '1=실측, 2=공인인증, 3=시뮬레이션/ML예측, 4=아키타입 폴백';
COMMENT ON COLUMN data_sources.source_type IS
    'measured | certified | simulation | prediction | lookup';

-- ---------------------------------------------------------------------------
-- 3. pipeline_runs 테이블
--    데이터 수집/변환 파이프라인 실행 이력. collect_energy.py 등이 기록한다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type        VARCHAR(50)  NOT NULL,
    -- 예: 'collect_energy_grade', 'collect_bldg_hub_elec', 'collect_kea_cert'
    source_id       INTEGER      REFERENCES data_sources(id) ON DELETE SET NULL,
    script_version  VARCHAR(30),
    -- Git commit hash 또는 semver (예: 'a3f9b12', 'v1.2.0')
    params          JSONB,
    -- 실행 파라미터: {"limit": 2000, "sigungu_codes": ["11"]}
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    rows_upserted   INTEGER,
    rows_skipped    INTEGER,
    rows_failed     INTEGER,
    status          pipeline_run_status NOT NULL DEFAULT 'running',
    error_message   TEXT,        -- 실패 시 오류 메시지
    host_name       VARCHAR(100) -- 실행 컨테이너/서버 이름
);

COMMENT ON TABLE  pipeline_runs IS
    '데이터 수집·변환 파이프라인 실행 이력. 각 collect_*.py 실행마다 1행 삽입.';
COMMENT ON COLUMN pipeline_runs.id IS
    'UUID PK — 복수 워커 병렬 실행 시 충돌 없음';
COMMENT ON COLUMN pipeline_runs.script_version IS
    'Git 커밋 해시 또는 semver. 재현성 확보를 위해 반드시 기록한다.';
COMMENT ON COLUMN pipeline_runs.params IS
    '실행 파라미터 JSONB. limit, 지역코드, API 키 마스킹 버전 등 포함.';

-- ---------------------------------------------------------------------------
-- 4. energy_results ALTER — 출처 추적 컬럼 추가
--    CRITICAL: 모든 컬럼은 NULLABLE 또는 DEFAULT 있음.
--              기존 INSERT 코드 (collect_energy.py _upsert_batch) 는 무수정.
-- ---------------------------------------------------------------------------

-- 4-1. 출처 참조
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS source_id         INTEGER
        REFERENCES data_sources(id) ON DELETE SET NULL;

-- 4-2. 파이프라인 실행 참조
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS pipeline_run_id   UUID
        REFERENCES pipeline_runs(id) ON DELETE SET NULL;

-- 4-3. 데이터 티어 (기존 행: archetype 폴백 = Tier 4)
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS data_tier         SMALLINT DEFAULT 4
        CHECK (data_tier BETWEEN 1 AND 4);

-- 4-4. 참조 연도 (실소비량 데이터가 어느 해 기준인지)
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS reference_year    SMALLINT;
-- 예: 2024 (건축HUB는 수개월 지연 → 수집 시점 - 1년 등)

-- 4-5. 현행 레코드 플래그 (동일 PNU에 여러 버전 있을 경우 최신 1건)
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS is_current        BOOLEAN DEFAULT TRUE;

-- 4-6. 이 레코드를 대체한 레코드 ID (superseded 체인)
--      NOTE: energy_results에 id 컬럼이 없으므로 FK 없는 INTEGER.
--            id 컬럼 추가 후 FK 연결 예정.
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS superseded_by     INTEGER;

-- 4-7. 원천 특화 메타데이터 (API 응답 원본 일부, 인증번호 등)
ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS source_metadata   JSONB;
-- 예: {"cert_no": "2024-123", "cert_date": "2024-05-01", "raw_grade": "1+등급"}

COMMENT ON COLUMN energy_results.source_id IS
    'data_sources.id FK. NULL = 마이그레이션 이전 레코드 (Tier 4 폴백으로 간주).';
COMMENT ON COLUMN energy_results.pipeline_run_id IS
    'pipeline_runs.id FK. NULL = 마이그레이션 이전 레코드 또는 수동 삽입.';
COMMENT ON COLUMN energy_results.data_tier IS
    '1=실측, 2=공인인증, 3=시뮬/예측, 4=폴백. DEFAULT 4로 기존 행 자동 보정.';
COMMENT ON COLUMN energy_results.reference_year IS
    '에너지 데이터 기준 연도. 건축HUB 실소비량은 수집 시점 약 1년 전.';
COMMENT ON COLUMN energy_results.is_current IS
    'TRUE = 이 PNU의 현재 유효 레코드. 더 높은 Tier 데이터가 들어오면 FALSE로 전환.';
COMMENT ON COLUMN energy_results.superseded_by IS
    '이 레코드를 대체한 energy_results.id. NULL이면 현재 유효하거나 체인 끝.';
COMMENT ON COLUMN energy_results.source_metadata IS
    '원천 특화 정보. 인증번호, 원본 등급 문자열, 사용 연월(yyyyMM) 등.';

-- ---------------------------------------------------------------------------
-- 5. model_registry 테이블
--    모델 메타데이터 카탈로그. 각 모델 패밀리(이름+타입)별 1행.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_registry (
    id              SERIAL PRIMARY KEY,
    model_name      VARCHAR(100) NOT NULL,
    model_type      model_type_enum NOT NULL,
    temporal_scale  temporal_scale_enum NOT NULL,
    target_variable target_variable_enum NOT NULL,
    description     TEXT,
    stage           model_stage_enum NOT NULL DEFAULT 'dev',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_name, model_type, temporal_scale, target_variable)
);

COMMENT ON TABLE  model_registry IS
    '예측 모델 카탈로그. 모델 패밀리 단위로 1행. 버전은 model_versions에 분리.';
COMMENT ON COLUMN model_registry.model_name IS
    '사람이 읽을 수 있는 식별자. 예: "xgb_eui_annual_v1", "patchtst_hourly_elec"';
COMMENT ON COLUMN model_registry.stage IS
    'dev → staging → production → archived 순으로 승격. production은 동시 1개 권장.';

-- updated_at 자동 갱신 트리거
CREATE OR REPLACE FUNCTION fn_update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_model_registry_updated_at ON model_registry;
CREATE TRIGGER trg_model_registry_updated_at
    BEFORE UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION fn_update_updated_at();

-- ---------------------------------------------------------------------------
-- 6. model_versions 테이블
--    모델 버전별 훈련 메타데이터 및 검증 성능 지표.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_versions (
    id                      SERIAL PRIMARY KEY,
    model_id                INTEGER NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
    version_tag             VARCHAR(50) NOT NULL,
    -- 예: 'v1.0.0', '20260327-abc1234'
    training_data_snapshot  DATE,
    -- 훈련 데이터 스냅샷 날짜 (재현성 기준점)
    training_source_keys    TEXT[],
    -- 훈련에 사용된 data_sources.source_key 목록
    -- 예: ARRAY['bldg_energy_hub_elec', 'bldg_energy_hub_gas', 'energy_grade_cert']
    val_rmse                REAL,    -- 검증 RMSE (kWh/m²·년 또는 kWh)
    val_mape                REAL,    -- 검증 MAPE (%)
    val_r2                  REAL,    -- 검증 R²
    val_mae                 REAL,    -- 검증 MAE
    artifact_path           TEXT,
    -- 모델 파일 경로. 예: 's3://building-energy-models/xgb_eui/v1.0.0/model.pkl'
    --                    또는 '/models/xgb_eui_v1.pkl' (로컬 컨테이너)
    hyperparams             JSONB,
    -- 훈련 하이퍼파라미터. 예: {"n_estimators": 500, "max_depth": 6, "lr": 0.05}
    feature_names           TEXT[],  -- 입력 피처 목록
    train_rows              INTEGER, -- 훈련 데이터 행 수
    val_rows                INTEGER, -- 검증 데이터 행 수
    promoted_to_production  TIMESTAMPTZ, -- production 승격 시각 (NULL=미승격)
    archived_at             TIMESTAMPTZ, -- archived 처리 시각
    notes                   TEXT,    -- 자유 기술 (실험 메모, 데이터 이슈 등)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_id, version_tag)
);

COMMENT ON TABLE  model_versions IS
    '모델 버전별 훈련 파라미터, 검증 지표, 아티팩트 경로. model_registry의 자식.';
COMMENT ON COLUMN model_versions.training_data_snapshot IS
    '훈련 데이터 생성 기준일. 동일 날짜로 재학습하면 동일 결과가 나와야 한다 (재현성).';
COMMENT ON COLUMN model_versions.training_source_keys IS
    'data_sources.source_key 배열. 어떤 원천 데이터로 학습했는지 추적한다.';
COMMENT ON COLUMN model_versions.artifact_path IS
    '모델 아티팩트 위치. S3 URI 또는 컨테이너 절대경로. NULL이면 미저장/분실.';
COMMENT ON COLUMN model_versions.hyperparams IS
    '재학습 재현에 필요한 모든 하이퍼파라미터. 기본값과 달라진 값만이 아닌 전체 기록.';

-- ---------------------------------------------------------------------------
-- 7. energy_predictions 테이블 (PARTITION BY RANGE)
--    모델 추론 결과를 파티션별로 저장. 과거 예측값과 실제값 비교를 위해 보존.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS energy_predictions (
    id              BIGSERIAL,
    pnu             VARCHAR(19)         NOT NULL,
    model_version_id INTEGER             NOT NULL
                        REFERENCES model_versions(id) ON DELETE CASCADE,
    predicted_at    TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    -- 예측 수행 시각. 파티션 키 기준.
    temporal_scale  temporal_scale_enum NOT NULL,
    horizon_days    INTEGER,
    -- 예측 지평: 0=당일, 1=1일 후, 365=1년 후
    target_variable target_variable_enum NOT NULL,
    predicted_eui   REAL,                -- 예측값 (kWh/m²·년 또는 kWh)
    actual_eui      REAL,                -- 실제값 (나중에 채워짐, NULL 허용)
    error_abs       REAL,                -- |predicted - actual|
    error_pct       REAL,                -- |predicted - actual| / actual × 100
    confidence_low  REAL,                -- 예측 구간 하한 (옵션)
    confidence_high REAL,                -- 예측 구간 상한 (옵션)
    features_used   JSONB,               -- 추론 시 사용된 피처 스냅샷 (선택)
    PRIMARY KEY (id, predicted_at)
) PARTITION BY RANGE (predicted_at);

COMMENT ON TABLE  energy_predictions IS
    '모델 예측 결과. predicted_at 기준 월별 파티션으로 분리하여 오래된 예측 보관 비용 절감.';
COMMENT ON COLUMN energy_predictions.predicted_at IS
    '파티션 키. 매월 1일 기준 파티션 사전 생성 필요.';
COMMENT ON COLUMN energy_predictions.actual_eui IS
    'NULL = 아직 실측값 없음. 실측 데이터 수집 후 UPDATE로 채운다.';
COMMENT ON COLUMN energy_predictions.error_abs IS
    'actual_eui 채워진 후 자동 계산 또는 트리거로 업데이트.';
COMMENT ON COLUMN energy_predictions.features_used IS
    '디버깅 및 모델 모니터링용. 프로덕션에서는 용량 절감을 위해 NULL 허용.';

-- 연도별 파티션 생성 (월 단위 파티션보다 연 단위가 관리 비용 낮음)
-- 건물 에너지 데이터는 연간 업데이트가 주이므로 연도 파티션으로 충분하다.

-- 현재 연도: 2026
CREATE TABLE IF NOT EXISTS energy_predictions_2025
    PARTITION OF energy_predictions
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

CREATE TABLE IF NOT EXISTS energy_predictions_2026
    PARTITION OF energy_predictions
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

-- 다음 연도: 2027
CREATE TABLE IF NOT EXISTS energy_predictions_2027
    PARTITION OF energy_predictions
    FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');

-- 과거 데이터 및 미래 확장을 위한 기본 파티션 (범위 외 행 보호)
CREATE TABLE IF NOT EXISTS energy_predictions_default
    PARTITION OF energy_predictions
    DEFAULT;

COMMENT ON TABLE energy_predictions_2025 IS '2025년 예측 파티션';
COMMENT ON TABLE energy_predictions_2026 IS '2026년 예측 파티션 (현재 연도)';
COMMENT ON TABLE energy_predictions_2027 IS '2027년 예측 파티션 (다음 연도 사전 생성)';
COMMENT ON TABLE energy_predictions_default IS '범위 외 행 보호용 기본 파티션';

-- ---------------------------------------------------------------------------
-- 8. model_accuracy_summary Materialized View
--    모델 버전별 예측 정확도 집계. 대시보드 및 모델 비교용.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS model_accuracy_summary;

CREATE MATERIALIZED VIEW model_accuracy_summary AS
SELECT
    mv.id                           AS model_version_id,
    mr.model_name,
    mr.model_type,
    mv.version_tag,
    mr.stage,
    ep.temporal_scale,
    ep.target_variable,
    COUNT(ep.id)                    AS prediction_count,
    COUNT(ep.actual_eui)            AS evaluated_count,
    -- RMSE: sqrt(mean(error²))
    ROUND(
        SQRT(AVG(ep.error_abs * ep.error_abs))::NUMERIC, 3
    )                               AS rmse,
    -- MAPE: mean(|error| / actual × 100)
    ROUND(
        AVG(ep.error_pct)::NUMERIC, 2
    )                               AS mape,
    -- R²: 1 - MSE / Variance(actual)  — window function 미사용 버전
    ROUND(
        CASE
            WHEN VARIANCE(ep.actual_eui) = 0 THEN NULL
            ELSE 1.0 - (AVG(ep.error_abs * ep.error_abs) / NULLIF(VARIANCE(ep.actual_eui), 0))
        END::NUMERIC, 4
    )                               AS r_squared,
    -- 편의 지표
    ROUND(AVG(ep.predicted_eui)::NUMERIC, 1)  AS avg_predicted,
    ROUND(AVG(ep.actual_eui)::NUMERIC,    1)  AS avg_actual,
    ROUND(MAX(ep.error_abs)::NUMERIC,     1)  AS max_error_abs,
    MIN(ep.predicted_at)                      AS first_prediction_at,
    MAX(ep.predicted_at)                      AS last_prediction_at,
    NOW()                                     AS refreshed_at
FROM energy_predictions ep
JOIN model_versions mv  ON ep.model_version_id = mv.id
JOIN model_registry mr  ON mv.model_id = mr.id
WHERE ep.actual_eui IS NOT NULL   -- 실측값 있는 건만 평가
  AND ep.actual_eui >= 5          -- EUI≈0인 이상치 제외 (MAPE 분모 폭발 방지)
GROUP BY
    mv.id, mr.model_name, mr.model_type, mv.version_tag,
    mr.stage, ep.temporal_scale, ep.target_variable;

COMMENT ON MATERIALIZED VIEW model_accuracy_summary IS
    '모델 버전별 RMSE/MAPE/R² 집계. REFRESH MATERIALIZED VIEW CONCURRENTLY로 무중단 갱신 가능. '
    '예측 후 실측값 업데이트 시 주기적(일 1회 이상) 갱신 권장.';

-- model_accuracy_summary 고유 인덱스 (CONCURRENTLY refresh 요건)
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_accuracy_uniq
    ON model_accuracy_summary (model_version_id, temporal_scale, target_variable);

-- ---------------------------------------------------------------------------
-- 9. 인덱스
-- ---------------------------------------------------------------------------

-- data_sources
CREATE INDEX IF NOT EXISTS idx_data_sources_tier
    ON data_sources (data_tier);
CREATE INDEX IF NOT EXISTS idx_data_sources_active
    ON data_sources (is_active)
    WHERE is_active = TRUE;

-- pipeline_runs
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_source_id
    ON pipeline_runs (source_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at
    ON pipeline_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON pipeline_runs (status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_run_type
    ON pipeline_runs (run_type, started_at DESC);

-- energy_results (새 컬럼 인덱스)
CREATE INDEX IF NOT EXISTS idx_energy_results_source_id
    ON energy_results (source_id);
CREATE INDEX IF NOT EXISTS idx_energy_results_pipeline_run_id
    ON energy_results (pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_energy_results_data_tier
    ON energy_results (data_tier);
CREATE INDEX IF NOT EXISTS idx_energy_results_is_current
    ON energy_results (is_current)
    WHERE is_current = TRUE;
CREATE INDEX IF NOT EXISTS idx_energy_results_reference_year
    ON energy_results (reference_year);

-- model_registry
CREATE INDEX IF NOT EXISTS idx_model_registry_stage
    ON model_registry (stage);
CREATE INDEX IF NOT EXISTS idx_model_registry_type_scale
    ON model_registry (model_type, temporal_scale);

-- model_versions
CREATE INDEX IF NOT EXISTS idx_model_versions_model_id
    ON model_versions (model_id);
CREATE INDEX IF NOT EXISTS idx_model_versions_snapshot
    ON model_versions (training_data_snapshot DESC);
CREATE INDEX IF NOT EXISTS idx_model_versions_promoted
    ON model_versions (promoted_to_production DESC NULLS LAST)
    WHERE promoted_to_production IS NOT NULL;

-- energy_predictions (파티션 테이블 인덱스 → 각 파티션 자동 상속)
CREATE INDEX IF NOT EXISTS idx_energy_pred_pnu
    ON energy_predictions (pnu, predicted_at DESC);
CREATE INDEX IF NOT EXISTS idx_energy_pred_model_version
    ON energy_predictions (model_version_id, predicted_at DESC);
CREATE INDEX IF NOT EXISTS idx_energy_pred_needs_eval
    ON energy_predictions (pnu, temporal_scale)
    WHERE actual_eui IS NULL;

-- ---------------------------------------------------------------------------
-- 10. 초기 데이터: data_sources 7건
-- ---------------------------------------------------------------------------

INSERT INTO data_sources
    (source_key, display_name, data_tier, source_type,
     api_endpoint, data_go_kr_id, update_cadence, coverage_note)
VALUES
    (
        'bldg_energy_hub_elec',
        '국토교통부 건축HUB 건물에너지정보 — 전기',
        1,
        'measured',
        'https://apis.data.go.kr/1613000/BldEngyHubService/getBeElctyUsgInfo',
        '15135963',
        'monthly',
        '지번별 월별 전기 실소비량. 단독주택·200세대 미만 공동주택 제외. 약 6개월 지연.'
    ),
    (
        'bldg_energy_hub_gas',
        '국토교통부 건축HUB 건물에너지정보 — 가스',
        1,
        'measured',
        'https://apis.data.go.kr/1613000/BldEngyHubService/getBeGasUsgInfo',
        '15135963',
        'monthly',
        '지번별 월별 가스 실소비량. 도시가스 미공급 지역 제외. 약 6개월 지연.'
    ),
    (
        'energy_grade_cert',
        '한국에너지공단 건축물 에너지효율등급 인증',
        2,
        'certified',
        'https://apis.data.go.kr/B553530/BEEC/BEEC_01_LIST',
        '15100521',
        'annual',
        '인증 건물 1차에너지소비량 (kWh/m²·년). 본인증(q3=2) 기준. 서울 약 수천 건.'
    ),
    (
        'seoul_open_data',
        '서울 열린데이터광장 녹색건축인증',
        2,
        'certified',
        'http://openapi.seoul.go.kr:8088/{key}/json/GreenBuildingInfo',
        NULL,
        'annual',
        '서울시 녹색건축인증 건물 EUI. 서울 열린데이터광장 전용 API.'
    ),
    (
        'energyplus_sim',
        'EnergyPlus/OpenStudio 시뮬레이션',
        3,
        'simulation',
        NULL,
        NULL,
        'on_demand',
        '아키타입 40종 × 8760시간 시뮬레이션. Phase 4 구현 예정. 현재 미연동.'
    ),
    (
        'ml_prediction',
        'ML 예측 모델 (XGBoost/LSTM/PatchTST)',
        3,
        'prediction',
        NULL,
        NULL,
        'on_demand',
        '훈련된 ML 모델 추론 결과. model_registry / model_versions 참조. Phase 4 구현 예정.'
    ),
    (
        'archetype_lookup',
        '아키타입 룩업 폴백 (Tier 4)',
        4,
        'lookup',
        NULL,
        NULL,
        'on_demand',
        '용도·준공연도·규모 기반 40종 아키타입 매핑. 실소비량 데이터 없는 건물 폴백.'
    )
ON CONFLICT (source_key) DO UPDATE
    SET display_name   = EXCLUDED.display_name,
        data_tier      = EXCLUDED.data_tier,
        source_type    = EXCLUDED.source_type,
        api_endpoint   = EXCLUDED.api_endpoint,
        data_go_kr_id  = EXCLUDED.data_go_kr_id,
        update_cadence = EXCLUDED.update_cadence,
        coverage_note  = EXCLUDED.coverage_note;

-- ---------------------------------------------------------------------------
-- 11. 마이그레이션 완료 검증 쿼리 (실행 후 결과 확인용 주석)
-- ---------------------------------------------------------------------------

-- 아래 쿼리를 실행하여 마이그레이션 성공 여부를 확인한다:
--
-- SELECT source_key, display_name, data_tier
-- FROM data_sources
-- ORDER BY data_tier, source_key;
-- → 7행 반환 확인
--
-- SELECT column_name, data_type, column_default, is_nullable
-- FROM information_schema.columns
-- WHERE table_name = 'energy_results'
--   AND column_name IN
--       ('source_id','pipeline_run_id','data_tier','reference_year',
--        'is_current','superseded_by','source_metadata')
-- ORDER BY ordinal_position;
-- → 7컬럼 모두 nullable 또는 default 있음 확인
--
-- SELECT schemaname, tablename, partitionexpression
-- FROM pg_partitions
-- WHERE tablename LIKE 'energy_predictions%';
-- → 2025~2027 + default 파티션 4행 확인
--
-- SELECT COUNT(*) FROM model_accuracy_summary;
-- → 0 (아직 예측 없음, 오류 없이 반환되면 정상)
