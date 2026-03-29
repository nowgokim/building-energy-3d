-- migration_concurrently_v1.sql
-- MV CONCURRENTLY REFRESH를 위한 UNIQUE INDEX 추가
-- 실행: docker compose exec db psql -U postgres -d buildings -f /docker-entrypoint-initdb.d/migration_concurrently_v1.sql
--
-- 주의: UNIQUE INDEX 생성은 테이블 잠금 없이 진행되나 대용량(766K)은 수 분 소요.
--       서비스 중 실행 가능.

\echo '=== migration_concurrently_v1: CONCURRENTLY UNIQUE INDEX 추가 ==='

-- 1. buildings_enriched: gid 기준 UNIQUE (각 footprint → 1행 보장)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_enriched_gid_uniq
    ON buildings_enriched(gid);
\echo 'buildings_enriched UNIQUE INDEX 완료'

-- 2. building_fire_risk: gid 기준 UNIQUE
--    (pnu는 동일 단지 내 복수 동이 공유 → gid 사용)
--    ※ fire_risk.sql이 gid 컬럼을 포함해야 함 (재생성 시 자동 포함)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_fire_risk_gid_uniq
    ON building_fire_risk(gid);
\echo 'building_fire_risk UNIQUE INDEX 완료'

-- 3. model_accuracy_summary: 이미 migration_provenance_v1.sql에서 생성됨
--    중복 실행 방어
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_accuracy_uniq
    ON model_accuracy_summary (model_version_id, temporal_scale, target_variable);
\echo 'model_accuracy_summary UNIQUE INDEX 확인 완료'

-- 검증
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename IN ('buildings_enriched', 'building_fire_risk', 'model_accuracy_summary')
  AND indexname LIKE '%uniq%'
ORDER BY tablename, indexname;

\echo '=== 마이그레이션 완료 ==='
