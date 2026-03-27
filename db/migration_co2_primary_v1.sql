-- CO2 배출량 + 1차에너지 환산 컬럼 추가
-- 실행 순서: migration_provenance_v1.sql 이후
--
-- 배출계수 (환경부 2023 국가 온실가스 배출계수):
--   전력      0.4781 kgCO₂eq/kWh
--   도시가스   0.2036 kgCO₂eq/kWh  (LNG 연소 기준)
--   지역난방   0.1218 kgCO₂eq/kWh  (한국지역난방공사)
--
-- 1차에너지 환산계수 (건축물에너지절약설계기준, 2025.1.1 시행):
--   전력      2.75
--   도시가스   1.1
--   지역난방   0.614

-- ── 1. energy_results 컬럼 추가 ─────────────────────────────────────────────

ALTER TABLE energy_results
    ADD COLUMN IF NOT EXISTS co2_kg_yr             REAL,
    ADD COLUMN IF NOT EXISTS co2_kg_m2             REAL,
    ADD COLUMN IF NOT EXISTS primary_energy_kwh_yr REAL,
    ADD COLUMN IF NOT EXISTS primary_energy_kwh_m2 REAL;

COMMENT ON COLUMN energy_results.co2_kg_yr IS
    'CO2 배출량 (kgCO₂eq/yr). 배출계수: 전력 0.4781, 가스 0.2036, 지역난방 0.1218.';
COMMENT ON COLUMN energy_results.co2_kg_m2 IS
    'CO2 강도 (kgCO₂eq/m²·yr) = co2_kg_yr / 연면적.';
COMMENT ON COLUMN energy_results.primary_energy_kwh_yr IS
    '1차에너지 소요량 (kWh/yr). 환산계수: 전력 2.75, 가스 1.1, 지역난방 0.614.';
COMMENT ON COLUMN energy_results.primary_energy_kwh_m2 IS
    '1차에너지 강도 (kWh/m²·yr). 한국 에너지절약설계기준 ZEB 지표 (목표 150 kWh/m²·yr 이하).';

-- ── 2. fn_recompute_co2_primary ──────────────────────────────────────────────
--
-- 용도별 에너지믹스 (archetype / Tier 4 fallback):
--   apartment          전력 35%, 가스 65%
--   residential_single 전력 30%, 가스 70%
--   office             전력 80%, 가스 20%
--   retail             전력 85%, 가스 15%
--   education          전력 60%, 가스 40%
--   hospital           전력 55%, 가스 35%, 지역난방 10%
--   warehouse          전력 60%, 가스 40%
--   cultural           전력 70%, 가스 30%
--   기타               전력 65%, 가스 35%
--
-- Tier C 실측 (simulation_type = 'tier_c_metered'):
--   source_metadata.elec_kwh / gas_kwh / dh_kwh 실측값 사용
--
-- 호출: SELECT fn_recompute_co2_primary();

CREATE OR REPLACE FUNCTION fn_recompute_co2_primary()
RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    -- ── 경로 A: Tier C 실측 — source_metadata 절대값(kWh/yr) 사용 ────────────
    -- total_energy = 절대 kWh/yr → co2_yr 먼저 계산, 면적으로 나눠 m²값 도출
    WITH tier_c AS (
        SELECT
            er.pnu,
            GREATEST(COALESCE((er.source_metadata->>'elec_kwh')::REAL, 0), 0) AS e,
            GREATEST(COALESCE((er.source_metadata->>'gas_kwh' )::REAL, 0), 0) AS g,
            GREATEST(COALESCE((er.source_metadata->>'dh_kwh'  )::REAL, 0), 0) AS d,
            NULLIF(be.total_area, 0) AS area
        FROM energy_results er
        JOIN buildings_enriched be ON be.pnu = er.pnu
        WHERE er.simulation_type = 'tier_c_metered'
          AND er.source_metadata IS NOT NULL
          AND er.total_energy IS NOT NULL AND er.total_energy > 0
    )
    UPDATE energy_results er
    SET
        co2_kg_yr             = ROUND((c.e*0.4781 + c.g*0.2036 + c.d*0.1218)::NUMERIC, 1)::REAL,
        co2_kg_m2             = CASE WHEN c.area IS NOT NULL
                                     THEN ROUND(((c.e*0.4781+c.g*0.2036+c.d*0.1218)/c.area)::NUMERIC,2)::REAL
                                     ELSE NULL END,
        primary_energy_kwh_yr = ROUND((c.e*2.75 + c.g*1.1 + c.d*0.614)::NUMERIC, 1)::REAL,
        primary_energy_kwh_m2 = CASE WHEN c.area IS NOT NULL
                                     THEN ROUND(((c.e*2.75+c.g*1.1+c.d*0.614)/c.area)::NUMERIC,1)::REAL
                                     ELSE NULL END
    FROM tier_c c
    WHERE er.pnu = c.pnu;

    -- ── 경로 B: archetype / Tier 1/2/4 — total_energy = EUI (kWh/m²/yr) ────
    -- co2_kg_m2 = EUI × 가중 배출계수 (면적 나누기 불필요)
    -- co2_kg_yr = co2_kg_m2 × 면적
    WITH eui_based AS (
        SELECT
            er.pnu,
            -- 용도별 전력 EUI 비율
            COALESCE(er.total_energy, 0) * (
                CASE be.usage_type
                    WHEN '공동주택'          THEN 0.35
                    WHEN '단독주택'          THEN 0.30
                    WHEN '업무시설'          THEN 0.80
                    WHEN '제1종근린생활시설' THEN 0.85
                    WHEN '제2종근린생활시설' THEN 0.85
                    WHEN '판매시설'          THEN 0.85
                    WHEN '교육연구시설'      THEN 0.60
                    WHEN '의료시설'          THEN 0.55
                    WHEN '창고시설'          THEN 0.60
                    WHEN '공장'              THEN 0.60
                    WHEN '문화및집회시설'    THEN 0.70
                    WHEN '종교시설'          THEN 0.70
                    ELSE 0.65
                END
            ) AS e_m2,
            COALESCE(er.total_energy, 0) * (
                CASE be.usage_type
                    WHEN '공동주택'          THEN 0.65
                    WHEN '단독주택'          THEN 0.70
                    WHEN '업무시설'          THEN 0.20
                    WHEN '제1종근린생활시설' THEN 0.15
                    WHEN '제2종근린생활시설' THEN 0.15
                    WHEN '판매시설'          THEN 0.15
                    WHEN '교육연구시설'      THEN 0.40
                    WHEN '의료시설'          THEN 0.35
                    WHEN '창고시설'          THEN 0.40
                    WHEN '공장'              THEN 0.40
                    WHEN '문화및집회시설'    THEN 0.30
                    WHEN '종교시설'          THEN 0.30
                    ELSE 0.35
                END
            ) AS g_m2,
            COALESCE(er.total_energy, 0) * (
                CASE be.usage_type WHEN '의료시설' THEN 0.10 ELSE 0.0 END
            ) AS d_m2,
            NULLIF(be.total_area, 0) AS area
        FROM energy_results er
        JOIN buildings_enriched be ON be.pnu = er.pnu
        WHERE (er.simulation_type IS NULL OR er.simulation_type != 'tier_c_metered')
          AND er.total_energy IS NOT NULL AND er.total_energy > 0
    )
    UPDATE energy_results er
    SET
        -- EUI 기반: co2_kg_m2 = EUI_fraction × factor (단위: kgCO₂/m²/yr)
        co2_kg_m2             = ROUND((c.e_m2*0.4781 + c.g_m2*0.2036 + c.d_m2*0.1218)::NUMERIC, 2)::REAL,
        co2_kg_yr             = CASE WHEN c.area IS NOT NULL
                                     THEN ROUND(((c.e_m2*0.4781+c.g_m2*0.2036+c.d_m2*0.1218)*c.area)::NUMERIC,1)::REAL
                                     ELSE NULL END,
        primary_energy_kwh_m2 = ROUND((c.e_m2*2.75 + c.g_m2*1.1 + c.d_m2*0.614)::NUMERIC, 1)::REAL,
        primary_energy_kwh_yr = CASE WHEN c.area IS NOT NULL
                                     THEN ROUND(((c.e_m2*2.75+c.g_m2*1.1+c.d_m2*0.614)*c.area)::NUMERIC,1)::REAL
                                     ELSE NULL END
    FROM eui_based c
    WHERE er.pnu = c.pnu;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;

COMMENT ON FUNCTION fn_recompute_co2_primary() IS
    'energy_results 전체에 co2_kg_yr/m2, primary_energy_kwh_yr/m2 일괄 계산.'
    ' Tier C: source_metadata 실측값 우선.'
    ' 기타: usage_type별 에너지믹스 추정.'
    ' 호출: SELECT fn_recompute_co2_primary();';
