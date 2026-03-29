-- 3D Building Energy Platform - DB 초기화
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;

-- 1. 건물 Footprint (GIS건물통합정보)
CREATE TABLE IF NOT EXISTS building_footprints (
    gid         SERIAL PRIMARY KEY,
    pnu         VARCHAR(19),
    bld_mgt_sn  VARCHAR(25),
    bld_nm      VARCHAR(200),
    dong_nm     VARCHAR(100),
    usage_type  VARCHAR(100),
    grnd_flr    INTEGER,
    ugrnd_flr   INTEGER,
    height      REAL,
    approval_date VARCHAR(8),
    geom        GEOMETRY(MultiPolygon, 4326)
);
CREATE INDEX IF NOT EXISTS idx_fp_pnu ON building_footprints(pnu);
CREATE INDEX IF NOT EXISTS idx_fp_bld_mgt ON building_footprints(bld_mgt_sn);
CREATE INDEX IF NOT EXISTS idx_fp_geom ON building_footprints USING GIST(geom);

-- 2. 건축물대장 속성
CREATE TABLE IF NOT EXISTS building_ledger (
    id              SERIAL PRIMARY KEY,
    pnu             VARCHAR(19),
    bld_mgt_sn      VARCHAR(25),
    bld_nm          VARCHAR(200),
    dong_nm         VARCHAR(100),
    main_purps_cd   VARCHAR(5),
    main_purps_nm   VARCHAR(100),
    strct_cd        VARCHAR(5),
    strct_nm        VARCHAR(100),
    grnd_flr_cnt    INTEGER,
    ugrnd_flr_cnt   INTEGER,
    bld_ht          REAL,
    tot_area        REAL,
    bld_area        REAL,
    use_apr_day     VARCHAR(8),
    enrgy_eff_rate  VARCHAR(10),
    epi_score       REAL,
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ledger_pnu ON building_ledger(pnu);
CREATE INDEX IF NOT EXISTS idx_ledger_bld_mgt ON building_ledger(bld_mgt_sn);

-- 3. 에너지 시뮬레이션 결과
CREATE TABLE IF NOT EXISTS energy_results (
    id              SERIAL PRIMARY KEY,
    pnu             VARCHAR(19) NOT NULL,
    archetype_id    INTEGER,
    heating         REAL,
    cooling         REAL,
    hot_water       REAL,
    lighting        REAL,
    ventilation     REAL,
    total_energy    REAL,
    wall_uvalue     REAL,
    roof_uvalue     REAL,
    window_uvalue   REAL,
    wwr             REAL,
    simulation_type VARCHAR(20) DEFAULT 'archetype',
    simulated_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_energy_pnu ON energy_results(pnu);
CREATE INDEX IF NOT EXISTS idx_energy_results_pnu_current ON energy_results(pnu, is_current) WHERE is_current = TRUE;

-- 4. 건물 원형
CREATE TABLE IF NOT EXISTS building_archetypes (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE,
    usage_category  VARCHAR(50),
    vintage_class   VARCHAR(20),
    size_class      VARCHAR(10),
    structure_type  VARCHAR(20),
    climate_zone    VARCHAR(10) DEFAULT '중부2',
    wall_uvalue     REAL,
    roof_uvalue     REAL,
    floor_uvalue    REAL,
    window_uvalue   REAL,
    default_wwr     REAL,
    occupancy_density REAL,
    lighting_power    REAL,
    equipment_power   REAL,
    ref_heating     REAL,
    ref_cooling     REAL,
    ref_total       REAL
);
