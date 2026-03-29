# Known Issues — 미해결 과제

> 이 문서는 데이터 품질 문제와 미해결 기술 과제를 추적한다.
> 해결 시 해당 항목을 ~~취소선~~ 처리하고 해결 날짜를 기재한다.

---

## KI-001: 미분류 건물 187,000건 (용도 '미분류')

**발견일**: 2026-03-27
**심각도**: 중간 (에너지 시뮬레이션 정확도에 영향)
**현재 상태**: ⚠️ 부분 미해결

### 현황

| 원인 | 건수 | 해결 가능 여부 |
|------|------|--------------|
| 서울 미등록 건물 (건축물대장 없음) | ~116,000 | ❌ 데이터 근본 부재 |
| 경기 부천시 미수집 (KI-002 참조) | ~70,000 | ✅ 수집 스크립트 수정으로 해결 |
| 인천 경계부 건물 | ~1,000 | △ 추후 인천 수집 시 해결 |

### 서울 116K 미등록 건물 상세

VWorld footprint에는 포함되어 있으나 건축물대장(data.go.kr)에 등록되지 않은 건물.
주로 무허가 건물, 가건물, 임시시설 등이 해당. **API로는 해결 불가**, 이 건물들은
`usage_type='미분류'`, `vintage_class='pre-1980'` (기본값) 으로 처리된다.

### 영향

- `buildings_enriched` 뷰의 `usage_type='미분류'` 건물 → 에너지 아키타입이 기본값 적용
- 필터 UI의 용도별 통계 왜곡
- 에너지 등급 없음, EUI 추정값 신뢰도 낮음

### 완화 조치 (적용됨)

- `l_parent` LATERAL JOIN fallback: PNU 부번 불일치(`...0065` → `...0000`) 처리 — 309K → 187K 개선
- MV REFRESH로 103K 개선
- 부천시 수집 추가 예정 (KI-002)

---

## KI-002: 부천시/경기도 건축물대장 미수집

**발견일**: 2026-03-27
**심각도**: 높음
**현재 상태**: ⚠️ 부분 해결 (2026-03-28)

### 실태 조사 결과 (2026-03-28)

VWorld footprint DB에 경기도 건물 약 **175K건** 포함. 구성:

| 구분 | 건수 | API 가용 | 해결 방법 |
|------|------|---------|---------|
| 부천시 (41190/41195/41197/41199) | ~3.2K footprint | ❌ | 영구 데이터 갭 (아래 설명) |
| 기타 경기도 (성남/광명/구리/안산 등) | ~135K | ✅ | `collect_gyeonggi_ledger()` |

#### 부천시 API 완전 불가 — 영구 데이터 갭

실증 테스트 결과 (`2026-03-28`):
- `sigunguCd=41190` (부천시 직속): `totalCount=0`
- `sigunguCd=41195/41197/41199` (구 행정구): `totalCount=0`
- 동일 API로 성남시(41131), 광명시(41281) 등은 정상 반환 확인

**원인**: 2016년 부천시 일반구 폐지 시 국토부 건축물대장 DB 마이그레이션 불완전.
`getBrRecapTitleInfo` API가 부천시 코드에 대해 데이터를 반환하지 않음.

#### 부천시 공장 파일 임포트 완료 (2026-03-28)

data.go.kr 데이터셋 15144153 ("경기도 부천시_건축물대장_20250627")을 Playwright로 다운로드.
- 원본: 12,798행 (공장 건물만) → 중복 제거 후 2,302건 → `building_ledger` 적재 완료
- 부천 footprint(3,161건) 중 191건(6%) 이제 용도 '공장'으로 분류됨
- 나머지 2,970건은 주택·상업 등 비공장 건물로 이 데이터셋에 포함 안 됨
- **근본적 해결 불가**: 종합 건축물대장 파일 데이터 미공개, API 영구 갭 지속

재적재 명령:
```bash
docker compose exec api python -m src.data_ingestion.import_bucheon_file \
  --file scratch/bucheon_ledger.csv
docker compose exec db psql -U postgres -d buildings -c \
  "REFRESH MATERIALIZED VIEW buildings_enriched;"
docker compose exec db psql -U postgres -d buildings -c \
  "REFRESH MATERIALIZED VIEW building_fire_risk;"
```

#### 기타 경기도 수집 완료 (2026-03-28)

DB의 footprint PNU에서 직접 (sigungu, bdong) 조합 추출 → API 수집 실행.
160개 조합, 총괄표제부 + 표제부 수집.

---

## KI-003: 이상 데이터 (데이터 품질)

**발견일**: 2026-03-27
**심각도**: 낮음
**현재 상태**: ⚠️ 미해결

| 항목 | 건수 | 내용 |
|------|------|------|
| `built_year < 1900 OR > 2026` | 46건 | 오기입 추정. 에너지 아키타입 기본값 적용 중 |
| `height = 683m` | 1건 | PNU 4121010300107640001. GIS 원본 오류 추정 |
| KEA 인증 건물 co2_kg_m2 NULL | 62건 | 에너지 사용량 미등록 또는 계산 오류 |

### 완화 조치 (미적용)

- `built_year` 범위 제한: views.sql에서 `1900 <= built_year <= 2030` CASE 추가
- `height` 상한 제한: `LEAST(height, 650.0)` (롯데타워 555m가 국내 최고)
- KEA co2 NULL: `energy_results` 재계산 Celery task 실행

---

## ~~KI-004: filter-result-badge z-index 겹침~~

**발견일**: 2026-03-27
**심각도**: 낮음 (UI 표시 문제)
**현재 상태**: ✅ 해결 (2026-03-28)

`#filter-result-badge` z-index: 99 → 101로 수정. `vworld.html` 적용 완료.

---

## ~~KI-005: kea_cert 음수 EUI 3건~~

**발견일**: 2026-03-28
**심각도**: 낮음 (3건, 데이터 품질)
**현재 상태**: ✅ 해결 (2026-03-28)

Tier 2 음수 EUI 3건 DELETE → Korean_BB calibrated Tier 4로 재삽입.

| PNU | 구 EUI | 신 EUI | 신 유형 |
|-----|--------|--------|--------|
| 1138010300100980007 | -80.9 | 172.5 | korean_bb_calibrated |
| 1168010300100140000 | -28.1 | 64.0  | korean_bb_calibrated |
| 1135010600101070054 | -0.2  | 58.0  | korean_bb_calibrated |
