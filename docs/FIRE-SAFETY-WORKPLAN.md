# 화재 안전 확장 프로젝트 작업 계획서

**문서 버전**: 1.0
**작성일**: 2026-03-24
**관련 문서**: [Architecture](./ARCHITECTURE.md), [PRD](./PRD.md)

---

## 1. 프로젝트 개요

서울 전역 766K 건물 3D 플랫폼에 화재 안전 분석 기능을 단계적으로 추가한다. Phase F0에서 구축한 건물별 화재 위험 점수를 기반으로, 밀집도 분석·화재 확산 시뮬레이션·대피로 계산·실시간 기상 연동으로 기능을 확장한다.

플랫폼은 현재 빌딩 에너지 분석에 특화되어 있으나, 동일한 건물 데이터와 3D 뷰어 인프라를 재활용하여 화재 안전 분야로 영역을 넓힌다. 각 Phase는 독립 배포 가능하도록 설계하며, 이전 Phase의 데이터 구조에 의존하는 단계는 명시적으로 의존성을 표기한다.

---

## 2. 전체 로드맵

### 2.1 Phase 요약

| Phase | 명칭 | 기간 (추정) | 상태 | 주요 산출물 |
|-------|------|------------|------|------------|
| F0 | 건물 화재 위험 점수 | 완료 | ✅ DONE | `building_fire_risk` 뷰, 히트맵 API |
| F1 | 밀집도·클러스터·소방서 | 완료 | ✅ DONE | 클러스터 API, 소방서 25개 마커 |
| F2 | 화재 확산 시뮬레이션 | 완료 | ✅ DONE | BFS 엔진, Celery 태스크, 타임라인 애니메이션 UI |
| F3 | 대피로 계산 | 완료 | ✅ DONE | pgRouting Dijkstra, evacuation_points 35개 (25구) |
| F4 | 실시간 기상 연동 | 완료 | ✅ DONE | 기상청 API, /ws/weather WebSocket, Celery beat 1h |

### 2.2 Gantt 스타일 일정표

아래 표는 F0 완료 시점(2026-03-24)을 기준 Week 0으로 설정한다. 각 Phase의 시작 시점은 선행 Phase 완료를 전제로 한다.

```
Phase     W01  W02  W03  W04  W05  W06  W07  W08  W09  W10  W11  W12  W13  W14  W15  W16  W17  W18
──────────────────────────────────────────────────────────────────────────────────────────────────
F0 [완료] ████ (완료)
F1 [완료] ████ (완료)
F2        ████████████████████
F3                    ████████████
F4        (F2 시작 후) ────────────────────────████████
──────────────────────────────────────────────────────────────────────────────────────────────────
범례: ████ 개발  ────── 병행 가능 구간
```

> **현재 상태 (2026-03-27)**: F0~F4 전체 완료. 다음 단계: Phase 4 (에너지 예측 모델) 또는 Phase 5 (ZEB 전환 지도).

**주의사항**
- F2는 F1의 `building_adjacency` 테이블 완료 후 시작 가능하다.
- F3의 pgRouting 도로 네트워크 수집은 F1과 병행할 수 있으나, F2 완료 후 최종 통합한다.
- F4는 F2 완료 직후 병행 개발 가능하다 (확산 시뮬레이션 입력 파라미터를 기상 API가 자동 제공하는 구조이기 때문이다).

---

## 3. Phase F0 — 건물 화재 위험 점수 (완료)

### 3.1 완료 내역

| 항목 | 상세 | 완료일 |
|------|------|-------|
| `building_fire_risk` Materialized View | 구조·노후도·용도·높이 4개 요소 가중 점수 | 2026-03-24 |
| GET /api/v1/fire/risk/{pnu} | 건물 단건 위험 점수 조회 | 2026-03-24 |
| GET /api/v1/fire/risk?bbox | 영역 단위 위험 점수 목록 | 2026-03-24 |
| GET /api/v1/fire/stats | 구역별 통계 (평균, 고위험 건물 수 등) | 2026-03-24 |
| 프론트엔드 히트맵 토글 | 에너지 뷰 ↔ 화재 위험 뷰 전환 | 2026-03-24 |
| 건물 상세 패널 통합 | 클릭 시 화재 위험 점수 + 요소별 기여도 표시 | 2026-03-24 |

### 3.2 점수 산정 기준 (100점 척도)

실제 구현(db/fire_risk.sql) 기준 **최대 100점**:

| 요소 | 배점 | 세부 기준 |
|------|------|----------|
| 구조 위험도 | 40점 | 목조 40 / 조적·벽돌 30 / 철골·S조 20 / RC·SRC·기타 15 |
| 연령 위험도 | 30점 | 1980년 미만 30 / 2000년 미만 20 / 2010년 미만 12 / 이후 5 / 미상 25 |
| 용도 위험도 | 20점 | 공장·창고·위험물 20 / 노유자·의료 18 / 숙박·고시원 16 / 공동주택 10 / 사무 8 / 기타 10 |
| 층수 위험도 | 10점 | `MIN(MAX(floors_above - 3, 0) × 2, 10)` — 4층 이상부터 2점씩 증가, 상한 10점 |

**위험 등급 분류**: HIGH ≥ 60 / MEDIUM ≥ 35 / LOW < 35

*전체 쿼리는 db/fire_risk.sql 참조.*

---

## 4. Phase F1 — 밀집도·클러스터·소방서

### 4.1 목표

개별 건물 위험 점수에서 나아가, 건물 밀집 패턴과 소방 대응 가능성을 결합한 지역 단위 위험 평가 체계를 구축한다.

### 4.2 태스크 목록

| 번호 | 태스크 | 예상 소요 | 담당 영역 | 비고 |
|------|--------|----------|----------|------|
| F1-01 | `building_adjacency` 테이블 생성 (Celery 배치, 구역 분할) | 3일 | DB / 배치 | F2 핵심 의존성 |
| F1-02 | `fire_risk` 뷰에 밀집도 점수(+10pts) 추가 | 1일 | DB | F1-01 완료 후 |
| F1-03 | ST_ClusterDBSCAN 고위험 클러스터 탐지 + API | 2일 | DB / API | PostGIS 2.3+ 필요 |
| F1-04 | 서울 소방서·안전센터 데이터 수집 | 1일 | 데이터 수집 | 서울 열린데이터광장 |
| F1-05 | GET /api/v1/fire/stations | 1일 | API | 소방서 목록 + 위치 |
| F1-06 | GET /api/v1/fire/coverage/{pnu} | 2일 | API | 최근접 소방서 응답거리 |
| F1-07 | POST /api/v1/fire/retrofit (파라미터 기반 시뮬레이터) | 2일 | API | DB 변경 없음, 계산식만 |
| F1-08 | 프론트엔드: 클러스터 폴리곤 렌더링 | 2일 | Frontend | CesiumJS PolygonGraphics |
| F1-09 | 프론트엔드: 소방서 마커 + 응답시간 패널 | 1일 | Frontend | BillboardGraphics |

**예상 총 기간**: 2~3주

### 4.3 의존성

```
F1-01 완료 → F1-02, F1-03 시작 가능
F1-04 완료 → F1-05, F1-06 시작 가능
F1-08, F1-09는 F1-05 API 완료 후 시작 가능
F1-01 완료 → F2 시작 가능 (F1 전체 완료 불필요)
```

### 4.4 위험 요소

| 위험 | 영향 | 대응 방안 |
|------|------|----------|
| 소방서 데이터 정확도 | 안전센터 위치 오류 시 응답거리 부정확 | 수집 후 VWorld 지도에서 육안 교차검증 |
| DBSCAN 파라미터 튜닝 | eps·min_samples 기본값 부적합 시 클러스터 과분할 | 마포구 데이터로 파라미터 검증 후 전체 적용 |
| `building_adjacency` 계산 시간 | 766K 건물 전수 계산 시 수 시간 소요 가능 | 구(자치구) 단위 분할 처리, Celery 병렬 큐 활용 |

### 4.5 API 명세 (계획)

```
GET  /api/v1/fire/clusters?bbox={bbox}&min_risk={0-100}
     → [{ cluster_id, centroid, building_count, avg_risk, risk_level }]

GET  /api/v1/fire/stations?bbox={bbox}
     → [{ station_id, name, address, lat, lng, type }]

GET  /api/v1/fire/coverage/{pnu}
     → { pnu, nearest_station, distance_m, estimated_response_min }

POST /api/v1/fire/retrofit
     body: { pnu, measures: ["sprinkler", "fire_door", "insulation"] }
     → { original_score, projected_score, score_delta, cost_estimate_krw }
```

---

## 5. Phase F2 — 화재 확산 시뮬레이션

### 5.1 목표

특정 건물에서 화재 발생 시 인접 건물로의 확산 패턴을 시뮬레이션한다. 바람 방향·속도를 입력받아 BFS 기반 확산 알고리즘을 실행하고, 시간 단계별 피해 건물 목록을 반환한다. 프론트엔드에서는 CesiumJS 타임라인 애니메이션으로 확산 과정을 시각화한다.

### 5.2 태스크 목록

| 번호 | 태스크 | 예상 소요 | 담당 영역 | 비고 |
|------|--------|----------|----------|------|
| F2-01 | `building_adjacency` 검증 및 가중치 추가 (거리·소재·노출면) | 2일 | DB | ✅ DONE (3,554,436행 적재, spread_weight 포함) |
| F2-02 | SQL-backed BFS 엔진 (`fire_spread.py`) | 2일 | 시뮬레이션 | ✅ DONE (networkx 불필요, 온디맨드 DB 쿼리) |
| F2-03 | BFS 확산 알고리즘 구현 (바람 방향·속도 보정) | 3일 | 시뮬레이션 | ✅ DONE (cos 유사도 wind_factor) |
| F2-04 | `fire_scenario_results` 테이블 설계 및 생성 | 1일 | DB | ✅ DONE (fire_safety_f1.sql에 포함) |
| F2-05 | Celery async 태스크 래퍼 (`run_fire_scenario`) | 2일 | 인프라 | ✅ DONE |
| F2-06 | POST /api/v1/fire/scenario + GET /api/v1/fire/scenario/{id} | 2일 | API | ✅ DONE (+ /scenarios 목록 + /task/{id} 폴링) |
| F2-07 | 예상 피해 통계 계산 (건물 수, 연면적, 위험 등급별) | 1일 | API | ✅ DONE (stats JSONB 저장) |
| F2-08 | 프론트엔드: 바람 방향·속도 입력 UI | 2일 | Frontend | 대기 |
| F2-09 | CesiumJS TimeIntervalCollection 타임라인 애니메이션 | 3일 | Frontend | 대기 (색상 단계 변화 포함) |
| F2-10 | 피해 통계 패널 (사이드바) | 1일 | Frontend | 대기 |

**예상 총 기간**: 4~6주

### 5.3 알고리즘 설계

```
입력: 발화 건물 PNU, 바람_방향(0-360°), 바람_속도(m/s), 시뮬레이션_시간(분)

각 시간 단계 t:
  1. 현재 발화 건물 집합 B_t에서 인접 건물 후보 목록 추출
  2. 각 후보 건물에 대해 확산 확률 P 계산:
       P = base_prob(소재, 거리)
         × wind_factor(바람 방향과 인접 방향의 일치도)
         × risk_factor(building_fire_risk 점수 반영)
  3. P > threshold 이면 B_{t+1}에 추가
  4. 시간 단계별 발화 건물 집합 저장

출력: { step: int, buildings: [pnu], cumulative_count: int, area_m2: float }[]
```

### 5.4 의존성

```
F1-01 (building_adjacency 테이블 완료) → F2-01 시작 가능
F2-02, F2-03 완료 → F2-05, F2-06 진행
F2-06 완료 → F2-08, F2-09 진행
```

### 5.5 위험 요소

| 위험 | 영향 | 대응 방안 |
|------|------|----------|
| adjacency 계산 시간 | 766K 건물 전수 30m 이내 쌍 계산 시 수 시간 소요 | 구 단위 분할 + Celery 병렬 실행 + GIST 인덱스 최적화 |
| 확산 모델 검증 | 실제 화재 사례와 시뮬레이션 결과 괴리 | 서울 과거 화재 사례(소방청 공개 데이터) 3건 이상으로 back-test |
| 메모리 사용량 | 766K 노드 networkx 그래프 메모리 과다 | 청크 처리 또는 adjacency list만 DB에 유지하고 온디맨드 로드 |
| 시나리오 결과 저장 크기 | 단일 시나리오 결과가 수천 건물 × 수십 단계 | JSONB 압축 저장, 오래된 시나리오 자동 만료(TTL) |

---

## 6. Phase F3 — 대피로 계산

### 6.1 목표

발화 건물 또는 위험 구역에서 가장 가까운 피난 집결지까지의 최적 대피 경로를 계산한다. 도로 네트워크 기반 pgRouting을 활용하며, 프론트엔드에서 3D 화살표 경로로 시각화한다.

### 6.2 태스크 목록

| 번호 | 태스크 | 예상 소요 | 담당 영역 | 비고 |
|------|--------|----------|----------|------|
| F3-01 | Docker 이미지 교체: `postgis/postgis` → `pgrouting/pgrouting` | 1일 | 인프라 | DB 마이그레이션 필요 |
| F3-02 | OSM 서울 도로 데이터 다운로드 + osm2pgrouting 적재 | 2일 | 데이터 수집 | Geofabrik 한국 데이터 사용 |
| F3-03 | pgr_dijkstra 대피 경로 계산 함수 구현 | 2일 | DB / 시뮬레이션 |  |
| F3-04 | 피난 집결지 데이터 수집 (서울시 공공데이터) | 1일 | 데이터 수집 | 공원, 학교, 지정 집결지 |
| F3-05 | `evacuation_points` 테이블 생성 및 적재 | 1일 | DB |  |
| F3-06 | GET /api/v1/fire/evacuation/{pnu} | 2일 | API | 최적 경로 + 대안 경로 |
| F3-07 | 프론트엔드: 3D 화살표 경로 렌더링 | 3일 | Frontend | CesiumJS PolylineArrowMaterialProperty |
| F3-08 | 집결지 마커 + 수용 인원 패널 | 1일 | Frontend |  |

**예상 총 기간**: 3~4주

### 6.3 의존성

```
F3-01 (Docker 이미지 교체) → F3-02, F3-03 시작 가능
F3-02 완료 → F3-03 진행
F3-03, F3-04, F3-05 완료 → F3-06 시작 가능
F3-06 완료 → F3-07, F3-08 진행

도로 네트워크 수집(F3-01~02)은 F1과 병행 가능.
단, pgRouting 도입으로 인한 DB 마이그레이션은 F2 완료 후 수행 권장.
```

### 6.4 위험 요소

| 위험 | 영향 | 대응 방안 |
|------|------|----------|
| Docker 이미지 변경 | 기존 PostGIS 확장 설정 유실 가능 | `docker compose exec db pg_dump` 백업 후 이미지 교체 |
| OSM 도로 데이터 품질 | 서울 골목길·보행 전용 도로 누락 가능 | 차량 도로 + 보행 도로 레이어 분리 적재 |
| 피난 집결지 데이터 불완전 | 비공식 집결지 누락 | 서울시 방재과 공개 자료 + 국가재난안전포털 교차 수집 |

---

## 7. Phase F4 — 실시간 기상 연동

### 7.1 목표

기상청 동네예보 API에서 바람 방향·속도를 자동 수집하여 F2 화재 확산 시뮬레이션의 입력 파라미터로 자동 주입한다. HTTP 폴링 방식의 프론트엔드를 WebSocket으로 전환하여 실시간 업데이트를 지원한다.

### 7.2 태스크 목록

| 번호 | 태스크 | 예상 소요 | 담당 영역 | 비고 |
|------|--------|----------|----------|------|
| F4-01 | 기상청 동네예보 API 연동 모듈 | 2일 | 데이터 수집 | data.go.kr 인증 필요 |
| F4-02 | Celery beat 스케줄 태스크 (1시간 주기 기상 갱신) | 1일 | 인프라 |  |
| F4-03 | `weather_snapshots` 테이블 (서울 격자별 바람 데이터) | 1일 | DB |  |
| F4-04 | 확산 시뮬레이션 자동 실행 (현재 기상 기반) | 1일 | 시뮬레이션 | F2-05 Celery 태스크 재활용 |
| F4-05 | FastAPI WebSocket 엔드포인트 구현 | 2일 | API | `/ws/fire/updates` |
| F4-06 | 프론트엔드: HTTP polling → WebSocket 전환 | 2일 | Frontend |  |

**예상 총 기간**: 2~3주

### 7.3 의존성

```
F2 완료 → F4-01~04 시작 가능 (F4는 F2의 확산 시뮬레이션 위에 구축)
F4-05 완료 → F4-06 시작 가능
```

---

## 8. 기술 스택 변경사항

기존 스택(FastAPI + PostGIS + CesiumJS)을 기반으로 Phase별 라이브러리를 추가한다.

### 8.1 Phase별 추가 라이브러리

| Phase | 영역 | 추가 항목 | 버전 | 비고 |
|-------|------|----------|------|------|
| F1 | DB | PostGIS ST_ClusterDBSCAN | PostGIS 2.3+ (현재 사용 중) | 별도 설치 불필요 |
| F1 | Python | `scikit-learn` | 1.x | DBSCAN 대안 구현용 (선택) |
| F2 | Python | `networkx` | 3.x | 그래프 확산 알고리즘 |
| F2 | Python | `numpy` | 1.x | 확산 확률 계산 |
| F3 | DB | `pgrouting/pgrouting` Docker 이미지 | 3.x | postgis 이미지 대체 |
| F3 | 데이터 도구 | `osm2pgrouting` | 2.x | OSM → pgRouting 적재 |
| F4 | Python | `websockets` 또는 `starlette` WS | FastAPI 내장 | WebSocket 엔드포인트 |
| F4 | Frontend | 브라우저 WebSocket API | 표준 | 별도 라이브러리 불필요 |

### 8.2 Docker Compose 변경

F3에서 DB 이미지를 교체한다. 기존 `postgis/postgis:15-3.4`에서 `pgrouting/pgrouting:15-3.4-3.4` 로 변경한다. pgRouting 이미지는 PostGIS를 포함하므로 기존 기능은 유지된다.

```yaml
# docker-compose.yml 변경 예시 (F3 시점)
services:
  db:
    image: pgrouting/pgrouting:15-3.4-3.4  # 변경
    # 나머지 설정 동일
```

---

## 9. 테스트 계획

### 9.1 Phase별 완료 기준

각 Phase는 아래 기준을 모두 충족해야 완료로 간주한다.

#### Phase F1 완료 기준

| 기준 | 측정 방법 |
|------|----------|
| `building_adjacency` 적재 완료 | `SELECT COUNT(*) FROM building_adjacency` ≥ 예상 쌍 수 |
| 클러스터 API 응답 정확성 | 마포구 bbox 요청 시 알려진 고밀도 구역 클러스터 포함 확인 |
| 소방서 API 응답 | 서울 25개 소방서 본서 조회 가능 (✅ 25건 완료; 안전센터 확대는 F1 후속 개선) |
| 응답거리 계산 | 임의 PNU 10건에 대해 응답거리 계산 오류율 0% |
| 리트로핏 API | `measures` 파라미터 조합 5가지 이상 정상 응답 |
| 프론트엔드 | 클러스터 폴리곤·소방서 마커 렌더링 오류 없음 |

#### Phase F2 완료 기준

| 기준 | 측정 방법 |
|------|----------|
| BFS 알고리즘 | 발화 건물 1동에서 30분 시뮬레이션 결과 단조 증가 (건물 수) |
| 확산 방향성 | 바람 방향 바꿀 때 확산 방향 변화 육안 검증 |
| 비동기 처리 | POST 요청 후 202 Accepted → GET으로 결과 폴링 정상 동작 |
| 캐싱 | 동일 파라미터 재요청 시 Redis 캐시 HIT 확인 |
| 타임라인 애니메이션 | CesiumJS에서 시간 단계별 건물 색상 전환 오류 없음 |
| 피해 통계 | 발화 건물 수·연면적 합산 값이 DB 집계와 일치 |

#### Phase F3 완료 기준

| 기준 | 측정 방법 |
|------|----------|
| pgRouting 설치 | `SELECT pgr_version()` 정상 응답 |
| OSM 도로 적재 | 서울 전역 `ways` 테이블 레코드 수 > 500,000 |
| 경로 계산 | 마포구 임의 지점 10쌍 경로 계산 성공률 100% |
| 대피경로 API | `/fire/evacuation/{pnu}` 응답 시간 < 2초 |
| 3D 화살표 렌더링 | 경로 선택 시 CesiumJS에서 화살표 렌더링 오류 없음 |

#### Phase F4 완료 기준

| 기준 | 측정 방법 |
|------|----------|
| 기상 API 연동 | Celery beat 작업 실행 후 `weather_snapshots` 갱신 확인 |
| 자동 시뮬레이션 | 기상 갱신 시 확산 시나리오 자동 생성 확인 |
| WebSocket | 클라이언트 연결 후 1시간 내 기상 업데이트 수신 확인 |

### 9.2 회귀 테스트 유지 기준

- F0에서 작성된 화재 위험 API 테스트는 이후 모든 Phase에서 통과해야 한다.
- `building_fire_risk` 뷰의 기본 점수 계산 로직은 Phase F1 밀집도 점수 추가 후에도 단독 항목별 점수가 변하지 않아야 한다.

---

## 10. 데이터 수집 계획

### 10.1 Phase별 필요 데이터 및 수집 방법

#### F1: 서울 소방서·안전센터

| 항목 | 소스 | URL | 형식 | 수집 방법 |
|------|------|-----|------|----------|
| 소방서 위치 | 서울 열린데이터광장 | data.seoul.go.kr | JSON/CSV | REST API 또는 CSV 다운로드 |
| 119안전센터 위치 | 서울 열린데이터광장 | data.seoul.go.kr | JSON/CSV | REST API 또는 CSV 다운로드 |

수집 스크립트는 `src/data_ingestion/collect_fire_stations.py`로 작성한다. 기존 `collect_ledger.py` 패턴을 따른다.

```python
# 수집 후 DB 적재 대상 테이블
CREATE TABLE fire_stations (
    station_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT,          -- '소방서' | '안전센터'
    address      TEXT,
    lat          NUMERIC(10,7),
    lng          NUMERIC(10,7),
    geom         GEOMETRY(Point, 4326),
    updated_at   TIMESTAMP DEFAULT NOW()
);
```

#### F3: OSM 서울 도로 데이터

| 항목 | 소스 | URL | 형식 | 수집 방법 |
|------|------|-----|------|----------|
| 한국 도로 네트워크 | Geofabrik | download.geofabrik.de/asia/south-korea-latest.osm.pbf | OSM PBF | wget 다운로드 후 osm2pgrouting 변환 |

```bash
# 수집 및 적재 절차
wget https://download.geofabrik.de/asia/south-korea-latest.osm.pbf
osm2pgrouting \
  --file south-korea-latest.osm.pbf \
  --conf /usr/share/osm2pgrouting/mapconfig_for_pedestrians.xml \
  --dbname buildings \
  --username postgres \
  --host localhost \
  --port 5432 \
  --chunk 50000
```

서울 특별시 경계 내 데이터만 추출하여 적재 크기를 줄인다 (`--bbox 126.7 37.4 127.2 37.7`).

#### F3: 피난 집결지

| 항목 | 소스 | URL | 형식 | 수집 방법 |
|------|------|-----|------|----------|
| 임시주거시설·집결지 | 국가재난안전포털 | safekorea.go.kr | 공개 다운로드 | CSV 수동 다운로드 |
| 서울시 공원 | 서울 열린데이터광장 | data.seoul.go.kr | JSON | REST API |
| 학교 현황 | 공공데이터포털 | data.go.kr | JSON/CSV | REST API |

#### F4: 기상청 동네예보

| 항목 | 소스 | API 명 | 형식 | 수집 방법 |
|------|------|--------|------|----------|
| 동네예보 (바람 방향·속도) | 기상청 공공데이터 | 동네예보 조회서비스 | JSON | data.go.kr API 키 필요 |

수집 대상 필드: `wsd` (풍속, m/s), `vec` (풍향, deg). 서울 격자 전체(약 40×40) 1시간 주기 수집.

### 10.2 API 키 관리

신규 API 키는 모두 `.env` 파일에 추가한다. 키 이름 규칙은 기존 컨벤션을 따른다.

```bash
# .env 추가 항목
SEOUL_DATA_API_KEY=...       # 서울 열린데이터광장 (기존)
KMA_API_KEY=...              # 기상청 동네예보
```

---

## 11. 마일스톤 & 릴리즈 기준

### 11.1 마일스톤 목록

| 마일스톤 | 설명 | 완료 기준 |
|---------|------|----------|
| M-F0 | 화재 위험 기반 기능 출시 | Phase F0 완료 (2026-03-24) ✅ |
| M-F1 | 지역 위험 분석 출시 | Phase F1 테스트 기준 전체 통과 |
| M-F2 | 확산 시뮬레이션 출시 | Phase F2 테스트 기준 전체 통과 + 백테스트 1건 이상 |
| M-F3 | 대피 경로 출시 | Phase F3 테스트 기준 전체 통과 |
| M-F4 | 실시간 연동 출시 | Phase F4 테스트 기준 전체 통과 |

### 11.2 릴리즈 기준 (공통)

아래 항목을 모두 충족해야 릴리즈 가능하다.

- 해당 Phase의 모든 유닛·통합 테스트 통과
- API 응답 시간 P95 < 3초 (시나리오 계산 비동기 엔드포인트 제외)
- 프론트엔드 기능 오류 없음 (Chrome 최신 버전 기준)
- `ARCHITECTURE.md` API 명세 업데이트 완료
- F0 회귀 테스트 통과 (화재 위험 기본 기능 이상 없음)

### 11.3 Phase F2 추가 릴리즈 기준

F2는 시뮬레이션 결과의 신뢰성이 중요하므로 추가 기준을 적용한다.

- 서울 과거 화재 사례 3건 이상으로 back-test 수행, 확산 범위 오차 50% 이내
- 시뮬레이션 결과에 "추정 모델 기반, 실제 화재와 차이 있을 수 있음" 면책 문구 UI에 표시

---

## 12. 부록: 파일·테이블 네이밍 컨벤션

Phase 추가에 따른 신규 파일 및 테이블은 기존 컨벤션을 따른다.

### 12.1 신규 파일

```
src/
├── data_ingestion/
│   ├── collect_fire_stations.py   # F1: 소방서 데이터 수집
│   └── collect_weather.py         # F4: 기상청 연동
├── simulation/
│   ├── fire_spread.py             # F2: 확산 알고리즘 (networkx BFS)
│   └── evacuation.py              # F3: 대피경로 계산 (pgRouting 래퍼)
└── visualization/
    ├── fire.py                    # F1+F2: /api/v1/fire/* 라우터
    └── evacuation.py              # F3: /api/v1/fire/evacuation/* 라우터
```

### 12.2 신규 DB 테이블·뷰

```sql
-- F1
building_adjacency        -- 인접 건물 쌍 (PNU_A, PNU_B, distance_m)
fire_stations             -- 소방서·안전센터 위치

-- F2
fire_scenario_results     -- 시나리오별 시간 단계 결과 (JSONB)

-- F3
evacuation_points         -- 피난 집결지 (공원, 학교, 지정 집결지)
-- pgRouting 생성 테이블
ways                      -- OSM 도로 네트워크
ways_vertices_pgr         -- 도로 정점

-- F4
weather_snapshots         -- 동네예보 격자별 바람 데이터
```

---

*문서 버전 1.0 — 2026-03-24 작성*
*다음 업데이트 예정: Phase F1 완료 시점에 실제 소요 시간 및 위험 요소 발생 내역 반영*
