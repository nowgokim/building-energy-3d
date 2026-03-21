# RFC: 데이터 파이프라인

**문서 버전**: 1.1
**작성일**: 2026-03-21
**최종 수정**: 2026-03-21 (전문가 리뷰 반영: 집합건물 1:N, 도로명주소, 국가공간정보포털 폐쇄)
**관련 문서**: [PRD](./PRD.md) | [Architecture](./ARCHITECTURE.md)
**대상 지역**: 서울특별시 마포구 (시군구코드: 11440)

---

## 1. 개요

마포구 약 25,000~30,000동의 건물을 3D 에너지 시각화하기 위한 데이터 수집 → 통합 → 3D Tiles 생성 파이프라인.

### 1.1 파이프라인 전체 흐름

```
[1단계: 수집]          [2단계: 적재/통합]       [3단계: 3D 생성]        [4단계: 서빙]
GIS건물통합정보(SHP) ─→ ogr2ogr ──→ PostGIS ─→ ST_Extrude ──→ pg2b3dm ──→ S3+CDN
건축물대장(API) ──────→ Python ───→ PostGIS ─┘   (3D)          (3D Tiles)
                                      ↑
                                PNU JOIN (19자리)
```

---

## 2. 데이터 소스 상세

### 2.1 GIS건물통합정보

**출처**: https://www.data.go.kr/data/15083092/fileData.do
**포맷**: SHP (Shapefile)
**좌표계**: EPSG:5174 (한국 중부원점, Bessel 타원체)
**갱신**: 월 1회 (일별 갱신본도 존재: data.go.kr/data/15052097)

**주요 필드:**

| 필드명 | 코드 | 설명 | 용도 |
|--------|------|------|------|
| geometry | - | MultiPolygon (건물 footprint) | 3D 익스트루전 입력 |
| 건축물용도 | A9 | 건물 주용도 | 원형 분류 |
| 사용승인일자 | A13 | 사용승인일 (건축년도 대용) | 원형 분류, U-value 매핑 |
| 높이 | A16 | 건물 높이 (m) | 3D 익스트루전 |
| 지상층수 | A32 | 지상 층수 | 높이 추정 폴백 |
| PNU | - | 19자리 필지 고유번호 | JOIN 키 |

**좌표 변환 주의**: EPSG:5174 → EPSG:4326(WGS84) 변환 시 약 300m 오프셋 발생 가능. ogr2ogr의 `-s_srs`/`-t_srs` 파라미터 사용.

### 2.2 건축물대장 API

**출처**: https://www.data.go.kr/data/15134735/openapi.do
**포맷**: JSON (`&_type=json` 파라미터)
**Rate Limit**: 개발 1,000건/일, 운영 100,000건/일

**총괄표제부 주요 응답 필드:**

| 필드 | 설명 | 용도 |
|------|------|------|
| 시군구코드 | 5자리 | PNU 구성 |
| 법정동코드 | 5자리 | PNU 구성 |
| 대지구분코드 | 산/대지 구분 | PNU 구성 |
| 번, 지 | 본번/부번 | PNU 구성 |
| 건물명 | - | 표시용 |
| 주용도코드, 주용도명 | - | 원형 분류 |
| 구조코드, 구조명 | RC/철골/조적 등 | 열용량 추정 |
| 지상층수, 지하층수 | - | 높이 추정 |
| 건물높이 | m | 3D 익스트루전 |
| 연면적, 건축면적 | m² | 에너지 계산 |
| 사용승인일 | YYYYMMDD | 건축년도, U-value 매핑 |
| 에너지효율등급 | 1+++~7 | 시각화 |
| EPI점수 | 에너지성능지표 | 에너지 분석 |
| 친환경건축물등급 | - | 참고 |

**PNU 생성 로직:**

```python
# 건축물대장 응답으로부터 PNU 19자리 구성
transform_dict = {"0": "1", "1": "2", "2": "3"}
pnu = (
    row['시군구코드']           # 5자리 (시도2 + 시군구3)
    + row['법정동코드']          # 5자리 (읍면동3 + 리2)
    + transform_dict[row['대지구분코드']]  # 1자리 (산:2, 대지:1)
    + row['번'].zfill(4)        # 4자리 본번
    + row['지'].zfill(4)        # 4자리 부번
)  # = 19자리
```

### 2.3 마포구 법정동코드 목록

마포구 (11440) 법정동 26개:

행정표준코드관리시스템(code.go.kr)에서 조회.
PublicDataReader로 프로그래밍 조회:

```python
import PublicDataReader as pdr

code = pdr.code_bdong()
mapo = code[code['시군구명'].str.contains('마포구')]
mapo_bdong_codes = mapo['법정동코드'].tolist()
# 아현동, 공덕동, 신공덕동, 도화동, 마포동, 용강동,
# 토정동, 대흥동, 염리동, 노고산동, 신수동, 현석동,
# 구수동, 창전동, 상수동, 하중동, 합정동, 망원동,
# 연남동, 성산동, 중동, 상암동, 서교동, 동교동,
# 당인동 등
```

---

## 3. 수집 파이프라인 상세

### 3.1 Step 1: GIS건물통합정보 벌크 수집

```bash
# 1. data.go.kr에서 SHP 파일 다운로드 (수동 또는 자동화)
# 마포구 데이터 추출 (전국 파일에서 시군구코드 11440 필터)

# 2. QGIS CLI 또는 ogr2ogr로 마포구 필터링 + 좌표 변환
ogr2ogr \
  -f "PostgreSQL" \
  PG:"host=localhost dbname=buildings user=postgres password=${DB_PASSWORD}" \
  -nlt MULTIPOLYGON \
  -nln building_footprints \
  -lco GEOMETRY_NAME=geom \
  -lco FID=gid \
  -s_srs EPSG:5174 \
  -t_srs EPSG:4326 \
  -where "시군구코드 = '11440'" \
  전국_GIS건물통합정보.shp

# 3. 공간 인덱스 생성
psql -d buildings -c "CREATE INDEX idx_fp_geom ON building_footprints USING GIST(geom);"
psql -d buildings -c "CREATE INDEX idx_fp_pnu ON building_footprints(pnu);"
```

**예상 처리 시간**: SHP 로딩 약 5~10분 (마포구 25,000~30,000건)

### 3.2 Step 2: 건축물대장 API 수집

```python
# src/data_ingestion/collect_ledger.py

from PublicDataReader import BuildingLedger
import PublicDataReader as pdr
import pandas as pd
from sqlalchemy import create_engine
import time

service_key = os.environ["DATA_GO_KR_API_KEY"]
api = BuildingLedger(service_key)
engine = create_engine(os.environ["DATABASE_URL"])

# 마포구 법정동 코드 조회
code = pdr.code_bdong()
mapo_codes = code[
    (code['시군구코드'] == '11440') &
    (code['폐지여부'] == '존재')
]['법정동코드'].tolist()

all_buildings = []

for bdong in mapo_codes:
    try:
        df = api.get_data(
            ledger_type="총괄표제부",
            sigungu_code="11440",
            bdong_code=bdong[-5:],  # 하위 5자리 (읍면동+리)
        )
        if df is not None and len(df) > 0:
            all_buildings.append(df)
        time.sleep(0.5)  # Rate limit 방어
    except Exception as e:
        logging.error(f"법정동 {bdong} 수집 실패: {e}")
        continue

result = pd.concat(all_buildings, ignore_index=True)

# PNU 컬럼 생성
transform = {"0": "1", "1": "2", "2": "3"}
result['pnu'] = (
    result['시군구코드']
    + result['법정동코드']
    + result['대지구분코드'].map(transform).fillna('1')
    + result['번'].str.zfill(4)
    + result['지'].str.zfill(4)
)

# PostgreSQL 적재
result.to_sql('building_ledger', engine, if_exists='replace', index=False)
```

**예상 소요 시간**:
- 개발계정 (1,000건/일): ~25~30일 → **비실용적**
- 운영계정 (100,000건/일): ~1일 충분
- **권장**: 운영계정 즉시 신청

### 3.2.1 집합건물(아파트) 1:N 매칭 처리

**문제**: 1개 PNU에 수십~수백 개 건축물대장 레코드가 존재 (아파트 단지).
건축물대장 계층: 총괄표제부(단지) → 표제부(동별) → 전유부(세대별)

```python
# 집합건물 처리: 표제부(동별) 수준으로 수집
for bdong in mapo_codes:
    # 총괄표제부: 단지 전체 정보
    df_master = api.get_data(ledger_type="총괄표제부", sigungu_code="11440", bdong_code=bdong[-5:])

    # 표제부: 동별 정보 (건물 1동 = footprint 1개에 대응)
    df_title = api.get_data(ledger_type="표제부", sigungu_code="11440", bdong_code=bdong[-5:])

    # JOIN 키: PNU(19자리) + 동명칭
    # 독립건물: PNU만으로 1:1 매칭
    # 집합건물: PNU + 동명칭으로 footprint와 매칭
```

**건물관리번호 (25자리) 활용**:
```
BD_MGT_SN = PNU(19자리) + 건물고유번호(6자리)
```
- 전국 유일한 건물 식별자
- 행정구역 변경에도 불변
- GIS건물통합정보 SHP에도 포함 → **최우선 JOIN 키**로 사용

### 3.2.2 도로명주소 API 연동 (검색 기능용)

```python
# 행안부 도로명주소 API (data.go.kr/data/15057017)
# 사용자 주소 검색 → 좌표 + PNU 반환

import requests

def search_address(keyword: str) -> dict:
    """도로명주소 검색 → 좌표 + PNU"""
    resp = requests.get(
        "https://business.juso.go.kr/addrlink/addrLinkApi.do",
        params={
            "confmKey": os.environ["JUSO_API_KEY"],
            "keyword": keyword,
            "resultType": "json",
            "countPerPage": 10,
        }
    )
    results = resp.json()['results']['juso']
    # 반환: 도로명주소, 지번주소, 우편번호, 좌표(위경도)
    return results
```

### 3.3 Step 3: PNU 기반 JOIN

```sql
-- Materialized View: footprint(2D 폴리곤) + ledger(속성) 통합
CREATE MATERIALIZED VIEW buildings_enriched AS
SELECT
    f.gid,
    f.pnu,
    f.geom,                                                    -- 2D footprint
    COALESCE(l.건물명, '') AS building_name,
    COALESCE(l.주용도명, f.건축물용도, '미분류') AS usage_type,
    l.구조명 AS structure_type,

    -- 높이 결정 (우선순위: GIS높이 > 대장높이 > 층수x3.3 > 10m)
    COALESCE(
        NULLIF(f.높이, 0),
        NULLIF(l.건물높이, 0),
        GREATEST(COALESCE(l.지상층수, f.지상층수, 3), 1) * 3.3,
        10.0
    ) AS height,

    COALESCE(l.지상층수, f.지상층수, 3) AS floors_above,
    l.지하층수 AS floors_below,
    l.연면적 AS total_area,
    l.건축면적 AS building_area,

    -- 건축년도 (사용승인일 기반)
    COALESCE(l.사용승인일, f.사용승인일자)::date AS approval_date,
    EXTRACT(YEAR FROM COALESCE(l.사용승인일, f.사용승인일자)::date) AS built_year,

    l.에너지효율등급 AS energy_grade,
    l.epi점수 AS epi_score,

    -- 원형 분류용 파생 컬럼
    CASE
        WHEN EXTRACT(YEAR FROM COALESCE(l.사용승인일, f.사용승인일자)::date) < 2001
            THEN 'pre-2001'
        WHEN EXTRACT(YEAR FROM COALESCE(l.사용승인일, f.사용승인일자)::date) < 2010
            THEN '2001-2009'
        WHEN EXTRACT(YEAR FROM COALESCE(l.사용승인일, f.사용승인일자)::date) < 2017
            THEN '2010-2016'
        ELSE '2017-present'
    END AS vintage_class,

    CASE
        WHEN l.연면적 < 500 THEN 'small'
        WHEN l.연면적 < 3000 THEN 'medium'
        ELSE 'large'
    END AS size_class

FROM building_footprints f
LEFT JOIN building_ledger l
    ON f.pnu = l.pnu
WHERE ST_IsValid(f.geom)        -- 유효한 지오메트리만
  AND f.pnu IS NOT NULL;        -- PNU 있는 건물만

-- 인덱스
CREATE INDEX idx_enriched_geom ON buildings_enriched USING GIST(geom);
CREATE INDEX idx_enriched_pnu ON buildings_enriched(pnu);
CREATE INDEX idx_enriched_usage ON buildings_enriched(usage_type);
CREATE INDEX idx_enriched_grade ON buildings_enriched(energy_grade);
```

**매칭 품질 검증:**

```sql
-- 매칭률 확인
SELECT
    COUNT(*) AS total_footprints,
    COUNT(building_name) AS matched_with_ledger,
    ROUND(100.0 * COUNT(building_name) / COUNT(*), 1) AS match_rate_pct
FROM buildings_enriched;
-- 목표: 90% 이상

-- 높이 데이터 품질
SELECT
    COUNT(*) AS total,
    COUNT(CASE WHEN height > 0 AND height < 200 THEN 1 END) AS valid_height,
    COUNT(CASE WHEN height IS NULL OR height = 0 THEN 1 END) AS missing_height,
    AVG(height) AS avg_height,
    MAX(height) AS max_height
FROM buildings_enriched;

-- 미매칭 건물 기록
CREATE TABLE unmatched_buildings AS
SELECT gid, pnu, geom
FROM building_footprints f
WHERE NOT EXISTS (
    SELECT 1 FROM building_ledger l WHERE f.pnu = l.pnu
);
```

---

## 4. 3D Tiles 생성 파이프라인

### 4.1 PostGIS 3D 익스트루전

PostGIS `ST_Extrude` 함수 사용 (SFCGAL 백엔드 필요):

```sql
-- SFCGAL 확장 활성화
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;

-- 3D 지오메트리 컬럼 추가
ALTER TABLE buildings_enriched ADD COLUMN IF NOT EXISTS geom_3d geometry;

-- 2D footprint → 3D PolyhedralSurface 익스트루전
UPDATE buildings_enriched
SET geom_3d = ST_Extrude(
    ST_Force2D(geom),  -- 2D로 강제 (z값 제거)
    0, 0, height       -- x=0, y=0, z=height
);

-- 인덱스
CREATE INDEX idx_enriched_geom3d ON buildings_enriched USING GIST(geom_3d);
```

### 4.2 pg2b3dm 실행

```bash
# pg2b3dm 설치 (.NET 도구)
dotnet tool install -g pg2b3dm

# 3D Tiles 생성
pg2b3dm \
  -h localhost \
  -U postgres \
  -d buildings \
  -t buildings_enriched \
  -c geom_3d \
  --idcolumn gid \
  --attributecolumns pnu,building_name,usage_type,energy_grade,height,floors_above,total_area,vintage_class,epi_score \
  -o ./output_tiles/mapo \
  --geometricerrors 500,100,50,10 \
  --maxfeatures 200
```

**pg2b3dm 파라미터 설명:**

| 파라미터 | 값 | 설명 |
|----------|-----|------|
| `-c` | geom_3d | 3D 지오메트리 컬럼 |
| `--idcolumn` | gid | 건물 고유 ID |
| `--attributecolumns` | pnu,... | 3D Tiles 메타데이터에 포함할 속성 |
| `--geometricerrors` | 500,100,50,10 | HLOD 단계별 geometricError (m) |
| `--maxfeatures` | 200 | 타일당 최대 건물 수 |

**출력 구조:**

```
output_tiles/mapo/
├── tileset.json           # 루트 매니페스트
└── content/
    ├── 0.b3dm             # Tier 1 (도시 개요)
    ├── 1_0.b3dm           # Tier 2 (지구)
    ├── 1_1.b3dm
    ├── ...
    └── 3_15_12.b3dm       # Tier 3 (거리)
```

### 4.3 대안: trimesh 기반 glTF 생성

pg2b3dm 대신 Python에서 직접 glTF 생성 시:

```python
# src/tile_generation/generate_gltf.py
import trimesh
from shapely import wkb
import numpy as np

def building_to_mesh(footprint_wkb: bytes, height: float, color: list) -> trimesh.Trimesh:
    """건물 footprint WKB → 3D 메시"""
    polygon = wkb.loads(footprint_wkb)
    mesh = trimesh.creation.extrude_polygon(polygon, height)
    mesh.visual.face_colors = color  # [R, G, B, A]
    return mesh

def batch_export_glb(buildings: list, output_path: str):
    """건물 메시 배치 → 단일 GLB 파일"""
    meshes = []
    for b in buildings:
        color = energy_grade_to_color(b['energy_grade'])
        mesh = building_to_mesh(b['geom_wkb'], b['height'], color)
        meshes.append(mesh)

    scene = trimesh.Scene(meshes)
    scene.export(output_path, file_type='glb')

def energy_grade_to_color(grade: str) -> list:
    """에너지등급 → RGBA 색상"""
    colors = {
        '1+++': [0, 180, 0, 200],
        '1++':  [50, 205, 50, 200],
        '1+':   [154, 205, 50, 200],
        '1':    [255, 255, 0, 200],
        '2':    [255, 215, 0, 200],
        '3':    [255, 165, 0, 200],
        '4':    [255, 140, 0, 200],
        '5':    [255, 0, 0, 200],
        '6':    [139, 0, 0, 200],
        '7':    [128, 0, 0, 200],
    }
    return colors.get(grade, [128, 128, 128, 150])
```

### 4.4 S3 + CDN 배포

```bash
# S3 업로드
aws s3 sync ./output_tiles/mapo \
  s3://building-energy-tiles/mapo/ \
  --content-type application/octet-stream \
  --cache-control "public, max-age=86400"

# tileset.json은 별도 content-type
aws s3 cp ./output_tiles/mapo/tileset.json \
  s3://building-energy-tiles/mapo/tileset.json \
  --content-type application/json \
  --cache-control "public, max-age=3600"
```

CesiumJS 로딩:

```javascript
const tileset = await Cesium.Cesium3DTileset.fromUrl(
    'https://cdn.example.com/mapo/tileset.json'
);
viewer.scene.primitives.add(tileset);
```

---

## 5. 데이터 갱신 전략

### 5.1 Celery 태스크 정의

```python
# src/data_ingestion/tasks.py

@celery.task(bind=True, max_retries=3, rate_limit='100/m')
def sync_building_ledger(self, sigungu_code='11440'):
    """건축물대장 주간 델타 동기화"""
    # 1. 마지막 동기화 이후 변경분 조회
    # 2. 법정동별 API 호출
    # 3. PostgreSQL upsert (ON CONFLICT pnu)
    # 4. buildings_enriched Materialized View 갱신

@celery.task
def sync_gis_footprints():
    """GIS건물통합정보 월간 벌크 갱신"""
    # 1. SHP 파일 다운로드
    # 2. ogr2ogr로 PostGIS 적재 (TRUNCATE + INSERT)
    # 3. buildings_enriched 갱신
    # 4. 3D Tiles 재생성 트리거

@celery.task
def regenerate_tiles():
    """3D Tiles 재생성 + S3 업로드"""
    # 1. pg2b3dm 실행
    # 2. aws s3 sync
    # 3. CDN 캐시 무효화

@celery.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # 매주 월요일 새벽 3시: 건축물대장 동기화
    sender.add_periodic_task(
        crontab(hour=3, minute=0, day_of_week=1),
        sync_building_ledger.s()
    )
    # 매월 1일 새벽 2시: GIS건물통합정보 동기화
    sender.add_periodic_task(
        crontab(hour=2, minute=0, day_of_month=1),
        sync_gis_footprints.s()
    )
```

### 5.2 갱신 후 파이프라인

```
데이터 갱신 트리거
    │
    ▼
PostgreSQL 데이터 업데이트
    │
    ▼
REFRESH MATERIALIZED VIEW buildings_enriched
    │
    ▼
3D 지오메트리 재계산 (ST_Extrude)
    │
    ▼
pg2b3dm 재실행
    │
    ▼
S3 업로드 + CDN 캐시 무효화
```

---

## 6. 데이터 품질 대응

| 문제 | 빈도 | 대응 |
|------|------|------|
| PNU 매칭 실패 (다세대 등) | ~10% | 동명칭 보조 매칭 → 면적 유사도 매칭 → unmatched 테이블 기록 |
| 높이 데이터 없음 | ~5% | 층수 x 3.3m 폴백 → 기본 10m |
| 좌표계 오프셋 | 전체 | ogr2ogr `-s_srs EPSG:5174 -t_srs EPSG:4326` |
| 건축년도 없음 | ~3% | 구조유형으로 추정 (조적→pre-2001, RC→2001+) |
| 에너지등급 없음 | ~70% | '미보유'로 표시, 시뮬레이션 결과로 대체 |
| 유효하지 않은 geometry | ~1% | ST_MakeValid() 또는 제외 |
| 법정동코드 폐지 | 소수 | PublicDataReader 폐지여부='존재' 필터 |

---

## 7. 검증 체크리스트

### 7.1 수집 단계

- [ ] GIS건물통합정보 SHP 마포구 필터링 → 25,000~30,000건 확인
- [ ] 건축물대장 API 마포구 전수 수집 → 레코드 수 비교
- [ ] 좌표 변환 검증: 마포구청 좌표 (126.9014, 37.5633) 부근에 건물 분포 확인

### 7.2 통합 단계

- [ ] PNU JOIN 매칭률: 90% 이상
- [ ] 높이 데이터: NULL/0 비율 5% 이내
- [ ] 이상치 검출: 높이 > 200m, 연면적 > 100,000m² 건물 수동 확인
- [ ] 용도 분류: 미분류 비율 5% 이내

### 7.3 3D Tiles 단계

- [ ] tileset.json 정상 생성
- [ ] CesiumJS 로딩 성공 (마포구 위치에 건물 표시)
- [ ] 건물 형태 육안 확인 (주요 랜드마크: 상암 MBC, 홍대입구 등)
- [ ] 클릭 시 속성 조회 (pnu, usage_type, energy_grade)
- [ ] 성능: 30fps 이상 (뷰포트 10,000건물)

---

## 8. 필요 사전 작업

| 작업 | 담당 | 기한 |
|------|------|------|
| 공공데이터포털 가입 + API 키 발급 | 사용자 | 즉시 |
| 공공데이터포털 **운영계정** 신청 | 사용자 | 즉시 (승인 수일) |
| VWorld 가입 + 인증키 발급 | 사용자 | 즉시 |
| GIS건물통합정보 SHP 파일 다운로드 | 사용자 | Phase 1 시작 시 |
| PostgreSQL + PostGIS + SFCGAL 설치 | 개발 | Phase 1 시작 시 |
| pg2b3dm 설치 (.NET 8 런타임) | 개발 | Phase 1 시작 시 |
