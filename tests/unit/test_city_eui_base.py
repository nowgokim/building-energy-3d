# -*- coding: utf-8 -*-
"""
유닛 테스트: city_eui_base.py (Phase 4-B 도시별 EUI 보정비)

커버:
- get_city_ratio() 정상 동작 (알려진 도시, 자기 도시, 소문자 입력)
- 미지원 도시/아키타입 → None 반환
- get_city_factor alias 동작
- 전체 10개 도시 × 아파트 데이터 존재 확인
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.simulation.city_eui_base import (
    ARCHETYPE_TO_EMS,
    CITY_EUI_BASE,
    get_city_factor,
    get_city_ratio,
)

_KNOWN_ARCHETYPES = list(ARCHETYPE_TO_EMS.keys())
_KNOWN_CITIES = [
    "seoul", "busan", "daegu", "incheon", "gwangju",
    "daejeon", "ulsan", "cheongju", "gangneung", "jeju",
]


class TestGetCityRatio:
    def test_same_city_ratio_is_1(self):
        """같은 도시를 기준과 대상으로 쓰면 비율 = 1.0"""
        ratio = get_city_ratio("office", "seoul", reference="seoul")
        assert ratio == pytest.approx(1.0, abs=1e-4)

    def test_returns_float_for_known_inputs(self):
        ratio = get_city_ratio("office", "busan", reference="seoul")
        assert isinstance(ratio, float)
        assert 0.5 < ratio < 2.0  # 물리적으로 극단값은 없음

    def test_ratio_is_positive(self):
        for city in _KNOWN_CITIES:
            ratio = get_city_ratio("apartment_highrise", city, reference="seoul")
            assert ratio is not None and ratio > 0

    def test_unknown_archetype_returns_none(self):
        assert get_city_ratio("nonexistent_type", "seoul") is None

    def test_unknown_city_returns_none(self):
        assert get_city_ratio("office", "pyongyang") is None

    def test_unknown_reference_returns_none(self):
        assert get_city_ratio("office", "seoul", reference="unknown") is None

    def test_daejeon_vs_seoul_apartment(self):
        """대전은 서울 대비 약간 높은 EUI (냉방 부하↑) — 방향성 확인"""
        ratio = get_city_ratio("apartment_highrise", "daejeon", reference="seoul")
        # CITY_EUI_BASE: 대전 200.2, 서울 198.2 → ratio > 1
        assert ratio is not None
        assert ratio > 1.0

    def test_incheon_vs_seoul_apartment(self):
        """인천은 서울 대비 약간 낮은 EUI (해안 기후) — 방향성 확인"""
        ratio = get_city_ratio("apartment_midrise", "incheon", reference="seoul")
        assert ratio is not None
        assert ratio < 1.0


class TestGetCityFactor:
    def test_alias_same_result_as_get_city_ratio(self):
        r1 = get_city_ratio("hotel", "busan", reference="seoul")
        r2 = get_city_factor("hotel", "busan", reference="seoul")
        assert r1 == r2

    def test_alias_is_callable(self):
        assert callable(get_city_factor)


class TestCityEuiBaseCoverage:
    def test_all_10_cities_covered_for_apartment(self):
        """10개 도시 × Apartment 모두 데이터 존재"""
        for city in _KNOWN_CITIES:
            ratio = get_city_ratio("apartment_highrise", city)
            assert ratio is not None, f"Missing data for city={city}"

    def test_all_mapped_archetypes_have_seoul_data(self):
        """ARCHETYPE_TO_EMS 매핑된 아키타입 전부 서울 데이터 존재"""
        for archetype in _KNOWN_ARCHETYPES:
            ratio = get_city_ratio(archetype, "seoul", reference="seoul")
            assert ratio is not None, f"Missing Seoul data for archetype={archetype}"

    def test_city_eui_base_has_150_entries(self):
        """15 building × 10 city = 150 항목"""
        assert len(CITY_EUI_BASE) == 150

    def test_all_entries_have_required_keys(self):
        for key, stats in CITY_EUI_BASE.items():
            assert "median" in stats, f"Missing median: {key}"
            assert "p10" in stats, f"Missing p10: {key}"
            assert "p90" in stats, f"Missing p90: {key}"
            assert stats["p10"] <= stats["median"] <= stats["p90"], \
                f"EUI percentile order violated: {key}"
