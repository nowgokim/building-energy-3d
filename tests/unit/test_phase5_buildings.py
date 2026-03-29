# -*- coding: utf-8 -*-
"""
유닛 테스트: Phase 5 buildings API 엔드포인트

커버:
- GET /district-stats  — 응답 구조, zeb_threshold 포함
- GET /compare         — 유효하지 않은 PNU → 400, 구조 확인
- GET /retrofit-priority — 응답 구조, rank 순서
- GET /{pnu}           — envelope 섹션 존재 (F-DET-05)
- Redis 캐시 히트       — DB 조회 없이 캐시 반환

DB 없이 실행: SQLAlchemy Session을 MagicMock으로 대체하고
get_db_dependency FastAPI 의존성을 오버라이드한다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# FastAPI TestClient — src.main 전체 대신 최소 앱만 구성
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.shared.database import get_db_dependency
from src.visualization.buildings import router as buildings_router

# 테스트 전용 최소 앱 (Celery/Redis 의존 라우터 제외)
app = FastAPI()
app.include_router(buildings_router)


# ---------------------------------------------------------------------------
# 헬퍼: SQLAlchemy Row 모조 객체
# ---------------------------------------------------------------------------

def _row(**kwargs) -> Any:
    """속성 접근 가능한 가짜 Row 객체를 반환한다."""
    return types.SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# 헬퍼: DB Mock 세션
# ---------------------------------------------------------------------------

def _make_db_session(rows_map: dict[str, list]) -> MagicMock:
    """sql 텍스트 키워드별로 다른 rows를 반환하는 mock DB 세션."""
    session = MagicMock()

    def _execute(sql, params=None):
        sql_str = str(sql)
        for keyword, rows in rows_map.items():
            if keyword in sql_str:
                result = MagicMock()
                result.fetchone.return_value = rows[0] if rows else None
                result.fetchall.return_value = rows
                result.mappings.return_value.one_or_none.return_value = (
                    dict(rows[0].__dict__) if rows else None
                )
                return result
        # fallback
        result = MagicMock()
        result.fetchone.return_value = None
        result.fetchall.return_value = []
        return result

    session.execute.side_effect = _execute
    return session


# ---------------------------------------------------------------------------
# 픽스처: 캐시 비활성화 (항상 Miss)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_redis_cache(monkeypatch):
    """모든 테스트에서 Redis 캐시를 비활성화한다 (Miss 처리)."""
    monkeypatch.setattr("src.visualization.buildings._cache_get", lambda key: None)
    monkeypatch.setattr("src.visualization.buildings._cache_set", lambda key, data, ttl: None)


# ---------------------------------------------------------------------------
# /district-stats
# ---------------------------------------------------------------------------

class TestDistrictStats:
    _DISTRICT_ROW = _row(
        sigungu_cd="11110",
        building_count=1000,
        avg_eui=180.5,
        min_eui=60.0,
        max_eui=350.0,
        zeb_count=50,
        non_zeb_count=950,
        center_lng=126.9,
        center_lat=37.5,
    )

    def test_returns_districts_key(self):
        db = _make_db_session({"sigungu_cd": [self._DISTRICT_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/district-stats")
            assert res.status_code == 200
            data = res.json()
            assert "districts" in data
            assert "zeb_threshold" in data
        finally:
            app.dependency_overrides.clear()

    def test_district_entry_has_required_fields(self):
        db = _make_db_session({"sigungu_cd": [self._DISTRICT_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/district-stats")
            districts = res.json()["districts"]
            assert len(districts) == 1
            d = districts[0]
            for key in ("sigungu_cd", "name", "building_count", "avg_eui",
                        "zeb_count", "non_zeb_count", "zeb_pct"):
                assert key in d, f"Missing key: {key}"
        finally:
            app.dependency_overrides.clear()

    def test_zeb_pct_calculation(self):
        db = _make_db_session({"sigungu_cd": [self._DISTRICT_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/district-stats")
            d = res.json()["districts"][0]
            # zeb_count=50, total=1000 → 5.0%
            assert d["zeb_pct"] == pytest.approx(5.0, abs=0.1)
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /compare — 유효성 검사
# ---------------------------------------------------------------------------

class TestCompareBuildingsValidation:
    def test_invalid_pnu_returns_400(self):
        client = TestClient(app)
        res = client.get("/api/v1/buildings/compare?pnu1=invalid&pnu2=1111010100100030000")
        assert res.status_code == 400

    def test_both_invalid_pnu_returns_400(self):
        client = TestClient(app)
        res = client.get("/api/v1/buildings/compare?pnu1=abc&pnu2=xyz")
        assert res.status_code == 400

    def test_missing_params_returns_422(self):
        client = TestClient(app)
        res = client.get("/api/v1/buildings/compare")
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# /retrofit-priority
# ---------------------------------------------------------------------------

class TestRetrofitPriority:
    _RETRO_ROW = _row(
        pnu="1111010100100030000",
        building_name="테스트 빌딩",
        usage_type="업무시설",
        vintage_class="1980-2000",
        built_year=1990,
        total_area=5000.0,
        floors_above=10,
        energy_grade=None,
        sigungu_cd="11110",
        eui=220.5,
        total_kwh_yr=1102500.0,
        zeb_gap=70.5,
        data_tier=4,
        lng=126.9,
        lat=37.5,
    )

    def test_returns_buildings_list(self):
        db = _make_db_session({"total_kwh_yr": [self._RETRO_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/retrofit-priority")
            assert res.status_code == 200
            data = res.json()
            assert "buildings" in data
            assert "zeb_threshold" in data
            assert "total_returned" in data
        finally:
            app.dependency_overrides.clear()

    def test_limit_param_accepted(self):
        db = _make_db_session({"total_kwh_yr": []})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/retrofit-priority?limit=5")
            assert res.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_limit_over_100_returns_422(self):
        client = TestClient(app)
        res = client.get("/api/v1/buildings/retrofit-priority?limit=101")
        assert res.status_code == 422

    def test_rank_starts_at_1(self):
        db = _make_db_session({"total_kwh_yr": [self._RETRO_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get("/api/v1/buildings/retrofit-priority")
            buildings = res.json()["buildings"]
            if buildings:
                assert buildings[0]["rank"] == 1
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /{pnu} — F-DET-05 envelope 섹션
# ---------------------------------------------------------------------------

class TestBuildingDetailEnvelope:
    _PNU = "1111010100100030000"

    _DETAIL_ROW = _row(
        pnu="1111010100100030000",
        building_name="테스트 건물",
        usage_type="업무시설",
        vintage_class="1980-2000",
        built_year=1990,
        total_area=3000.0,
        floors_above=8,
        floors_below=1,
        height=26.4,
        structure_type="철근콘크리트구조",
        energy_grade=None,
        total_energy=180.5,
        heating=60.0,
        cooling=40.0,
        hot_water=20.0,
        lighting=35.0,
        ventilation=25.0,
        data_tier=4,
        simulation_type="korean_bb_calibrated",
        co2_kg_m2=45.0,
        primary_energy_kwh_m2=None,
        geometry=None,
        lng=126.9,
        lat=37.5,
    )

    def test_envelope_section_exists(self):
        db = _make_db_session({"b.pnu = :pnu": [self._DETAIL_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get(f"/api/v1/buildings/{self._PNU}")
            assert res.status_code == 200
            props = res.json()["properties"]
            assert "envelope" in props, "F-DET-05: envelope 섹션 미존재"
        finally:
            app.dependency_overrides.clear()

    def test_envelope_has_uvalue_keys(self):
        db = _make_db_session({"b.pnu = :pnu": [self._DETAIL_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get(f"/api/v1/buildings/{self._PNU}")
            env = res.json()["properties"].get("envelope", {})
            for key in ("wall_uvalue", "roof_uvalue", "window_uvalue", "wwr",
                        "archetype_usage", "archetype_vintage"):
                assert key in env, f"envelope 키 누락: {key}"
        finally:
            app.dependency_overrides.clear()

    def test_uvalue_is_positive_float(self):
        db = _make_db_session({"b.pnu = :pnu": [self._DETAIL_ROW]})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get(f"/api/v1/buildings/{self._PNU}")
            env = res.json()["properties"].get("envelope", {})
            for key in ("wall_uvalue", "roof_uvalue", "window_uvalue"):
                assert isinstance(env[key], (int, float)) and env[key] > 0, \
                    f"{key} must be positive float, got {env.get(key)}"
        finally:
            app.dependency_overrides.clear()

    def test_invalid_pnu_returns_400(self):
        client = TestClient(app)
        res = client.get("/api/v1/buildings/invalid-pnu")
        assert res.status_code == 400

    def test_not_found_returns_404(self):
        db = _make_db_session({"b.pnu = :pnu": []})
        app.dependency_overrides[get_db_dependency] = lambda: db
        try:
            client = TestClient(app)
            res = client.get(f"/api/v1/buildings/{self._PNU}")
            assert res.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Redis 캐시 동작
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def test_district_stats_cache_hit_skips_db(self):
        """캐시 히트 시 DB execute 호출 없음."""
        db = MagicMock()
        app.dependency_overrides[get_db_dependency] = lambda: db

        cached_data = {"zeb_threshold": 150, "districts": [{"sigungu_cd": "11110"}]}
        with patch("src.visualization.buildings._cache_get", return_value=cached_data):
            try:
                client = TestClient(app)
                res = client.get("/api/v1/buildings/district-stats")
                assert res.status_code == 200
                assert res.json() == cached_data
                db.execute.assert_not_called()
            finally:
                app.dependency_overrides.clear()

    def test_building_detail_cache_hit_skips_db(self):
        """건물 상세 캐시 히트 시 DB 미조회."""
        _PNU = "1111010100100030000"
        db = MagicMock()
        app.dependency_overrides[get_db_dependency] = lambda: db

        cached_data = {"type": "Feature", "geometry": None, "properties": {"pnu": _PNU}}
        with patch("src.visualization.buildings._cache_get", return_value=cached_data):
            try:
                client = TestClient(app)
                res = client.get(f"/api/v1/buildings/{_PNU}")
                assert res.status_code == 200
                assert res.json()["properties"]["pnu"] == _PNU
                db.execute.assert_not_called()
            finally:
                app.dependency_overrides.clear()
