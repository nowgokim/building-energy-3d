# -*- coding: utf-8 -*-
"""
Tier C 시계열 예측 파이프라인 유닛 테스트.

테스트 범위
----------
1. BuildingTimeSeries — 데이터 변환 및 결측 보간
2. LagFeatureBuilder — lag 피처 생성 정확성
3. LagXGBoostPredictor — 학습/예측/직렬화
4. predict_timeseries — 통합 진입점 로직
5. 공휴일 피처 — is_holiday, is_work_day 정확성
6. 경계값 — 이력 부족, 0 소비량, 단일 에너지원 등
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.simulation.ml_timeseries import (
    KOREA_HOLIDAYS,
    BuildingTimeSeries,
    DailyConsumption,
    LagFeatureBuilder,
    LagXGBoostConfig,
    LagXGBoostPredictor,
    PerBuildingModelRegistry,
    predict_timeseries,
)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

def _make_ts(
    pnu: str = "TEST_PNU",
    days: int = 400,
    base_kwh: float = 100.0,
    usage_type: str = "office",
    floor_area: float = 3000.0,
    start_date: date = date(2024, 1, 1),
) -> BuildingTimeSeries:
    """days 길이의 합성 일별 소비 시계열 생성."""
    records = []
    rng = np.random.default_rng(42)
    for i in range(days):
        d = start_date + timedelta(days=i)
        # 주간/주말 패턴 + 노이즈
        is_weekend = d.weekday() >= 5
        kwh = base_kwh * (0.5 if is_weekend else 1.0) + rng.normal(0, 5)
        records.append(DailyConsumption(
            consumption_date=d,
            elec_kwh=max(kwh * 0.6, 0),
            gas_kwh=max(kwh * 0.4, 0),
        ))
    return BuildingTimeSeries(
        pnu=pnu, floor_area=floor_area,
        usage_type=usage_type, records=records,
    )


@pytest.fixture
def ts_400d() -> BuildingTimeSeries:
    return _make_ts(days=400)


@pytest.fixture
def ts_short() -> BuildingTimeSeries:
    return _make_ts(days=100)


@pytest.fixture
def ts_minimal() -> BuildingTimeSeries:
    """최소 유효 이력 (180일)."""
    return _make_ts(days=180)


# ---------------------------------------------------------------------------
# 1. DailyConsumption + BuildingTimeSeries 테스트
# ---------------------------------------------------------------------------

class TestDailyConsumption:
    def test_total_sums_elec_and_gas(self):
        rec = DailyConsumption(
            consumption_date=date(2025, 6, 15),
            elec_kwh=60.0, gas_kwh=40.0,
        )
        assert rec.total() == pytest.approx(100.0)

    def test_total_kwh_overrides_sum(self):
        rec = DailyConsumption(
            consumption_date=date(2025, 6, 15),
            elec_kwh=60.0, gas_kwh=40.0, total_kwh=200.0,
        )
        assert rec.total() == pytest.approx(200.0)

    def test_none_energy_treated_as_zero(self):
        rec = DailyConsumption(
            consumption_date=date(2025, 1, 1),
            elec_kwh=None, gas_kwh=None,
        )
        assert rec.total() == pytest.approx(0.0)

    def test_only_elec(self):
        rec = DailyConsumption(
            consumption_date=date(2025, 3, 1), elec_kwh=80.0
        )
        assert rec.total() == pytest.approx(80.0)


class TestBuildingTimeSeries:
    def test_to_dataframe_sorted_by_date(self, ts_400d):
        df = ts_400d.to_dataframe()
        assert df["ds"].is_monotonic_increasing

    def test_to_dataframe_row_count(self, ts_400d):
        df = ts_400d.to_dataframe()
        # 결측일 없이 400일 연속 데이터이므로 400행이어야 한다
        assert len(df) == 400

    def test_interpolation_fills_gap(self):
        """3일 연속 결측(전체 None)이 선형 보간으로 0이 아닌 값으로 채워져야 한다."""
        records = []
        for i in range(30):
            d = date(2025, 1, 1) + timedelta(days=i)
            if 10 <= i <= 12:
                # 세 필드 모두 None → 보간 대상 (실제 결측)
                records.append(DailyConsumption(consumption_date=d))
            else:
                records.append(DailyConsumption(consumption_date=d, total_kwh=100.0))
        ts = BuildingTimeSeries(pnu="GAP", floor_area=1000.0, usage_type="office",
                                records=records)
        df = ts.to_dataframe()
        # 결측 구간이 선형 보간으로 100 근방 값으로 채워졌는지 확인
        gap_mask = (df["ds"] >= pd.Timestamp("2025-01-11")) & (df["ds"] <= pd.Timestamp("2025-01-13"))
        assert (df.loc[gap_mask, "y"] > 0).all()

    def test_has_sufficient_history_true(self, ts_400d):
        assert ts_400d.has_sufficient_history(min_days=365) is True

    def test_has_sufficient_history_false_short(self, ts_short):
        assert ts_short.has_sufficient_history(min_days=365) is False

    def test_has_sufficient_history_empty(self):
        ts = BuildingTimeSeries(pnu="EMPTY", floor_area=1000.0, usage_type="office")
        assert ts.has_sufficient_history() is False

    def test_to_dataframe_includes_y_column(self, ts_400d):
        df = ts_400d.to_dataframe()
        assert "y" in df.columns

    def test_y_all_non_negative(self, ts_400d):
        df = ts_400d.to_dataframe()
        assert (df["y"] >= 0).all()


# ---------------------------------------------------------------------------
# 2. LagFeatureBuilder 테스트
# ---------------------------------------------------------------------------

class TestLagFeatureBuilder:
    @pytest.fixture
    def builder(self) -> LagFeatureBuilder:
        return LagFeatureBuilder()

    @pytest.fixture
    def base_df(self, ts_400d) -> pd.DataFrame:
        return ts_400d.to_dataframe()

    def test_output_contains_lag_columns(self, builder, base_df):
        df = builder.build(base_df)
        for lag in LagFeatureBuilder.LAGS:
            assert f"lag_{lag}d" in df.columns

    def test_output_contains_rolling_columns(self, builder, base_df):
        df = builder.build(base_df)
        for w in LagFeatureBuilder.ROLLING_WINDOWS:
            assert f"roll_mean_{w}d" in df.columns
            assert f"roll_std_{w}d" in df.columns

    def test_weekend_flag_correct(self, builder, base_df):
        df = builder.build(base_df)
        # ds가 토요일(5)이면 is_weekend=1
        sat_rows = df[df["ds"].dt.dayofweek == 5]
        assert (sat_rows["is_weekend"] == 1).all()
        # 월요일(0)이면 is_weekend=0
        mon_rows = df[df["ds"].dt.dayofweek == 0]
        assert (mon_rows["is_weekend"] == 0).all()

    def test_holiday_flag_for_known_holiday(self, builder, ts_400d):
        """2025-01-01(신정)이 포함된 시계열에서 is_holiday=1 이어야 한다."""
        df = ts_400d.to_dataframe()
        df = builder.build(df)
        new_year_row = df[df["ds"] == pd.Timestamp("2025-01-01")]
        if not new_year_row.empty:
            assert new_year_row["is_holiday"].iloc[0] == 1

    def test_work_day_is_zero_on_weekend(self, builder, base_df):
        df = builder.build(base_df)
        weekend_rows = df[df["is_weekend"] == 1]
        assert (weekend_rows["is_work_day"] == 0).all()

    def test_lag_1d_matches_previous_y(self, builder, base_df):
        df = builder.build(base_df)
        # lag_1d 행[i]은 y 행[i-1]과 같아야 한다
        valid = df.dropna(subset=["lag_1d"])
        if len(valid) < 2:
            pytest.skip("데이터 부족")
        for _, row in valid.head(5).iterrows():
            lag_date = row["ds"] - pd.Timedelta(days=1)
            prev_row = df[df["ds"] == lag_date]
            if not prev_row.empty:
                assert row["lag_1d"] == pytest.approx(prev_row["y"].iloc[0], rel=1e-3)

    def test_tmy_weather_attached_when_no_weather_df(self, builder, base_df):
        df = builder.build(base_df, weather_df=None)
        assert "HDD18" in df.columns
        assert "CDD24" in df.columns
        assert "avg_temp" in df.columns
        # 1월은 HDD18 > 0 이어야 한다
        jan_rows = df[df["ds"].dt.month == 1]
        if not jan_rows.empty:
            assert jan_rows["HDD18"].iloc[0] > 0

    def test_feature_column_list_matches_builder(self, builder):
        """get_feature_columns()의 컬럼이 build() 출력에 모두 존재해야 한다."""
        ts = _make_ts(days=100)
        df = ts.to_dataframe()
        df = builder.build(df)
        for col in builder.get_feature_columns():
            assert col in df.columns, f"피처 컬럼 누락: {col}"


# ---------------------------------------------------------------------------
# 3. LagXGBoostPredictor 테스트 (xgboost 없으면 skip)
# ---------------------------------------------------------------------------

try:
    import xgboost as _xgb_check  # noqa: F401
    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False

_skip_if_no_xgboost = pytest.mark.skipif(
    not _HAS_XGBOOST,
    reason="xgboost 미설치 — 시계열 모델 테스트 건너뜀",
)


@pytest.fixture
def fitted_ts_predictor(ts_400d) -> LagXGBoostPredictor:
    pytest.importorskip("xgboost")
    cfg = LagXGBoostConfig(n_estimators=20, early_stopping_rounds=5)
    p = LagXGBoostPredictor(cfg)
    p.fit(ts_400d)
    return p


@_skip_if_no_xgboost
class TestLagXGBoostPredictor:
    def test_fit_returns_self(self, ts_400d):
        cfg = LagXGBoostConfig(n_estimators=10)
        p = LagXGBoostPredictor(cfg)
        result = p.fit(ts_400d)
        assert result is p

    def test_predict_output_shape(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=7)
        assert len(result) == 7
        assert "y_pred" in result.columns
        assert "y_lower" in result.columns
        assert "y_upper" in result.columns

    def test_predict_dates_are_future(self, fitted_ts_predictor, ts_400d):
        """예측 날짜가 이력의 마지막 날짜 이후여야 한다."""
        last_date = pd.Timestamp(max(r.consumption_date for r in ts_400d.records))
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=7)
        for ds in result["ds"]:
            assert pd.Timestamp(ds) > last_date

    def test_predict_non_negative(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=7)
        assert (result["y_pred"] >= 0).all()
        assert (result["y_lower"] >= 0).all()

    def test_interval_ordering(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=7)
        assert (result["y_lower"] <= result["y_pred"]).all()
        assert (result["y_pred"] <= result["y_upper"]).all()

    def test_horizon_1(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=1)
        assert len(result) == 1

    def test_horizon_30(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict(ts_400d, horizon_days=30)
        assert len(result) == 30

    def test_predict_daily_eui_has_eui_columns(self, fitted_ts_predictor, ts_400d):
        result = fitted_ts_predictor.predict_daily_eui(ts_400d, horizon_days=7)
        assert "eui_pred" in result.columns
        assert "eui_lower" in result.columns
        assert "eui_upper" in result.columns

    def test_eui_scaling(self, fitted_ts_predictor, ts_400d):
        """EUI = kWh / floor_area 로 올바르게 환산되어야 한다."""
        df_kwh = fitted_ts_predictor.predict(ts_400d, horizon_days=3)
        df_eui = fitted_ts_predictor.predict_daily_eui(ts_400d, horizon_days=3)
        area = ts_400d.floor_area
        for i in range(3):
            expected = df_kwh["y_pred"].iloc[i] / area
            assert df_eui["eui_pred"].iloc[i] == pytest.approx(expected, rel=1e-3)

    def test_save_and_load_roundtrip(self, fitted_ts_predictor, ts_400d, tmp_path):
        model_path = tmp_path / "ts_model.pkl"
        fitted_ts_predictor.save(model_path)
        assert model_path.exists()

        loaded = LagXGBoostPredictor.load(model_path)
        pred_orig = fitted_ts_predictor.predict(ts_400d, horizon_days=3)
        pred_loaded = loaded.predict(ts_400d, horizon_days=3)
        pd.testing.assert_frame_equal(
            pred_orig.reset_index(drop=True),
            pred_loaded.reset_index(drop=True),
            check_exact=False, rtol=1e-4,
        )

    def test_fit_short_history_raises(self):
        """30일 미만 이력은 ValueError가 발생해야 한다."""
        ts_tiny = _make_ts(days=15)
        p = LagXGBoostPredictor(LagXGBoostConfig(n_estimators=5))
        with pytest.raises(ValueError, match="학습 데이터 부족"):
            p.fit(ts_tiny)

    def test_predict_before_fit_raises(self, ts_400d):
        p = LagXGBoostPredictor()
        with pytest.raises(RuntimeError, match="fit"):
            p.predict(ts_400d)

    def test_residual_p90_positive_after_fit(self, ts_400d):
        p = LagXGBoostPredictor(LagXGBoostConfig(n_estimators=10))
        p.fit(ts_400d)
        assert p._residual_p90 >= 0.0


# ---------------------------------------------------------------------------
# 4. predict_timeseries 통합 진입점 테스트
# ---------------------------------------------------------------------------

@_skip_if_no_xgboost
class TestPredictTimeseries:
    def test_basic_prediction(self, ts_400d):
        result = predict_timeseries(ts_400d, horizon_days=7)
        assert len(result) == 7
        assert "eui_pred" in result.columns

    def test_insufficient_history_raises(self, ts_short):
        with pytest.raises(ValueError, match="이력 부족"):
            predict_timeseries(ts_short, horizon_days=7)

    def test_model_saved_to_registry(self, ts_400d, tmp_path):
        registry = PerBuildingModelRegistry(base_model_dir=tmp_path / "models")
        predict_timeseries(ts_400d, horizon_days=3, model_registry=registry)
        assert registry.has_model(ts_400d.pnu)

    def test_model_loaded_from_registry_on_second_call(self, ts_400d, tmp_path):
        registry = PerBuildingModelRegistry(base_model_dir=tmp_path / "models")
        # 첫 번째 호출: 학습 + 저장
        predict_timeseries(ts_400d, horizon_days=3, model_registry=registry)
        # 두 번째 호출: 저장된 모델 로드 (에러 없으면 통과)
        result = predict_timeseries(ts_400d, horizon_days=3, model_registry=registry)
        assert len(result) == 3

    def test_invalid_predictor_type_raises(self, ts_400d):
        with pytest.raises(ValueError, match="알 수 없는"):
            predict_timeseries(ts_400d, predictor_type="invalid_type")

    def test_minimal_history_succeeds(self, ts_minimal):
        """정확히 180일 이력도 예측 가능해야 한다."""
        result = predict_timeseries(ts_minimal, horizon_days=7)
        assert len(result) == 7


# ---------------------------------------------------------------------------
# 5. 경계값 테스트
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_all_zero_consumption(self):
        """소비량이 0인 이력도 피처 생성은 가능해야 한다."""
        records = [
            DailyConsumption(
                consumption_date=date(2024, 1, 1) + timedelta(days=i),
                total_kwh=0.0,
            )
            for i in range(200)
        ]
        ts = BuildingTimeSeries(pnu="ZERO", floor_area=1000.0,
                                usage_type="warehouse", records=records)
        df = ts.to_dataframe()
        builder = LagFeatureBuilder()
        feat_df = builder.build(df)
        assert not feat_df.empty

    def test_single_energy_type_elec_only(self):
        """전기만 있는 건물도 total()이 올바르게 계산되어야 한다."""
        rec = DailyConsumption(
            consumption_date=date(2025, 1, 1), elec_kwh=150.0, gas_kwh=None
        )
        assert rec.total() == pytest.approx(150.0)

    @pytest.mark.skipif(not _HAS_XGBOOST, reason="xgboost 미설치")
    def test_floor_area_zero_handled_in_eui(self, ts_400d):
        """floor_area=0이면 나눗셈 오류 없이 매우 큰 EUI가 나와야 한다."""
        ts = BuildingTimeSeries(
            pnu=ts_400d.pnu,
            floor_area=0.0,
            usage_type=ts_400d.usage_type,
            records=ts_400d.records,
        )
        # predict_daily_eui 내부에서 max(floor_area, 1.0) 처리
        p = LagXGBoostPredictor(LagXGBoostConfig(n_estimators=10))
        p.fit(ts_400d)   # 원본(floor_area=3000)으로 학습
        result = p.predict_daily_eui(ts, horizon_days=3)
        # floor_area=1로 처리되어 y_pred * 1 = eui_pred
        for i in range(3):
            assert result["eui_pred"].iloc[i] == pytest.approx(
                result["y_pred"].iloc[i], rel=1e-3
            )

    def test_korea_holidays_coverage(self):
        """공휴일 집합이 비어 있지 않아야 한다."""
        assert len(KOREA_HOLIDAYS) > 0

    def test_holiday_is_not_work_day(self):
        builder = LagFeatureBuilder()
        ts = _make_ts(days=400, start_date=date(2024, 10, 1))
        df = ts.to_dataframe()
        df = builder.build(df)
        # 공휴일 행의 is_work_day = 0 이어야 한다
        holiday_rows = df[df["is_holiday"] == 1]
        if not holiday_rows.empty:
            assert (holiday_rows["is_work_day"] == 0).all()
