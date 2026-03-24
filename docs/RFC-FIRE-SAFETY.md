# RFC: 화재 안전 확장 (Fire Safety Extension)

**문서 버전**: 1.0
**작성일**: 2026-03-24
**상태**: Draft
**관련 문서**: [PRD](./PRD.md) | [Architecture](./ARCHITECTURE.md) | [RFC-Data-Pipeline](./RFC-DATA-PIPELINE.md) | [RFC-Energy-Simulation](./RFC-ENERGY-SIMULATION.md)
**대상 지역**: 서울특별시 (766,386동 footprint 기준)

---

## 1. 개요

### 1.1 배경 및 목표

3D 건물 에너지 플랫폼의 공간 데이터 인프라(PostGIS, VWorld WebGL, 서울 전역 footprint)는 화재 안전 분야에도 직접 활용할 수 있다. 에너지 시뮬레이션이 건물의 물리적 특성(구조, 면적, 건축년도)을 연료삼아 동작하듯, 화재 안전 모듈은 같은 데이터로 위험도 평가, 확산 예측, 대피 경로 최적화를 수행한다.

이 RFC는 화재 안전 기능을 3단계(F1~F3)로 추가하는 아키텍처를 기술한다. 각 단계는 독립적으로 배포 가능하며, 이전 단계의 산출물을 다음 단계의 입력으로 사용하는 점진적 구조다.

**목표:**

- **F1**: 고위험 건물 밀집 클러스터 식별 + 소방서 도달 범위 시각화
- **F2**: 바람 방향·속도를 반영한 화재 확산 시나리오 시뮬레이션 (비동기 Celery)
- **F3**: pgRouting 기반 최적 대피 경로 산출 및 3D 화살표 오버레이

### 1.2 범위 외

- 실시간 화재 감지 센서 연동 (IoT)
- 소방청 공식 위험 등급 인증 또는 법적 효력
- 건물 내부 층별 대피 경로 (실내 GIS 미포함)

---

## 2. 현재 시스템 상태 (Phase F0 기준선)

### 2.1 기존 인프라

Phase F0에서 구축된 화재 관련 기반은 다음과 같다.

| 구성 요소 | 내용 |
|-----------|------|
| `building_fire_risk` | 건축년도·구조·용도 기반 개별 건물 위험 점수 (0~100) — Materialized View |
| `/api/v1/fire/risk` | 뷰포트 내 건물 화재 위험 점수 조회 API |
| 건축물대장 연동 | 구조코드(목조/조적/RC/철골), 사용승인일, 주용도 — PNU JOIN으로 조회 가능 |
| VWorld 뷰어 | 에너지 오버레이와 동일한 레이어 방식으로 위험도 색상 오버레이 가능 |

### 2.2 F0 한계

`building_fire_risk`는 건물 단위의 독립 점수다. 이웃 건물과의 공간 관계, 소방 접근성, 확산 경로, 대피 경로는 전혀 반영되지 않는다. F1~F3는 이 공백을 채운다.

### 2.3 현재 DB 스키마 (관련 테이블)

```
building_footprints      -- 766,386건 footprint (VWorld LT_C_SPBD)
building_ledger          -- 건축물대장 표제부 (구조, 건축년도, 면적)
buildings_enriched       -- Materialized View: footprint + ledger JOIN
building_centroids       -- Point GiST, 0.07ms KNN pick
building_fire_risk       -- Materialized View: F0 위험 점수
```

---

## 3. Phase F1: 밀집도 분석 + 클러스터 + 소방서 커버리지

### 3.1 목표

서울 내 화재 고위험 건물이 공간적으로 집중된 구역을 클러스터로 식별하고, 소방서별 도달 가능 범위를 오버레이한다. 행정 경계 단위의 통계가 아닌 실제 건물 위치 기반의 공간 군집 분석이다.

### 3.2 신규 DB 테이블

#### 3.2.1 building_adjacency — 인접 건물 그래프

```sql
CREATE TABLE building_adjacency (
    source_pnu  VARCHAR(19) NOT NULL,
    target_pnu  VARCHAR(19) NOT NULL,
    distance_m  REAL        NOT NULL,
    shared_edge BOOLEAN     DEFAULT FALSE,
    PRIMARY KEY (source_pnu, target_pnu)
);

CREATE INDEX idx_adjacency_source ON building_adjacency(source_pnu);
CREATE INDEX idx_adjacency_target ON building_adjacency(target_pnu);
```

30m 이내 모든 건물 쌍을 사전 계산한다. 이 테이블은 F2 확산 시뮬레이션의 그래프 기반이기도 하다. 생성 쿼리는 §7.1에서 다룬다.

#### 3.2.2 fire_stations — 소방서 및 안전센터

```sql
CREATE TABLE fire_stations (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    station_type    VARCHAR(20)  NOT NULL,  -- 'fire_station' | 'safety_center'
    district        VARCHAR(50),
    geom            GEOMETRY(Point, 4326) NOT NULL,
    ladder_height   REAL,                   -- 사다리차 최대 높이 (m)
    vehicle_count   INTEGER
);

CREATE INDEX idx_fire_stations_geom ON fire_stations USING GIST(geom);
```

초기 데이터는 서울시 열린데이터광장(소방청 공공데이터)에서 수집한다. `station_type`은 본서(소방서)와 안전센터(구 파출소)를 구분한다.

#### 3.2.3 fire_risk_clusters — 고위험 클러스터

```sql
CREATE TABLE fire_risk_clusters (
    cluster_id      INTEGER      NOT NULL,
    risk_level      VARCHAR(10)  NOT NULL,  -- 'CRITICAL' | 'HIGH' | 'MEDIUM'
    building_count  INTEGER      NOT NULL,
    avg_risk_score  REAL,
    geom            GEOMETRY(MultiPolygon, 4326) NOT NULL,
    computed_at     TIMESTAMP    DEFAULT NOW()
);

CREATE INDEX idx_fire_clusters_geom ON fire_risk_clusters USING GIST(geom);
```

### 3.3 클러스터링 알고리즘

ST_ClusterDBSCAN을 사용하여 HIGH 이상 위험 건물(점수 ≥ 60)의 공간 군집을 계산한다.

```sql
-- 클러스터 계산 (Celery 태스크로 주기적 실행)
INSERT INTO fire_risk_clusters (cluster_id, risk_level, building_count, avg_risk_score, geom)
SELECT
    cluster_id,
    CASE
        WHEN avg_score >= 80 THEN 'CRITICAL'
        WHEN avg_score >= 60 THEN 'HIGH'
        ELSE 'MEDIUM'
    END AS risk_level,
    cnt,
    avg_score,
    ST_ConvexHull(ST_Collect(geom)) AS geom
FROM (
    SELECT
        ST_ClusterDBSCAN(c.geom, eps := 0.0005, minpoints := 3)
            OVER () AS cluster_id,
        r.risk_score,
        c.geom
    FROM building_centroids c
    JOIN building_fire_risk r USING (pnu)
    WHERE r.risk_score >= 60
) sub
WHERE cluster_id IS NOT NULL
GROUP BY cluster_id
HAVING COUNT(*) >= 3;
```

`eps=0.0005`는 위도 기준 약 55m에 해당한다. 서울 구도심 골목길 단위로 클러스터가 형성되도록 조정된 값이다.

### 3.4 신규 API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/v1/fire/clusters` | 뷰포트 내 고위험 클러스터 목록 (GeoJSON) |
| GET | `/api/v1/fire/stations` | 소방서·안전센터 위치 목록 |
| GET | `/api/v1/fire/coverage/{pnu}` | 특정 건물까지 최인접 소방서 도달 시간 추정 |

`/fire/coverage/{pnu}` 응답 예시:

```json
{
  "pnu": "1144010100100010000",
  "nearest_station": {
    "id": 12,
    "name": "마포소방서",
    "distance_m": 1240,
    "estimated_minutes": 4.2
  },
  "ladder_accessible": true,
  "building_height_m": 18.5
}
```

도달 시간 추정은 직선 거리 기반 단순 모델(v = 30 km/h 가정)이다. F3에서 pgRouting 도로망 기반으로 교체된다.

### 3.5 프론트엔드

VWorld 뷰어(`vworld.html`)에 두 레이어를 추가한다.

- **클러스터 레이어**: `fire_risk_clusters` GeoJSON을 CesiumJS `DataSource`로 렌더링. CRITICAL은 빨간색, HIGH는 주황색 반투명 폴리곤
- **소방서 레이어**: `fire_stations`를 소방차 아이콘 빌보드로 표시. 클릭 시 사다리 높이·차량 수 패널 노출

---

## 4. Phase F2: 화재 확산 시뮬레이션

### 4.1 목표

사용자가 원점 건물, 바람 방향, 바람 속도를 지정하면 건물 간 화재 전파 경로와 시간대별 피해 범위를 계산하여 3D 애니메이션으로 보여준다.

### 4.2 확산 모델

#### 4.2.1 그래프 구조

`building_adjacency` 테이블에서 networkx 방향 그래프를 로드한다. 노드는 PNU, 엣지 가중치는 전파 확률의 역수(낮을수록 빠른 전파)다.

```python
import networkx as nx

def load_adjacency_graph(db_session) -> nx.DiGraph:
    rows = db_session.execute(
        "SELECT source_pnu, target_pnu, distance_m FROM building_adjacency"
    ).fetchall()
    G = nx.DiGraph()
    for source, target, dist in rows:
        G.add_edge(source, target, distance_m=dist)
    return G
```

#### 4.2.2 전파 확률 모델

이웃 건물 B로의 전파 확률 P(A→B)는 구조, 거리, 바람의 세 인자로 결정된다.

```
P(A→B) = base_prob(structure_B) × distance_factor(d) × wind_factor(θ, v)
```

**base_prob — 구조별 기본 확률:**

| 구조 | base_prob | 근거 |
|------|-----------|------|
| 목조 | 0.80 | 목재 연소 속도 高 |
| 조적 | 0.60 | 목재 내부재 + 벽돌 외피 |
| 철골 | 0.40 | 고온 좌굴 위험, 외장재 연소 |
| RC(철근콘크리트) | 0.30 | 내화성능 가장 높음 |

**distance_factor — 거리 감쇠:**

```
distance_factor(d) = 1 / (1 + d / 10)
```

d는 건물 간 거리(m), 10m는 복사열 전달의 반감 거리다. 10m 간격이면 0.5, 30m 이상이면 0.25 미만이다.

**wind_factor — 바람 방향·속도 보정:**

```
wind_factor(θ, v) = max(0.2, cos(θ)) × (1 + v / 5)
```

θ는 바람 방향과 A→B 벡터 사이의 각도(radian), v는 풍속(m/s)다. 바람 반대 방향(θ=π)이라도 최소 0.2를 보장하여 사방 전파 가능성을 표현한다.

#### 4.2.3 BFS 시뮬레이션 루프

```python
from collections import deque
import time

def simulate_spread(
    G: nx.DiGraph,
    origin_pnu: str,
    wind_direction: float,  # 도 (0=북, 90=동)
    wind_speed: float,      # m/s
    building_structures: dict[str, str],
    building_positions: dict[str, tuple],  # pnu -> (lon, lat)
    max_steps: int = 20,
    p_threshold: float = 0.15,
) -> dict:
    """
    Returns:
        spread_timeline: {step: [pnu, ...], ...}
    """
    affected = {origin_pnu: 0}   # pnu -> step
    queue = deque([(origin_pnu, 0)])
    timeline = {0: [origin_pnu]}

    while queue:
        current, step = queue.popleft()
        if step >= max_steps:
            continue

        for _, neighbor, data in G.out_edges(current, data=True):
            if neighbor in affected:
                continue

            d = data["distance_m"]
            structure = building_structures.get(neighbor, "RC")
            angle = _angle_to_wind(
                building_positions[current],
                building_positions[neighbor],
                wind_direction,
            )
            prob = (
                BASE_PROB[structure]
                * (1 / (1 + d / 10))
                * max(0.2, math.cos(angle))
                * (1 + wind_speed / 5)
            )
            prob = min(prob, 1.0)

            if prob >= p_threshold:
                next_step = step + 1
                affected[neighbor] = next_step
                timeline.setdefault(next_step, []).append(neighbor)
                queue.append((neighbor, next_step))

    return {"affected_pnus": list(affected.keys()), "spread_timeline": timeline}
```

확률이 `p_threshold`(기본 0.15) 미만이면 전파가 일어나지 않는 것으로 처리한다.

### 4.3 신규 DB 테이블

```sql
CREATE TABLE fire_scenario_results (
    scenario_id      UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_pnu       VARCHAR(19) NOT NULL,
    wind_direction   REAL       NOT NULL,   -- 도 (0~360)
    wind_speed       REAL       NOT NULL,   -- m/s
    affected_pnus    VARCHAR(19)[],
    spread_timeline  JSONB      NOT NULL,   -- {step: [pnu, ...]}
    total_buildings  INTEGER,
    computed_at      TIMESTAMP  DEFAULT NOW()
);

CREATE INDEX idx_scenario_origin ON fire_scenario_results(origin_pnu);
CREATE INDEX idx_scenario_computed ON fire_scenario_results(computed_at DESC);
```

`spread_timeline` JSONB 예시:

```json
{
  "0": ["1144010100100010000"],
  "1": ["1144010100100010001", "1144010100100010002"],
  "2": ["1144010100100010005"],
  "3": ["1144010100100010008", "1144010100100010009"]
}
```

### 4.4 Celery 태스크 설계

시뮬레이션은 Celery 비동기 태스크로 실행된다. networkx 그래프 로딩(약 5~10초)이 병목이므로 워커 프로세스 시작 시 한 번 로드하고 메모리에 유지한다.

```python
# src/fire_safety/tasks.py

from celery import Celery
from src.shared.celery_app import celery_app

# 워커 시작 시 그래프 1회 로드 (module-level singleton)
_GRAPH: nx.DiGraph | None = None
_STRUCTURES: dict | None = None
_POSITIONS: dict | None = None

@celery_app.on_after_finalize.connect
def _preload_graph(sender, **kwargs):
    global _GRAPH, _STRUCTURES, _POSITIONS
    with SessionLocal() as db:
        _GRAPH = load_adjacency_graph(db)
        _STRUCTURES = load_building_structures(db)
        _POSITIONS = load_building_positions(db)


@celery_app.task(bind=True, name="fire.simulate_scenario")
def simulate_scenario_task(
    self,
    origin_pnu: str,
    wind_direction: float,
    wind_speed: float,
) -> str:
    """Returns scenario_id (UUID)."""
    result = simulate_spread(
        _GRAPH, origin_pnu, wind_direction, wind_speed, _STRUCTURES, _POSITIONS
    )
    with SessionLocal() as db:
        scenario_id = _save_scenario(db, origin_pnu, wind_direction, wind_speed, result)
    # Redis 캐시 (1시간)
    redis_client.setex(f"scenario:{scenario_id}", 3600, json.dumps(result))
    return str(scenario_id)
```

### 4.5 신규 API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/api/v1/fire/scenario` | 확산 시나리오 계산 요청 (Celery 태스크 발행) |
| GET | `/api/v1/fire/scenario/{id}` | 시나리오 결과 조회 (Redis 캐시 우선) |
| GET | `/api/v1/fire/scenario/{id}/status` | 계산 완료 여부 폴링 |

`POST /api/v1/fire/scenario` 요청:

```json
{
  "origin_pnu": "1144010100100010000",
  "wind_direction": 225,
  "wind_speed": 5.0
}
```

응답 (즉시 반환, 202 Accepted):

```json
{
  "scenario_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "pending",
  "poll_url": "/api/v1/fire/scenario/3fa85f64-5717-4562-b3fc-2c963f66afa6/status"
}
```

### 4.6 프론트엔드 — 타임라인 애니메이션

CesiumJS의 `TimeIntervalCollection`을 활용하여 확산 단계별 건물 색상을 시간 축에 바인딩한다.

```javascript
// spread_timeline: {0: [pnu,...], 1: [pnu,...], ...}
function applySpreadAnimation(viewer, spreadTimeline, startTime) {
  const steps = Object.keys(spreadTimeline).map(Number).sort((a, b) => a - b);
  const stepDurationSec = 10; // 시각화 1스텝 = 10초

  steps.forEach((step) => {
    const pnus = spreadTimeline[step];
    const stepStart = Cesium.JulianDate.addSeconds(
      startTime, step * stepDurationSec, new Cesium.JulianDate()
    );
    const stepEnd = Cesium.JulianDate.addSeconds(
      startTime, (step + 1) * stepDurationSec, new Cesium.JulianDate()
    );

    pnus.forEach((pnu) => {
      const entity = viewer.entities.getById(pnu);
      if (!entity) return;
      entity.availability = new Cesium.TimeIntervalCollection([
        new Cesium.TimeInterval({ start: stepStart, stop: Cesium.JulianDate.fromIso8601("2099-01-01") })
      ]);
      entity.polygon.material = new Cesium.ColorMaterialProperty(
        Cesium.Color.ORANGERED.withAlpha(0.75)
      );
    });
  });

  viewer.clock.startTime = startTime;
  viewer.clock.shouldAnimate = true;
}
```

---

## 5. Phase F3: 대피 경로 (pgRouting)

### 5.1 목표

특정 건물에서 가장 가까운 대피 지점(공원, 학교, 광장)까지의 최단 도로 경로를 계산하고 3D 화살표로 시각화한다.

### 5.2 신규 인프라

#### 5.2.1 pgRouting 익스텐션 설치

```sql
CREATE EXTENSION IF NOT EXISTS pgrouting;
```

`docker-compose.yml`의 `db` 서비스에서 PostGIS 16-3.4 이미지가 pgRouting을 내장한다. 별도 이미지 변경은 불필요하다.

#### 5.2.2 OSM 도로망 적재

OpenStreetMap에서 서울 도로망을 추출하여 pgRouting 형식으로 적재한다.

```bash
# osm2pgrouting으로 서울 OSM 파일 적재
osm2pgrouting \
  --file seoul.osm.pbf \
  --conf /usr/share/osm2pgrouting/mapconfig.xml \
  --dbname buildings \
  --host localhost \
  --port 5434 \
  --username postgres \
  --clean
```

결과로 생성되는 핵심 테이블:

```
ways           -- 도로 세그먼트 (source, target, cost, reverse_cost, geom)
ways_vertices_pgr -- 도로 교차점 노드
```

#### 5.2.3 대피 지점 테이블

```sql
CREATE TABLE evacuation_points (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100),
    point_type  VARCHAR(20),  -- 'park' | 'school' | 'plaza'
    geom        GEOMETRY(Point, 4326) NOT NULL,
    capacity    INTEGER,      -- 수용 인원 (알 수 있는 경우)
    nearest_vertex_id BIGINT  -- ways_vertices_pgr.id (사전 계산)
);

CREATE INDEX idx_evac_geom ON evacuation_points USING GIST(geom);
```

서울시 공원, 초중고교, 대형 광장 위치는 서울 열린데이터광장에서 수집한다.

### 5.3 pgRouting 쿼리

건물 중심점에서 가장 가까운 도로 노드를 시작점으로, 가장 가까운 대피 지점의 도로 노드를 종점으로 최단 경로를 계산한다.

```sql
-- 1단계: 건물 중심점에서 가장 가까운 도로 노드 조회 (KNN)
SELECT id AS start_vertex
FROM ways_vertices_pgr
ORDER BY geom <-> (
    SELECT geom FROM building_centroids WHERE pnu = :pnu
)
LIMIT 1;

-- 2단계: 가장 가까운 대피 지점 도로 노드 조회
SELECT nearest_vertex_id AS end_vertex, name, point_type
FROM evacuation_points
ORDER BY geom <-> (
    SELECT geom FROM building_centroids WHERE pnu = :pnu
)
LIMIT 3;  -- 상위 3개 대피 지점 후보

-- 3단계: pgr_dijkstra 실행
SELECT
    r.seq, r.edge, w.geom, r.cost
FROM pgr_dijkstra(
    'SELECT gid AS id, source, target, length_m AS cost, length_m AS reverse_cost FROM ways',
    :start_vertex,
    :end_vertex,
    directed := false
) AS r
JOIN ways w ON r.edge = w.gid
ORDER BY r.seq;
```

### 5.4 Python API 구현

```python
# src/fire_safety/evacuation.py

async def get_evacuation_route(pnu: str, db) -> EvacuationRouteResponse:
    start_vertex = await _find_nearest_vertex(pnu, db)
    candidates = await _find_nearest_evacuation_points(pnu, db, limit=3)

    routes = []
    for ep in candidates:
        path_rows = await db.execute(
            DIJKSTRA_QUERY,
            {"start": start_vertex, "end": ep.nearest_vertex_id}
        )
        route_geom = _rows_to_linestring(path_rows)
        routes.append(EvacuationRoute(
            evacuation_point=ep,
            path_geom=route_geom,
            total_distance_m=sum(r.cost for r in path_rows),
        ))

    # 가장 짧은 경로 우선 반환
    routes.sort(key=lambda r: r.total_distance_m)
    return EvacuationRouteResponse(pnu=pnu, routes=routes)
```

### 5.5 신규 API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/v1/fire/evacuation/{pnu}` | 특정 건물의 대피 경로 (최대 3개) |
| GET | `/api/v1/fire/evacuation-points` | 뷰포트 내 대피 지점 목록 |

`GET /api/v1/fire/evacuation/{pnu}` 응답 예시:

```json
{
  "pnu": "1144010100100010000",
  "routes": [
    {
      "rank": 1,
      "evacuation_point": { "name": "마포구 망원한강공원", "point_type": "park" },
      "total_distance_m": 820,
      "estimated_walk_minutes": 10,
      "path": {
        "type": "LineString",
        "coordinates": [[126.91, 37.55], [126.912, 37.551], ...]
      }
    }
  ]
}
```

### 5.6 프론트엔드 — 3D 화살표 오버레이

CesiumJS의 `PolylineArrowMaterialProperty`로 대피 경로를 3D 공간에 방향 화살표로 표시한다.

```javascript
function renderEvacuationRoute(viewer, routeGeoJson) {
  const positions = routeGeoJson.coordinates.map(([lon, lat]) =>
    Cesium.Cartesian3.fromDegrees(lon, lat, 5) // 지면에서 5m 위
  );

  viewer.entities.add({
    polyline: {
      positions,
      width: 6,
      material: new Cesium.PolylineArrowMaterialProperty(
        Cesium.Color.LIMEGREEN
      ),
      clampToGround: false,
    },
  });
}
```

---

## 6. 데이터 흐름 다이어그램

### 6.1 전체 파이프라인

```
[외부 데이터 소스]
  VWorld SHP (766K footprint)
  건축물대장 API (구조, 건축년도)
  소방청 공공데이터 (소방서 위치)
  OpenStreetMap (도로망)
  서울시 열린데이터광장 (공원, 학교)
         │
         ▼
[PostGIS 원본 테이블]
  building_footprints ──┐
  building_ledger      ──┤──→ buildings_enriched (Materialized View)
                          │         │
                          │         ▼
                          │   building_fire_risk (F0 위험 점수)
                          │         │
                          ▼         ▼
                   building_centroids
                          │
                  ┌────────┼────────────────────┐
                  │        │                    │
                  ▼        ▼                    ▼
         [F1 처리]      [F2 처리]           [F3 처리]
    ST_ClusterDBSCAN  networkx BFS       pgr_dijkstra
    (Celery 주기적)   (Celery 온디맨드)   (FastAPI 동기)
          │                │                   │
          ▼                ▼                   ▼
  fire_risk_clusters  fire_scenario_results  경로 GeoJSON
          │                │                   │ (캐시 없음,
          │           Redis 캐시 1h             │  pgRouting 빠름)
          │                │                   │
          └────────────────┴───────────────────┘
                           │
                           ▼
                    [FastAPI REST API]
                     /api/v1/fire/*
                           │
                           ▼
               [프론트엔드 (VWorld + CesiumJS)]
                클러스터 폴리곤 레이어
                확산 타임라인 애니메이션
                대피 경로 화살표
```

### 6.2 F2 시나리오 요청 흐름

```
브라우저                FastAPI              Celery Worker        Redis / PostgreSQL
   │                      │                       │                      │
   │─ POST /fire/scenario ─▶                       │                      │
   │                      │─ task.delay() ────────▶                      │
   │◀─ 202 {scenario_id} ──│                       │                      │
   │                      │                       │─ load graph (메모리)  │
   │─ GET /scenario/{id}/status ─▶                │                      │
   │◀─ {status: "pending"} │                       │─ BFS 실행 ───────────▶
   │                      │                       │                      │
   │  (5~30초 후)          │                       │◀─ 결과 저장 ──────────│
   │─ GET /scenario/{id}/status ─▶                │                      │
   │◀─ {status: "done"} ───│                       │                      │
   │                      │                       │                      │
   │─ GET /scenario/{id} ──▶                       │                      │
   │                      │◀─ Redis GET ──────────────────────────────── │
   │◀─ 200 {timeline, ...} │                       │                      │
```

---

## 7. 성능 고려사항

### 7.1 building_adjacency 생성 전략 (766K × 766K 문제)

순진한 자기 조인으로는 766,386² ≈ 5,870억 쌍을 평가해야 한다. PostGIS 공간 인덱스를 활용하더라도 단일 쿼리 실행은 현실적이지 않다.

**분할 처리 전략:**

```python
# src/fire_safety/build_adjacency.py
# 행정동 단위로 분할하여 순차 처리

CHUNK_QUERY = """
INSERT INTO building_adjacency (source_pnu, target_pnu, distance_m)
SELECT
    a.pnu,
    b.pnu,
    ST_Distance(a.geom::geography, b.geom::geography) AS distance_m
FROM building_centroids a
JOIN building_centroids b
  ON ST_DWithin(a.geom, b.geom, 0.0003)  -- 약 30m (위도 기준)
  AND a.pnu < b.pnu                       -- 중복 방지 (무방향 그래프)
  AND a.pnu != b.pnu
WHERE a.pnu LIKE :dong_prefix || '%'      -- 행정동 단위 분할
ON CONFLICT DO NOTHING;
"""

def build_adjacency_by_dong():
    dong_codes = fetch_all_dong_codes()   # 서울 424개 행정동
    for dong in dong_codes:
        execute(CHUNK_QUERY, {"dong_prefix": dong})
        commit()
        log(f"Completed dong: {dong}")
```

예상 처리량: 행정동당 평균 1,800동 × 30m 이내 평균 20쌍 ≈ 36,000건. 424개 행정동 × 36,000 ≈ 1,530만 행. 완료 시간 추정 약 2~4시간 (단일 워커).

**인덱스 전략:**

```sql
-- 생성 전: 인덱스 비활성화 (삽입 속도 3~5x 향상)
ALTER TABLE building_adjacency DISABLE TRIGGER ALL;
-- 대량 INSERT 완료 후
ALTER TABLE building_adjacency ENABLE TRIGGER ALL;

-- GiST 인덱스는 building_centroids.geom에 이미 존재 (ST_DWithin 활용)
-- building_adjacency는 B-tree로 충분
CREATE INDEX idx_adj_source ON building_adjacency(source_pnu);
CREATE INDEX idx_adj_target ON building_adjacency(target_pnu);
```

### 7.2 networkx 그래프 메모리 사용량

1,530만 엣지 기준 networkx DiGraph 메모리 소비:

| 항목 | 추정 크기 |
|------|-----------|
| 노드 766K개 | ~120 MB |
| 엣지 1,530만 개 | ~2.4 GB |
| 합계 | ~2.5 GB |

Celery 워커 1개당 2.5 GB가 필요하다. 워커 수가 늘어나면 메모리가 선형 증가한다. 완화 방법:

1. **단순화 그래프**: distance_m만 저장 (속성 최소화)
2. **scipy.sparse**: networkx 대신 sparse 행렬로 BFS (메모리 70% 절약 가능)
3. **범위 제한 BFS**: 시뮬레이션 원점에서 1km 반경 서브그래프만 로드 (온디맨드 방식)

초기 구현은 방법 3(범위 제한 BFS)를 채택한다. 서울 전역 그래프가 필요한 시나리오는 드물고, 1km 반경이면 실제 화재 확산 범위를 충분히 커버한다.

### 7.3 pgRouting 응답 시간

`pgr_dijkstra`는 도로 노드 수 기준 성능이 결정된다. 서울 OSM 기준 노드 약 50만~80만 개. 출발~도착 평균 노드 간격이 2km 이내라면 응답 시간 100~500ms 수준이다. 별도 캐싱 없이 FastAPI 동기 응답으로 처리한다.

`nearest_vertex_id`는 `evacuation_points` 테이블에 사전 계산해 두므로 매 요청마다 KNN을 두 번 실행하는 오버헤드는 없다.

### 7.4 클러스터 재계산 주기

`fire_risk_clusters`는 건축물대장이 갱신될 때(월 1회) 재계산한다. Celery Beat 스케줄로 등록한다.

```python
# src/shared/celery_app.py
beat_schedule = {
    "refresh-fire-clusters-monthly": {
        "task": "fire.refresh_clusters",
        "schedule": crontab(day_of_month=1, hour=2, minute=0),
    },
    "refresh-fire-risk-materialized-view": {
        "task": "fire.refresh_risk_view",
        "schedule": crontab(day_of_month=1, hour=1, minute=30),
    },
}
```

---

## 8. ADR — 아키텍처 결정 사항

### ADR-F01: BFS vs Dijkstra (확산 시뮬레이션 알고리즘)

**결정**: BFS (확률 임계값 방식)

**배경**: Dijkstra는 최단 경로 탐색에 최적화되어 있다. 화재 확산은 "가장 빠른 단일 경로"가 아니라 "시간 단계별 다중 경로 동시 전파"를 표현해야 한다.

**결론**: BFS로 단계를 동기화하면 시각화에 필요한 `step → [pnu, ...]` 타임라인을 자연스럽게 생성할 수 있다. 확률 임계값(0.15)으로 비현실적인 장거리 확산을 제한한다.

**재검토 조건**: 풍속이 10 m/s 이상인 강풍 시나리오에서 확산 속도 보정이 필요하면 가중치 기반 Dijkstra로 교체를 검토한다.

---

### ADR-F02: 화재 확산 그래프 — 전체 로드 vs 서브그래프 온디맨드

**결정**: 서브그래프 온디맨드 (1km 반경 제한)

**배경**: 서울 전역 766K 노드 × 1,530만 엣지 그래프를 워커 시작 시 전체 로드하면 워커당 ~2.5 GB가 필요하다.

**결론**: 시뮬레이션 요청 시 원점 건물 기준 1km 반경의 서브그래프를 DB에서 동적으로 로드한다. 1km 반경은 대형 화재 시나리오에서도 충분한 범위다. DB 쿼리 오버헤드(~500ms)는 시뮬레이션 전체 시간(수초)에 비해 허용 범위다.

**재검토 조건**: 동일 원점 반복 요청이 많아지면 Redis에 서브그래프를 직렬화하여 30분 캐싱하는 방식으로 전환한다.

---

### ADR-F03: pgRouting 도로망 — OSM vs 서울시 NGIS

**결정**: OpenStreetMap (osm2pgrouting)

**배경**: 국가GIS(NGIS) 도로망은 공공데이터포털에서 SHP 형태로 제공되나, pgRouting용 토폴로지 구성(pgr_createTopology)이 추가로 필요하다. OSM은 osm2pgrouting 도구가 토폴로지를 자동으로 구성해 준다.

**결론**: 초기 구현은 OSM을 사용한다. 서울 도심의 OSM 데이터 커버리지는 99% 이상으로 대피 경로 계산에 충분하다. 정확도가 중요한 향후 단계에서 NGIS 교체를 검토한다.

---

### ADR-F04: 확산 시뮬레이션 결과 저장 — DB 저장 vs Redis 전용

**결정**: DB 저장 (fire_scenario_results) + Redis 캐시 (1시간)

**배경**: Redis만 사용하면 결과 재조회가 TTL 이후 불가능하다. 동일 조건 시나리오를 반복 요청하면 재계산 비용이 발생한다.

**결론**: DB에 영구 저장하고 Redis는 빠른 응답 캐시로만 사용한다. 90일 이상 된 시나리오는 Celery Beat로 주기적으로 삭제하여 테이블 크기를 관리한다.

---

### ADR-F05: F1 클러스터 경계 — ConvexHull vs ConcaveHull

**결정**: ST_ConvexHull (초기 구현)

**배경**: ST_ConcaveHull은 클러스터 형상을 더 정확하게 표현하지만, 계산 비용이 높고 파라미터(target_percent) 튜닝이 필요하다.

**결론**: MVP는 ST_ConvexHull로 빠르게 구현한다. 클러스터 형상이 실제로 오목한 경우(ㄱ자 형태 골목 등)에 시각적 문제가 생기면 ST_ConcaveHull(0.7)로 교체한다.

---

## 9. 구현 단계 요약

| 단계 | 핵심 작업 | 예상 소요 |
|------|-----------|-----------|
| **F1** | building_adjacency 생성 스크립트, fire_stations 데이터 수집, ST_ClusterDBSCAN Celery 태스크, 3개 API, VWorld 클러스터 레이어 | 2~3주 |
| **F2** | fire_scenario_results 테이블, networkx 서브그래프 로더, BFS 확산 로직, Celery 태스크, 시나리오 API, CesiumJS TimeInterval 애니메이션 | 3~4주 |
| **F3** | pgRouting 설치, osm2pgrouting 적재, evacuation_points 데이터 수집, pgr_dijkstra 쿼리, 대피 경로 API, PolylineArrow 렌더러 | 2~3주 |

---

*이 문서는 구현 시작 전 팀 리뷰를 거쳐 확정한다. 알고리즘 파라미터(base_prob, distance_factor, p_threshold)는 실데이터 검증 후 조정될 수 있다.*
