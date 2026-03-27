-- Migration v4: 크로스-SGG 인접 보정 (temp-table dedup 버전)
-- 1단계: DISTINCT ON으로 PNU당 1행 temp table 생성 + GiST 인덱스
-- 2단계: 서로 다른 SGG 건물 쌍 중 25m 이내인 것만 처리

-- 임시 dedup 테이블
CREATE TEMP TABLE _bldg_dedup AS
SELECT DISTINCT ON (pnu)
    pnu,
    geom::geography AS geog,
    structure_class
FROM buildings_enriched
WHERE pnu LIKE '11%'
  AND geom IS NOT NULL
ORDER BY pnu;

CREATE INDEX ON _bldg_dedup USING gist(geog);
ANALYZE _bldg_dedup;

-- 크로스-SGG 인접 upsert
BEGIN;

INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m, spread_weight)
WITH pairs AS (
    SELECT
        a.pnu  AS pnu_a,
        b.pnu  AS pnu_b,
        ST_Distance(a.geog, b.geog)::REAL AS dist,
        GREATEST(0.0,
            (1.0 - ST_Distance(a.geog, b.geog)::REAL / 25.0)
            * CASE
                WHEN a.structure_class = 'masonry'
                  OR b.structure_class = 'masonry'
                THEN 1.2
                ELSE 0.8
              END
        )::REAL AS weight
    FROM _bldg_dedup a
    JOIN _bldg_dedup b
      ON a.pnu < b.pnu
     AND LEFT(a.pnu, 5) <> LEFT(b.pnu, 5)
     AND ST_DWithin(a.geog, b.geog, 25.0)
)
SELECT pnu_a, pnu_b, dist, weight FROM pairs
UNION ALL
SELECT pnu_b, pnu_a, dist, weight FROM pairs
ON CONFLICT (source_pnu, target_pnu) DO UPDATE
  SET distance_m    = EXCLUDED.distance_m,
      spread_weight = EXCLUDED.spread_weight;

COMMIT;
