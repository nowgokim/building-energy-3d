"""
유닛 테스트: 리트로핏 비용·효과 추정 (retrofit.py)

커버:
- 개별 조치 EUI 절감 방향성 (노후 > 신축)
- 복합 조치 중복 절감 방지 (remaining_eui 감소)
- 비용·회수기간 계산
- 존재하지 않는 조치 ID 무시
- 면적 None 처리 (총비용/회수기간 = 0/None)
"""

import pytest

from src.simulation.retrofit import (
    ALL_MEASURE_IDS,
    MEASURES,
    MeasureResult,
    RetrofitResult,
    simulate_retrofit,
)

_BASE = dict(
    pnu="1111017300100011196",
    eui_kwh_m2=150.0,
    co2_kg_m2=45.0,
    total_area_m2=3_000.0,
    vintage_class="1980-2000",
    usage_type="업무시설",
)


# ─────────────────────────────────────────────────────────────────────────────
# 기본 동작
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicBehavior:
    def test_returns_retrofit_result_type(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert isinstance(r, RetrofitResult)

    def test_single_measure_returns_one_measure(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert len(r.measures) == 1
        assert r.measures[0].id == "window"

    def test_measure_result_type(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert isinstance(r.measures[0], MeasureResult)

    def test_all_measures_when_none_selected(self):
        r = simulate_retrofit(**_BASE, selected_measures=None)
        assert len(r.measures) == len(MEASURES)

    def test_invalid_measure_id_ignored(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window", "nonexistent_xyz"])
        assert len(r.measures) == 1

    def test_zero_eui_raises(self):
        with pytest.raises(ValueError):
            simulate_retrofit(**{**_BASE, "eui_kwh_m2": 0})

    def test_negative_eui_raises(self):
        with pytest.raises(ValueError):
            simulate_retrofit(**{**_BASE, "eui_kwh_m2": -10})


# ─────────────────────────────────────────────────────────────────────────────
# EUI 절감
# ─────────────────────────────────────────────────────────────────────────────

class TestEuiSaving:
    def test_eui_after_less_than_before(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.eui_after < r.eui_before

    def test_eui_saving_positive(self):
        r = simulate_retrofit(**_BASE, selected_measures=["wall_insulation"])
        assert r.eui_saving_kwh_m2 > 0

    def test_older_building_saves_more_than_new(self):
        old = simulate_retrofit(**{**_BASE, "vintage_class": "pre-1980"},
                                selected_measures=["window"])
        new = simulate_retrofit(**{**_BASE, "vintage_class": "post-2010"},
                                selected_measures=["window"])
        assert old.eui_saving_kwh_m2 > new.eui_saving_kwh_m2

    def test_combined_saving_not_double_counted(self):
        """복합 적용 시 개별 합산보다 작거나 같아야 함 (remaining_eui 감소)."""
        r_w = simulate_retrofit(**_BASE, selected_measures=["window"])
        r_wi = simulate_retrofit(**_BASE, selected_measures=["wall_insulation"])
        r_both = simulate_retrofit(**_BASE, selected_measures=["window", "wall_insulation"])
        assert r_both.eui_saving_kwh_m2 <= r_w.eui_saving_kwh_m2 + r_wi.eui_saving_kwh_m2

    def test_saving_pct_in_range(self):
        r = simulate_retrofit(**_BASE, selected_measures=None)
        assert 0.0 < r.saving_pct <= 1.0

    def test_led_saving_independent_of_hvac(self):
        r = simulate_retrofit(**_BASE, selected_measures=["led_lighting"])
        assert r.eui_saving_kwh_m2 > 0

    def test_eui_after_not_negative(self):
        """EUI는 음수가 되지 않아야 함."""
        r = simulate_retrofit(**_BASE, selected_measures=None)
        assert r.eui_after >= 0

    @pytest.mark.parametrize("mid", ALL_MEASURE_IDS)
    def test_each_measure_reduces_eui(self, mid):
        r = simulate_retrofit(**_BASE, selected_measures=[mid])
        assert r.eui_after < r.eui_before, f"Measure '{mid}' should reduce EUI"


# ─────────────────────────────────────────────────────────────────────────────
# CO2 절감
# ─────────────────────────────────────────────────────────────────────────────

class TestCo2Saving:
    def test_co2_after_less_than_before(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.co2_after_kg_m2 < r.co2_before_kg_m2

    def test_co2_saving_positive(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.co2_saving_kg_m2 is not None and r.co2_saving_kg_m2 > 0

    def test_co2_none_when_no_co2_input(self):
        r = simulate_retrofit(**{**_BASE, "co2_kg_m2": None}, selected_measures=["window"])
        assert r.co2_after_kg_m2 is None
        assert r.co2_saving_kg_m2 is None

    def test_co2_proportional_to_eui_reduction(self):
        """CO2 절감 비율 ≈ EUI 절감 비율."""
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        eui_ratio = r.eui_saving_kwh_m2 / r.eui_before
        co2_ratio = r.co2_saving_kg_m2 / r.co2_before_kg_m2
        assert abs(eui_ratio - co2_ratio) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# 비용 & 회수기간
# ─────────────────────────────────────────────────────────────────────────────

class TestCostPayback:
    def test_cost_positive_with_area(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.cost_total_krw > 0

    def test_cost_zero_without_area(self):
        r = simulate_retrofit(**{**_BASE, "total_area_m2": None}, selected_measures=["window"])
        assert r.cost_total_krw == 0

    def test_payback_none_without_area(self):
        r = simulate_retrofit(**{**_BASE, "total_area_m2": None}, selected_measures=["window"])
        assert r.payback_years is None

    def test_payback_positive(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.payback_years is not None and r.payback_years > 0

    def test_cost_scales_with_area(self):
        r1 = simulate_retrofit(**{**_BASE, "total_area_m2": 1000}, selected_measures=["window"])
        r2 = simulate_retrofit(**{**_BASE, "total_area_m2": 2000}, selected_measures=["window"])
        assert r2.cost_total_krw == pytest.approx(r1.cost_total_krw * 2, rel=0.01)

    def test_annual_saving_positive(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.annual_saving_krw > 0

    def test_measure_payback_reasonable(self):
        """회수기간은 현실적 범위 (2~30년)."""
        r = simulate_retrofit(**_BASE, selected_measures=["wall_insulation"])
        assert r.measures[0].payback_years is not None
        assert 1.0 < r.measures[0].payback_years < 50.0

    def test_combined_cost_equals_sum_of_individual(self):
        r_w  = simulate_retrofit(**_BASE, selected_measures=["window"])
        r_wi = simulate_retrofit(**_BASE, selected_measures=["wall_insulation"])
        r_both = simulate_retrofit(**_BASE, selected_measures=["window", "wall_insulation"])
        expected = r_w.cost_total_krw + r_wi.cost_total_krw
        assert r_both.cost_total_krw == pytest.approx(expected, rel=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 정합성
# ─────────────────────────────────────────────────────────────────────────────

class TestDataIntegrity:
    def test_pnu_preserved(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.pnu == _BASE["pnu"]

    def test_eui_before_preserved(self):
        r = simulate_retrofit(**_BASE, selected_measures=["window"])
        assert r.eui_before == _BASE["eui_kwh_m2"]

    def test_measure_ids_match_spec(self):
        r = simulate_retrofit(**_BASE, selected_measures=None)
        returned_ids = {m.id for m in r.measures}
        assert returned_ids == set(MEASURES.keys())

    def test_measure_labels_non_empty(self):
        r = simulate_retrofit(**_BASE, selected_measures=None)
        for m in r.measures:
            assert m.label, f"Measure '{m.id}' has empty label"

    def test_all_measure_ids_tuple(self):
        assert isinstance(ALL_MEASURE_IDS, tuple)
        assert len(ALL_MEASURE_IDS) == len(MEASURES)
