# Phase 4 — EnergyPlus 연동 및 예측 모델 계획

> **최초 작성**: 2026-03-28 | **최종 갱신**: 2026-03-29
> **상태**: Phase A/A' 완료, Phase B 착수 대기, Phase C~E 설계 완료

---

## 1. 현재 상태 (2026-03-29)

### 완료된 작업

| 항목 | 파일 | 내용 |
|------|------|------|
| Korean_BB EUI 룩업 | `src/simulation/eui_lookup.py` | 350개 조합 (14×5×5) 자동 생성 ✅ |
| Tier1 보정계수 | `src/simulation/calibration_factors.py` | 실측 89건 기반 용도별 계수 ✅ |
| 770K 건물 EUI 적용 | `src/data_ingestion/populate_korean_bb_eui.py` | Tier4 COPY bulk 완료 ✅ |
| ML XGBoost | `src/simulation/ml_xgboost.py` | Tier A/B 예측 (StratifiedKFold, P10/P90) ✅ |
| 시계열 단기예측 | `src/simulation/ml_timeseries.py` | Tier C lag features ✅ |

### 현재 EUI 커버리지

| Tier | 건수 | 방식 |
|------|------|------|
| Tier 1 | 90건 | 실측 직접 입력 |
| Tier 2 | 3,588건 | KEA 인증등급 → EUI 변환 |
| Tier 4 | 645,241건 | Korean_BB EUI × Tier1 보정계수 |
| Tier 4 fallback | 1,531건 | archetype ARCHETYPE_PARAMS 기본값 |

---

## 2. EUI 산출 로직 (현재)

```
EUI(건물) = EUI_lookup(archetype, vintage, city) × calibration_factor(usage_type)
```

- **EUI_lookup**: Korean_BB 350개 조합 중앙값 (`eui_lookup.py`)
- **calibration_factor**: Tier1 실측 89건으로 보정 (`calibration_factors.py`)
- **city 매핑**: 서울 기준 (경기/인천 → 서울 폴백, 지방 5개 도시 직접 매핑)
- **archetype 매핑**: `src/simulation/archetypes.py` `_map_to_korean_bb()` 함수

---

## 3. Phase B — ems_transformer E0 도시별 기준 EUI (다음 착수)

### 목적

Korean_BB는 5개 도시만 커버. ems_transformer E0 446K건에서 10개 도시 × 15개 아키타입 기준 EUI를 추출하면:
- 서울/부산/대구/인천/광주/대전/청주/강릉/울산/제주 도시별 기후 차이 반영
- 현재 경기도 건물의 "서울 폴백" 오차 개선

### 전제 조건 체크

| 항목 | 경로 | 상태 |
|------|------|------|
| ems_transformer E0 npy | `8.simulation/ems_transformer/gcs_download/npy/` | 로컬 확인 필요 |
| all_metadata.jsonl | 동 경로 | 로컬 확인 필요 |
| ems_transformer CLAUDE.md | `8.simulation/ems_transformer/CLAUDE.md` | 데이터 구조 확인 필요 |

**착수 전 확인 명령:**
```bash
ls "E:/projects/ems_transformer/gcs_download/npy/" | head -5
wc -l "E:/projects/ems_transformer/gcs_download/all_metadata.jsonl"
```

### 출력 목표

```python
# AUTO-GENERATED from ems_transformer E0 (446K sims)
# src/simulation/city_eui_base.py
CITY_EUI_BASE: dict[tuple[str, str], dict] = {
    ("LargeOffice", "Seoul"):  {"median": 185.3, "p10": 142.1, "p90": 241.7, "n": 8234},
    ("LargeOffice", "Busan"):  {"median": 179.8, ...},
    ("Apartment", "Incheon"):  {"median": 112.4, ...},
    ...  # 15 archetypes × 10 cities = 150개 조합
}
```

---

## 4. Phase C — 아키타입 확장 (83 → 120종)

### 현재 83종 = 9 용도 × 4 연대 × 2~3 구조

부족한 세분화:
- **지역난방 공동주택**: 서울·인천 고층 아파트 → 난방 방식이 근본적으로 다름
- **온돌 단독주택**: 바닥복사난방 에너지 특성
- **데이터센터/물류**: 24시간 냉방 중심 — `warehouse` 대리 부정확
- **복합용도**: 주거+상업 혼합 건물 (서울 다세대 주거 빈도 높음)

### 확장 계획

| 추가 아키타입 | 기존 | 개선 |
|-------------|------|------|
| `apartment_district_heating` | `apartment_highrise` | 지역난방 계수 보정 |
| `apartment_ondol` | `apartment_midrise` | 온돌 바닥난방 |
| `datacenter` | `warehouse` | PUE 기반 EUI |
| `mixed_residential_commercial` | `office` fallback | 혼합용도 |

---

## 5. Phase D — EnergyPlus 실 연동

### 목적

현재 EUI는 Korean_BB 시뮬 결과를 집계한 "통계 룩업" 방식. Phase D는 **개별 건물 IDF 생성 → 시뮬 실행 → 결과 저장** 파이프라인.

### 범위

- **대상**: Tier 1~2 실측/인증 건물 + 리트로핏 시나리오 분석 요청 건물
- **도구**: geomeppy (Python IDF 편집) + EnergyPlus 24.1
- **실행 환경**: RTX 4090 서버 로컬 (Celery worker GPU task)

### 아키텍처

```
건물 PNU → buildings_enriched 파라미터 추출
         → geomeppy IDF 생성 (footprint + 높이 + 용도 + 구조)
         → EnergyPlus 실행 (8760시간)
         → 결과 파싱 → energy_results (Tier 3)
         → buildings_enriched MV REFRESH
```

### 단계별 작업

| 단계 | 작업 | 예상 소요 |
|------|------|---------|
| D-1 | geomeppy + EnergyPlus Docker 이미지 준비 | 1일 |
| D-2 | `src/simulation/energyplus_runner.py` 작성 | 2일 |
| D-3 | 10개 건물 파일럿 시뮬 + 실측 교차검증 | 1일 |
| D-4 | Celery task + `energy_results` Tier 3 저장 | 1일 |
| D-5 | API 엔드포인트 `/api/v1/simulate/{pnu}` | 1일 |

---

## 6. Phase E — ML 예측 모델 고도화

### 현재 상태

- `ml_xgboost.py`: Tier A/B 연간 EUI 예측 (StratifiedKFold, 5개 특징)
- `ml_timeseries.py`: Tier C 단기 시계열 예측 (lag features)

### Phase E 목표

| 항목 | 현재 | 목표 |
|------|------|------|
| 특징 수 | 5개 | 20개 (기후/시간/용도 원핫) |
| 데이터 | Tier1 90건 | Tier1+2+C (수백~수천 건) |
| 시간 단위 | 연간 | 일단위 + 시간단위 |
| 모델 | XGBoost | XGBoost → LSTM → (선택) PatchTST |

**착수 조건**: Tier C 계량기 데이터 12개월 이상 축적 시

---

## 7. 우선순위 및 의존성

```
Phase B (ems_transformer E0 추출)
  ↓ 완료 후
Phase C (아키타입 확장 83→120)
  ↓ 완료 후
Phase D-1 (EnergyPlus Docker)
  ↓ 완료 후
Phase D-2~5 (시뮬 파이프라인)
  ↓ (병렬 가능)
Phase E (ML 고도화) — Tier C 데이터 12개월 후 착수
```

**즉시 착수 가능:**
- Phase B: ems_transformer 로컬 데이터 확인 후 착수
- Phase D-1: geomeppy/EnergyPlus Docker 환경 구성

---

## 8. 미결 사항

| 항목 | 내용 | 결정 필요 |
|------|------|----------|
| ems_transformer 데이터 경로 | GCS vs 로컬 | ems_transformer 프로젝트 확인 |
| EnergyPlus 버전 | 24.1 권장 (최신 안정) | Docker 이미지 선택 |
| 지역난방 요금 체계 | EUI → 비용 환산 시 필요 | 한국지역난방공사 API |
| `residential_single` 갭 | Korean_BB에 단독주택 아키타입 없음 | `apartment_midrise` 대리 허용 확정 필요 |
| Tier C 데이터 충분성 | ML Phase E 착수 조건 | 12개월 → 2027-03 이후 |

---

*관련 파일: `src/simulation/archetypes.py` · `src/simulation/eui_lookup.py` · `docs/RFC-ENERGY-SIMULATION.md`*
