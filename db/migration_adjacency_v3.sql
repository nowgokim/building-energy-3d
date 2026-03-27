-- Migration v3: building_adjacency 단방향 → 양방향 보정
-- BFS는 source_pnu로만 이웃 조회 → 역방향 행이 없으면 절반 이상 누락
-- 실행: docker compose exec db psql -U postgres -d buildings -f /migrations/migration_adjacency_v3.sql
-- 예상 소요: 3.5M 행 기준 약 2~5분

BEGIN;

-- 역방향 행 삽입 (이미 존재하면 upsert)
INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m, spread_weight)
SELECT
    target_pnu AS source_pnu,
    source_pnu AS target_pnu,
    distance_m,
    spread_weight
FROM building_adjacency
ON CONFLICT (source_pnu, target_pnu) DO NOTHING;

-- spread_weight가 0.0인 행(v2 migration 미실행분) 값 채우기
UPDATE building_adjacency ba
SET spread_weight = GREATEST(0.0,
    (1.0 - ba.distance_m / 25.0)
    * CASE
        WHEN b_src.structure_class = 'masonry'
          OR b_tgt.structure_class = 'masonry'
        THEN 1.2
        ELSE 0.8
      END
)::REAL
FROM buildings_enriched b_src,
     buildings_enriched b_tgt
WHERE b_src.pnu = ba.source_pnu
  AND b_tgt.pnu = ba.target_pnu
  AND ba.spread_weight = 0.0;

-- BFS 최적화 인덱스
CREATE INDEX IF NOT EXISTS idx_adjacency_source_spread
    ON building_adjacency (source_pnu)
    WHERE spread_weight > 0.0;

COMMIT;
