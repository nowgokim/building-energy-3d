# -*- coding: utf-8 -*-
"""
XGBoost EUI 예측 파이프라인 유닛 테스트.

테스트 범위
----------
1. FeaturePipeline — 피처 생성 정확성 및 결측 처리
2. build_training_dataset — Tier1/Tier2 통합 및 가중치
3. XGBoostEUIPredictor — 학습/예측/직렬화
4. predict_energy — 추론 파이프라인 우선순위 로직
5. 경계값 — 음수 EUI, 면적=0, built_year=None 등

모든 테스트는 실제 XGBoost 설치 없이도 피처 파이프라인까지 검증 가능하도록
xgboost import를 pytest.importorskip 으로 조건부 처리한다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 프로젝트 루트를 sys.path에 추가 (pytest.ini 없을 때 대비)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.simulation.ml_xgboost import (
    FEATURE_NAMES,
    SEOUL_TMY_ANNUAL,
    BuildingRecord,
    FeaturePipeline,
    XGBoostConfig,
    XGBoostEUIPredictor,
    build_training_dataset,
    predict_energy,
    EnergyPrediction,
    KEA_PRIMARY_TO_FINAL,
    USAGE_ENCODING,
    VINTAGE_ENCODING,
)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_record() -> BuildingRecord:
    """2001-2010 RC 아파트, 3000 m², 15층, 2005년 준공."""
    return BuildingRecord(
        pnu="1101010100100010001",
        usage_type="apartment",
        vintage_class="2001-2010",
        structure_type="RC",
        built_year=2005,
        total_area=3000.0,
        floors=15,
        wall_uvalue=0.47,
        roof_uvalue=0.29,
        window_uvalue=2.40,
        wwr=0.35,
        ref_heating=55.0,
        ref_cooling=22.0,
    )


@pytest.fixture
def office_record() -> BuildingRecord:
    """post-2010 steel 업무시설, 10000 m², 30층."""
    return BuildingRecord(
        pnu="1101010100100010002",
        usage_type="office",
        vintage_class="post-2010",
        structure_type="steel",
        built_year=2018,
        total_area=10000.0,
        floors=30,
        wall_uvalue=0.25,
        roof_uvalue=0.16,
        window_uvalue=1.40,
        wwr=0.55,
        ref_heating=20.0,
        ref_cooling=36.0,
    )


@pytest.fixture
def pipeline() -> FeaturePipeline:
    return FeaturePipeline()


# ---------------------------------------------------------------------------
# 1. FeaturePipeline 테스트
# ---------------------------------------------------------------------------

class TestFeaturePipeline:
    def test_output_columns_match_feature_names(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert list(df.columns) == FEATURE_NAMES, (
            "피처 컬럼 순서가 FEATURE_NAMES와 다름"
        )

    def test_single_row_shape(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert df.shape == (1, len(FEATURE_NAMES))

    def test_batch_shape(self, pipeline, sample_record, office_record):
        df = pipeline.transform([sample_record, office_record])
        assert df.shape == (2, len(FEATURE_NAMES))

    def test_no_nan_in_output(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert not df.isnull().any().any(), "피처에 NaN이 있어서는 안 된다"

    def test_total_area_log_positive(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert df["total_area_log"].iloc[0] > 0

    def test_area_zero_handled(self, pipeline, sample_record):
        """면적 0 입력 시 max(0.0, 1.0) = 1.0 으로 클램핑 후 log1p(1.0) ≈ 0.693이어야 한다."""
        rec = BuildingRecord(**{**sample_record.__dict__, "total_area": 0.0})
        df = pipeline.transform([rec])
        # area_safe = max(0.0, 1.0) = 1.0 → log1p(1.0) = ln(2) ≈ 0.6931
        assert df["total_area_log"].iloc[0] == pytest.approx(np.log1p(1.0), rel=1e-3)

    def test_built_year_none_defaults_to_age_40(self, pipeline, sample_record):
        rec = BuildingRecord(**{**sample_record.__dict__, "built_year": None})
        df = pipeline.transform([rec])
        assert df["built_year_age"].iloc[0] == pytest.approx(40.0)

    def test_age_clamped_at_120(self, pipeline, sample_record):
        rec = BuildingRecord(**{**sample_record.__dict__, "built_year": 1800})
        df = pipeline.transform([rec])
        assert df["built_year_age"].iloc[0] <= 120.0

    def test_tmy_fallback_when_weather_none(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert df["HDD18_annual"].iloc[0] == pytest.approx(
            SEOUL_TMY_ANNUAL["HDD18"], rel=1e-3
        )
        assert df["CDD24_annual"].iloc[0] == pytest.approx(
            SEOUL_TMY_ANNUAL["CDD24"], rel=1e-3
        )

    def test_custom_weather_overrides_tmy(self, pipeline, sample_record):
        rec = BuildingRecord(
            **{**sample_record.__dict__,
               "HDD18_annual": 999.0,
               "CDD24_annual": 333.0}
        )
        df = pipeline.transform_one(rec)
        assert df["HDD18_annual"].iloc[0] == pytest.approx(999.0)
        assert df["CDD24_annual"].iloc[0] == pytest.approx(333.0)

    def test_interaction_features_nonzero(self, pipeline, sample_record):
        df = pipeline.transform_one(sample_record)
        assert df["wall_uvalue_x_HDD18"].iloc[0] > 0
        assert df["window_uvalue_x_CDD24"].iloc[0] > 0

    def test_usage_encoding_range(self, pipeline):
        for usage, code in USAGE_ENCODING.items():
            rec = BuildingRecord(
                pnu="TEST", usage_type=usage, vintage_class="2001-2010",
                structure_type="RC", built_year=2005, total_area=500.0, floors=5,
                wall_uvalue=0.47, roof_uvalue=0.29, window_uvalue=2.40,
                wwr=0.35, ref_heating=55.0, ref_cooling=22.0,
            )
            df = pipeline.transform_one(rec)
            assert df["usage_type_enc"].iloc[0] == float(code)

    def test_vintage_encoding_all_classes(self, pipeline, sample_record):
        for vc, code in VINTAGE_ENCODING.items():
            rec = BuildingRecord(**{**sample_record.__dict__, "vintage_class": vc})
            df = pipeline.transform_one(rec)
            assert df["vintage_class_enc"].iloc[0] == float(code)


# ---------------------------------------------------------------------------
# 2. build_training_dataset 테스트
# ---------------------------------------------------------------------------

def _make_tier1_records(n: int = 5) -> list[dict]:
    return [
        {
            "pnu": f"TIER1_{i:04d}",
            "eui": 80.0 + i * 5,
            "usage_type": "office",
            "built_year": 2005,
            "total_area": 3000.0 + i * 100,
            "structure_type": "RC",
            "floors": 10,
        }
        for i in range(n)
    ]


def _make_tier2_records(n: int = 10) -> list[dict]:
    return [
        {
            "pnu": f"TIER2_{i:04d}",
            "primary_energy": 150.0 + i * 3,
            "usage_type": "office",
            "built_year": 2000,
            "total_area": 2000.0,
            "structure_type": "steel",
            "floors": 8,
        }
        for i in range(n)
    ]


class TestBuildTrainingDataset:
    def test_total_rows(self):
        t1 = _make_tier1_records(5)
        t2 = _make_tier2_records(10)
        ds = build_training_dataset(t1, t2)
        assert len(ds.y) == 15

    def test_tier1_weight_higher_than_tier2(self):
        t1 = _make_tier1_records(3)
        t2 = _make_tier2_records(3)
        ds = build_training_dataset(t1, t2, tier1_weight=5.0, tier2_weight=1.0)
        tier1_w = ds.weights[ds.source_tier == 1]
        tier2_w = ds.weights[ds.source_tier == 2]
        assert (tier1_w > tier2_w).all()

    def test_no_duplicate_pnu(self):
        """Tier1과 Tier2에 동일 PNU 있으면 Tier1만 남아야 한다."""
        t1 = [{"pnu": "SAME_PNU", "eui": 100.0, "usage_type": "office",
               "built_year": 2005, "total_area": 3000.0, "structure_type": "RC", "floors": 10}]
        t2 = [{"pnu": "SAME_PNU", "primary_energy": 200.0, "usage_type": "office",
               "built_year": 2005, "total_area": 3000.0, "structure_type": "RC", "floors": 10}]
        ds = build_training_dataset(t1, t2)
        assert len(ds.y) == 1
        assert ds.source_tier[0] == 1

    def test_tier2_eui_conversion_applies_usage_factor(self):
        """Tier2 1차에너지 → EUI 환산이 용도별 계수를 사용하는지 확인."""
        t2 = [{"pnu": "CONV_TEST", "primary_energy": 200.0, "usage_type": "apartment",
               "built_year": 2005, "total_area": 3000.0, "structure_type": "RC", "floors": 10}]
        ds = build_training_dataset([], t2)
        expected = 200.0 * KEA_PRIMARY_TO_FINAL["apartment"]
        assert ds.y[0] == pytest.approx(expected, rel=1e-3)

    def test_feature_dataframe_columns_correct(self):
        t1 = _make_tier1_records(3)
        ds = build_training_dataset(t1, [])
        assert list(ds.X.columns) == FEATURE_NAMES

    def test_source_tier_values(self):
        t1 = _make_tier1_records(3)
        t2 = _make_tier2_records(5)
        ds = build_training_dataset(t1, t2)
        assert set(ds.source_tier.tolist()).issubset({1, 2})

    def test_stratified_split_tier1_all_in_train(self):
        pytest.importorskip("sklearn", reason="stratified_split은 scikit-learn 필요")
        t1 = _make_tier1_records(10)
        t2 = _make_tier2_records(50)
        ds = build_training_dataset(t1, t2)
        vintage_classes = ds.X["vintage_class_enc"].values.astype(int)
        train_ds, val_ds = ds.stratified_split(vintage_classes, val_ratio=0.2)
        # Tier1은 모두 훈련셋에 있어야 한다
        assert (train_ds.source_tier == 1).sum() == 10
        assert (val_ds.source_tier == 1).sum() == 0

    def test_synthetic_data_added_when_requested(self):
        t1 = _make_tier1_records(3)
        t2 = _make_tier2_records(5)
        ds = build_training_dataset(
            t1, t2,
            include_archetype_synthetic=True,
        )
        assert 4 in ds.source_tier  # Tier4 합성 데이터 포함

    def test_empty_tier1_valid(self):
        """Tier1 없이 Tier2만으로도 데이터셋이 생성되어야 한다."""
        t2 = _make_tier2_records(5)
        ds = build_training_dataset([], t2)
        assert len(ds.y) == 5


# ---------------------------------------------------------------------------
# 3. XGBoostEUIPredictor 테스트 (xgboost 없으면 skip)
# ---------------------------------------------------------------------------

try:
    import xgboost as _xgb_check  # noqa: F401
    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False

_skip_if_no_xgboost = pytest.mark.skipif(
    not _HAS_XGBOOST,
    reason="xgboost 패키지 미설치 — Tier B 모델 테스트 건너뜀",
)


@pytest.fixture
def small_dataset():
    """30건 소규모 데이터셋 (빠른 테스트용)."""
    t1 = _make_tier1_records(10)
    t2 = _make_tier2_records(20)
    return build_training_dataset(t1, t2)


@pytest.fixture
def fitted_predictor(small_dataset):
    pytest.importorskip("xgboost")
    config = XGBoostConfig(
        n_estimators=30,
        fit_quantile_models=True,
        early_stopping_rounds=5,
    )
    predictor = XGBoostEUIPredictor(config=config)
    predictor.fit(small_dataset)
    return predictor


@_skip_if_no_xgboost
class TestXGBoostEUIPredictor:
    def test_fit_returns_self(self, small_dataset):
        cfg = XGBoostConfig(n_estimators=10, fit_quantile_models=False)
        p = XGBoostEUIPredictor(cfg)
        result = p.fit(small_dataset)
        assert result is p

    def test_predict_shape(self, fitted_predictor, pipeline, sample_record):
        features = pipeline.transform_one(sample_record)
        preds = fitted_predictor.predict(features)
        assert preds.shape == (1,)

    def test_predict_positive_eui(self, fitted_predictor, pipeline, sample_record):
        features = pipeline.transform_one(sample_record)
        preds = fitted_predictor.predict(features)
        assert preds[0] > 0, "EUI 예측값은 양수여야 한다"

    def test_predict_minimum_clamp(self, fitted_predictor, pipeline, sample_record):
        """모델이 음수나 극소값을 예측해도 최소 10 kWh/m²·년이 보장되어야 한다."""
        features = pipeline.transform_one(sample_record)
        preds = fitted_predictor.predict(features)
        assert preds[0] >= 10.0

    def test_predict_with_interval_shape(self, fitted_predictor, pipeline, sample_record):
        features = pipeline.transform_one(sample_record)
        mean, lower, upper = fitted_predictor.predict_with_interval(features)
        assert mean.shape == (1,)
        assert lower.shape == (1,)
        assert upper.shape == (1,)

    def test_interval_ordering(self, fitted_predictor, pipeline, sample_record):
        """lower <= mean <= upper 이어야 한다."""
        features = pipeline.transform_one(sample_record)
        mean, lower, upper = fitted_predictor.predict_with_interval(features)
        assert lower[0] <= mean[0] <= upper[0]

    def test_batch_predict(self, fitted_predictor, pipeline, sample_record, office_record):
        features = pipeline.transform([sample_record, office_record])
        preds = fitted_predictor.predict(features)
        assert preds.shape == (2,)
        assert (preds > 0).all()

    def test_feature_importance_returns_all_features(self, fitted_predictor):
        fi = fitted_predictor.feature_importance()
        assert len(fi) == len(FEATURE_NAMES)
        assert set(fi["feature"].tolist()) == set(FEATURE_NAMES)

    def test_feature_importance_sums_to_one(self, fitted_predictor):
        """feature_importance()는 gain 정규화 후 합이 1.0 이어야 한다."""
        fi = fitted_predictor.feature_importance()
        assert fi["importance"].sum() == pytest.approx(1.0, abs=1e-6)

    def test_predict_from_record(self, fitted_predictor, sample_record):
        result = fitted_predictor.predict_from_record(sample_record)
        assert "eui_predicted" in result
        assert "eui_lower" in result
        assert "eui_upper" in result
        assert result["eui_predicted"] > 0

    def test_save_and_load(self, fitted_predictor, pipeline, sample_record, tmp_path):
        model_path = tmp_path / "test_model.pkl"
        fitted_predictor.save(model_path)
        assert model_path.exists()

        loaded = XGBoostEUIPredictor.load(model_path)
        features = pipeline.transform_one(sample_record)
        orig_pred = fitted_predictor.predict(features)[0]
        loaded_pred = loaded.predict(features)[0]
        assert orig_pred == pytest.approx(loaded_pred, rel=1e-5)

    def test_predict_before_fit_raises(self, pipeline, sample_record):
        predictor = XGBoostEUIPredictor()
        features = pipeline.transform_one(sample_record)
        with pytest.raises(RuntimeError, match="fit"):
            predictor.predict(features)

    def test_cross_validate_returns_metrics(self, small_dataset):
        cfg = XGBoostConfig(n_estimators=10, fit_quantile_models=False)
        p = XGBoostEUIPredictor(cfg)
        metrics = p.cross_validate(small_dataset, n_splits=3)
        required = {"rmse_mean", "rmse_std", "mape_mean", "mape_std", "r2_mean", "r2_std"}
        assert required.issubset(set(metrics.keys()))
        assert metrics["rmse_mean"] >= 0
        assert 0 <= metrics["mape_mean"] <= 100


# ---------------------------------------------------------------------------
# 4. predict_energy 추론 파이프라인 테스트
# ---------------------------------------------------------------------------

@_skip_if_no_xgboost
class TestPredictEnergy:
    @pytest.fixture
    def mock_model(self, fitted_predictor):
        return fitted_predictor

    def test_tier1_measured_returns_without_model(self):
        """Tier1 실측 데이터가 있으면 모델 없이 직접 반환해야 한다."""
        db_row = {
            "data_tier": 1,
            "eui": 95.0,
            "usage_type": "office",
            "vintage_class": "2001-2010",
            "structure_class": "RC",
            "built_year": 2005,
            "total_area": 3000.0,
            "floors_above": 10,
        }
        # mock_model 없이도 동작해야 한다
        cfg = XGBoostConfig(n_estimators=5, fit_quantile_models=False)
        dummy = XGBoostEUIPredictor(cfg)
        ds = build_training_dataset(_make_tier1_records(5), _make_tier2_records(5))
        dummy.fit(ds)

        result = predict_energy("TEST_PNU", date(2026, 3, 27), db_row, dummy)
        assert isinstance(result, EnergyPrediction)
        assert result.data_tier_used == 1
        assert result.eui_predicted == pytest.approx(95.0)
        assert result.pnu == "TEST_PNU"

    def test_tier2_uses_model_with_cert_correction(self, mock_model):
        """Tier2 KEA 인증 데이터가 있으면 모델 예측과 인증값의 가중 평균이 적용되어야 한다."""
        db_row = {
            "data_tier": 2,
            "epi_score": 140.0,  # 1차에너지 kWh/m²·년
            "usage_type": "office",
            "vintage_class": "2001-2010",
            "structure_class": "RC",
            "built_year": 2005,
            "total_area": 3000.0,
            "floors_above": 10,
        }
        result = predict_energy("CERT_PNU", date(2026, 3, 27), db_row, mock_model)
        assert result.data_tier_used == 2
        assert result.eui_predicted > 0

    def test_tier3_uses_global_model(self, mock_model):
        """Tier3 이상은 XGBoost 글로벌 모델로 예측해야 한다."""
        db_row = {
            "data_tier": 3,
            "usage_type": "apartment",
            "vintage_class": "1980-2000",
            "structure_class": "RC",
            "built_year": 1990,
            "total_area": 2000.0,
            "floors_above": 8,
        }
        result = predict_energy("ML_PNU", date(2026, 3, 27), db_row, mock_model)
        assert result.data_tier_used == 3
        assert result.eui_predicted >= 10.0

    def test_snapshot_features_empty_by_default(self, mock_model):
        db_row = {
            "data_tier": 4,
            "usage_type": "apartment",
            "vintage_class": "post-2010",
            "structure_class": "RC",
            "built_year": 2015,
            "total_area": 1000.0,
            "floors_above": 5,
        }
        result = predict_energy("SNAP_PNU", date(2026, 3, 27), db_row, mock_model,
                                snapshot_features=False)
        assert result.features_snapshot == {}

    def test_snapshot_features_populated_when_requested(self, mock_model):
        db_row = {
            "data_tier": 4,
            "usage_type": "apartment",
            "vintage_class": "post-2010",
            "structure_class": "RC",
            "built_year": 2015,
            "total_area": 1000.0,
            "floors_above": 5,
        }
        result = predict_energy("SNAP_PNU", date(2026, 3, 27), db_row, mock_model,
                                snapshot_features=True)
        assert set(result.features_snapshot.keys()) == set(FEATURE_NAMES)

    def test_confidence_interval_valid(self, mock_model):
        db_row = {
            "data_tier": 4,
            "usage_type": "retail",
            "vintage_class": "1980-2000",
            "structure_class": "steel",
            "built_year": 1995,
            "total_area": 5000.0,
            "floors_above": 6,
        }
        result = predict_energy("CI_PNU", date(2026, 3, 27), db_row, mock_model)
        assert result.eui_lower <= result.eui_predicted <= result.eui_upper


# ---------------------------------------------------------------------------
# 5. 경계값 테스트
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_pre_1980_masonry_warehouse(self, pipeline):
        """희귀 조합(pre-1980 masonry 창고)도 피처 생성이 실패하지 않아야 한다."""
        rec = BuildingRecord(
            pnu="EDGE_001", usage_type="warehouse",
            vintage_class="pre-1980", structure_type="masonry",
            built_year=1965, total_area=8000.0, floors=2,
            wall_uvalue=2.50, roof_uvalue=2.80, window_uvalue=5.80,
            wwr=0.10, ref_heating=60.0, ref_cooling=10.0,
        )
        df = pipeline.transform_one(rec)
        assert not df.isnull().any().any()

    def test_very_large_building(self, pipeline):
        """초대형 건물(100만 m²)도 처리 가능해야 한다."""
        rec = BuildingRecord(
            pnu="HUGE_001", usage_type="mixed_use",
            vintage_class="post-2010", structure_type="steel",
            built_year=2020, total_area=1_000_000.0, floors=80,
            wall_uvalue=0.25, roof_uvalue=0.16, window_uvalue=1.40,
            wwr=0.50, ref_heating=22.0, ref_cooling=36.0,
        )
        df = pipeline.transform_one(rec)
        assert np.isfinite(df["total_area_log"].iloc[0])

    def test_unknown_usage_type_defaults_to_zero(self, pipeline, sample_record):
        """미등록 용도는 USAGE_ENCODING에서 기본값 0(apartment)이 적용된다."""
        rec = BuildingRecord(
            **{**sample_record.__dict__, "usage_type": "unknown_type"}
        )
        df = pipeline.transform_one(rec)
        # get() miss → 기본값 0
        assert df["usage_type_enc"].iloc[0] == pytest.approx(0.0)

    def test_tier2_kea_conversion_all_usage_types(self):
        """모든 용도 타입에 대해 KEA 환산이 적용되어야 한다."""
        usage_types = list(KEA_PRIMARY_TO_FINAL.keys())
        for usage in usage_types:
            t2 = [{"pnu": f"KEA_{usage}", "primary_energy": 200.0,
                   "usage_type": usage, "built_year": 2005,
                   "total_area": 3000.0, "structure_type": "RC", "floors": 10}]
            ds = build_training_dataset([], t2)
            expected = 200.0 * KEA_PRIMARY_TO_FINAL[usage]
            assert ds.y[0] == pytest.approx(expected, rel=1e-3), (
                f"{usage} 용도 KEA 환산 오류"
            )

    def test_seoul_tmy_monthly_all_months_present(self):
        """서울 TMY 데이터가 1~12월 전체를 포함해야 한다."""
        from src.simulation.ml_xgboost import SEOUL_TMY_MONTHLY
        assert set(SEOUL_TMY_MONTHLY.keys()) == set(range(1, 13))

    def test_feature_names_no_duplicates(self):
        assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES)), (
            "FEATURE_NAMES에 중복이 있어서는 안 된다"
        )

    def test_pipeline_deterministic(self, pipeline, sample_record):
        """동일 입력에 대해 항상 동일한 피처가 생성되어야 한다."""
        df1 = pipeline.transform_one(sample_record)
        df2 = pipeline.transform_one(sample_record)
        pd.testing.assert_frame_equal(df1, df2)
