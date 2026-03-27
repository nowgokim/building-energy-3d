-- Migration: building_adjacency 테이블에 spread_weight 컬럼 추가
-- Phase F2 화재 확산 BFS에 필요한 전파 가중치
-- 실행: docker compose exec db psql -U postgres -d buildings -f /path/to/migration_adjacency_v2.sql

ALTER TABLE building_adjacency
    ADD COLUMN IF NOT EXISTS spread_weight REAL DEFAULT 0.0;

COMMENT ON COLUMN building_adjacency.spread_weight IS
    '화재 전파 가중치 (0~1.0): (1 - dist/25) × 구조계수. BFS 확산 확률 계산에 사용';

COMMENT ON TABLE building_adjacency IS
    'Phase F1/F2: 25m 이내 인접 건물 관계 그래프. 양방향 저장 (source→target, target→source). build_adjacency.py로 생성.';

-- 기존 행 spread_weight 일괄 계산 (컬럼 추가 후 값이 0.0인 행 대상)
-- 공식: (1 - dist_m / 25) × 구조계수  (masonry=1.2, RC=0.8)
-- 두 건물 중 하나라도 masonry/목조이면 masonry 계수 적용
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

-- 인덱스: BFS frontier 조회 최적화 (source_pnu → 인접 이웃 목록)
CREATE INDEX IF NOT EXISTS idx_adjacency_source_spread
    ON building_adjacency (source_pnu)
    WHERE spread_weight > 0.0;
