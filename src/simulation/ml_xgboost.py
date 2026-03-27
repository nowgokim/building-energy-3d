"""
XGBoost 기반 건물 연간/일별 EUI 예측 파이프라인 (Tier B).

아키텍처 개요
-----------
Tier A (EnergyPlus)  →  83종 archetype 물리 기반 기준값 (오프라인 생성)
Tier B (이 모듈)     →  XGBoost 글로벌 모델: 서울 전체 766K건 대상
                        훈련 데이터: Tier1 실측 89건 + Tier2 KEA 인증 3,192건

설계 원칙
---------
- EnergyPredictor ABC 구현체. predict() 하나로 호출측 인터페이스 고정.
- 피처 파이프라인은 별도 FeaturePipeline 클래스로 분리 (테스트 가능).
- 학습 데이터 부족 문제는 Tier2 KEA 데이터 도메인 적응 + 교차검증으로 처리.
- model_registry / model_versions 테이블과 연동하여 아티팩트 추적.
"""

from __future__ import annotations

import logging
import pickle
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

if sys.platform == "win32":
    # Windows에서 한글 로그 출력 보장
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 서울 기상 기준값 (TMY 기반, 기상청 Seoul 관측소 30년 평균)
# 월별 HDD18(난방도일, 기준 18°C), CDD24(냉방도일, 기준 24°C)
SEOUL_TMY_MONTHLY: dict[int, dict[str, float]] = {
    1:  {"HDD18": 529.0, "CDD24":  0.0, "solar_rad": 3.1, "avg_temp": -2.4},
    2:  {"HDD18": 390.0, "CDD24":  0.0, "solar_rad": 4.0, "avg_temp":  0.4},
    3:  {"HDD18": 195.0, "CDD24":  0.0, "solar_rad": 4.9, "avg_temp":  5.6},
    4:  {"HDD18":  30.0, "CDD24":  0.0, "solar_rad": 5.5, "avg_temp": 12.5},
    5:  {"HDD18":   0.0, "CDD24":  0.0, "solar_rad": 5.8, "avg_temp": 18.0},
    6:  {"HDD18":   0.0, "CDD24": 12.0, "solar_rad": 5.2, "avg_temp": 22.6},
    7:  {"HDD18":   0.0, "CDD24": 93.0, "solar_rad": 4.5, "avg_temp": 25.7},
    8:  {"HDD18":   0.0, "CDD24": 91.0, "solar_rad": 4.8, "avg_temp": 26.4},
    9:  {"HDD18":   0.0, "CDD24":  3.0, "solar_rad": 4.9, "avg_temp": 21.3},
    10: {"HDD18":  45.0, "CDD24":  0.0, "solar_rad": 4.1, "avg_temp": 14.5},
    11: {"HDD18": 225.0, "CDD24":  0.0, "solar_rad": 3.2, "avg_temp":  7.0},
    12: {"HDD18": 450.0, "CDD24":  0.0, "solar_rad": 2.8, "avg_temp":  0.3},
}

# 연간 합계 (피처 생성에 사용)
SEOUL_TMY_ANNUAL: dict[str, float] = {
    "HDD18": sum(v["HDD18"] for v in SEOUL_TMY_MONTHLY.values()),          # 1864
    "CDD24": sum(v["CDD24"] for v in SEOUL_TMY_MONTHLY.values()),          #  199
    "solar_rad_avg": sum(v["solar_rad"] for v in SEOUL_TMY_MONTHLY.values()) / 12,
    "avg_temp_annual": sum(v["avg_temp"] for v in SEOUL_TMY_MONTHLY.values()) / 12,
}

# 용도 인코딩 (Tier2 KEA 1차에너지→EUI 환산 시에도 사용)
USAGE_ENCODING: dict[str, int] = {
    "apartment": 0,
    "residential_single": 1,
    "office": 2,
    "retail": 3,
    "education": 4,
    "hospital": 5,
    "warehouse": 6,
    "cultural": 7,
    "mixed_use": 8,
}

VINTAGE_ENCODING: dict[str, int] = {
    "pre-1980": 0,
    "1980-2000": 1,
    "2001-2010": 2,
    "post-2010": 3,
}

STRUCTURE_ENCODING: dict[str, int] = {
    "RC": 0,
    "steel": 1,
    "masonry": 2,
}

# Tier2 KEA 데이터: 1차에너지소비량(kWh/m²·년) → EUI(kWh/m²·년) 환산 계수
# 용도별 1차에너지→최종에너지 환산 (KEPCO 기준 전력 2.75배, 가스 1.1배)
# 단순화: 용도별 전기/가스 비율 고려한 가중 평균 역산 계수
KEA_PRIMARY_TO_FINAL: dict[str, float] = {
    "apartment": 0.62,       # 가스 난방 비중 높음 → 1차에너지 환산 낮음
    "residential_single": 0.60,
    "office": 0.45,          # 전기 비중 높음 → 1차에너지 환산 높음
    "retail": 0.42,
    "education": 0.55,
    "hospital": 0.48,
    "warehouse": 0.52,
    "cultural": 0.53,
    "mixed_use": 0.50,
}

# 피처 목록 (학습/추론 순서 고정)
FEATURE_NAMES: list[str] = [
    # 물리 피처 (archetype 파라미터)
    "wall_uvalue",
    "roof_uvalue",
    "window_uvalue",
    "wwr",
    "ref_heating",
    "ref_cooling",
    # 건물 메타
    "vintage_class_enc",
    "usage_type_enc",
    "structure_type_enc",
    "total_area_log",        # log1p 변환
    "floors",
    "built_year_age",        # 2026 - built_year
    # 날씨 (서울 TMY 기준, 향후 실황 대체 가능)
    "HDD18_annual",
    "CDD24_annual",
    "solar_rad_avg",
    "avg_temp_annual",
    # 상호작용 피처
    "wall_uvalue_x_HDD18",   # 외피×냉난방도일 = 열손실 프록시
    "window_uvalue_x_CDD24", # 창호×냉방도일
    "area_x_usage",          # 면적×용도 상호작용
]


# ---------------------------------------------------------------------------
# 추상 기반 클래스 (Pluggable Architecture)
# ---------------------------------------------------------------------------

class EnergyPredictor(ABC):
    """에너지 예측 모델의 공통 인터페이스.

    모든 구체 예측기는 이 클래스를 상속하고 predict()를 구현한다.
    호출측(visualization/buildings.py 등)은 이 타입에만 의존한다.
    """

    @abstractmethod
    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """
        Parameters
        ----------
        features:
            FEATURE_NAMES 순서의 피처 DataFrame. 행 수 = 예측 건물 수.

        Returns
        -------
        np.ndarray
            예측 EUI (kWh/m²·년), shape (n_buildings,).
        """

    @abstractmethod
    def predict_with_interval(
        self, features: pd.DataFrame, quantiles: tuple[float, float] = (0.1, 0.9)
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Returns
        -------
        (mean, lower, upper) 각각 shape (n_buildings,).
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """model_registry.model_name 과 일치하는 식별자."""

    @property
    @abstractmethod
    def feature_names(self) -> list[str]:
        """이 모델이 기대하는 피처 이름 목록."""


# ---------------------------------------------------------------------------
# 피처 파이프라인
# ---------------------------------------------------------------------------

@dataclass
class BuildingRecord:
    """단일 건물의 원시 속성. FeaturePipeline 입력 DTO."""

    pnu: str
    usage_type: str             # 영문 archetype key
    vintage_class: str          # "pre-1980" | "1980-2000" | "2001-2010" | "post-2010"
    structure_type: str         # "RC" | "steel" | "masonry"
    built_year: int | None
    total_area: float           # m²
    floors: int                 # 지상층수
    # archetype 파라미터 (match_archetype() 결과)
    wall_uvalue: float
    roof_uvalue: float
    window_uvalue: float
    wwr: float
    ref_heating: float          # kWh/m²·년
    ref_cooling: float          # kWh/m²·년
    # 날씨 (None이면 서울 TMY 사용)
    HDD18_annual: float | None = None
    CDD24_annual: float | None = None
    solar_rad_avg: float | None = None
    avg_temp_annual: float | None = None


class FeaturePipeline:
    """BuildingRecord → FEATURE_NAMES 순서의 피처 벡터 변환.

    설계 의도
    ---------
    - 학습 시와 추론 시 동일한 변환이 적용됨을 보장한다.
    - 날씨 값이 None이면 서울 TMY 연간 값으로 폴백한다.
    - 결측값은 합리적 대체값으로 채운다 (imputation).
    """

    REF_YEAR: int = 2026

    def transform(self, records: list[BuildingRecord]) -> pd.DataFrame:
        """BuildingRecord 목록을 학습/추론용 DataFrame으로 변환."""
        rows: list[dict[str, float]] = []
        for r in records:
            rows.append(self._record_to_row(r))
        df = pd.DataFrame(rows, columns=FEATURE_NAMES)
        return df

    def transform_one(self, record: BuildingRecord) -> pd.DataFrame:
        """단건 변환. predict() 단건 호출 편의 메서드."""
        return self.transform([record])

    def _record_to_row(self, r: BuildingRecord) -> dict[str, float]:
        age = (self.REF_YEAR - r.built_year) if r.built_year else 40
        age = max(0, min(age, 120))  # 0~120년 범위 고정

        HDD18 = r.HDD18_annual if r.HDD18_annual is not None else SEOUL_TMY_ANNUAL["HDD18"]
        CDD24 = r.CDD24_annual if r.CDD24_annual is not None else SEOUL_TMY_ANNUAL["CDD24"]
        solar = r.solar_rad_avg if r.solar_rad_avg is not None else SEOUL_TMY_ANNUAL["solar_rad_avg"]
        tavg  = r.avg_temp_annual if r.avg_temp_annual is not None else SEOUL_TMY_ANNUAL["avg_temp_annual"]

        usage_enc     = USAGE_ENCODING.get(r.usage_type, 0)
        vintage_enc   = VINTAGE_ENCODING.get(r.vintage_class, 1)
        structure_enc = STRUCTURE_ENCODING.get(r.structure_type, 0)

        area_safe = max(r.total_area, 1.0)
        floors_safe = max(r.floors, 1)

        return {
            "wall_uvalue":          r.wall_uvalue,
            "roof_uvalue":          r.roof_uvalue,
            "window_uvalue":        r.window_uvalue,
            "wwr":                  r.wwr,
            "ref_heating":          r.ref_heating,
            "ref_cooling":          r.ref_cooling,
            "vintage_class_enc":    float(vintage_enc),
            "usage_type_enc":       float(usage_enc),
            "structure_type_enc":   float(structure_enc),
            "total_area_log":       float(np.log1p(area_safe)),
            "floors":               float(floors_safe),
            "built_year_age":       float(age),
            "HDD18_annual":         HDD18,
            "CDD24_annual":         CDD24,
            "solar_rad_avg":        solar,
            "avg_temp_annual":      tavg,
            # 상호작용
            "wall_uvalue_x_HDD18":  r.wall_uvalue * HDD18,
            "window_uvalue_x_CDD24": r.window_uvalue * CDD24,
            "area_x_usage":         float(np.log1p(area_safe)) * float(usage_enc + 1),
        }


# ---------------------------------------------------------------------------
# 학습 데이터 구성 전략
# ---------------------------------------------------------------------------

@dataclass
class TrainingDataset:
    """Tier1 + Tier2 통합 학습 데이터셋.

    데이터 부족 전략
    ---------------
    1. Tier1 (89건): 실측 EUI → 직접 학습 타깃.
    2. Tier2 (3,192건): KEA 1차에너지소비량 → EUI 환산 후 학습.
       단, Tier1과 동일 건물이 있을 경우 Tier1 우선.
    3. 가중치 차등 부여: Tier1 샘플에 higher weight (기본 5x).
    4. 교차검증: StratifiedKFold (vintage_class 기준 층화).
       소수 클래스(pre-1980, post-2010) 검증셋에 포함 보장.
    5. 합성 데이터 (선택): Tier4 archetype 645K건에서 물리 기반 노이즈 추가
       → 피처 공간 커버리지 확장 (weight=0.1로 낮춤).

    속성
    ----
    X: 피처 DataFrame (FEATURE_NAMES 컬럼)
    y: EUI 타깃 (kWh/m²·년)
    weights: 샘플 가중치
    source_tier: 각 샘플의 데이터 Tier (1/2/4)
    pnu_index: 추적용 PNU 목록
    """

    X: pd.DataFrame
    y: np.ndarray
    weights: np.ndarray
    source_tier: np.ndarray
    pnu_index: list[str]

    def stratified_split(
        self,
        vintage_classes: np.ndarray,
        val_ratio: float = 0.2,
        random_state: int = 42,
    ) -> tuple["TrainingDataset", "TrainingDataset"]:
        """vintage_class 기준 층화 분할.

        Tier1 샘플은 항상 훈련셋에 포함 (89건은 검증셋 낭비 불가).
        """
        from sklearn.model_selection import StratifiedShuffleSplit

        # Tier1은 무조건 train
        tier1_mask = self.source_tier == 1
        tier2plus_idx = np.where(~tier1_mask)[0]

        splitter = StratifiedShuffleSplit(
            n_splits=1, test_size=val_ratio, random_state=random_state
        )
        vintage_subset = vintage_classes[tier2plus_idx]
        train_sub_idx, val_sub_idx = next(splitter.split(tier2plus_idx, vintage_subset))

        train_idx = np.concatenate([np.where(tier1_mask)[0], tier2plus_idx[train_sub_idx]])
        val_idx = tier2plus_idx[val_sub_idx]

        def _subset(ds: "TrainingDataset", idx: np.ndarray) -> "TrainingDataset":
            return TrainingDataset(
                X=ds.X.iloc[idx].reset_index(drop=True),
                y=ds.y[idx],
                weights=ds.weights[idx],
                source_tier=ds.source_tier[idx],
                pnu_index=[ds.pnu_index[i] for i in idx],
            )

        return _subset(self, train_idx), _subset(self, val_idx)


def build_training_dataset(
    tier1_records: list[dict[str, Any]],   # 실측 건물: {pnu, eui, **building_attrs}
    tier2_records: list[dict[str, Any]],   # KEA 인증: {pnu, primary_energy, usage_type, ...}
    pipeline: FeaturePipeline | None = None,
    tier1_weight: float = 5.0,
    tier2_weight: float = 1.0,
    include_archetype_synthetic: bool = False,
    synthetic_noise_pct: float = 0.10,
    random_state: int = 42,
) -> TrainingDataset:
    """Tier1 + Tier2 데이터를 통합하여 TrainingDataset을 생성한다.

    Parameters
    ----------
    tier1_records:
        실측 EUI 데이터. 각 dict는 최소 ``pnu``, ``eui``,
        그리고 BuildingRecord 생성에 필요한 건물 속성을 포함해야 한다.
    tier2_records:
        KEA 1차에너지소비량 데이터. 각 dict는 최소 ``pnu``,
        ``primary_energy`` (kWh/m²·년), ``usage_type`` 을 포함해야 한다.
    pipeline:
        피처 파이프라인 인스턴스. None이면 기본 FeaturePipeline() 사용.
    tier1_weight:
        Tier1 샘플에 부여할 상대 가중치. 기본값 5.0.
    tier2_weight:
        Tier2 샘플 가중치. 기본값 1.0.
    include_archetype_synthetic:
        True이면 archetype 피처 공간에서 합성 데이터 추가 (가중치 0.1).
        피처 공간 커버리지 확장이 목적이며, 기본값 False.
    synthetic_noise_pct:
        합성 타깃 값에 추가할 가우시안 노이즈 비율. 기본 10%.
    random_state:
        합성 데이터 노이즈 시드.

    Returns
    -------
    TrainingDataset
        통합 데이터셋. Tier1 89건 + Tier2 3,192건 = 약 3,281건.
    """
    if pipeline is None:
        pipeline = FeaturePipeline()

    all_records: list[BuildingRecord] = []
    all_eui: list[float] = []
    all_weights: list[float] = []
    all_tiers: list[int] = []
    all_pnus: list[str] = []

    # Tier1: 실측 EUI 직접 사용
    for rec in tier1_records:
        br = _dict_to_building_record(rec)
        all_records.append(br)
        all_eui.append(float(rec["eui"]))
        all_weights.append(tier1_weight)
        all_tiers.append(1)
        all_pnus.append(rec["pnu"])

    # Tier2: KEA 1차에너지 → EUI 환산
    tier1_pnu_set = {r["pnu"] for r in tier1_records}
    for rec in tier2_records:
        if rec["pnu"] in tier1_pnu_set:
            # 동일 건물은 Tier1 우선 → 중복 제거
            continue
        br = _dict_to_building_record(rec)
        primary_energy = float(rec["primary_energy"])
        usage_key = br.usage_type
        conversion = KEA_PRIMARY_TO_FINAL.get(usage_key, 0.50)
        eui = primary_energy * conversion
        all_records.append(br)
        all_eui.append(eui)
        all_weights.append(tier2_weight)
        all_tiers.append(2)
        all_pnus.append(rec["pnu"])

    X = pipeline.transform(all_records)
    y = np.array(all_eui, dtype=np.float64)
    weights = np.array(all_weights, dtype=np.float64)
    tiers = np.array(all_tiers, dtype=np.int8)

    if include_archetype_synthetic:
        X_syn, y_syn, w_syn, t_syn, pnu_syn = _generate_synthetic_from_archetypes(
            pipeline=pipeline,
            n_samples=min(len(all_records) * 3, 5000),
            noise_pct=synthetic_noise_pct,
            rng=np.random.default_rng(random_state),
        )
        X = pd.concat([X, X_syn], ignore_index=True)
        y = np.concatenate([y, y_syn])
        weights = np.concatenate([weights, w_syn])
        tiers = np.concatenate([tiers, t_syn])
        all_pnus.extend(pnu_syn)

    logger.info(
        "학습 데이터셋 구성 완료: Tier1=%d건, Tier2=%d건%s → 총 %d건",
        sum(1 for t in tiers if t == 1),
        sum(1 for t in tiers if t == 2),
        f", 합성={sum(1 for t in tiers if t == 4)}건"
        if include_archetype_synthetic else "",
        len(y),
    )
    return TrainingDataset(
        X=X,
        y=y,
        weights=weights,
        source_tier=tiers,
        pnu_index=all_pnus,
    )


def _dict_to_building_record(d: dict[str, Any]) -> BuildingRecord:
    """API 응답 dict → BuildingRecord 변환. 결측 필드는 archetype 폴백."""
    from src.simulation.archetypes import match_archetype

    usage = d.get("usage_type", "apartment")
    built_year = d.get("built_year") or d.get("built_yr")
    total_area = float(d.get("total_area") or d.get("tot_area") or 500.0)
    structure = d.get("structure_type") or d.get("strct_nm") or "RC"
    floors = int(d.get("floors") or d.get("grnd_flr_cnt") or 5)

    arch = match_archetype(usage, built_year or 1990, total_area, structure)

    return BuildingRecord(
        pnu=d.get("pnu", ""),
        usage_type=arch["usage_type"],
        vintage_class=arch["vintage_class"],
        structure_type=arch["structure_type"],
        built_year=built_year,
        total_area=total_area,
        floors=floors,
        wall_uvalue=arch["wall_uvalue"],
        roof_uvalue=arch["roof_uvalue"],
        window_uvalue=arch["window_uvalue"],
        wwr=arch["wwr"],
        ref_heating=arch["ref_heating"],
        ref_cooling=arch["ref_cooling"],
    )


def _generate_synthetic_from_archetypes(
    pipeline: FeaturePipeline,
    n_samples: int,
    noise_pct: float,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """archetype 파라미터 공간에서 합성 학습 샘플 생성.

    각 archetype의 ref_total에 가우시안 노이즈를 추가하여 피처 공간을
    확장한다. 가중치는 0.1로 낮게 설정하여 실측 데이터의 영향력을 유지한다.
    """
    from src.simulation.archetypes import ARCHETYPE_PARAMS, _classify_vintage

    keys = list(ARCHETYPE_PARAMS.keys())
    chosen_keys = [keys[i % len(keys)] for i in range(n_samples)]

    records: list[BuildingRecord] = []
    eui_targets: list[float] = []
    syn_pnus: list[str] = []

    for i, (usage, vintage, structure) in enumerate(chosen_keys):
        params = ARCHETYPE_PARAMS[(usage, vintage, structure)]
        built_yr = {"pre-1980": 1965, "1980-2000": 1990,
                    "2001-2010": 2005, "post-2010": 2015}[vintage]
        area = float(rng.uniform(200, 10000))
        floors = int(rng.integers(2, 20))

        noise = float(rng.normal(0, params["ref_total"] * noise_pct))
        target_eui = max(20.0, params["ref_total"] * KEA_PRIMARY_TO_FINAL.get(usage, 0.5) + noise)

        rec = BuildingRecord(
            pnu=f"SYN_{i:06d}",
            usage_type=usage,
            vintage_class=vintage,
            structure_type=structure,
            built_year=built_yr,
            total_area=area,
            floors=floors,
            wall_uvalue=params["wall_uvalue"],
            roof_uvalue=params["roof_uvalue"],
            window_uvalue=params["window_uvalue"],
            wwr=params["wwr"],
            ref_heating=params["ref_heating"],
            ref_cooling=params["ref_cooling"],
        )
        records.append(rec)
        eui_targets.append(target_eui)
        syn_pnus.append(f"SYN_{i:06d}")

    X_syn = pipeline.transform(records)
    y_syn = np.array(eui_targets, dtype=np.float64)
    w_syn = np.full(n_samples, 0.1, dtype=np.float64)
    t_syn = np.full(n_samples, 4, dtype=np.int8)

    return X_syn, y_syn, w_syn, t_syn, syn_pnus


# ---------------------------------------------------------------------------
# XGBoost 예측기 구현
# ---------------------------------------------------------------------------

@dataclass
class XGBoostConfig:
    """XGBoost 하이퍼파라미터 설정."""

    n_estimators: int = 500
    max_depth: int = 6
    learning_rate: float = 0.05
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 5
    gamma: float = 0.1
    reg_alpha: float = 0.1      # L1 정규화 (희소 피처 억제)
    reg_lambda: float = 1.0     # L2 정규화
    objective: str = "reg:squarederror"
    eval_metric: str = "rmse"
    n_jobs: int = -1
    random_state: int = 42
    # 예측 구간 추정용 quantile 모델 추가 학습 여부
    fit_quantile_models: bool = True
    quantile_alpha_low: float = 0.1
    quantile_alpha_high: float = 0.9
    early_stopping_rounds: int = 30


class XGBoostEUIPredictor(EnergyPredictor):
    """Tier B: 서울 전체 건물 EUI 연간 예측 XGBoost 모델.

    단일 글로벌 모델로 766K 건물 모두를 커버한다.
    usage_type, vintage_class, structure_type 을 숫자 인코딩하여
    분류 없이 단일 회귀 트리로 처리한다 (트리 구조가 자연스럽게
    용도별 분기를 학습함).

    예측 구간
    ---------
    fit_quantile_models=True 이면 10th / 90th 분위수 모델을 추가 학습한다.
    predict_with_interval()로 (mean, lower, upper) 세 값을 반환한다.
    분위수 회귀는 ``objective="reg:quantileerror"`` (XGBoost 2.0+) 를 사용한다.
    """

    def __init__(
        self,
        config: XGBoostConfig | None = None,
        pipeline: FeaturePipeline | None = None,
    ) -> None:
        self._config = config or XGBoostConfig()
        self._pipeline = pipeline or FeaturePipeline()
        self._model: Any = None           # xgb.XGBRegressor (mean)
        self._model_low: Any = None       # xgb.XGBRegressor (q10)
        self._model_high: Any = None      # xgb.XGBRegressor (q90)
        self._is_fitted: bool = False

    @property
    def model_name(self) -> str:
        return "xgb_eui_annual_global"

    @property
    def feature_names(self) -> list[str]:
        return FEATURE_NAMES

    # ------------------------------------------------------------------
    # 학습
    # ------------------------------------------------------------------

    def fit(
        self,
        dataset: TrainingDataset,
        eval_dataset: TrainingDataset | None = None,
        verbose_eval: int = 50,
    ) -> "XGBoostEUIPredictor":
        """모델을 학습한다.

        Parameters
        ----------
        dataset:
            훈련 데이터 (build_training_dataset() 반환값).
        eval_dataset:
            검증 데이터. None이면 훈련 데이터의 20%를 자동 분리.
        verbose_eval:
            XGBoost eval 로그 출력 간격. 0이면 비활성화.

        Returns
        -------
        self
        """
        try:
            import xgboost as xgb
        except ImportError as e:
            raise ImportError(
                "xgboost 패키지가 필요합니다: pip install xgboost>=2.0"
            ) from e

        X_train = dataset.X[FEATURE_NAMES].values
        y_train = dataset.y
        w_train = dataset.weights

        eval_set = None
        if eval_dataset is not None:
            X_val = eval_dataset.X[FEATURE_NAMES].values
            y_val = eval_dataset.y
            eval_set = [(X_val, y_val)]

        cfg = self._config

        # 평균 모델 (MSE)
        self._model = xgb.XGBRegressor(
            n_estimators=cfg.n_estimators,
            max_depth=cfg.max_depth,
            learning_rate=cfg.learning_rate,
            subsample=cfg.subsample,
            colsample_bytree=cfg.colsample_bytree,
            min_child_weight=cfg.min_child_weight,
            gamma=cfg.gamma,
            reg_alpha=cfg.reg_alpha,
            reg_lambda=cfg.reg_lambda,
            objective=cfg.objective,
            eval_metric=cfg.eval_metric,
            n_jobs=cfg.n_jobs,
            random_state=cfg.random_state,
            early_stopping_rounds=cfg.early_stopping_rounds if eval_set else None,
            verbosity=0,
        )
        fit_kwargs: dict[str, Any] = {
            "sample_weight": w_train,
            "verbose": verbose_eval,
        }
        if eval_set:
            fit_kwargs["eval_set"] = eval_set

        self._model.fit(X_train, y_train, **fit_kwargs)
        logger.info(
            "XGBoost 평균 모델 학습 완료: best_iteration=%s",
            getattr(self._model, "best_iteration", "N/A"),
        )

        # 분위수 모델 (예측 구간 추정)
        if cfg.fit_quantile_models:
            self._model_low = xgb.XGBRegressor(
                n_estimators=cfg.n_estimators,
                max_depth=cfg.max_depth,
                learning_rate=cfg.learning_rate,
                subsample=cfg.subsample,
                colsample_bytree=cfg.colsample_bytree,
                objective="reg:quantileerror",
                quantile_alpha=cfg.quantile_alpha_low,
                n_jobs=cfg.n_jobs,
                random_state=cfg.random_state,
                verbosity=0,
            )
            self._model_low.fit(X_train, y_train, sample_weight=w_train,
                                verbose=False)

            self._model_high = xgb.XGBRegressor(
                n_estimators=cfg.n_estimators,
                max_depth=cfg.max_depth,
                learning_rate=cfg.learning_rate,
                subsample=cfg.subsample,
                colsample_bytree=cfg.colsample_bytree,
                objective="reg:quantileerror",
                quantile_alpha=cfg.quantile_alpha_high,
                n_jobs=cfg.n_jobs,
                random_state=cfg.random_state,
                verbosity=0,
            )
            self._model_high.fit(X_train, y_train, sample_weight=w_train,
                                 verbose=False)
            logger.info("분위수 모델(q10/q90) 학습 완료")

        self._is_fitted = True
        return self

    # ------------------------------------------------------------------
    # 추론
    # ------------------------------------------------------------------

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """평균 EUI 예측 (kWh/m²·년)."""
        self._check_fitted()
        X = features[FEATURE_NAMES].values
        result: np.ndarray = self._model.predict(X)
        return result.clip(min=10.0)  # 물리적 하한 10 kWh/m²·년

    def predict_with_interval(
        self,
        features: pd.DataFrame,
        quantiles: tuple[float, float] = (0.1, 0.9),
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """평균 + 예측 구간 반환.

        Notes
        -----
        lower <= mean <= upper 순서를 반드시 보장한다.
        분위수 모델이 교차(lower > mean 또는 upper < mean)하는 경우
        mean 기준 ±20% 경험적 구간으로 폴백한다.
        """
        self._check_fitted()
        mean = self.predict(features)

        if self._model_low is None or self._model_high is None:
            # 분위수 모델 미학습: ±20% 경험적 구간 사용
            margin = mean * 0.2
            return mean, (mean - margin).clip(min=5.0), mean + margin

        X = features[FEATURE_NAMES].values
        lower: np.ndarray = self._model_low.predict(X).clip(min=5.0)
        upper: np.ndarray = self._model_high.predict(X)

        # 분위수 모델 교차 방지: lower > mean 또는 upper < mean 교정
        lower = np.minimum(lower, mean)
        upper = np.maximum(upper, mean)
        # 물리적 하한 보장
        upper = upper.clip(min=10.0)

        return mean, lower, upper

    def predict_from_record(self, record: BuildingRecord) -> dict[str, float]:
        """단일 BuildingRecord → 예측 결과 dict 반환 편의 메서드."""
        features = self._pipeline.transform_one(record)
        mean, lower, upper = self.predict_with_interval(features)
        return {
            "eui_predicted": float(mean[0]),
            "eui_lower": float(lower[0]),
            "eui_upper": float(upper[0]),
        }

    # ------------------------------------------------------------------
    # 피처 중요도
    # ------------------------------------------------------------------

    def feature_importance(self) -> pd.DataFrame:
        """피처 중요도 DataFrame 반환 (gain 기준 내림차순, 합계 1.0으로 정규화).

        모델 해석 및 피처 선택에 활용한다.

        Notes
        -----
        XGBoost ``feature_importances_`` 는 weight(출현 빈도) 기준으로
        합이 1.0이 아닐 수 있다.  gain 기준으로 수동 정규화하여 합이 1.0이 되도록
        보장한다.
        """
        self._check_fitted()
        # get_booster().get_score(importance_type="gain") 은 사용되지 않은 피처를
        # 반환하지 않으므로, FEATURE_NAMES 전체를 0으로 초기화한 후 채운다.
        raw: dict[str, float] = self._model.get_booster().get_score(
            importance_type="gain"
        )
        importance = np.array(
            [raw.get(f"f{i}", 0.0) for i in range(len(FEATURE_NAMES))],
            dtype=np.float64,
        )
        total = importance.sum()
        if total > 0:
            importance = importance / total
        df = pd.DataFrame({
            "feature": FEATURE_NAMES,
            "importance": importance,
        }).sort_values("importance", ascending=False).reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 직렬화
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """모델 아티팩트를 pickle로 저장한다.

        Notes
        -----
        XGBoost 네이티브 포맷(.ubj) 대신 pickle을 사용하는 이유:
        - 분위수 모델 3개 + 파이프라인 설정을 단일 파일로 관리.
        - model_versions.artifact_path 에 이 경로를 기록한다.
        """
        self._check_fitted()
        payload = {
            "model": self._model,
            "model_low": self._model_low,
            "model_high": self._model_high,
            "config": self._config,
            "feature_names": FEATURE_NAMES,
            "saved_at": datetime.now().isoformat(),
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("모델 저장 완료: %s", path)

    @classmethod
    def load(cls, path: str | Path) -> "XGBoostEUIPredictor":
        """저장된 모델 로드."""
        with open(path, "rb") as f:
            payload = pickle.load(f)
        predictor = cls(config=payload["config"])
        predictor._model = payload["model"]
        predictor._model_low = payload["model_low"]
        predictor._model_high = payload["model_high"]
        predictor._is_fitted = True
        logger.info("모델 로드 완료: %s", path)
        return predictor

    # ------------------------------------------------------------------
    # 교차검증
    # ------------------------------------------------------------------

    def cross_validate(
        self,
        dataset: TrainingDataset,
        n_splits: int = 5,
        stratify_by: str = "vintage",
    ) -> dict[str, float]:
        """층화 K-Fold 교차검증 수행.

        Parameters
        ----------
        dataset:
            전체 학습 데이터.
        n_splits:
            폴드 수. 데이터가 89건 + 3K건이므로 5-fold 권장.
        stratify_by:
            층화 기준 컬럼. ``"vintage"`` 이면 vintage_class_enc 사용.

        Returns
        -------
        dict
            {"rmse_mean", "rmse_std", "mape_mean", "mape_std", "r2_mean", "r2_std"}
        """
        try:
            from sklearn.model_selection import StratifiedKFold
            from sklearn.metrics import mean_squared_error, r2_score
            import xgboost as xgb
        except ImportError as e:
            raise ImportError("scikit-learn 및 xgboost 필요") from e

        # Tier1 샘플은 모든 fold의 훈련셋에 강제 포함
        tier1_mask = dataset.source_tier == 1
        tier1_X = dataset.X[FEATURE_NAMES].values[tier1_mask]
        tier1_y = dataset.y[tier1_mask]
        tier1_w = dataset.weights[tier1_mask]

        tier2plus_X = dataset.X[FEATURE_NAMES].values[~tier1_mask]
        tier2plus_y = dataset.y[~tier1_mask]
        tier2plus_w = dataset.weights[~tier1_mask]

        # stratify_by 파라미터를 실제 층화에 반영한다.
        # vintage_class_enc 컬럼으로 층화하면 소수 클래스도 각 fold에 포함된다.
        if stratify_by == "vintage":
            strat_labels = dataset.X["vintage_class_enc"].values[~tier1_mask].astype(int)
        else:
            strat_labels = dataset.source_tier[~tier1_mask].astype(int)

        kf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
        rmse_scores, mape_scores, r2_scores = [], [], []

        cfg = self._config
        for fold, (train_sub, val_sub) in enumerate(kf.split(tier2plus_X, strat_labels)):
            X_tr = np.vstack([tier1_X, tier2plus_X[train_sub]])
            y_tr = np.concatenate([tier1_y, tier2plus_y[train_sub]])
            w_tr = np.concatenate([tier1_w, tier2plus_w[train_sub]])

            X_val = tier2plus_X[val_sub]
            y_val = tier2plus_y[val_sub]

            m = xgb.XGBRegressor(
                n_estimators=cfg.n_estimators,
                max_depth=cfg.max_depth,
                learning_rate=cfg.learning_rate,
                subsample=cfg.subsample,
                colsample_bytree=cfg.colsample_bytree,
                min_child_weight=cfg.min_child_weight,
                objective=cfg.objective,
                n_jobs=cfg.n_jobs,
                random_state=cfg.random_state,
                verbosity=0,
            )
            m.fit(X_tr, y_tr, sample_weight=w_tr, verbose=False)
            preds = m.predict(X_val).clip(min=10.0)

            rmse = float(np.sqrt(mean_squared_error(y_val, preds)))
            mape = float(np.mean(np.abs((y_val - preds) / (y_val + 1e-6))) * 100)
            r2   = float(r2_score(y_val, preds))

            rmse_scores.append(rmse)
            mape_scores.append(mape)
            r2_scores.append(r2)
            logger.info("Fold %d: RMSE=%.1f  MAPE=%.1f%%  R²=%.3f", fold + 1, rmse, mape, r2)

        return {
            "rmse_mean":  float(np.mean(rmse_scores)),
            "rmse_std":   float(np.std(rmse_scores)),
            "mape_mean":  float(np.mean(mape_scores)),
            "mape_std":   float(np.std(mape_scores)),
            "r2_mean":    float(np.mean(r2_scores)),
            "r2_std":     float(np.std(r2_scores)),
        }

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------

    def _check_fitted(self) -> None:
        if not self._is_fitted or self._model is None:
            raise RuntimeError(
                "모델이 학습되지 않았습니다. fit() 또는 load()를 먼저 호출하세요."
            )


# ---------------------------------------------------------------------------
# 추론 파이프라인 (predict_energy 진입점)
# ---------------------------------------------------------------------------

@dataclass
class EnergyPrediction:
    """predict_energy() 반환 타입."""

    pnu: str
    target_date: date
    eui_predicted: float          # kWh/m²·년
    eui_lower: float
    eui_upper: float
    data_tier_used: int           # 1=실측, 2=KEA, 3=ML, 4=archetype
    model_version_id: int | None  # model_versions.id
    features_snapshot: dict[str, float] = field(default_factory=dict)


def predict_energy(
    pnu: str,
    target_date: date,
    db_row: dict[str, Any],
    global_model: XGBoostEUIPredictor,
    pipeline: FeaturePipeline | None = None,
    model_version_id: int | None = None,
    snapshot_features: bool = False,
) -> EnergyPrediction:
    """단일 건물의 EUI를 예측하고 EnergyPrediction을 반환한다.

    추론 우선순위
    ----------
    1. data_tier == 1 (실측 계량): 직접 반환 (모델 불필요)
    2. data_tier == 2 (KEA 인증): 모델 예측 + 인증값 교정
    3. data_tier >= 3: 글로벌 XGBoost 모델 추론
    4. 폴백: archetype ref_total × 환산계수

    Parameters
    ----------
    pnu:
        건물 PNU.
    target_date:
        예측 대상 날짜 (energy_predictions.predicted_at 기록용).
    db_row:
        buildings_enriched 뷰의 단일 행 (dict).
        필수 컬럼: usage_type, vintage_class, structure_class,
                  built_year, total_area, floors_above.
    global_model:
        학습 완료된 XGBoostEUIPredictor 인스턴스.
    pipeline:
        피처 파이프라인. None이면 기본 인스턴스 사용.
    model_version_id:
        model_versions.id. energy_predictions 테이블 저장에 사용.
    snapshot_features:
        True이면 EnergyPrediction.features_snapshot 에 피처 값 저장.
        디버깅/모니터링 목적이며 프로덕션에서는 False 권장.
    """
    if pipeline is None:
        pipeline = FeaturePipeline()

    data_tier = int(db_row.get("data_tier", 4))

    # Tier1: 실측 계량 데이터 있으면 모델 불필요
    if data_tier == 1:
        measured_eui = float(db_row.get("eui") or db_row.get("total_energy") or 0.0)
        if measured_eui > 0:
            return EnergyPrediction(
                pnu=pnu,
                target_date=target_date,
                eui_predicted=measured_eui,
                eui_lower=measured_eui * 0.9,
                eui_upper=measured_eui * 1.1,
                data_tier_used=1,
                model_version_id=None,
            )

    # BuildingRecord 구성
    record = _dict_to_building_record({**db_row, "pnu": pnu})

    features = pipeline.transform_one(record)
    mean, lower, upper = global_model.predict_with_interval(features)

    # Tier2: KEA 인증값으로 예측 보정
    if data_tier == 2:
        cert_primary = float(db_row.get("epi_score") or 0.0)
        if cert_primary > 0:
            cert_eui = cert_primary * KEA_PRIMARY_TO_FINAL.get(record.usage_type, 0.5)
            # 예측값과 인증값의 가중 평균 (인증 신뢰도 70%)
            corrected = mean[0] * 0.3 + cert_eui * 0.7
            # 보정 전후 비율을 lower/upper에도 동일하게 적용하여 구간 일관성 유지
            ratio = corrected / mean[0] if mean[0] > 0 else 1.0
            mean[0] = corrected
            lower[0] = lower[0] * ratio
            upper[0] = upper[0] * ratio

    snapshot = {}
    if snapshot_features:
        snapshot = dict(zip(FEATURE_NAMES, features[FEATURE_NAMES].values[0].tolist()))

    return EnergyPrediction(
        pnu=pnu,
        target_date=target_date,
        eui_predicted=float(mean[0]),
        eui_lower=float(lower[0]),
        eui_upper=float(upper[0]),
        data_tier_used=min(data_tier, 3),
        model_version_id=model_version_id,
        features_snapshot=snapshot,
    )
