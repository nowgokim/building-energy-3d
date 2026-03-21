-- buildings_enriched: GIS footprint + 건축물대장 속성 PNU JOIN
-- 이 뷰가 3D Tiles 생성과 API의 핵심 데이터 소스

DROP MATERIALIZED VIEW IF EXISTS buildings_enriched;

CREATE MATERIALIZED VIEW buildings_enriched AS
SELECT
    f.gid,
    COALESCE(f.pnu, l.pnu) AS pnu,
    f.geom,
    COALESCE(l.bld_nm, f.bld_nm, '') AS building_name,
    COALESCE(l.main_purps_nm, f.usage_type, '미분류') AS usage_type,
    l.strct_nm AS structure_type,

    -- 높이 (우선순위: GIS > 대장 > 층수x3.3 > 10m)
    COALESCE(
        NULLIF(f.height, 0),
        NULLIF(l.bld_ht, 0),
        GREATEST(COALESCE(l.grnd_flr_cnt, f.grnd_flr, 3), 1) * 3.3,
        10.0
    ) AS height,

    COALESCE(l.grnd_flr_cnt, f.grnd_flr, 3) AS floors_above,
    COALESCE(l.ugrnd_flr_cnt, f.ugrnd_flr, 0) AS floors_below,
    COALESCE(l.tot_area, 0) AS total_area,
    COALESCE(l.bld_area, 0) AS building_area,

    -- 건축년도
    CASE
        WHEN l.use_apr_day IS NOT NULL AND l.use_apr_day != ''
            THEN LEFT(l.use_apr_day, 4)::INTEGER
        WHEN f.approval_date IS NOT NULL AND f.approval_date != ''
            THEN LEFT(f.approval_date, 4)::INTEGER
        ELSE NULL
    END AS built_year,

    l.enrgy_eff_rate AS energy_grade,
    l.epi_score,

    -- 원형 분류
    CASE
        WHEN COALESCE(LEFT(l.use_apr_day, 4), LEFT(f.approval_date, 4), '1970')::INTEGER < 1980
            THEN 'pre-1980'
        WHEN COALESCE(LEFT(l.use_apr_day, 4), LEFT(f.approval_date, 4), '1970')::INTEGER <= 2000
            THEN '1980-2000'
        WHEN COALESCE(LEFT(l.use_apr_day, 4), LEFT(f.approval_date, 4), '1970')::INTEGER <= 2010
            THEN '2001-2010'
        ELSE 'post-2010'
    END AS vintage_class,

    CASE
        WHEN COALESCE(l.tot_area, 0) < 500 THEN 'small'
        WHEN COALESCE(l.tot_area, 0) < 3000 THEN 'medium'
        ELSE 'large'
    END AS size_class,

    -- 구조 분류
    CASE
        WHEN l.strct_nm LIKE '%철근콘크리트%' OR l.strct_nm LIKE '%RC%' THEN 'RC'
        WHEN l.strct_nm LIKE '%철골%' OR l.strct_nm LIKE '%S조%' THEN 'steel'
        WHEN l.strct_nm LIKE '%조적%' OR l.strct_nm LIKE '%벽돌%' OR l.strct_nm LIKE '%목%' THEN 'masonry'
        ELSE 'RC'
    END AS structure_class

FROM building_footprints f
LEFT JOIN building_ledger l
    ON f.pnu = l.pnu
    AND (
        f.dong_nm = l.dong_nm
        OR f.dong_nm IS NULL
        OR l.dong_nm IS NULL
        OR f.dong_nm = ''
        OR l.dong_nm = ''
    )
WHERE f.geom IS NOT NULL
  AND ST_IsValid(f.geom);

-- 인덱스
CREATE UNIQUE INDEX IF NOT EXISTS idx_enriched_gid ON buildings_enriched(gid);
CREATE INDEX IF NOT EXISTS idx_enriched_geom ON buildings_enriched USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_enriched_pnu ON buildings_enriched(pnu);
CREATE INDEX IF NOT EXISTS idx_enriched_usage ON buildings_enriched(usage_type);
CREATE INDEX IF NOT EXISTS idx_enriched_grade ON buildings_enriched(energy_grade);
CREATE INDEX IF NOT EXISTS idx_enriched_vintage ON buildings_enriched(vintage_class);
