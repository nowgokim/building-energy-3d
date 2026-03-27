# ADR-004: building_centroids 별도 테이블로 pick 성능 확보

**상태**: 확정
**결정일**: 2026-03-22
**결정자**: 프로젝트 팀

---

## 결정

건물 클릭 pick 쿼리는 `buildings_enriched.geom`(MultiPolygon)에 직접 KNN하지 않고, **`building_centroids`(Point) 테이블을 별도로 생성해서** KNN한다.

## 근거

| 방식 | 성능 |
|------|------|
| `buildings_enriched.geom` KNN (MultiPolygon GiST) | ~350ms (766K건) |
| `building_centroids` KNN (Point GiST) | **0.07ms** (5,000배 빠름) |

MultiPolygon GiST는 경계박스 기반 — 복잡한 폴리곤에서 KNN이 느리다.
Point GiST는 centroid 단순 좌표 → 즉각 응답.

## 영향 파일

- `db/views.sql` — building_centroids 생성 (SSOT)
- `docs/ARCHITECTURE.md §3.2.1` — DDL 참조
- `src/visualization/buildings.py` — pick 쿼리 (`building_centroids` 사용)

## 변경 조건

건물 데이터 갱신 시 `REFRESH MATERIALIZED VIEW buildings_enriched` 후 `building_centroids` 재생성 필요.
갱신 스크립트에 두 작업을 묶어야 한다.
