# CLAUDE.md - 프로젝트 개발 가이드

## 프로젝트 현재 상태

**단계: Phase 3.5 완료 + Phase F0~F4 완료 + Tier C Monitor 완료** — 2026-03-27 기준

### 구현 완료 (Phase 0~3)

| 모듈 | 상태 | 설명 |
|------|------|------|
| `docs/` | ✅ 100% | PRD v1.2, Architecture v1.2, RFC 문서 |
| `docker-compose.yml` | ✅ 100% | PostGIS(5434), Redis(6379), API(8000), Worker |
| `src/data_ingestion/` | ✅ 98% | VWorld **766,386건**(footprint) · 건축물대장 **1,160,817건**(25개 구) · **Tier1** 실측 89건 · **Tier2** 인증/등급 3,588건(KEA 3,192+등급 396, pg_trgm 재수집 완료) · **Tier4** 아키타입. upsert guard: `WHERE data_tier >= EXCLUDED.data_tier` |
| `src/geometry/` | ✅ 100% | PNU 생성/파싱, 좌표 변환 (EPSG:5174→4326) |
| `src/simulation/` | ⚠️ 40% | archetype **83종** (9용도×4연대×2~3구조) + 에너지 추정. EnergyPlus/ML 미연동 |
| `src/simulation/ml_xgboost.py` | ✅ 100% | Tier A/B XGBoost EUI 예측 (Tier1×5 가중치, StratifiedKFold, 예측구간) |
| `src/simulation/ml_timeseries.py` | ✅ 100% | Tier C 시계열 단기 예측 (lag features, XGBoost) |
| `db/` (Provenance) | ✅ 100% | data_sources(7건+tier_c_metered) + pipeline_runs + model_registry + model_versions + energy_predictions(파티션) + model_accuracy_summary MV |
| `src/tile_generation/` | ✅ 80% | trimesh GLB 생성 |
| `src/visualization/` | ✅ 100% | buildings/search/filter/pick/stats/centroids API + GZip |
| `db/` | ✅ 100% | init.sql + views.sql LATERAL JOIN (PNU 1:1 매칭) |
| `tests/unit/` | ✅ 74건 | PNU, 열화계수, 아키타입, 타일 색상, ML xgboost/timeseries |
| `frontend/` (React) | ✅ 85% | CesiumJS 직접 사용 + 에너지 색상 + 상세 패널 + ErrorBoundary |
| `frontend/` (VWorld) | ✅ 90% | **VWorld WebGL 3D API 3.0** 텍스처 건물 + 에너지 오버레이 (`/vworld.html`), 서울 전역 766K건 |

### 완료 (Tier C 실계량기 모니터링)

| 모듈 | 상태 | 설명 |
|------|------|------|
| `db/monitor_timeseries.sql` | ✅ 100% | `monitored_buildings` + `metered_readings`(월별 파티션) + `anomaly_log` + `metered_readings_daily` + `fn_sync_tier_c_to_energy_results()` |
| `src/visualization/monitor.py` | ✅ 100% | `/api/v1/monitor/*` 7개 엔드포인트 (Redis 캐시, CSV 업로드, WebSocket) |
| `src/shared/monitor_models.py` | ✅ 100% | Pydantic 스키마 8개 |
| `src/monitor/tasks.py` | ✅ 100% | Celery beat 1h 이상치 감지 (PostgreSQL window function, Redis pub/sub) |
| `frontend/monitor.html` + `src/monitor-main.tsx` | ✅ 100% | `/monitor` 별도 MPA 엔트리 (StrictMode 비활성화 — Leaflet 이중초기화 방지) |
| `frontend/src/pages/MonitorPage.tsx` | ✅ 100% | 30초 polling, fetchId race guard, 헤더 이상치 카운트 |
| `frontend/src/components/Monitor/` | ✅ 100% | BuildingListPanel, BuildingListItem, MultiLineChart, TimeseriesChartPanel, BuildingMiniMap, MonitorFilterBar, PeriodSelector, AnomalyBadge (16개 버그 수정 완료) |

**⚠️ DB 스키마 SSOT**: `db/monitor_timeseries.sql` (canonical). `migration_tier_c_timeseries_v1.sql` 은 deprecated.
**DB 마이그레이션 실행**:
```bash
docker compose exec db psql -U postgres -d buildings -f /docker-entrypoint-initdb.d/monitor_timeseries.sql
```

### 두 가지 프론트엔드 뷰어

| 뷰어 | 경로 | 3D 건물 | 텍스처 | 에너지 데이터 |
|------|------|---------|--------|-------------|
| **VWorld 뷰어** (권장) | `/vworld.html` | VWorld LoD3-4 (정부 공식) | ✅ 사진 텍스처 | ✅ 클릭→상세 패널 |
| React 뷰어 | `/` | 자체 익스트루전 | 프로시저럴 색상 | ✅ 색상 코딩 + 패널 |

### 완료 (Fire Safety)

| 모듈 | 상태 | 설명 |
|------|------|------|
| `db/fire_risk.sql` + MV | ✅ 100% | building_fire_risk (100점 척도: 구조40+연령30+용도20+층수10) |
| `src/fire_safety/` F1 | ✅ 100% | 소방서 25개 본서, building_adjacency 3,554,436행 |
| `src/fire_safety/fire_spread.py` | ✅ 100% | BFS 시뮬레이션 (SPREAD_THRESHOLD=0.25, wind_factor cos기반) |
| `src/fire_safety/risk.py` | ✅ 100% | 4개 엔드포인트 + Celery tasks.run_fire_scenario |
| vworld.html (F2) | ✅ 100% | 확산 시뮬 버튼, 컴파스 UI, 타임라인 컨트롤, 통계 패널 |
| F3 대피경로 | ✅ 100% | pgRouting Dijkstra + evacuation_points 35개 (25구 전체) |
| F4 기상 연동 | ✅ 100% | apihub.kma.go.kr + data.go.kr 폴백 + /ws/weather WebSocket + Celery beat 1h |

### 미착수 (Phase 4~5)

| 모듈 | 설명 |
|------|------|
| EnergyPlus 연동 | OpenStudio/geomeppy 시뮬레이션 (83 archetype × 8760시간) |
| 에너지 예측 모델 | Pluggable Architecture — EnergyPredictor ABC, ModelRegistry |
| 일단위 예측 | XGBoost (기본) → 사용자 커스텀 모델 교체 가능 |
| 시간단위 예측 | LSTM (기본) → 사용자 커스텀 모델 교체 가능 |
| 온돌 모델링 | 바닥복사난방 (공동주택) |
| 리트로핏 추정 | 창호/외단열 변경 효과 |
| UHI 보정 | 도시열섬 효과 반영 |
| Phase 4 | archetype 120종 + 지역난방 + 온돌 + EnergyPlus + 예측 모델 |
| Phase 4 | CO2 배출량 + 1차에너지 환산 + 리트로핏 비용 계산 |
| Phase 5 | 시간대 애니메이션 + 건물 비교 + ZEB 전환 지도 + 전국 확장 |

**화재 안전 관련 문서**: [PRD-FIRE-SAFETY.md](./docs/PRD-FIRE-SAFETY.md) · [RFC-FIRE-SAFETY.md](./docs/RFC-FIRE-SAFETY.md) · [FIRE-SAFETY-WORKPLAN.md](./docs/FIRE-SAFETY-WORKPLAN.md)

## ⚠️ 변경 관리 원칙 (수정 전 필독)

이 프로젝트는 같은 사실(vintage_class 구간, buildings_enriched 컬럼 등)이 여러 파일에 중복 기술되어 있다.
**수정 전에 반드시 [docs/IMPACT.md](./docs/IMPACT.md)를 확인하고 영향 파일을 동시에 수정한다.**

| SSOT | 연동 파일 |
|------|----------|
| `db/views.sql` | `ARCHITECTURE.md §3.2.1` · `RFC-ENERGY-SIMULATION.md §2.1` · `src/visualization/search.py` |
| `db/fire_risk.sql` | `FIRE-SAFETY-WORKPLAN.md §3.2` · `frontend/vworld.html` |
| `src/visualization/search.py` FilterRequest | `ARCHITECTURE.md API` · `frontend/vworld.html applyFilters()` |

기술 결정 배경: [docs/adr/](./docs/adr/) 폴더 (ADR-001~004 확정)

### 기술 스택 확정

| 영역 | 기술 |
|------|------|
| 백엔드 | FastAPI + SQLAlchemy + PostGIS + Celery + Redis + GZip |
| 프론트엔드 (React) | React 19 + CesiumJS (직접) + Zustand + TailwindCSS + Recharts |
| 프론트엔드 (VWorld) | VWorld WebGL 3D API 3.0 (Cesium 내장, `ws3d.viewer`) |
| 3D 건물 | VWorld LoD3-4 텍스처 (권장) / 자체 익스트루전 (대안) |
| 빌드 | Vite 6 + TypeScript |
| 데이터 | VWorld API (서울 전역 766K footprint) + data.go.kr HTTPS (건축물대장) |
| DB 최적화 | building_centroids 테이블 (Point GiST KNN, 0.07ms pick) |

### 주요 기술 결정 사항

1. **VWorld WebGL 3D API 3.0 채택**: 서울 LoD3-4 텍스처 건물 공식 지원. `ws3d.viewer`가 Cesium.Viewer이므로 Cesium API 직접 사용 가능
2. **Google Photorealistic 3D Tiles**: 한국 미지원 확인 (2026-03)
3. **VWorld 3D Data API (벌크 다운로드)**: 2019년 폐쇄 (국가보안). WebGL 뷰어로만 접근 가능
4. **PublicDataReader 라이브러리**: HTTPS 엔드포인트 직접 호출로 대체 (http→https 문제)
5. **React StrictMode**: Cesium Viewer lifecycle 충돌로 비활성화
6. **서버사이드 pick**: PostGIS KNN (`<->` 연산자, 3ms 응답) — 766K centroid 클라이언트 로딩 불필요
7. **건축물대장 2단계 수집**: 총괄표제부(단지) + 표제부(동별) → LATERAL JOIN으로 PNU당 최적 1건 매칭
8. **EllipsoidTerrainProvider**: 지형 제거로 건물 높이 정확도 확보 (React 뷰어)
9. **성능 최적화**: RequestScheduler, fog, GZip, AbortController, Entity eviction
10. **서울 전역 확장**: 766,386건 footprint (자동 타일 분할, 126개 타일)
11. **building_centroids 테이블**: Point GiST KNN으로 pick 0.07ms (MultiPolygon KNN 대비 5000x 빠름)

### Git 브랜치 전략

- `main`: 안정 버전, PR 통해 머지
- `docs/*`: 문서 업데이트
- `feat/*`: 기능 개발
- **원상 복구**: `git revert <commit>` 또는 `git checkout main -- <file>`

## 빌드 & 실행

```bash
# 개발 환경 (Docker Compose)
docker compose up -d              # PostGIS, Redis, FastAPI, Celery worker
docker compose exec api bash      # API 컨테이너 접속

# 백엔드
pip install -e ".[dev]"           # 개발 의존성 설치
uvicorn src.main:app --reload     # FastAPI 개발 서버

# 프론트엔드
cd frontend && npm install && npm run dev   # Vite 개발 서버

# Celery worker
celery -A src.shared.celery_app worker --loglevel=info

# 데이터 파이프라인 (수동 실행)
python -m src.data_ingestion.collect_footprints   # GIS건물통합정보 적재
python -m src.data_ingestion.collect_ledger       # 건축물대장 수집
python -m src.tile_generation.generate            # 3D Tiles 생성
```

## 테스트

```bash
pytest                           # 전체 테스트
pytest tests/unit/               # 유닛 테스트
pytest tests/integration/        # 통합 테스트 (PostGIS 필요)
pytest -k "test_pnu"             # 특정 테스트
```

### 테스트 최소 요구사항

**최우선 테스트 대상** (이 영역은 코드 작성 시 반드시 테스트 동반):
1. **PNU 조인 로직** — 1:1 매칭, 집합건물 1:N, 미매칭 처리. 프로젝트 데이터 정합성의 핵심
2. **3D geometry 생성** — footprint 익스트루전, 좌표 변환(EPSG:5174→4326), 높이 계산
3. **3D Tiles 생성** — pg2b3dm/trimesh 출력물 유효성
4. **API 계약** — 엔드포인트 요청/응답 스키마 검증

일반 규칙:
- **파이프라인 로직**: PNU 매칭, 좌표 변환, 데이터 클렌징은 픽스처 기반 유닛 테스트 필수
- **버그 수정**: 해당 버그를 재현하는 회귀 테스트 포함
- **시뮬레이션**: 원형 매칭 로직, 열화계수 계산은 경계값 테스트 포함

### 테스트 파일 배치 (계획 — 각 모듈 구현 시 함께 생성)

```
tests/                             # 현재: 디렉토리만 존재
├── unit/                          # 각 모듈 구현 시 테스트 파일 추가
│   ├── test_pnu_matching.py       # PNU 매칭 로직
│   ├── test_degradation.py        # 열화계수 계산
│   ├── test_archetype_mapping.py  # 원형 분류
│   └── test_energy_prediction.py  # ML 예측 인터페이스
├── integration/                   # PostGIS 컨테이너 필요
│   ├── test_postgis_pipeline.py   # PostGIS 적재/조인
│   ├── test_api_buildings.py      # /api/v1/buildings 엔드포인트
│   └── test_tile_generation.py    # 3D Tiles 생성
└── fixtures/
    ├── sample_footprints.geojson  # 테스트용 건물 footprint
    ├── sample_ledger.json         # 테스트용 건축물대장 응답
    └── sample_archetype.json      # 테스트용 원형 데이터
```

## 코드 스타일 & 컨벤션

### 언어별 기본

- Python: PEP 8, type hints 사용, Black formatter
- TypeScript: ESLint + Prettier, 엄격 모드
- SQL: 대문자 키워드, snake_case 테이블/컬럼

### 프로젝트 특화 네이밍

```
src/
├── data_ingestion/          # 외부 데이터 수집 (API, SHP)
│   ├── collect_*.py         # 수집 스크립트: collect_ledger, collect_footprints
│   ├── parsers/             # 응답 파싱: parse_ledger_response()
│   └── tasks.py             # Celery 태스크: sync_building_ledger()
├── geometry/                # 공간 처리 (PNU 매칭, 좌표 변환)
│   ├── pnu.py               # PNU 관련: generate_pnu(), match_pnu_to_footprint()
│   ├── transform.py         # 좌표 변환: epsg5174_to_4326()
│   └── extrude.py           # 3D 생성: extrude_footprint()
├── simulation/              # 에너지 시뮬레이션
│   ├── archetypes.py        # 원형 정의: match_archetype(), get_archetype_params()
│   ├── degradation.py       # 열화계수: apply_degradation()
│   ├── ml_interface.py      # ML 인터페이스 (추상 클래스)
│   ├── ml_xgboost.py        # XGBoost 구현 (사용자 교체 가능)
│   └── tasks.py             # Celery: simulate_archetype()
├── tile_generation/         # 3D Tiles 생성/배포
│   ├── generate.py          # pg2b3dm 실행
│   ├── deploy.py            # S3 업로드
│   └── tasks.py             # Celery: regenerate_tiles()
├── visualization/           # FastAPI 라우터 (API 엔드포인트)
│   ├── buildings.py         # /api/v1/buildings/*
│   ├── search.py            # /api/v1/search (도로명주소)
│   ├── filter.py            # /api/v1/filter
│   └── simulation.py        # /api/v1/simulate
└── shared/                  # 공유 인프라 (유틸리티 버킷 금지)
    ├── database.py          # PostGIS 연결, SQLAlchemy engine
    ├── cache.py             # Redis 연결
    ├── config.py            # 환경설정 (Pydantic Settings)
    └── models.py            # Pydantic 스키마 (요청/응답)
```

**규칙:**
- 로직은 반드시 도메인 폴더(data_ingestion, geometry, simulation 등) 안에 배치
- `shared/`에는 인프라 연결만. 비즈니스 로직이나 범용 유틸리티 금지
- 새 모듈 추가 시 기존 도메인 폴더에 맞는 곳에 배치. 새 폴더 생성은 RFC 논의 후

### FastAPI 라우터 컨벤션

```python
# 라우터 파일: src/visualization/{domain}.py
router = APIRouter(prefix="/api/v1/{domain}", tags=["{domain}"])

# 엔드포인트 네이밍: HTTP 메서드 + 리소스
@router.get("/buildings")           # 목록 조회
@router.get("/buildings/{pnu}")     # 단건 조회
@router.post("/filter")            # 필터 검색
```

## 커밋 & PR 가이드

### 커밋 메시지

```
<type>: <설명>

type: feat, fix, docs, refactor, test, chore
```

### PR 규칙 (문서 우선 워크플로우)

1. **설계문서 변경과 구현은 가능하면 분리**
   - RFC/Architecture 변경 → 별도 PR로 먼저 리뷰
   - 구현 PR은 승인된 설계문서 참조

2. **인터페이스/동작 변경 시 관련 문서 동시 업데이트**
   - API 엔드포인트 변경 → Architecture.md API 명세 업데이트
   - 데이터 스키마 변경 → RFC-DATA-PIPELINE.md 업데이트
   - 시뮬레이션 로직 변경 → RFC-ENERGY-SIMULATION.md 업데이트

3. **PR 본문에 관련 문서 섹션 명시**
   ```
   Related docs: docs/RFC-DATA-PIPELINE.md §3.2 (PNU 매칭)
   ```

## 보안

### 절대 커밋 금지

- `.env` 파일
- API 키, 비밀번호, 인증 토큰
- SHP/CSV 원본 데이터 파일 (대용량 + 라이선스)
- `credentials.json`, `*.pem`

### 환경변수 (.env 예시)

```bash
# Database
DATABASE_URL=postgresql://postgres:password@localhost:5432/buildings

# Redis
REDIS_URL=redis://localhost:6379

# 공공데이터포털 API 키
DATA_GO_KR_API_KEY=your_api_key_here

# VWorld API 키
VWORLD_API_KEY=your_api_key_here

# 도로명주소 API 키
JUSO_API_KEY=your_api_key_here

# 서울 열린데이터광장 API 키
SEOUL_DATA_API_KEY=your_api_key_here

# AWS (3D Tiles 배포)
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
S3_TILES_BUCKET=building-energy-tiles
CLOUDFRONT_DISTRIBUTION_ID=your_distribution_id
```

## 주요 문서

- [PRD](docs/PRD.md) - 제품 요구사항
- [Architecture](docs/ARCHITECTURE.md) - 시스템 아키텍처
- [RFC: Data Pipeline](docs/RFC-DATA-PIPELINE.md) - 데이터 파이프라인
- [RFC: Energy Simulation](docs/RFC-ENERGY-SIMULATION.md) - 에너지 시뮬레이션
