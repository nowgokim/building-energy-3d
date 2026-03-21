"""열화계수 계산 테스트"""
import pytest
from src.simulation.archetypes import apply_degradation, match_archetype, estimate_energy


class TestDegradation:
    def test_new_building(self):
        """신축 (10년 이내): 열화계수 1.0"""
        result = apply_degradation(0.24, 2020)
        assert result == pytest.approx(0.24, rel=0.01)

    def test_10_to_20_years(self):
        """10~20년: 열화계수 1.3"""
        result = apply_degradation(0.47, 2010)
        assert result == pytest.approx(0.47 * 1.3, rel=0.01)

    def test_20_to_30_years(self):
        """20~30년: 열화계수 1.7"""
        result = apply_degradation(0.58, 2000)
        assert result == pytest.approx(0.58 * 1.7, rel=0.01)

    def test_over_30_years(self):
        """30년 초과: 열화계수 2.0 (상한)"""
        result = apply_degradation(0.58, 1990)
        assert result == pytest.approx(0.58 * 2.0, rel=0.01)

    def test_none_year(self):
        """건축년도 None: 최대 열화 적용"""
        result = apply_degradation(0.47, None)
        assert result == pytest.approx(0.47 * 2.0, rel=0.01)


class TestMatchArchetype:
    def test_apartment(self):
        """공동주택 원형 매칭"""
        arch = match_archetype("공동주택", 2015, 5000, "RC")
        assert arch["wall_uvalue"] > 0
        assert arch["wwr"] > 0

    def test_office(self):
        """사무 원형 매칭"""
        arch = match_archetype("업무시설", 2005, 2000, "RC")
        assert arch["wall_uvalue"] > 0

    def test_unknown_usage_fallback(self):
        """알 수 없는 용도 → 폴백"""
        arch = match_archetype("우주정거장", 2020, 1000, "RC")
        assert arch is not None
        assert "wall_uvalue" in arch


class TestEstimateEnergy:
    def test_apartment_benchmark(self):
        """공동주택 에너지 벤치마크 범위 검증 (~136 kWh/m2)"""
        arch = match_archetype("공동주택", 2015, 5000, "RC")
        energy = estimate_energy(arch)
        assert 80 < energy["total_energy"] < 250  # 합리적 범위
        assert energy["heating"] > 0
        assert energy["cooling"] > 0

    def test_energy_breakdown_sum(self):
        """에너지 분해 합계 = total"""
        arch = match_archetype("업무시설", 2010, 3000, "steel")
        energy = estimate_energy(arch)
        component_sum = (
            energy["heating"] + energy["cooling"] + energy["hot_water"]
            + energy["lighting"] + energy["ventilation"]
        )
        assert component_sum == pytest.approx(energy["total_energy"], rel=0.01)
