# 시스템 아키텍처 설계서

**문서 버전**: 1.1
**작성일**: 2026-03-21
**최종 수정**: 2026-03-21 (전문가 리뷰 반영)
**관련 문서**: [PRD](./PRD.md)

---

## 1. 시스템 개요

### 1.1 목적

서울특별시 마포구(시군구코드 11440) 약 25,000~30,000동의 건물을 3D로 시각화하고, 에너지 시뮬레이션 결과를 오버레이하는 웹 플랫폼.

### 1.2 시스템 경계

```
┌─ 본 시스템 범위 ──────────────────────────────────────────────┐
│                                                               │
│  [데이터 수집] → [저장/처리] → [3D 생성] → [웹 서빙/뷰어]       │
│                                                               │
└───────────────────────────────────────────────────────────────┘
        ↑                                              ↑
   외부 시스템                                     사용자 브라우저
   - 공공데이터포털 API                            - Chrome/Edge/Firefox
   - GIS건물통합정보 (SHP)                         - WebGL 2.0
   - VWorld WebGL API
   - 기상청 TMY 기상파일
```

### 1.3 핵심 설계 원칙

1. **데이터 우선**: 파이프라인이 곧 제품. 데이터 품질이 최우선
2. **정적 서빙 극대화**: 3D Tiles, PMTiles는 S3+CDN 정적 파일로 서빙
3. **모듈러 모놀리스**: MVP는 단일 FastAPI 앱, 모듈 경계 명확히 분리
4. **벌크 우선**: API 호출 최소화, SHP/CSV 벌크 다운로드 우선
5. **원형(Archetype) 기반**: 개별 건물 시뮬레이션 대신 원형 매칭으로 스케일

---

## 2. 전체 아키텍처

### 2.1 컴포넌트 다이어그램

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT (웹 브라우저)                          │
│                                                                     │
│   React App                                                         │
│   ├── Resium (CesiumJS React 래퍼)                                  │
│   │   ├── 3D Tiles Viewer  ←── CDN (S3+CloudFront)                 │
│   │   ├── PMTiles Basemap  ←── S3 정적 파일                         │
│   │   └── VWorld Imagery   ←── VWorld WebGL API                    │
│   ├── 건물 상세 패널 (React Component)                               │
│   ├── 필터/검색 UI                                                   │
│   └── 통계 대시보드                                                   │
│                                                                     │
└────────────────────────┬────────────────────────────────────────────┘
                         │ HTTP/WebSocket
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     BACKEND (FastAPI 모듈러 모놀리스)                 │
│                                                                     │
│   ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐        │
│   │ visualization│  │ data_ingest  │  │ simulation         │        │
│   │ (API Router) │  │ (API Router) │  │ (API Router)       │        │
│   │              │  │              │  │                    │        │
│   │ GET /buildings│  │ POST /sync   │  │ GET /energy/{id}   │        │
│   │ GET /stats   │  │ GET /status  │  │ POST /simulate     │        │
│   │ GET /filter  │  │              │  │ GET /archetypes    │        │
│   └──────┬───────┘  └──────┬───────┘  └──────┬─────────────┘        │
│          │                 │                  │                      │
│   ┌──────┴─────────────────┴──────────────────┴──────┐              │
│   │                   shared                          │              │
│   │  ├── database.py  (PostGIS 연결, SQLAlchemy)      │              │
│   │  ├── cache.py     (Redis 연결)                    │              │
│   │  ├── config.py    (환경설정, API 키)               │              │
│   │  └── models.py    (Pydantic 스키마)               │              │
│   └───────────────────────────────────────────────────┘              │
│                                                                     │
└────────────────────────┬────────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
┌──────────────┐  ┌───────────┐  ┌──────────────┐
│  PostgreSQL  │  │   Redis   │  │ Celery       │
│  + PostGIS   │  │           │  │ Workers      │
│              │  │ - 캐시     │  │              │
│ - buildings  │  │ - 태스크큐  │  │ - 데이터동기화│
│ - energy     │  │ - 세션     │  │ - 타일생성    │
│ - archetypes │  │           │  │ - 시뮬레이션   │
└──────────────┘  └───────────┘  └──────────────┘
```

### 2.2 데이터 흐름 다이어그램

```
                    ┌──────────────────────┐
                    │   외부 데이터 소스      │
                    ├──────────────────────┤
                    │ GIS건물통합정보 (SHP)  │──── 월 1회 벌크
                    │ 건축물대장 (REST API)  │──── 주 1회 델타
                    │ 건물에너지정보 (API)    │──── 분기 1회
                    │ 기상청 TMY (파일)      │──── 1회 수집
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   ETL / 수집 레이어    │
                    ├──────────────────────┤
                    │ ogr2ogr (SHP→PostGIS)│
                    │ PublicDataReader (API)│
                    │ 데이터 검증/클렌징     │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   PostGIS 통합 DB     │
                    ├──────────────────────┤
                    │ building_footprints  │ ← 2D 폴리곤 + PNU
                    │ building_ledger      │ ← 속성 (층수,용도,년도)
                    │ buildings_enriched   │ ← Materialized View (JOIN)
                    │ energy_results       │ ← 시뮬레이션 결과
                    │ building_archetypes  │ ← 원형 정의/매핑
                    └──────┬───────┬───────┘
                           │       │
              ┌────────────┘       └────────────┐
              ▼                                  ▼
    ┌──────────────────┐              ┌──────────────────┐
    │ 3D Tiles 생성     │              │ 에너지 시뮬레이션   │
    ├──────────────────┤              ├──────────────────┤
    │ ST_Extrude (3D)  │              │ 원형 매칭          │
    │ pg2b3dm (변환)    │              │ OpenStudio/E+     │
    │ Draco 압축       │              │ ML 대리모델 추론    │
    └────────┬─────────┘              └────────┬─────────┘
             │                                  │
             ▼                                  │
    ┌──────────────────┐                        │
    │ S3 + CloudFront  │                        │
    │ (정적 3D Tiles)   │                        │
    └────────┬─────────┘                        │
             │                                  │
             └──────────────┬───────────────────┘
                            ▼
                 ┌──────────────────┐
                 │  CesiumJS 뷰어   │
                 │  (3D 건물 + 에너지)│
                 └──────────────────┘
```

---

## 3. 상세 컴포넌트 설계

### 3.1 데이터 수집 모듈 (`src/data_ingestion/`)

#### 3.1.1 GIS건물통합정보 수집

```
입력: SHP 파일 (data.go.kr 벌크 다운로드)
처리: ogr2ogr → PostGIS
출력: building_footprints 테이블

명령:
$ ogr2ogr -f "PostgreSQL" \
    PG:"host=localhost dbname=buildings user=postgres" \
    -nlt MULTIPOLYGON -nln building_footprints \
    -lco GEOMETRY_NAME=geom \
    -lco FID=gid \
    -s_srs EPSG:5174 -t_srs EPSG:4326 \
    마포구_건물통합정보.shp
```

마포구 필터링: 시군구코드 `11440` 기준

#### 3.1.2 건축물대장 API 수집

```python
# PublicDataReader 활용
from PublicDataReader import BuildingLedger

api = BuildingLedger(service_key="YOUR_API_KEY")

# 마포구 법정동코드 목록으로 순회
for bdong_code in mapo_bdong_codes:
    df = api.get_data(
        ledger_type="총괄표제부",
        sigungu_code="11440",
        bdong_code=bdong_code
    )
    # → PostgreSQL building_ledger 테이블에 적재
```

#### 3.1.3 데이터 동기화 전략

| 데이터 | 초기 수집 | 갱신 방식 | 갱신 주기 | Celery Task |
|--------|----------|----------|----------|------------|
| GIS건물통합정보 | SHP 벌크 | SHP 재다운로드 | 월 1회 | `sync_gis_footprints` |
| 건축물대장 | API 전수 (법정동별) | API 델타 (변경분) | 주 1회 | `sync_building_ledger` |
| 건물에너지정보 | API 전수 | API 전수 | 분기 1회 | `sync_energy_data` |

### 3.2 데이터베이스 설계 (`src/shared/`)

#### 3.2.1 핵심 테이블

```sql
-- 1. 건물 Footprint (GIS건물통합정보에서)
CREATE TABLE building_footprints (
    gid         SERIAL PRIMARY KEY,
    pnu         VARCHAR(19) NOT NULL,     -- PK: 19자리 PNU 코드
    bld_nm      VARCHAR(200),             -- 건물명
    dong_nm     VARCHAR(100),             -- 동명칭 (다세대 구분)
    grnd_flr    INTEGER,                  -- 지상 층수
    ugrnd_flr   INTEGER,                  -- 지하 층수
    bld_ht      REAL,                     -- 건물 높이 (m)
    geom        GEOMETRY(MultiPolygon, 4326)  -- 2D footprint
);
CREATE INDEX idx_footprints_pnu ON building_footprints(pnu);
CREATE INDEX idx_footprints_geom ON building_footprints USING GIST(geom);

-- 2. 건축물대장 속성
CREATE TABLE building_ledger (
    id              SERIAL PRIMARY KEY,
    pnu             VARCHAR(19) NOT NULL,
    bld_nm          VARCHAR(200),
    dong_nm         VARCHAR(100),
    main_purps_cd   VARCHAR(5),           -- 주용도코드
    main_purps_nm   VARCHAR(100),         -- 주용도명
    strct_cd        VARCHAR(5),           -- 구조코드
    strct_nm        VARCHAR(100),         -- 구조명
    grnd_flr_cnt    INTEGER,              -- 지상 층수
    ugrnd_flr_cnt   INTEGER,             -- 지하 층수
    bld_ht          REAL,                 -- 높이 (m)
    tot_area        REAL,                 -- 연면적 (m2)
    bld_area        REAL,                 -- 건축면적 (m2)
    use_apr_day     DATE,                 -- 사용승인일 (건축년도 대용)
    enrgy_eff_rate  VARCHAR(10),          -- 에너지효율등급
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_ledger_pnu ON building_ledger(pnu);

-- 3. 통합 뷰 (핵심 JOIN)
CREATE MATERIALIZED VIEW buildings_enriched AS
SELECT
    f.gid,
    f.pnu,
    f.geom,
    COALESCE(f.dong_nm, l.dong_nm) AS dong_nm,
    COALESCE(f.bld_ht, l.bld_ht, l.grnd_flr_cnt * 3.3, 10.0) AS height,
    l.main_purps_nm AS usage_type,
    l.strct_nm AS structure_type,
    l.grnd_flr_cnt AS floors_above,
    l.ugrnd_flr_cnt AS floors_below,
    l.tot_area,
    l.bld_area,
    l.use_apr_day,
    l.enrgy_eff_rate AS energy_grade,
    -- 원형 분류용 파생 컬럼
    CASE
        WHEN l.use_apr_day < '2001-01-01' THEN 'pre-2001'
        WHEN l.use_apr_day < '2010-01-01' THEN '2001-2009'
        WHEN l.use_apr_day < '2017-01-01' THEN '2010-2016'
        ELSE '2017-present'
    END AS vintage_class,
    CASE
        WHEN l.tot_area < 500 THEN 'small'
        WHEN l.tot_area < 3000 THEN 'medium'
        ELSE 'large'
    END AS size_class
FROM building_footprints f
LEFT JOIN building_ledger l
    ON f.pnu = l.pnu
    AND (f.dong_nm = l.dong_nm OR f.dong_nm IS NULL OR l.dong_nm IS NULL);

CREATE INDEX idx_enriched_geom ON buildings_enriched USING GIST(geom);
CREATE INDEX idx_enriched_pnu ON buildings_enriched(pnu);
COMMENT ON MATERIALIZED VIEW buildings_enriched IS
    'GIS footprint + 건축물대장 속성 PNU JOIN. 높이 우선순위: footprint > ledger > 층수x3.3 > 10m 기본값';

-- 4. 에너지 시뮬레이션 결과
CREATE TABLE energy_results (
    id              SERIAL PRIMARY KEY,
    pnu             VARCHAR(19) NOT NULL,
    archetype_id    INTEGER REFERENCES building_archetypes(id),
    -- 연간 에너지 (kWh/m2/yr)
    heating         REAL,
    cooling         REAL,
    hot_water       REAL,
    lighting        REAL,
    ventilation     REAL,
    total_energy    REAL,
    -- 외피 파라미터 (추정값)
    wall_uvalue     REAL,     -- W/m2K
    roof_uvalue     REAL,
    window_uvalue   REAL,
    wwr             REAL,     -- 창면적비 (0~1)
    -- 메타
    simulation_type VARCHAR(20),  -- 'energyplus' | 'ml_surrogate'
    simulated_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_energy_pnu ON energy_results(pnu);

-- 5. 건물 원형 정의
CREATE TABLE building_archetypes (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100),
    usage_category  VARCHAR(50),   -- 주거, 사무, 상업, 교육 ...
    vintage_class   VARCHAR(20),   -- pre-2001, 2001-2009, ...
    size_class      VARCHAR(10),   -- small, medium, large
    climate_zone    VARCHAR(10),   -- 중부1, 중부2, 남부, 제주
    -- 기본 외피 파라미터 (국토부 고시 기반)
    wall_uvalue     REAL,
    roof_uvalue     REAL,
    floor_uvalue    REAL,
    window_uvalue   REAL,
    default_wwr     REAL,
    -- 기본 내부 부하
    occupancy_density REAL,        -- 인/m2
    lighting_power    REAL,        -- W/m2
    equipment_power   REAL,        -- W/m2
    -- 시뮬레이션 결과 (원형 대표값)
    ref_heating     REAL,
    ref_cooling     REAL,
    ref_total       REAL,
    -- IDF/OSM 파일 경로
    energyplus_idf  VARCHAR(500),
    openstudio_osm  VARCHAR(500)
);
```

#### 3.2.2 PNU 매칭 전략

```
PNU 코드 구조 (19자리):
┌──┬───┬───┬──┬─┬────┬────┐
│시도│시군구│읍면동│ 리 │산│ 본번 │ 부번 │
│ 2 │  3  │  3  │ 2 │1│  4  │  4  │
└──┴───┴───┴──┴─┴────┴────┘

매칭 우선순위:
1. PNU + 동명칭 완전 매칭
2. PNU 매칭 (동명칭 NULL인 단독 건물)
3. PNU 매칭 + 면적 유사도 (다세대 폴백)
4. 매칭 실패 → 미매칭 건물 테이블에 기록 (수동 검토)
```

### 3.3 3D Tiles 생성 모듈 (`src/tile_generation/`)

#### 3.3.1 LoD1 생성 파이프라인

```
Step 1: PostGIS에서 3D 익스트루전
────────────────────────────────
  buildings_enriched.geom (2D Polygon)
  + height 컬럼
  → ST_Extrude(geom, 0, 0, height)
  → 3D PolyhedralSurface

Step 2: pg2b3dm 변환
────────────────────
  $ pg2b3dm \
    -h localhost -U postgres -d buildings \
    -t buildings_enriched \
    -c geom_3d \
    --idcolumn gid \
    --attributecolumns pnu,usage_type,energy_grade,height,floors_above,total_energy \
    -o ./output_tiles/mapo \
    --geometricerrors 500,200,50,10 \
    --maxfeatures 200

  출력:
  output_tiles/mapo/
  ├── tileset.json
  └── content/
      ├── 0_0_0.b3dm      (Tier 1: 도시)
      ├── 1_0_0.b3dm      (Tier 2: 지구)
      ├── ...
      └── 3_15_12.b3dm    (Tier 3: 거리)

Step 3: S3 업로드
────────────────
  $ aws s3 sync ./output_tiles/mapo \
    s3://building-energy-3d-tiles/mapo/ \
    --content-encoding gzip
```

#### 3.3.2 HLOD 설정

pg2b3dm `--geometricerrors` 파라미터로 3단계 HLOD 구현:

| Tier | geometricError | maxfeatures/tile | 내용 |
|------|---------------|-----------------|------|
| 1 | 500m | 전체 | 마포구 전체 개요 |
| 2 | 50m | 200 | 블록/동 단위 |
| 3 | 10m | 50 | 개별 건물 상세 |

#### 3.3.3 에너지 색상 코딩

CesiumJS에서 3D Tiles 스타일링:

```javascript
tileset.style = new Cesium.Cesium3DTileStyle({
    color: {
        conditions: [
            ["${energy_grade} === '1+++'", "color('green', 0.8)"],
            ["${energy_grade} === '1++'",  "color('limegreen', 0.8)"],
            ["${energy_grade} === '1+'",   "color('yellowgreen', 0.8)"],
            ["${energy_grade} === '1'",    "color('yellow', 0.8)"],
            ["${energy_grade} === '2'",    "color('gold', 0.8)"],
            ["${energy_grade} === '3'",    "color('orange', 0.8)"],
            ["${energy_grade} === '4'",    "color('darkorange', 0.8)"],
            ["${energy_grade} === '5'",    "color('red', 0.8)"],
            ["${energy_grade} === '6'",    "color('darkred', 0.8)"],
            ["${energy_grade} === '7'",    "color('maroon', 0.8)"],
            ["true", "color('gray', 0.6)"]  // 등급 미보유
        ]
    }
});
```

### 3.4 베이스맵 (`PMTiles`)

#### 3.4.1 생성

```bash
# 한국 OSM 데이터 → PMTiles
# 1. Geofabrik에서 한국 PBF 다운로드
wget https://download.geofabrik.de/asia/south-korea-latest.osm.pbf

# 2. Planetiler로 PMTiles 생성
java -jar planetiler.jar \
  --osm-path=south-korea-latest.osm.pbf \
  --output=korea.pmtiles \
  --nodemap-type=array \
  --storage=mmap
```

#### 3.4.2 서빙

- `korea.pmtiles` 파일을 S3에 업로드
- CesiumJS에서 MapLibre 이미지 레이어로 로딩하거나
- 별도 MapLibre GL JS 인스턴스로 2D 베이스맵 제공

### 3.5 에너지 시뮬레이션 모듈 (`src/simulation/`)

#### 3.5.1 원형 매칭 흐름

```
buildings_enriched 레코드
        │
        ▼
  원형 분류 (usage_type × vintage_class × size_class × climate_zone)
        │
        ▼
  building_archetypes 테이블 매칭
        │
        ├── 매칭 성공 → 원형의 시뮬레이션 결과 적용
        │
        └── 매칭 실패 → 가장 유사한 원형 선택 (거리 기반)
```

#### 3.5.2 한국 외피 기준 매핑 (국토부 고시)

마포구 = 중부2 지역

| 건축년도 | 벽체 U-value | 지붕 U-value | 창호 U-value | 근거 |
|----------|-------------|-------------|-------------|------|
| ~2001 | 0.58 | 0.41 | 3.40 | 2001년 이전 기준 |
| 2001~2010 | 0.47 | 0.29 | 2.70 | 에너지절약설계기준 개정 |
| 2010~2017 | 0.35 | 0.20 | 1.80 | 강화 기준 |
| 2017~ | 0.24 | 0.15 | 1.20 | 현행 기준 (고시 2024-421호) |

*주의: 실제 값은 국토부 고시 원문에서 정확한 수치 확인 필요*

#### 3.5.3 시뮬레이션 실행 전략

```
Phase 1 (MVP): 원형 직접 매핑
─────────────────────────────
  원형 200~500개 사전 시뮬레이션 (EnergyPlus)
  → 결과를 building_archetypes 테이블에 저장
  → 각 건물은 가장 가까운 원형의 결과를 사용

Phase 2: ML 대리모델 (사용자 추후 대체)
──────────────────────────────────────
  원형 시뮬레이션 결과를 학습 데이터로 활용
  → XGBoost 학습 (입력: 용도, 면적, 높이, 건축년도, U-value 등)
  → 건물별 맞춤 예측 (원형 보간)
  → 인터페이스: predict(building_features) → energy_breakdown
```

#### 3.5.4 Celery 태스크 정의

```python
# src/simulation/tasks.py

@celery.task(bind=True, max_retries=3)
def run_archetype_simulation(self, archetype_id: int):
    """단일 원형의 EnergyPlus 시뮬레이션 실행"""
    # 1. archetype 파라미터 로드
    # 2. IDF 생성 (OpenStudio SDK)
    # 3. EnergyPlus 실행
    # 4. 결과 파싱 → building_archetypes 테이블 업데이트

@celery.task
def match_buildings_to_archetypes():
    """모든 건물을 원형에 매칭하고 에너지 결과 할당"""
    # buildings_enriched의 각 건물 → 최적 archetype 매칭
    # → energy_results 테이블에 결과 저장

@celery.task
def regenerate_3d_tiles():
    """데이터 변경 후 3D Tiles 재생성"""
    # 1. pg2b3dm 실행
    # 2. S3 업로드
    # 3. CDN 캐시 무효화
```

### 3.6 프론트엔드 (`React + Resium`)

#### 3.6.1 컴포넌트 구조

```
src/frontend/
├── App.tsx
├── components/
│   ├── MapViewer/
│   │   ├── CesiumViewer.tsx      # Resium Viewer 래퍼
│   │   ├── BuildingTileset.tsx   # 3D Tiles 로딩 + 스타일링
│   │   ├── BasemapLayer.tsx      # PMTiles 또는 VWorld 베이스
│   │   └── EnergyOverlay.tsx     # 에너지 색상 코딩 로직
│   ├── Panel/
│   │   ├── BuildingDetail.tsx    # 건물 클릭 시 상세 정보
│   │   ├── EnergyChart.tsx       # 에너지 분해 차트
│   │   └── EnvelopeInfo.tsx      # 외피 정보 표시
│   ├── Controls/
│   │   ├── SearchBar.tsx         # 주소 검색
│   │   ├── FilterPanel.tsx       # 에너지등급/년도/용도 필터
│   │   └── LayerControl.tsx      # 레이어 토글
│   └── Dashboard/
│       ├── StatsPanel.tsx        # 현재 뷰 통계
│       └── Legend.tsx            # 색상 범례
├── hooks/
│   ├── useBuildings.ts           # 건물 데이터 fetch
│   ├── useEnergyData.ts          # 에너지 데이터 fetch
│   └── useTilesetStyle.ts        # 3D Tiles 스타일 관리
├── api/
│   └── client.ts                 # FastAPI 통신
└── types/
    └── building.ts               # TypeScript 타입 정의
```

#### 3.6.2 CesiumJS 설정

```typescript
// CesiumViewer.tsx 핵심 설정
const viewerOptions = {
    terrainProvider: await CesiumTerrainProvider.fromUrl(
        IonResource.fromAssetId(1)  // Cesium World Terrain
    ),
    baseLayer: false,  // PMTiles로 대체
    animation: false,
    timeline: false,
    geocoder: false,   // 자체 검색으로 대체
};

// 마포구 초기 뷰
viewer.camera.flyTo({
    destination: Cartesian3.fromDegrees(126.9095, 37.5565, 3000),
    orientation: {
        heading: 0,
        pitch: -45 * (Math.PI / 180),
        roll: 0
    }
});
```

### 3.7 API 엔드포인트 명세

#### 3.7.1 건물 데이터 API — ✅ 구현 완료

| Method | Path | 설명 | 응답 | 상태 |
|--------|------|------|------|------|
| GET | `/api/v1/buildings/` | 건물 목록 (bbox/필터) | GeoJSON FeatureCollection (LIMIT 3000) | ✅ |
| GET | `/api/v1/buildings/{pnu}` | 건물 상세 + 에너지 분해 | Building GeoJSON Feature | ✅ |
| GET | `/api/v1/buildings/stats` | 현재 뷰 통계 (등급/용도 분포) | Stats JSON | ✅ |
| GET | `/api/v1/buildings/pick` | 클릭 위치 최근접 건물 (PostGIS KNN) | `{pnu, building_name}` | ✅ |
| GET | `/api/v1/buildings/centroids` | 경량 centroid 목록 | `{count, centroids[]}` | ✅ |

#### 3.7.2 필터/검색 API — ✅ 구현 완료

| Method | Path | 설명 | 파라미터 | 상태 |
|--------|------|------|---------|------|
| GET | `/api/v1/search` | 건물명 검색 (ILIKE) | `q` (max 100자) | ✅ |
| POST | `/api/v1/filter` | 다중 조건 필터 | `energy_grades[]`, `vintage_classes[]`, `usage_types[]`, `bbox[]` | ✅ |
| GET | `/api/v1/filter/export` | 필터 결과 CSV 다운로드 | 동일 필터 파라미터 | ✅ |

#### 3.7.3 에너지 예측 + 시뮬레이션 API — Phase 4 예정

| Method | Path | 설명 | 상태 |
|--------|------|------|------|
| GET | `/api/v1/buildings/{pnu}/energy/daily` | 일단위 에너지 예측 (`?date=&model=`) | 미구현 |
| GET | `/api/v1/buildings/{pnu}/energy/hourly` | 시간단위 에너지 예측 (`?date=&model=`) | 미구현 |
| GET | `/api/v1/models` | 사용 가능한 예측 모델 목록 | 미구현 |
| GET | `/api/v1/archetypes` | 원형 목록 | 미구현 |
| POST | `/api/v1/simulate` | EnergyPlus 시뮬레이션 실행 (async) | 미구현 |
| GET | `/api/v1/simulate/{task_id}` | 시뮬레이션 상태 | 미구현 |

#### 3.7.4 관리 API — Phase 5 예정

| Method | Path | 설명 | 상태 |
|--------|------|------|------|
| POST | `/api/v1/admin/sync/footprints` | GIS건물통합정보 동기화 트리거 | 미구현 (CLI 스크립트로 수동 실행) |
| POST | `/api/v1/admin/sync/ledger` | 건축물대장 동기화 트리거 | 미구현 |
| POST | `/api/v1/admin/tiles/regenerate` | 3D Tiles 재생성 트리거 | 미구현 |
| GET | `/api/v1/admin/status` | 시스템 상태 | 미구현 (`/health` 엔드포인트만 존재) |

---

## 3.8 에너지 예측 모델 아키텍처 (Pluggable Model Architecture)

### 설계 원칙

**모델 교체 가능성 (Pluggability)**: 모든 예측 모델은 동일한 추상 인터페이스(`EnergyPredictor`)를 구현하며, API 파라미터(`?model=`)로 런타임에 모델을 선택할 수 있다. 기본 모델을 사용자가 개발한 커스텀 모델로 언제든 교체 가능하다.

```
┌─────────────────────────────────────────────────┐
│            EnergyPredictor (ABC)                 │
│  predict_daily(building, date, weather) → dict   │
│  predict_hourly(building, date, weather) → list  │
│  model_info() → dict                            │
└──────────────┬──────────────────┬───────────────┘
               │                  │
    ┌──────────┴───┐    ┌────────┴────────┐
    │ Built-in     │    │ User Custom     │
    ├──────────────┤    ├─────────────────┤
    │ XGBoost      │    │ MyCustomModel   │
    │ LSTM         │    │ TransformerModel│
    │ Archetype    │    │ PhysicsModel    │
    └──────────────┘    └─────────────────┘
               │                  │
    ┌──────────┴──────────────────┴───────┐
    │          ModelRegistry               │
    │  register(name, predictor_class)     │
    │  get(name) → EnergyPredictor         │
    │  list() → [model_info, ...]          │
    └──────────────────────────────────────┘
```

### 추상 인터페이스

```python
# src/simulation/predictor_base.py
class EnergyPredictor(ABC):
    @abstractmethod
    def predict_daily(self, building: dict, date: str, weather: dict) -> dict:
        """일단위 예측 → {heating, cooling, hot_water, lighting, ventilation, total} (kWh/m²/day)"""

    @abstractmethod
    def predict_hourly(self, building: dict, date: str, weather: dict) -> list[dict]:
        """시간단위 예측 → [{hour, heating, cooling, ..., total}, ...] × 24 (kWh/m²/hr)"""

    @abstractmethod
    def model_info(self) -> dict:
        """모델 메타 → {id, name, version, type, algorithm, training_data, accuracy_metrics}"""
```

### 내장 모델

| 모델 ID | 예측 단위 | 알고리즘 | 학습 데이터 |
|---------|----------|---------|-----------|
| `archetype-annual-v1` | 연간 | 룩업 테이블 | 한국 건물 벤치마크 (현재 구현) |
| `xgboost-daily-v1` | 일단위 | XGBoost | EnergyPlus 시뮬레이션 (Phase 4) |
| `lstm-hourly-v1` | 시간단위 | LSTM | EnergyPlus hourly output (Phase 4) |

### 커스텀 모델 등록

```python
# src/simulation/custom/my_model.py
class MyPhysicsModel(EnergyPredictor):
    def predict_daily(self, building, date, weather):
        return {"heating": ..., "total": ...}  # 사용자 로직

# 등록
ModelRegistry.register("physics-custom-v1", MyPhysicsModel)
```

### API 모델 선택

```
GET /buildings/{pnu}/energy/daily?date=2026-03-23&model=xgboost-daily-v1
GET /buildings/{pnu}/energy/hourly?date=2026-03-23&model=lstm-hourly-v1
GET /models → [{id, name, version, type, algorithm}, ...]
```

### 파일 구조

```
src/simulation/
├── predictor_base.py       # EnergyPredictor ABC
├── model_registry.py       # ModelRegistry
├── archetypes.py           # archetype-annual-v1 (현재)
├── daily_predictor.py      # xgboost-daily-v1 (Phase 4)
├── hourly_predictor.py     # lstm-hourly-v1 (Phase 4)
├── weather.py              # 기상청 API
├── custom/                 # 사용자 커스텀 모델
│   └── README.md
└── ml_models/              # 학습된 모델 파일
    ├── daily_xgboost.joblib
    └── hourly_lstm.pt
```

---

## 3.9 프론트엔드 아키텍처 (2026-03 확정)

### 3.9.1 VWorld WebGL 3D 뷰어 (권장, `/vworld.html`)

```
VWorld WebGL 3D API 3.0
├── 스크립트: https://map.vworld.kr/js/webglMapInit.js.do?version=3.0&apiKey={KEY}
├── Cesium 내장: ws3d.viewer = Cesium.Viewer (Cesium API 직접 사용 가능)
├── 3D 건물: facility_build 레이어 (서울 LoD3-4 사진 텍스처)
├── 지형: 5m DEM
├── 위성사진: 25cm 정사영상
└── 에너지 오버레이: 우리 API → 클릭 → 상세 패널
```

**주요 특징:**
- VWorld이 좌표/높이/텍스처를 모두 정확히 처리 (좌표 불일치 문제 없음)
- `ws3d.viewer`가 Cesium.Viewer이므로 Entity, DataSource, Camera API 모두 사용 가능
- 별도 Cesium JS 파일 불필요 (VWorld 스크립트에 포함)
- 도메인 제한: API 키 발급 시 등록한 도메인에서만 작동

**성능 최적화:**
- `RequestScheduler.requestsByServer["xdworld.vworld.kr:443"] = 18` (동시 요청 18개)
- `viewer.scene.fog.enabled = true` (먼 거리 타일 컬링)
- `globe.maximumScreenSpaceError = 4` (원거리 디테일 축소)

### 3.9.2 React CesiumJS 뷰어 (대안, `/`)

```
React 19 + CesiumJS (직접) + Vite 6
├── 베이스맵: Bing Maps 위성 (Cesium Ion 기본)
├── 3D 건물: 자체 데이터 (72,930건) Entity 익스트루전
├── 에너지 색상: 소비량 기반 그라데이션 (green→orange)
├── 상세 패널: Zustand 상태 관리 + Recharts 에너지 분해 차트
├── 검색: 디바운스 건물명 검색
└── ErrorBoundary + WebGL context loss 핸들링
```

**제한사항:**
- 건물 텍스처 없음 (프로시저럴 색상만)
- Entity API 성능 한계 (5000건 이상 시 프레임 저하)
- 좌표/높이 불일치 가능 (VWorld 데이터 vs Bing 위성)

### 3.9.3 데이터 규모 (2026-03-23 기준)

| 테이블 | 건수 | 범위 |
|--------|------|------|
| `building_footprints` | **766,386** | 서울 전역 (25구) |
| `buildings_enriched` (MV) | **766,380** | footprint + ledger LATERAL JOIN |
| `energy_results` | **766,380** | archetype 기반 에너지 추정 |
| `building_ledger` | **22,191** | 마포구 (총괄표제부 + 표제부) |
| `building_centroids` | **766,380** | Point GiST KNN 전용 |

### 3.9.4 API 엔드포인트 성능 (766K건 기준)

| 엔드포인트 | DB 쿼리 시간 | HTTP 응답 시간 | 비고 |
|-----------|-------------|---------------|------|
| `/buildings/pick` | **0.07ms** | ~300ms | `building_centroids` Point KNN |
| `/buildings/stats` | **46ms** | ~100ms | 집계 (GZip 적용) |
| `/buildings/{pnu}` | ~1ms | ~50ms | 단건 PNU 조회 |
| `/buildings/` (bbox) | ~500ms | ~2s | GeoJSON 5000건 (React 뷰어) |

**성능 최적화 적용:**
- `building_centroids`: MultiPolygon GiST KNN (300ms) → Point GiST KNN (0.07ms)
- GZip 미들웨어: API 응답 60% 압축
- VWorld 뷰어: tileCacheSize=1000, RequestScheduler 12, fog

---

## 4. 배포 아키텍처

### 4.1 MVP 배포 (단일 서버)

```
┌─────────────────────────────────────┐
│           단일 서버 (또는 VPS)        │
│                                     │
│  ┌──────────┐  ┌──────────────┐     │
│  │ Nginx    │  │ PostgreSQL   │     │
│  │ (리버스  │  │ + PostGIS    │     │
│  │  프록시)  │  └──────────────┘     │
│  └────┬─────┘  ┌──────────────┐     │
│       │        │ Redis        │     │
│       ▼        └──────────────┘     │
│  ┌──────────┐  ┌──────────────┐     │
│  │ FastAPI  │  │ Celery Worker│     │
│  │ (Uvicorn)│  │              │     │
│  └──────────┘  └──────────────┘     │
│                                     │
└─────────────────────────────────────┘

정적 파일:
  3D Tiles → S3 + CloudFront (또는 로컬 Nginx)
  PMTiles  → S3 (또는 로컬 Nginx)
  React 빌드 → Nginx 직접 서빙
```

### 4.2 Docker Compose 구성

```yaml
services:
  db:
    image: postgis/postgis:16-3.4
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: buildings
      POSTGRES_PASSWORD: ${DB_PASSWORD}

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes

  api:
    build: .
    command: uvicorn src.main:app --host 0.0.0.0 --port 8000
    depends_on: [db, redis]
    environment:
      DATABASE_URL: postgresql://postgres:${DB_PASSWORD}@db/buildings
      REDIS_URL: redis://redis:6379

  worker:
    build: .
    command: celery -A src.shared.celery_app worker --loglevel=info
    depends_on: [db, redis]

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./frontend/dist:/usr/share/nginx/html
      - ./output_tiles:/usr/share/nginx/tiles  # MVP: 로컬 타일 서빙

volumes:
  pgdata:
```

---

## 5. 보안

| 영역 | 대책 |
|------|------|
| API 키 관리 | `.env` 파일, Docker secrets. 절대 코드에 포함 금지 |
| API 인증 | MVP: API Key 헤더. Phase 2: JWT |
| SQL Injection | SQLAlchemy ORM + Parameterized Query |
| CORS | FastAPI CORSMiddleware, 허용 오리진 제한 |
| Rate Limiting | FastAPI slowapi 또는 Nginx rate limit |
| 정부 API 키 보호 | 서버 사이드 프록시, 클라이언트 노출 금지 |

---

## 6. 모니터링 & 로깅

| 항목 | 도구 | 용도 |
|------|------|------|
| API 로그 | Python logging + structlog | 요청/응답 로깅 |
| Celery 모니터링 | Flower (웹 UI) | 태스크 상태/큐 모니터링 |
| DB 성능 | pg_stat_statements | 슬로우 쿼리 감지 |
| 에러 추적 | Sentry (Phase 2) | 프로덕션 에러 알림 |

---

## 7. 검증 계획

### 7.1 데이터 파이프라인 검증

1. 마포구 GIS건물통합정보 SHP 로딩 → PostGIS 건물 수 확인 (25,000~30,000)
2. 건축물대장 API 호출 → 마포구 법정동 전수 수집 → 레코드 수 비교
3. PNU JOIN 매칭률 확인 → 목표: 90% 이상
4. 높이 데이터 유효성: NULL 비율, 이상치(100m 초과) 검출

### 7.2 3D Tiles 검증

1. pg2b3dm 실행 → tileset.json 생성 확인
2. CesiumJS에서 로딩 → 건물 형태 육안 확인
3. 성능: 30fps 이상 유지되는지 (10,000건물 뷰포트)
4. 에너지 색상 코딩 → 알려진 건물의 등급과 색상 대조

### 7.3 에너지 시뮬레이션 검증

1. 원형 매칭: 마포구 건물 → 원형 배분 분포 확인
2. 시뮬레이션 결과 vs 공공건축물 에너지소비 실측 데이터 비교
3. 오차 목표: 30% 이내 (원형 기반 초기)

### 7.4 E2E 통합 검증

```
SHP 다운로드 → ogr2ogr → PostGIS
→ 건축물대장 API → PNU JOIN
→ pg2b3dm → 3D Tiles
→ S3 업로드 → CesiumJS 로딩
→ 건물 클릭 → API 호출 → 상세 정보 표시
→ 에너지 색상 코딩 정상 표시
```

---

## 부록: 기술 스택 버전

| 기술 | 버전 | 비고 |
|------|------|------|
| Python | 3.11+ | FastAPI, Celery |
| Node.js | 20 LTS | React 프론트엔드 빌드 |
| PostgreSQL | 16 | PostGIS 3.4 |
| Redis | 7 | Celery broker + cache |
| CesiumJS | 1.120+ | Resium 1.19+ |
| React | 18 | Vite 빌드 |
| pg2b3dm | 2.26+ | 3D Tiles 변환 |
| Planetiler | latest | PMTiles 생성 |
| Docker | 24+ | Docker Compose v2 |
