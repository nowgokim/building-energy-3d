# CLAUDE.md - 프로젝트 개발 가이드

## 프로젝트 현재 상태

**단계: MVP 구현 진행 중**

구현 완료:
- `docs/` — PRD, Architecture, RFC 문서 완성 (v1.1)
- `docker-compose.yml`, `Dockerfile`, `pyproject.toml` — 인프라 설정 완료
- `.env.example` — 환경변수 템플릿

구현 진행 중:
- `src/` — 백엔드 모듈 구현 중
- `tests/` — 디렉토리 구조만 존재, **테스트 파일은 각 모듈 구현 시 함께 작성**
- `frontend/` — 미착수

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
