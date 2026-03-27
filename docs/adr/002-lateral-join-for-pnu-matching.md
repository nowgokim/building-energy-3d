# ADR-002: buildings_enriched — 2단계 LATERAL JOIN으로 PNU 매칭

**상태**: 확정
**결정일**: 2026-03-22
**결정자**: 프로젝트 팀

---

## 결정

`buildings_enriched` Materialized View는 건축물대장을 **2단계 LATERAL JOIN**으로 조인한다.

```sql
-- l_energy: 에너지등급 있는 행 (총괄표제부 우선)
LEFT JOIN LATERAL (...WHERE enrgy_eff_rate IS NOT NULL ORDER BY tot_area DESC LIMIT 1) l_energy ON true
-- l_best: 구조/층수 정보 (지상층수 높은 표제부)
LEFT JOIN LATERAL (...ORDER BY grnd_flr_cnt DESC, tot_area DESC LIMIT 1) l_best ON true
```

## 근거

- 단순 `LEFT JOIN energy_results ON pnu = pnu`: energy_results에 PNU당 복수 행 존재 → fan-out → 1000건 LIMIT 오작동
- 총괄표제부(에너지등급)와 표제부(구조/층수)는 별도 레코드 → 단일 LATERAL로 둘 다 잡을 수 없음
- LATERAL + LIMIT 1 패턴이 fan-out 없이 PNU당 최적 1건 보장

## 영향 파일

- `db/views.sql` — SSOT (실제 DDL)
- `docs/ARCHITECTURE.md §3.2.1` — 참조용 DDL (views.sql과 동기 필수)
- `src/visualization/search.py` `_filtered_rows()` — energy_results도 동일 LATERAL 패턴 사용

## 변경 조건

건축물대장 스키마 변경 또는 에너지등급 데이터 구조 변경 시 재검토.

## 주의

`energy_results` 테이블은 별도 `LEFT JOIN LATERAL (...LIMIT 1)` 패턴 사용 (search.py). 단순 JOIN으로 되돌리면 count 오류 재발.
