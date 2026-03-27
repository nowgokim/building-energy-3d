"""
유닛 테스트: CO2 배출량 및 1차에너지 환산 (carbon.py)

배출계수 (한국 2023 기준):
  전력   0.4781 kgCO₂/kWh, 1차에너지계수 2.75
  가스   0.2036 kgCO₂/kWh, 1차에너지계수 1.1
  지역난방 0.1218 kgCO₂/kWh, 1차에너지계수 0.614
"""

import pytest

from src.simulation.carbon import (
    CO2_FACTOR_DISTRICT_HEAT,
    CO2_FACTOR_ELECTRICITY,
    CO2_FACTOR_GAS,
    PE_FACTOR_DISTRICT_HEAT,
    PE_FACTOR_ELECTRICITY,
    PE_FACTOR_GAS,
    USAGE_MIX,
    CarbonResult,
    compute_co2_primary,
)


# ─────────────────────────────────────────────────────────────────────────────
# 기본 케이스
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeCo2Primary:
    def test_zero_energy_returns_zeros(self):
        r = compute_co2_primary(total_energy_kwh=0, area_m2=1000)
        assert r.co2_kg_yr == 0.0
        assert r.co2_kg_m2 == 0.0
        assert r.primary_energy_kwh_yr == 0.0
        assert r.primary_energy_kwh_m2 == 0.0

    def test_negative_energy_returns_zeros(self):
        r = compute_co2_primary(total_energy_kwh=-100, area_m2=500)
        assert r.co2_kg_yr == 0.0

    def test_no_area_gives_none_intensity(self):
        r = compute_co2_primary(total_energy_kwh=100_000, usage_type="office")
        assert r.co2_kg_yr > 0
        assert r.co2_kg_m2 is None
        assert r.primary_energy_kwh_m2 is None

    def test_area_given_intensity_computed(self):
        r = compute_co2_primary(total_energy_kwh=100_000, area_m2=1000, usage_type="office")
        assert r.co2_kg_m2 is not None
        assert r.primary_energy_kwh_m2 is not None
        assert abs(r.co2_kg_m2 - r.co2_kg_yr / 1000) < 0.01

    def test_returns_carbon_result_type(self):
        r = compute_co2_primary(total_energy_kwh=50_000, area_m2=500)
        assert isinstance(r, CarbonResult)


# ─────────────────────────────────────────────────────────────────────────────
# 실측 소스 분리값 (Tier C)
# ─────────────────────────────────────────────────────────────────────────────

class TestMeasuredSourceSplit:
    def test_pure_electricity(self):
        kwh = 100_000
        r = compute_co2_primary(
            total_energy_kwh=kwh,
            elec_kwh=kwh, gas_kwh=0, dh_kwh=0,
        )
        expected_co2 = kwh * CO2_FACTOR_ELECTRICITY
        expected_pe  = kwh * PE_FACTOR_ELECTRICITY
        assert abs(r.co2_kg_yr - round(expected_co2, 1)) < 0.1
        assert abs(r.primary_energy_kwh_yr - round(expected_pe, 1)) < 0.1

    def test_pure_gas(self):
        kwh = 100_000
        r = compute_co2_primary(
            total_energy_kwh=kwh,
            elec_kwh=0, gas_kwh=kwh, dh_kwh=0,
        )
        expected_co2 = kwh * CO2_FACTOR_GAS
        assert abs(r.co2_kg_yr - round(expected_co2, 1)) < 0.1

    def test_pure_district_heat(self):
        kwh = 50_000
        r = compute_co2_primary(
            total_energy_kwh=kwh,
            elec_kwh=0, gas_kwh=0, dh_kwh=kwh,
        )
        expected_co2 = kwh * CO2_FACTOR_DISTRICT_HEAT
        assert abs(r.co2_kg_yr - round(expected_co2, 1)) < 0.1

    def test_mixed_sources_exact(self):
        e, g, d = 60_000, 30_000, 10_000
        r = compute_co2_primary(
            total_energy_kwh=e + g + d,
            elec_kwh=e, gas_kwh=g, dh_kwh=d,
        )
        expected_co2 = e * CO2_FACTOR_ELECTRICITY + g * CO2_FACTOR_GAS + d * CO2_FACTOR_DISTRICT_HEAT
        expected_pe  = e * PE_FACTOR_ELECTRICITY  + g * PE_FACTOR_GAS  + d * PE_FACTOR_DISTRICT_HEAT
        assert abs(r.co2_kg_yr - round(expected_co2, 1)) < 0.1
        assert abs(r.primary_energy_kwh_yr - round(expected_pe, 1)) < 0.1

    def test_negative_source_values_clamped_to_zero(self):
        r = compute_co2_primary(
            total_energy_kwh=100_000,
            elec_kwh=-5_000, gas_kwh=100_000, dh_kwh=0,
        )
        # elec_kwh should be clamped to 0
        expected_co2 = 100_000 * CO2_FACTOR_GAS
        assert abs(r.co2_kg_yr - round(expected_co2, 1)) < 0.1


# ─────────────────────────────────────────────────────────────────────────────
# 용도별 믹스 추정
# ─────────────────────────────────────────────────────────────────────────────

class TestUsageMix:
    def test_office_high_electricity_share(self):
        """업무시설은 전력 비중 80% → CO2가 전력 주도."""
        r_office = compute_co2_primary(total_energy_kwh=100_000, usage_type="office")
        r_apartment = compute_co2_primary(total_energy_kwh=100_000, usage_type="apartment")
        # 전력은 배출계수 높음(0.4781) → 업무시설이 더 높은 CO2
        assert r_office.co2_kg_yr > r_apartment.co2_kg_yr

    def test_apartment_high_gas_share_lower_co2(self):
        """공동주택은 가스 비중 65% → 상대적으로 낮은 CO2."""
        r = compute_co2_primary(total_energy_kwh=100_000, usage_type="apartment")
        frac_e, frac_g, frac_d = USAGE_MIX["apartment"]
        kwh = 100_000
        expected = kwh * frac_e * CO2_FACTOR_ELECTRICITY + kwh * frac_g * CO2_FACTOR_GAS
        assert abs(r.co2_kg_yr - round(expected, 1)) < 0.1

    def test_hospital_includes_district_heat(self):
        """병원은 지역난방 10% → dh_kwh > 0."""
        frac_e, frac_g, frac_d = USAGE_MIX["hospital"]
        assert frac_d == 0.10
        r = compute_co2_primary(total_energy_kwh=100_000, usage_type="hospital")
        # district heat 포함 계산 검증
        d = 100_000 * frac_d
        assert r.co2_kg_yr > 0

    def test_unknown_usage_uses_default_mix(self):
        r1 = compute_co2_primary(total_energy_kwh=100_000, usage_type="unknown_xyz")
        r2 = compute_co2_primary(total_energy_kwh=100_000, usage_type=None)
        assert r1.co2_kg_yr == r2.co2_kg_yr

    @pytest.mark.parametrize("usage", list(USAGE_MIX.keys()))
    def test_all_usage_types_produce_positive_co2(self, usage):
        r = compute_co2_primary(total_energy_kwh=50_000, area_m2=500, usage_type=usage)
        assert r.co2_kg_yr > 0
        assert r.primary_energy_kwh_yr > 0
        assert r.co2_kg_m2 is not None and r.co2_kg_m2 > 0
        assert r.primary_energy_kwh_m2 is not None and r.primary_energy_kwh_m2 > 0


# ─────────────────────────────────────────────────────────────────────────────
# 1차에너지 환산 (ZEB 기준 검증)
# ─────────────────────────────────────────────────────────────────────────────

class TestPrimaryEnergy:
    def test_office_primary_energy_higher_than_final(self):
        """전력 주도 업무시설은 1차에너지 > 최종에너지 (계수 2.75 > 1)."""
        total = 100_000
        r = compute_co2_primary(total_energy_kwh=total, usage_type="office")
        assert r.primary_energy_kwh_yr > total

    def test_apartment_primary_energy_close_to_final(self):
        """가스 주도 공동주택은 1차에너지 ≒ 최종에너지 × 1.x (계수 1.1)."""
        total = 100_000
        r = compute_co2_primary(total_energy_kwh=total, usage_type="apartment")
        # 전력 35%×2.75 + 가스 65%×1.1 = 0.9625+0.715 = 1.6775x
        assert total < r.primary_energy_kwh_yr < total * 2.0

    def test_zeb_threshold(self):
        """ZEB 5등급 목표: 1차에너지 ≤ 150 kWh/m²·yr."""
        # 사무소 3000m², 연간 전력 135,000 kWh (EUI ≈ 45 kWh/m², PE ≈ 123.75)
        r = compute_co2_primary(
            total_energy_kwh=135_000,
            area_m2=3_000,
            elec_kwh=135_000, gas_kwh=0, dh_kwh=0,
        )
        assert r.primary_energy_kwh_m2 is not None
        assert r.primary_energy_kwh_m2 < 150


# ─────────────────────────────────────────────────────────────────────────────
# 배출계수 / 환산계수 값 검증 (상수 변경 방지)
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_co2_factor_electricity(self):
        assert CO2_FACTOR_ELECTRICITY == pytest.approx(0.4781)

    def test_co2_factor_gas(self):
        assert CO2_FACTOR_GAS == pytest.approx(0.2036)

    def test_co2_factor_district_heat(self):
        assert CO2_FACTOR_DISTRICT_HEAT == pytest.approx(0.1218)

    def test_pe_factor_electricity(self):
        assert PE_FACTOR_ELECTRICITY == pytest.approx(2.75)

    def test_pe_factor_gas(self):
        assert PE_FACTOR_GAS == pytest.approx(1.1)

    def test_pe_factor_district_heat(self):
        assert PE_FACTOR_DISTRICT_HEAT == pytest.approx(0.614)

    def test_usage_mix_fractions_sum_to_one(self):
        for usage, (e, g, d) in USAGE_MIX.items():
            total = e + g + d
            assert total == pytest.approx(1.0, abs=1e-9), \
                f"Usage '{usage}' mix sum = {total} (expected 1.0)"
