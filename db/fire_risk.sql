-- Phase F0: 화재 위험도 Materialized View
-- 추가 데이터 수집 없이 buildings_enriched 컬럼만 사용
-- 실행: docker compose exec db psql -U postgres -d buildings -f /docker-entrypoint-initdb.d/fire_risk.sql

DROP MATERIALIZED VIEW IF EXISTS building_fire_risk;

CREATE MATERIALIZED VIEW building_fire_risk AS
WITH scored AS (
    SELECT
        pnu,
        -- 구조 위험도 (40점): 목조 최고위험, RC/SRC 최저
        CASE
            WHEN structure_type LIKE '%목%'                                  THEN 40
            WHEN structure_type LIKE '%조적%' OR structure_type LIKE '%벽돌%' THEN 30
            WHEN structure_type LIKE '%철골%' OR structure_type LIKE '%S조%'  THEN 20
            ELSE 15  -- RC/SRC/기타
        END AS structure_score,

        -- 연령 위험도 (30점): 구건물 = 소방법 이전 설계
        CASE
            WHEN built_year IS NULL  THEN 25
            WHEN built_year < 1980   THEN 30
            WHEN built_year < 2000   THEN 20
            WHEN built_year < 2010   THEN 12
            ELSE                          5
        END AS age_score,

        -- 용도 위험도 (20점): 대피 취약 + 위험물 용도 가중
        CASE
            WHEN usage_type LIKE '%공장%'   OR usage_type LIKE '%창고%'    THEN 20
            WHEN usage_type LIKE '%위험물%'                                THEN 20
            WHEN usage_type LIKE '%위락%'                                  THEN 15
            WHEN usage_type LIKE '%노유자%' OR usage_type LIKE '%의료%'    THEN 18
            WHEN usage_type LIKE '%숙박%'   OR usage_type LIKE '%고시원%'  THEN 16
            WHEN usage_type LIKE '%교육%'   OR usage_type LIKE '%문화%'
              OR usage_type LIKE '%집회%'                                  THEN 14
            WHEN usage_type LIKE '%단독주택%'                              THEN 12
            WHEN usage_type LIKE '%종교%'   OR usage_type LIKE '%근린%'    THEN 12
            WHEN usage_type LIKE '%공동주택%' OR usage_type LIKE '%아파트%' THEN 10
            WHEN usage_type LIKE '%사무%'   OR usage_type LIKE '%업무%'
              OR usage_type LIKE '%판매%'                                  THEN  8
            ELSE                                                                10
        END AS usage_score,

        -- 층수 위험도 (10점): 4층 이상부터 대피 난이도 증가
        LEAST(GREATEST(COALESCE(floors_above, 3) - 3, 0) * 2, 10) AS height_score

    FROM buildings_enriched
    WHERE pnu IS NOT NULL
)
SELECT
    pnu,
    structure_score,
    age_score,
    usage_score,
    height_score,
    (structure_score + age_score + usage_score + height_score) AS total_score,
    CASE
        WHEN (structure_score + age_score + usage_score + height_score) >= 60 THEN 'HIGH'
        WHEN (structure_score + age_score + usage_score + height_score) >= 35 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS risk_grade
FROM scored;

CREATE INDEX ON building_fire_risk(pnu);
CREATE INDEX ON building_fire_risk(risk_grade);
ANALYZE building_fire_risk;
