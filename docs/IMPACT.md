# 변경 영향도 맵 (IMPACT MAP)

**목적**: 어떤 파일을 수정하기 전에 이 표를 먼저 확인한다. 연쇄 수정 누락 방지.

**원칙**: 같은 사실이 여러 파일에 중복 기술된 경우, 한 곳을 수정하면 나머지도 **반드시 동시에** 수정한다.

---

## 데이터베이스 스키마

| 변경 대상 | SSOT | 함께 수정해야 할 파일 |
|-----------|------|---------------------|
| `vintage_class` 구간 정의 | `db/views.sql` | `docs/RFC-ENERGY-SIMULATION.md §2.1` · `src/simulation/archetypes.py` · `docs/adr/003-vintage-class.md` |
| `buildings_enriched` 컬럼 추가/삭제/이름 변경 | `db/views.sql` | `docs/ARCHITECTURE.md §3.2.1` · `src/visualization/buildings.py` · `src/visualization/search.py` · `src/visualization/filter.py` |
| `building_fire_risk` 점수 공식 변경 | `db/fire_risk.sql` | `docs/FIRE-SAFETY-WORKPLAN.md §3.2` · `frontend/vworld.html` (패널 표시) |
| `building_adjacency` 스키마 변경 | `db/fire_safety_f1.sql` | `docs/RFC-FIRE-SAFETY.md §3` |
| `building_centroids` 스키마 변경 | `db/init.sql` | `docs/ARCHITECTURE.md §3.2.1` · `src/visualization/buildings.py` (KNN pick) |
| `energy_results` 컬럼 추가 | `db/init.sql` | `docs/ARCHITECTURE.md §3.2.1` · `src/visualization/search.py` (`_filtered_rows`) |
| `energy_results.is_current` 필터 변경 | `src/visualization/buildings.py` | `src/visualization/search.py` (LATERAL JOIN) · `docs/ARCHITECTURE.md §3.2.1` |
| `data_sources` 테이블 스키마 | `db/migration_provenance_v1.sql` | `docs/ARCHITECTURE.md §3.2.3` · `src/data_ingestion/collect_energy.py` (simulation_type → source_key 매핑) |
| `pipeline_runs` 테이블 스키마 | `db/migration_provenance_v1.sql` | `docs/ARCHITECTURE.md §3.2.3` · Celery tasks (pipeline_run_id 전달) |
| `model_registry` + `model_versions` 스키마 | `db/migration_provenance_v1.sql` | `docs/ARCHITECTURE.md §3.2.3` · `src/simulation/` (Phase 4 ML 인터페이스) |
| `energy_predictions` 파티션 테이블 | `db/migration_provenance_v1.sql` | `docs/ARCHITECTURE.md §3.2.3` · Phase 4 배치 추론 태스크 |
| `model_accuracy_summary` MV 갱신 주기 | `db/migration_provenance_v1.sql` | Celery beat 스케줄 · `src/fire_safety/tasks.py` (갱신 훅 추가 예정) |

---

## API 엔드포인트

| 변경 대상 | SSOT | 함께 수정해야 할 파일 |
|-----------|------|---------------------|
| `/api/v1/buildings/*` 응답 필드 변경 | `src/visualization/buildings.py` | `docs/ARCHITECTURE.md API 명세` · `frontend/vworld.html` (JS fetch 처리) |
| `/api/v1/filter` 요청/응답 변경 | `src/visualization/search.py` | `docs/ARCHITECTURE.md` · `frontend/vworld.html` (`applyFilters()`) |
| `/api/v1/fire/*` 엔드포인트 추가 | `src/visualization/fire.py` | `docs/RFC-FIRE-SAFETY.md` · `docs/FIRE-SAFETY-WORKPLAN.md §4.5` · `frontend/vworld.html` |
| `/api/v1/monitor/*` 응답 필드 변경 | `src/visualization/monitor.py` | `src/shared/monitor_models.py` · `frontend/src/types/monitor.ts` · `frontend/src/api/monitorClient.ts` |
| FastAPI 라우터 prefix 변경 | `src/main.py` | `frontend/vworld.html` (API URL 하드코딩 확인) |

---

## Tier C 모니터링 스키마

| 변경 대상 | SSOT | 함께 수정해야 할 파일 |
|-----------|------|---------------------|
| `monitored_buildings` 컬럼 추가/변경 | `db/monitor_timeseries.sql` | `src/visualization/monitor.py` (SQL 쿼리) · `src/shared/monitor_models.py` · `frontend/src/types/monitor.ts` |
| `metered_readings` 스키마 변경 | `db/monitor_timeseries.sql` | `src/visualization/monitor.py` · `src/monitor/tasks.py` |
| `anomaly_log` 스키마 변경 | `db/monitor_timeseries.sql` | `src/monitor/tasks.py` · `src/visualization/monitor.py` (`/anomalies` 엔드포인트) |
| `metered_readings_daily` 스키마 변경 | `db/monitor_timeseries.sql` | `fn_sync_tier_c_to_energy_results()` (같은 파일 내) · Celery daily task |
| `fn_sync_tier_c_to_energy_results()` 변경 | `db/monitor_timeseries.sql` | `src/monitor/tasks.py` (호출부) |
| ⚠️ `migration_tier_c_timeseries_v1.sql` | **deprecated** | `db/monitor_timeseries.sql` 을 사용할 것 |

---

## 인프라

| 변경 대상 | SSOT | 함께 수정해야 할 파일 |
|-----------|------|---------------------|
| Docker Compose 서비스/포트 변경 | `docker-compose.yml` | `docs/ARCHITECTURE.md §2` · `CLAUDE.md 빌드 & 실행` · `.env.example` |
| DB 이미지 교체 (F3: pgrouting) | `docker-compose.yml` | `docs/FIRE-SAFETY-WORKPLAN.md §8.2` · `docs/adr/005-pgrouting.md` (신규 작성) |
| Python 의존성 추가 | `pyproject.toml` | `docs/ARCHITECTURE.md §8 기술 스택` |
| 환경변수 추가 | `.env.example` | `src/shared/config.py` · `CLAUDE.md 환경변수` |

---

## 문서

| 변경 대상 | SSOT | 함께 수정해야 할 파일 |
|-----------|------|---------------------|
| Phase 완료 상태 변경 | `CLAUDE.md` | `docs/PRD.md §11` · `docs/FIRE-SAFETY-WORKPLAN.md §2.1` · `memory/project_state.md` |
| 건물 건수 수치 업데이트 | 실제 DB 쿼리 | `CLAUDE.md` · `docs/PRD.md §2.2` · `docs/ARCHITECTURE.md §1.1` · `docs/RFC-DATA-PIPELINE.md §1` |
| 기술 결정 번복/변경 | `docs/adr/NNN-*.md` | `docs/ARCHITECTURE.md 주요 기술 결정` · `CLAUDE.md 주요 기술 결정 사항` |

---

## 수정 절차

```
1. 수정할 대상이 위 표에 있는지 확인
2. "함께 수정해야 할 파일" 목록 메모
3. 코드/SQL 수정
4. 문서 동시 수정
5. ADR이 있으면 ADR도 업데이트
6. 커밋 메시지에 "영향 파일: X, Y, Z 동시 수정" 명시
```

---

*최종 수정: 2026-03-27*
*이 파일 자체를 수정할 때는 관련 ADR이 있으면 함께 업데이트할 것.*
