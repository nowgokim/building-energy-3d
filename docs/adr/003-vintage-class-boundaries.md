# ADR-003: vintage_class 구간 정의

**상태**: 확정
**결정일**: 2026-03-25 (이전 RFC와 불일치 수정)
**결정자**: 프로젝트 팀

---

## 결정

`vintage_class`는 아래 4구간으로 확정한다. **DB와 archetype 코드가 동일한 값을 사용해야 매칭이 작동한다.**

| vintage_class 값 | 건축년도 범위 | 에너지 기준 배경 |
|-----------------|-------------|----------------|
| `pre-1980`      | ~ 1979      | 단열 기준 미적용 시대 |
| `1980-2000`     | 1980 ~ 2000 | 구 에너지절약설계기준 |
| `2001-2010`     | 2001 ~ 2010 | 에너지절약설계기준 개정 |
| `post-2010`     | 2011 ~      | 강화 기준 (현행) |

## SSOT

**`db/views.sql`** — 이 파일의 CASE 구문이 유일한 진실이다.

## 이전 버전과의 불일치 (수정 완료)

`docs/RFC-ENERGY-SIMULATION.md §2.1`이 이전에 `pre-2001 / 2001-2009 / 2010-2016 / 2017-present` 구간을 사용하고 있었다. 이 값으로 archetype 매칭하면 DB에서 아무 건물도 매칭되지 않는다. **2026-03-25에 수정 완료.**

## 영향 파일 (동시 수정 필수)

- `db/views.sql` — CASE 구문 (SSOT)
- `docs/RFC-ENERGY-SIMULATION.md §2.1` — 분류축 표
- `docs/RFC-ENERGY-SIMULATION.md §2.2` — U-value 매핑 표 (행 기준이 vintage_class와 일치해야 함)
- `src/simulation/archetypes.py` — archetype 매칭 로직 (Phase 4 구현 시)
- `docs/IMPACT.md` — vintage_class 행

## 변경 조건

한국 에너지절약설계기준 개정으로 주요 전환점이 추가될 경우 재검토. 단, DB 데이터 재처리(REFRESH MATERIALIZED VIEW) 필요.
