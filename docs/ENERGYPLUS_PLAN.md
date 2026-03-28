# EnergyPlus 통합 계획

> **작성일**: 2026-03-28
> **상태**: 계획 단계 — 구현 착수 전 검토 필요

---

## 1. 문제 정의

### 1.1 현재 EUI 산출 방식의 한계

`src/simulation/archetypes.py`의 `ARCHETYPE_PARAMS`는 (용도, 연대, 구조) 3-키 조합 83개에 대한 EUI 값을 수작업으로 하드코딩한다.

```python
("office", "1980-2000", "RC"): {"ref_total": 170.0, ...}  # 근거 불투명
```

**문제점**:

| 항목 | 현황 | 목표 |
|------|------|------|
| EUI 근거 | 문헌 기반 수작업 추정 | EnergyPlus 시뮬 통계 |
| 불확도 표현 | 없음 | P10/P90 구간 |
| 지역 보정 | 없음 (전국 단일값) | 도시별 기후 반영 |
| 연대 세분화 | 4단계 | 5단계 |
| 검증 방법 | 없음 | 실측 89건 교차검증 |

### 1.2 해결 목표

766K 건물 각각에 대해 **EnergyPlus 시뮬레이션 통계 기반 EUI**를 할당한다.
아키타입 룩업 방식은 유지하되, 룩업 테이블의 값을 시뮬 통계로 교체.

---

## 2. 보유 데이터 분석

### 2.1 Korean_BB — `8.simulation/Korean_BB/simulations/npy_tier_a/`

**특징**: 건물 에너지 성능 특성화 (빈티지별 기준 EUI)

| 항목 | 내용 |
|------|------|
| 총 시뮬 수 | 146,110건 |
| 아키타입 | 14종 (`apartment_highrise`, `apartment_midrise`, `hospital`, `hotel`, `large_office`, `office`, `restaurant_full`, `restaurant_quick`, `retail`, `school`, `small_office`, `strip_mall`, `university`, `warehouse`) |
| 빈티지(vintage) | 5단계: `v1_pre1990` / `v2_1991_2000` / `v3_2001_2010` / `v4_2011_2017` / `v5_2018_plus` |
| 도시 | 5개: `seoul`, `busan`, `daegu`, `gangneung`, `jeju` |
| 고유 조합 | 350개 (14 × 5 × 5) |
| 샘플/조합 | 평균 418건 (12D LHS 파라메트릭) |
| 출력 변수 | `hourly_heating`, `hourly_cooling`, `hourly_electricity`, `hourly_gas` 등 17종 `.npy` |
| EMS | 없음 (기준 건물 성능만) |

**장점**: 빈티지별 EUI 변화 정량화 가능 (핵심 차별점)

**한계**: 5개 도시만 커버, 10개 도시 미포함 (인천·광주·대전·청주·울산)

### 2.2 ems_transformer — `8.simulation/ems_transformer/gcs_download/npy/`

**특징**: EMS 전략별 에너지 절감 효과 정량화

| 항목 | 내용 |
|------|------|
| 총 시뮬 수 | 936,097건 (GCS) + 20K (로컬) |
| 아키타입 | 15종 (`MediumOffice` 추가, 빈티지 구분 없음) |
| EMS 전략 | E0(기준) + E1~E10 (10종 전략) |
| 도시 | **10개**: `Seoul`, `Busan`, `Daegu`, `Incheon`, `Gwangju`, `Daejeon`, `Cheongju`, `Gangneung`, `Ulsan`, `Jeju` |
| E0 시뮬 수 | **446,624건** (기준선 = EMS 미적용) |
| 출력 변수 | `hourly_electricity`, `hourly_temperature` (Korean_BB 대비 제한적) |
| 빈티지 | 없음 — 파라메트릭 파라미터로 부분 대리 가능 (`envelope_factor`) |

**장점**: 10개 도시 커버, 대규모 E0 기준선 446K건

**한계**: 빈티지별 EUI 직접 추출 불가, 출력 변수 제한적

### 2.3 두 데이터소스 비교

| 비교 항목 | Korean_BB | ems_transformer |
|----------|-----------|-----------------|
| 빈티지 구분 | ✅ 5단계 핵심 | ❌ 없음 |
| 도시 수 | ⚠️ 5개 | ✅ 10개 |
| 시뮬 수 | 146K | 956K |
| EMS 절감 | ❌ | ✅ E1~E10 |
| 출력 상세도 | ✅ 17종 변수 | ⚠️ 2종 |
| 상태 | ✅ 완료 | ⚠️ 계획/진행 중 |

---

## 3. 통합 전략

### 3.1 EUI 산출 공식

```
EUI(건물) = EUI_base(archetype, city) × vintage_factor(vintage)
```

- **EUI_base**: ems_transformer E0 통계 → 아키타입 × 10개 도시 기준 EUI
- **vintage_factor**: Korean_BB vintage별 상대 비율 → `v1_pre1990` 대비 각 연대 보정계수

예시:
```
EUI_base(LargeOffice, Seoul)     = 185.3 kWh/m²/yr  ← ems_transformer E0 중앙값
vintage_factor(v1_pre1990)       = 1.42               ← Korean_BB: pre1990 / v3_2001_2010 비율
vintage_factor(v3_2001_2010)     = 1.00  (기준)
vintage_factor(v5_2018_plus)     = 0.61

→ LargeOffice, Seoul, pre-1980:  185.3 × 1.42 = 263 kWh/m²/yr
→ LargeOffice, Seoul, 2001-2010: 185.3 × 1.00 = 185 kWh/m²/yr
```

### 3.2 EMS 절감 잠재량 (별도 기능)

```
savings_pct(archetype, city, ems_strategy) = (EUI_E0 - EUI_En) / EUI_E0 × 100
```

→ 클릭한 건물에 대해 "EMS E2 적용 시 XX% 절감 가능" 패널 표시 (building-energy-3d Phase 4~5)

---

## 4. 구현 단계

### Phase A — Korean_BB에서 빈티지 보정계수 추출

**전제 조건**: Korean_BB `npy_tier_a` 디렉토리 접근 가능 (현재 로컬)

**작업**:
1. 각 시뮬 디렉토리에서 `hourly_heating + hourly_cooling + hourly_electricity + hourly_gas` 합산 → annual total energy (kWh)
2. `metadata.json`의 `archetype`, `vintage`, `city` 추출
3. 아키타입 × 빈티지 × 도시별 EUI 중앙값 집계
4. 빈티지 보정계수 = `EUI(vintage) / EUI(v3_2001_2010)` (v3을 기준 1.0으로)
5. 출력: `src/simulation/vintage_factors.py` 자동 생성

**출력 형식**:
```python
# AUTO-GENERATED from Korean_BB npy_tier_a (146K sims)
VINTAGE_FACTORS: dict[tuple[str, str], float] = {
    ("apartment_highrise", "v1_pre1990"):  1.48,
    ("apartment_highrise", "v2_1991_2000"): 1.19,
    ("apartment_highrise", "v3_2001_2010"): 1.00,  # 기준
    ("apartment_highrise", "v4_2011_2017"): 0.78,
    ("apartment_highrise", "v5_2018_plus"): 0.58,
    ...
}
```

**핵심 파일**: `src/data_ingestion/extract_vintage_factors.py`
**즉시 착수 가능**: Korean_BB npy 로컬에 있음

---

### Phase B — ems_transformer E0에서 도시별 기준 EUI 추출

**전제 조건**: ems_transformer gcs_download 디렉토리 접근 가능 (로컬 936K건)

**작업**:
1. `all_metadata.jsonl`에서 `ems == "E0"` 필터 (446K건)
2. `building` + `city` 조합별 `hourly_electricity.npy` 합산 → annual EUI
3. 중앙값/P10/P90 집계
4. 출력: `src/simulation/city_eui_base.py` 자동 생성

**출력 형식**:
```python
# AUTO-GENERATED from ems_transformer E0 (446K sims)
CITY_EUI_BASE: dict[tuple[str, str], dict] = {
    ("LargeOffice", "Seoul"): {"median": 185.3, "p10": 142.1, "p90": 241.7, "n": 8234},
    ("LargeOffice", "Busan"): {"median": 179.8, ...},
    ...
}
```

**핵심 파일**: `src/data_ingestion/extract_city_eui_base.py`
**주의**: ems_transformer는 아직 계획/진행 단계 — 데이터 구조 최종 확정 필요

---

### Phase C — archetypes.py 통합 교체

**전제 조건**: Phase A, B 완료

**작업**:
1. `ARCHETYPE_PARAMS`의 `ref_total` → Phase A×B 결합 공식으로 교체
2. 아키타입 매핑 함수 추가 (building-energy-3d 용도 → Korean_BB/ems_transformer 명칭)
3. fallback 체인: `city_eui_base × vintage_factor` → `ARCHETYPE_PARAMS` (기존값)

**아키타입 매핑 테이블**:

| building-energy-3d `usage_type` | `floors_above` | Korean_BB | ems_transformer |
|--------------------------------|---------------|-----------|-----------------|
| `apartment` | ≥10 | `apartment_highrise` | `Apartment` |
| `apartment` | 4~9 | `apartment_midrise` | `Apartment` |
| `apartment` | 1~3 | `apartment_midrise` | `Apartment` |
| `residential_single` | — | `apartment_midrise` *(최근접)* | `Apartment` |
| `office` | ≥10 or ≥15K m² | `large_office` | `LargeOffice` |
| `office` | 중간 | `office` | `MediumOffice` |
| `office` | <3K m² | `small_office` | `SmallOffice` |
| `retail` | 단독 | `retail` | `Retail` |
| `retail` | 복합 | `strip_mall` | `RetailStripmall` |
| `education` | 초중고 | `school` | `School` |
| `education` | 대학 | `university` | `School` *(매핑)* |
| `hospital` | — | `hospital` | `Hospital` |
| `warehouse` | — | `warehouse` | `Warehouse` |
| `hotel` | — | `hotel` | `Hotel` |
| `cultural`, `mixed_use`, `미분류` | — | `office` *(fallback)* | `MediumOffice` |

**빈티지 매핑 테이블**:

| building-energy-3d | Korean_BB | 비고 |
|-------------------|-----------|------|
| `pre-1980` | `v1_pre1990` | |
| `1980-2000` | `v2_1991_2000` | |
| `2001-2010` | `v3_2001_2010` | 기준(factor=1.0) |
| `post-2010`, `built_year < 2018` | `v4_2011_2017` | |
| `post-2010`, `built_year ≥ 2018` | `v5_2018_plus` | `built_year` 필드 필요 |

---

### Phase D — DB 마이그레이션 및 배치 업데이트

**전제 조건**: Phase C 완료

**작업**:
1. `energy_results`에 `eui_source`, `eui_p10`, `eui_p90`, `eui_n_samples` 컬럼 추가
2. 배치 업데이트 스크립트 → 766K 건물 전체에 새 EUI 적용
3. `buildings_enriched` MV REFRESH

---

## 5. 파일 목록

| 파일 | Phase | 유형 |
|------|-------|------|
| `src/data_ingestion/extract_vintage_factors.py` | A | 신규 스크립트 |
| `src/simulation/vintage_factors.py` | A | 자동 생성 (커밋 여부 결정 필요) |
| `src/data_ingestion/extract_city_eui_base.py` | B | 신규 스크립트 |
| `src/simulation/city_eui_base.py` | B | 자동 생성 |
| `src/simulation/archetypes.py` | C | 수정 |
| `db/migration_energyplus_eui_v1.sql` | D | 신규 |
| `src/data_ingestion/update_eui_from_table.py` | D | 신규 스크립트 |
| `tests/unit/test_vintage_factors.py` | A | 신규 테스트 |
| `tests/unit/test_archetype_mapping.py` | C | 신규 테스트 |

---

## 6. 의존성 및 순서

```
Phase A (Korean_BB 추출)    ─┐
                              ├──→ Phase C (archetypes.py 교체) ──→ Phase D (DB)
Phase B (ems_transformer 추출)─┘
```

- **Phase A**: 즉시 착수 가능 (Korean_BB npy 로컬 완비)
- **Phase B**: ems_transformer 데이터 구조 확정 후 착수 (현재 계획 단계)
- Phase A만 완료해도 빈티지 보정계수 적용 가능 → 부분 개선 효과 있음

---

## 7. 미결 사항

| 항목 | 내용 | 결정 필요 |
|------|------|----------|
| ems_transformer npy EUI 단위 | `hourly_electricity`만 있음 — 가스 없음. 전기 EUI만 사용할지, Korean_BB로 보완할지 | 설계 결정 |
| `eui_table` git 커밋 여부 | `npy_tier_a` 경로 의존 — CI에서 재생성 불가 | 커밋 or 빌드 시 생성 |
| 서울 외 지역 도시 매핑 | 경기도 건물의 기후 도시 기본값 | 서울 or 가장 가까운 도시 |
| `residential_single` 갭 | Korean_BB에 단독주택 아키타입 없음 | `apartment_midrise` 대리 허용 여부 |
| Phase B 착수 시점 | ems_transformer 데이터 구조 확정 필요 | ems_transformer 프로젝트 일정 연동 |

---

*관련 문서: [`src/simulation/archetypes.py`](../src/simulation/archetypes.py) · [`docs/RFC-ENERGY-SIMULATION.md`](RFC-ENERGY-SIMULATION.md)*
