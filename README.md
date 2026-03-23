# 서울 3D 건물 에너지 지도 플랫폼

VWorld 3D 텍스처 건물 위에 에너지 데이터를 오버레이하는 웹 플랫폼.

**라이브 데모**: https://nowgokim.github.io/building-energy-3d/

## 주요 기능

- VWorld WebGL 3D API 기반 서울 전역 텍스처 건물
- 건물 클릭 → 에너지 상세 (등급, kWh/m², 에너지 분해 차트)
- 건물명/지번 검색 + 카메라 이동
- 건물 텍스처 오버레이 (사진 → 특정 건물 벽면 매핑)
- 766,386건 서울 전역 건물 데이터

## 기술 스택

| 영역 | 기술 |
|------|------|
| 백엔드 | FastAPI + PostGIS + SQLAlchemy + Redis |
| 프론트엔드 | VWorld WebGL 3D API 3.0 (Cesium 내장) |
| 데이터 | VWorld API (footprint) + data.go.kr (건축물대장) |
| 인프라 | Docker Compose (PostGIS, Redis, FastAPI, Celery) |

## 빠른 시작

### 1. Clone

```bash
git clone https://github.com/nowgokim/building-energy-3d.git
cd building-energy-3d
```

### 2. 환경 설정

```bash
copy .env.example .env
```

`.env` 파일을 열어 API 키를 입력:

```bash
# 필수
DATABASE_URL=postgresql://postgres:devpassword@127.0.0.1:5434/buildings
VWORLD_API_KEY=your_vworld_api_key        # vworld.kr에서 발급
DATA_GO_KR_API_KEY=your_data_go_kr_key    # data.go.kr에서 발급

# 선택
JUSO_API_KEY=your_juso_key                # juso.go.kr
SEOUL_DATA_API_KEY=your_seoul_key          # data.seoul.go.kr
```

### 3. Docker 실행

Docker Desktop을 먼저 시작한 후:

```bash
docker compose up -d
```

컨테이너 상태 확인:

```bash
docker compose ps
# db(5434), redis(6379), api(8000), worker 모두 Running 확인
```

> **주의**: 로컬에 PostgreSQL이 설치되어 있으면 포트 충돌 가능. Docker는 5434 포트를 사용합니다.

### 4. Python 가상환경

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

pip install -e ".[dev]"
```

### 5. 데이터 수집

순서대로 실행:

```bash
# 5a. VWorld 건물 footprint 수집 (서울 전역, ~20분)
python -c "
from src.shared.config import get_settings
from src.data_ingestion.collect_footprints import load_footprints_from_vworld
s = get_settings()
count = load_footprints_from_vworld(s.VWORLD_API_KEY, s.DATABASE_URL)
print(f'Loaded: {count}')
"

# 5b. 건축물대장 수집 — 마포구 (총괄표제부 + 표제부, ~15분)
python -c "
from src.shared.config import get_settings
from src.data_ingestion.collect_ledger import collect_mapo_ledger, collect_mapo_title
s = get_settings()
collect_mapo_ledger(s.DATA_GO_KR_API_KEY, s.DATABASE_URL)
collect_mapo_title(s.DATA_GO_KR_API_KEY, s.DATABASE_URL)
"

# 5c. Materialized View 갱신 + 에너지 추정
python -c "
from src.data_ingestion.pipeline import step_refresh_view, step_match_energy
step_refresh_view()
step_match_energy()
"

# 5d. Centroid 테이블 생성 (건물 클릭 API용)
python -c "
from src.shared.database import execute_sql
execute_sql('DROP TABLE IF EXISTS building_centroids')
execute_sql('''CREATE TABLE building_centroids AS
    SELECT gid, pnu, building_name, ST_Centroid(geom) as centroid
    FROM buildings_enriched''')
execute_sql('CREATE INDEX idx_bc_centroid ON building_centroids USING GIST(centroid)')
execute_sql('ANALYZE building_centroids')
print('Done')
"
```

### 6. 프론트엔드 실행

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

### 7. 접속

| URL | 설명 |
|-----|------|
| http://localhost:5173/vworld.html | **VWorld 3D 뷰어** (권장) |
| http://localhost:5173/ | React CesiumJS 뷰어 (대안) |
| http://localhost:8000/docs | FastAPI Swagger UI |
| http://localhost:8000/health | API 헬스 체크 |

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/v1/buildings/pick?lng=&lat=` | 클릭 위치 최근접 건물 (PostGIS KNN, 0.07ms) |
| GET | `/api/v1/buildings/{pnu}` | 건물 상세 + 에너지 분해 |
| GET | `/api/v1/buildings/stats` | 집계 통계 |
| GET | `/api/v1/buildings/` | 건물 목록 (bbox 필터, GeoJSON) |
| GET | `/api/v1/search?q=` | 건물명 검색 |
| POST | `/api/v1/filter` | 다중 조건 필터 |
| GET | `/api/v1/filter/export` | CSV 내보내기 |

## 테스트

```bash
pytest tests/unit/ -v
```

## 프로젝트 구조

```
├── src/
│   ├── data_ingestion/     # VWorld + 건축물대장 수집
│   ├── geometry/           # PNU 생성, 좌표 변환
│   ├── simulation/         # 에너지 archetype + 예측 모델
│   ├── tile_generation/    # 3D GLB 생성
│   ├── visualization/      # FastAPI 라우터 (API)
│   └── shared/             # DB, 설정, 캐시
├── frontend/
│   ├── vworld.html         # VWorld WebGL 3D 뷰어
│   └── src/                # React CesiumJS 뷰어
├── db/
│   ├── init.sql            # DB 스키마
│   └── views.sql           # Materialized View
├── docs/                   # PRD, Architecture, RFC + GitHub Pages
├── docker-compose.yml
└── .env.example
```

## 데이터 규모 (서울 전역)

| 테이블 | 건수 |
|--------|------|
| building_footprints | 766,386 |
| buildings_enriched (MV) | 766,380 |
| energy_results | 766,380 |
| building_centroids | 766,380 |
| building_ledger | 22,191 (마포구) |

## 문서

- [PRD (제품 요구사항)](docs/PRD.md)
- [Architecture (시스템 아키텍처)](docs/ARCHITECTURE.md)
- [RFC: Data Pipeline](docs/RFC-DATA-PIPELINE.md)
- [RFC: Energy Simulation](docs/RFC-ENERGY-SIMULATION.md)

## 라이선스

MIT
