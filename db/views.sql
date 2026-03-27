-- buildings_enriched: GIS footprint + 건축물대장 속성 PNU JOIN
-- 이 뷰가 3D Tiles 생성과 API의 핵심 데이터 소스

DROP MATERIALIZED VIEW IF EXISTS buildings_enriched;

CREATE MATERIALIZED VIEW buildings_enriched AS
SELECT
    f.gid,
    f.pnu AS pnu,
    f.geom,
    COALESCE(l_energy.bld_nm, l_best.bld_nm, l_parent.bld_nm, f.bld_nm, '') AS building_name,
    COALESCE(l_energy.main_purps_nm, l_best.main_purps_nm, l_parent.main_purps_nm, f.usage_type, '미분류') AS usage_type,
    COALESCE(l_best.strct_nm, l_parent.strct_nm) AS structure_type,

    -- 높이 (우선순위: GIS > 대장 > 층수x3.3 > 10m)
    COALESCE(
        NULLIF(f.height, 0),
        NULLIF(COALESCE(l_best.bld_ht, l_parent.bld_ht), 0),
        GREATEST(COALESCE(l_best.grnd_flr_cnt, l_parent.grnd_flr_cnt, f.grnd_flr, 3), 1) * 3.3,
        10.0
    ) AS height,

    COALESCE(l_best.grnd_flr_cnt, l_parent.grnd_flr_cnt, f.grnd_flr, 3) AS floors_above,
    COALESCE(l_best.ugrnd_flr_cnt, l_parent.ugrnd_flr_cnt, f.ugrnd_flr, 0) AS floors_below,
    COALESCE(l_energy.tot_area, l_best.tot_area, l_parent.tot_area, 0) AS total_area,
    COALESCE(l_energy.bld_area, l_best.bld_area, l_parent.bld_area, 0) AS building_area,

    -- 건축년도
    CASE
        WHEN COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day) IS NOT NULL
             AND COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day) != ''
            THEN LEFT(COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day), 4)::INTEGER
        WHEN f.approval_date IS NOT NULL AND f.approval_date != ''
            THEN LEFT(f.approval_date, 4)::INTEGER
        ELSE NULL
    END AS built_year,

    l_energy.enrgy_eff_rate AS energy_grade,
    l_energy.epi_score,

    -- 원형 분류
    CASE
        WHEN COALESCE(
            NULLIF(LEFT(NULLIF(COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day, ''),''), 4),''),
            NULLIF(LEFT(NULLIF(f.approval_date,''), 4),''),
            '1970')::INTEGER < 1980 THEN 'pre-1980'
        WHEN COALESCE(
            NULLIF(LEFT(NULLIF(COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day, ''),''), 4),''),
            NULLIF(LEFT(NULLIF(f.approval_date,''), 4),''),
            '1970')::INTEGER <= 2000 THEN '1980-2000'
        WHEN COALESCE(
            NULLIF(LEFT(NULLIF(COALESCE(l_energy.use_apr_day, l_best.use_apr_day, l_parent.use_apr_day, ''),''), 4),''),
            NULLIF(LEFT(NULLIF(f.approval_date,''), 4),''),
            '1970')::INTEGER <= 2010 THEN '2001-2010'
        ELSE 'post-2010'
    END AS vintage_class,

    CASE
        WHEN COALESCE(l_energy.tot_area, l_best.tot_area, l_parent.tot_area, 0) < 500 THEN 'small'
        WHEN COALESCE(l_energy.tot_area, l_best.tot_area, l_parent.tot_area, 0) < 3000 THEN 'medium'
        ELSE 'large'
    END AS size_class,

    -- 구조 분류
    CASE
        WHEN COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%철근콘크리트%'
          OR COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%RC%' THEN 'RC'
        WHEN COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%철골%'
          OR COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%S조%' THEN 'steel'
        WHEN COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%조적%'
          OR COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%벽돌%'
          OR COALESCE(l_best.strct_nm, l_parent.strct_nm) LIKE '%목%' THEN 'masonry'
        ELSE 'RC'
    END AS structure_class

FROM building_footprints f
-- 에너지등급 보유 레코드 우선 (총괄표제부)
LEFT JOIN LATERAL (
    SELECT enrgy_eff_rate, epi_score, main_purps_nm, bld_nm,
           tot_area, bld_area, use_apr_day
    FROM building_ledger
    WHERE pnu = f.pnu AND enrgy_eff_rate IS NOT NULL
    ORDER BY tot_area DESC NULLS LAST
    LIMIT 1
) l_energy ON true
-- 구조/층수 정보 최적 레코드 (표제부)
LEFT JOIN LATERAL (
    SELECT main_purps_nm, bld_nm, strct_nm, grnd_flr_cnt, ugrnd_flr_cnt,
           bld_ht, tot_area, bld_area, use_apr_day
    FROM building_ledger
    WHERE pnu = f.pnu
    ORDER BY grnd_flr_cnt DESC NULLS LAST, tot_area DESC NULLS LAST
    LIMIT 1
) l_best ON true
-- 부번 불일치 fallback: pnu 뒤 4자리를 0000으로 대체하여 모번지 레코드 조회
-- (예: ...0030065 → ...0030000) — l_best 미매칭 시에만 동작
LEFT JOIN LATERAL (
    SELECT main_purps_nm, bld_nm, strct_nm, grnd_flr_cnt, ugrnd_flr_cnt,
           bld_ht, tot_area, bld_area, use_apr_day
    FROM building_ledger
    WHERE pnu = LEFT(f.pnu, 15) || '0000'
      AND l_best.main_purps_nm IS NULL  -- 정확 매칭 없을 때만
    ORDER BY grnd_flr_cnt DESC NULLS LAST, tot_area DESC NULLS LAST
    LIMIT 1
) l_parent ON true
WHERE f.geom IS NOT NULL
  AND ST_IsValid(f.geom);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_enriched_gid ON buildings_enriched(gid);
CREATE INDEX IF NOT EXISTS idx_enriched_geom ON buildings_enriched USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_enriched_pnu ON buildings_enriched(pnu);
CREATE INDEX IF NOT EXISTS idx_enriched_usage ON buildings_enriched(usage_type);
CREATE INDEX IF NOT EXISTS idx_enriched_grade ON buildings_enriched(energy_grade);
CREATE INDEX IF NOT EXISTS idx_enriched_vintage ON buildings_enriched(vintage_class);
