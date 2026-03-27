-- Phase 4-A: energy_results.pnu UNIQUE constraint 추가
-- ON CONFLICT (pnu) DO UPDATE 를 위해 필요

-- 중복 pnu 있을 경우 최신 것만 남기고 삭제
DELETE FROM energy_results er
WHERE id NOT IN (
    SELECT MAX(id) FROM energy_results GROUP BY pnu
);

-- UNIQUE constraint 추가
ALTER TABLE energy_results
    ADD CONSTRAINT uq_energy_results_pnu UNIQUE (pnu);
