"""
Tier C: 계량 이력 보유 건물 전용 단기 시계열 예측 파이프라인.

대상
----
실제 전기/가스 계량기 시계열이 있는 건물 (data_tier=1).
건축HUB 월별 실소비량 데이터를 24개월 이상 보유한 건물.

설계 원칙
---------
- 건물별 개인화 모델과 전역 공유 파라미터를 분리한다.
- XGBoost lag 피처 모델을 기본 구현체로 제공한다 (Prophet 대비 배포 단순).
- 모델 선택 가이드라인을 코드 주석으로 상세히 기술한다.
- model_versions 테이블에 pnu 기반 개인화 모델 참조를 저장한다.

옵션 비교 (설계 판단 근거)
--------------------------
옵션 A — XGBoost + Lag 피처:
  장점: Tier B 모델과 동일 라이브러리, 배포 단순, 건물별 재학습 빠름
  단점: 시계열 외삽 성능 제한적, 긴 계절 주기 포착 약함
  적용: 2년 이상 이력 + 단순 패턴 건물

옵션 B — Prophet (Facebook):
  장점: 연/주별 계절성 자동 분해, 한국 공휴일 내장 지원, 해석 용이
  단점: 건물별 개별 모델 필수 (글로벌 불가), 설치 무거움(Stan 의존),
        배치 추론 병렬화 필요
  적용: 계절성 뚜렷 + 시각화/보고서 목적

옵션 C — LightGBM:
  장점: XGBoost 대비 학습 속도 3~5x 빠름, 메모리 효율, GPU 지원 우수
  단점: 하이퍼파라미터 민감도 높음 (learning_rate 과소 설정 위험)
  적용: 건물 수 많아 배치 재학습 속도가 병목인 경우

현재 구현: 옵션 A (XGBoost lag 피처). 옵션 B/C 교체 인터페이스 제공.
"""

from __future__ import annotations

import logging
import pickle
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 한국 공휴일 (근사값 — 실제 운영에서는 workalendar 또는 DB 테이블 사용 권장)
# 건물 에너지 예측에서 공휴일은 주요 패턴 변화 원인이다.
KOREA_HOLIDAYS_2025: frozenset[date] = frozenset([
    date(2025, 1, 1),   # 신정
    date(2025, 1, 28),  # 설날 연휴
    date(2025, 1, 29),  # 설날
    date(2025, 1, 30),  # 설날 연휴
    date(2025, 3, 1),   # 삼일절
    date(2025, 5, 5),   # 어린이날
    date(2025, 5, 6),   # 어린이날 대체
    date(2025, 6, 6),   # 현충일
    date(2025, 8, 15),  # 광복절
    date(2025, 10, 3),  # 개천절
    date(2025, 10, 5),  # 추석 연휴
    date(2025, 10, 6),  # 추석
    date(2025, 10, 7),  # 추석 연휴
    date(2025, 10, 9),  # 한글날
    date(2025, 12, 25), # 크리스마스
])

KOREA_HOLIDAYS_2026: frozenset[date] = frozenset([
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 설날
    date(2026, 3, 1),
    date(2026, 5, 5),
    date(2026, 6, 6),
    date(2026, 8, 15),
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),  # 추석
    date(2026, 10, 3),
    date(2026, 10, 9),
    date(2026, 12, 25),
])

KOREA_HOLIDAYS: frozenset[date] = KOREA_HOLIDAYS_2025 | KOREA_HOLIDAYS_2026


# ---------------------------------------------------------------------------
# 입력 데이터 구조
# ---------------------------------------------------------------------------

@dataclass
class DailyConsumption:
    """일별 에너지 소비 시계열 단일 레코드."""

    consumption_date: date
    elec_kwh: float | None = None   # 전기 소비량 (kWh)
    gas_kwh: float | None = None    # 가스 소비량 (kWh)
    total_kwh: float | None = None  # 합계 (None이면 elec+gas로 계산)

    def total(self) -> float:
        """총 에너지 소비량 kWh."""
        if self.total_kwh is not None:
            return self.total_kwh
        e = self.elec_kwh or 0.0
        g = self.gas_kwh or 0.0
        return e + g


@dataclass
class BuildingTimeSeries:
    """단일 건물의 에너지 시계열 데이터."""

    pnu: str
    floor_area: float               # 연면적 m² (EUI 환산용)
    usage_type: str                 # 영문 archetype key
    records: list[DailyConsumption] = field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        """시계열을 DataFrame으로 변환. 날짜 인덱스, 결측값 선형 보간.

        결측값 처리 규칙
        ---------------
        - total_kwh / elec_kwh / gas_kwh 모두 None인 레코드 → y=NaN (보간 대상)
        - total_kwh=0.0 명시 기록 → y=0.0 (실제 무소비; 보간 대상 아님)
        - reindex로 삽입된 날짜 갭 → NaN → 선형 보간
        """
        rows = []
        for r in self.records:
            # 세 값이 모두 None이면 NaN으로 표시하여 보간 대상으로 처리
            all_none = (r.total_kwh is None
                        and r.elec_kwh is None
                        and r.gas_kwh is None)
            rows.append({
                "ds": pd.Timestamp(r.consumption_date),
                "y": float("nan") if all_none else r.total(),
                "elec_kwh": r.elec_kwh if r.elec_kwh is not None else float("nan"),
                "gas_kwh": r.gas_kwh if r.gas_kwh is not None else float("nan"),
            })
        df = pd.DataFrame(rows).sort_values("ds").reset_index(drop=True)
        # 연속 날짜로 reindex → 날짜 갭도 NaN으로 채워져 보간됨
        df = df.set_index("ds").reindex(
            pd.date_range(df["ds"].min(), df["ds"].max(), freq="D")
        )
        df["y"] = df["y"].interpolate(method="linear", limit=7)
        # limit=7 초과 갭에서 남은 NaN을 0으로 채우면 이상치(급격한 소비 0)가 생긴다.
        # 경계값 외삽(forward/backward fill)으로 인접 실측값을 사용한다.
        df["y"] = df["y"].ffill().bfill().fillna(0.0)
        # elec_kwh, gas_kwh 결측도 동일하게 처리
        for col in ["elec_kwh", "gas_kwh"]:
            if col in df.columns:
                df[col] = df[col].ffill().bfill().fillna(0.0)
        df = df.reset_index().rename(columns={"index": "ds"})
        return df

    def has_sufficient_history(self, min_days: int = 365) -> bool:
        """예측에 필요한 최소 이력이 있는지 확인."""
        if not self.records:
            return False
        dates = [r.consumption_date for r in self.records]
        span = (max(dates) - min(dates)).days
        valid_count = sum(1 for r in self.records if r.total() > 0)
        return span >= min_days and valid_count >= min_days * 0.7


# ---------------------------------------------------------------------------
# Lag 피처 생성 (옵션 A 핵심)
# ---------------------------------------------------------------------------

class LagFeatureBuilder:
    """일별 소비 시계열에서 XGBoost 입력 피처를 생성한다.

    피처 설계 원칙
    -------------
    건물 에너지 소비의 주요 결정 요인:
    1. 자기 회귀 패턴  → 1일/7일/30일 lag
    2. 주간 주기성    → day_of_week (one-hot 불필요, 트리가 분기 학습)
    3. 계절성        → month, day_of_year, HDD/CDD
    4. 공휴일 효과   → is_holiday (업무시설 대폭 하락)
    5. 추세          → rolling 평균으로 수준 변화 포착
    """

    LAGS: list[int] = [1, 2, 3, 7, 14, 30]         # 자기회귀 lag (일)
    ROLLING_WINDOWS: list[int] = [7, 14, 30]        # 이동평균 윈도우

    def build(
        self,
        df: pd.DataFrame,
        weather_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """피처 DataFrame 생성.

        Parameters
        ----------
        df:
            BuildingTimeSeries.to_dataframe() 결과.
            컬럼: ds (Timestamp), y (kWh/일).
        weather_df:
            날씨 데이터. 컬럼: ds, HDD18, CDD24, avg_temp.
            None이면 월별 서울 TMY 값으로 폴백.

        Returns
        -------
        pd.DataFrame
            피처 컬럼 + target 컬럼 ``y``. NaN 행은 학습에서 제외한다.
        """
        df = df.copy()
        df["ds"] = pd.to_datetime(df["ds"])

        # 날짜 파생 피처
        df["day_of_week"]    = df["ds"].dt.dayofweek        # 0=월 ~ 6=일
        df["day_of_year"]    = df["ds"].dt.dayofyear
        df["month"]          = df["ds"].dt.month
        df["is_weekend"]     = (df["day_of_week"] >= 5).astype(int)
        df["is_holiday"]     = df["ds"].apply(
            lambda t: int(t.date() in KOREA_HOLIDAYS)
        )
        df["is_work_day"]    = ((df["is_weekend"] == 0) & (df["is_holiday"] == 0)).astype(int)

        # Lag 피처
        for lag in self.LAGS:
            df[f"lag_{lag}d"] = df["y"].shift(lag)

        # 이동평균/표준편차
        for window in self.ROLLING_WINDOWS:
            df[f"roll_mean_{window}d"] = df["y"].shift(1).rolling(window).mean()
            df[f"roll_std_{window}d"]  = df["y"].shift(1).rolling(window).std()

        # 이동평균 대비 편차 (추세 이탈 탐지)
        # ROLLING_WINDOWS[0]을 참조하여 하드코딩 의존성을 제거한다.
        _shortest_window = self.ROLLING_WINDOWS[0]
        df["dev_from_7d_avg"] = df["y"].shift(1) - df[f"roll_mean_{_shortest_window}d"]

        # 날씨 피처 결합
        if weather_df is not None:
            weather_df = weather_df.copy()
            weather_df["ds"] = pd.to_datetime(weather_df["ds"])
            df = df.merge(weather_df[["ds", "HDD18", "CDD24", "avg_temp"]],
                          on="ds", how="left")
        else:
            # TMY 월별 폴백
            df = self._attach_tmy_weather(df)

        return df

    def get_feature_columns(self) -> list[str]:
        """학습/추론에 사용할 피처 컬럼 목록 반환."""
        cols = [
            "day_of_week", "day_of_year", "month",
            "is_weekend", "is_holiday", "is_work_day",
            "dev_from_7d_avg",
            "HDD18", "CDD24", "avg_temp",
        ]
        for lag in self.LAGS:
            cols.append(f"lag_{lag}d")
        for window in self.ROLLING_WINDOWS:
            cols.append(f"roll_mean_{window}d")
            cols.append(f"roll_std_{window}d")
        return cols

    @staticmethod
    def _attach_tmy_weather(df: pd.DataFrame) -> pd.DataFrame:
        """월별 서울 TMY 날씨를 일별로 할당한다."""
        from src.simulation.ml_xgboost import SEOUL_TMY_MONTHLY

        df = df.copy()
        df["HDD18"]    = df["month"].map(lambda m: SEOUL_TMY_MONTHLY[m]["HDD18"] / 30)
        df["CDD24"]    = df["month"].map(lambda m: SEOUL_TMY_MONTHLY[m]["CDD24"] / 30)
        df["avg_temp"] = df["month"].map(lambda m: SEOUL_TMY_MONTHLY[m]["avg_temp"])
        return df


# ---------------------------------------------------------------------------
# 시계열 예측기 기반 클래스
# ---------------------------------------------------------------------------

class TimeSeriesPredictor:
    """건물별 단기 시계열 예측기의 공통 인터페이스."""

    def fit(self, ts: BuildingTimeSeries) -> "TimeSeriesPredictor":
        raise NotImplementedError

    def predict(
        self, ts: BuildingTimeSeries, horizon_days: int = 7
    ) -> pd.DataFrame:
        """
        Returns
        -------
        pd.DataFrame
            컬럼: ds (Timestamp), y_pred (kWh/일), y_lower, y_upper.
        """
        raise NotImplementedError

    def predict_daily_eui(
        self, ts: BuildingTimeSeries, horizon_days: int = 7
    ) -> pd.DataFrame:
        """kWh/일 예측을 EUI (kWh/m²·일)로 환산하여 반환."""
        df = self.predict(ts, horizon_days)
        area = max(ts.floor_area, 1.0)
        df["eui_pred"]  = df["y_pred"]  / area
        df["eui_lower"] = df["y_lower"] / area
        df["eui_upper"] = df["y_upper"] / area
        return df


# ---------------------------------------------------------------------------
# 옵션 A: XGBoost Lag 피처 모델 (기본 구현체)
# ---------------------------------------------------------------------------

@dataclass
class LagXGBoostConfig:
    """XGBoost lag 피처 모델 하이퍼파라미터."""

    n_estimators: int = 300
    max_depth: int = 5
    learning_rate: float = 0.08
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 3
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0
    n_jobs: int = -1
    random_state: int = 42
    early_stopping_rounds: int = 20


class LagXGBoostPredictor(TimeSeriesPredictor):
    """XGBoost + Lag 피처 기반 일별 에너지 소비 예측기.

    적합 시나리오
    -----------
    - 건축HUB 월별 데이터를 일별로 분배한 건물 (2년 이상 이력)
    - 단순하고 빠른 배포가 필요한 경우
    - 건물별 개인화가 필요하지만 Prophet 설치 비용을 피하고 싶은 경우

    예측 구간
    ---------
    잔차의 백분위수 기반 경험적 구간을 사용한다 (분위수 회귀 미사용).
    훈련 잔차 90번째 백분위수를 ±로 적용한다.
    """

    def __init__(self, config: LagXGBoostConfig | None = None) -> None:
        self._config = config or LagXGBoostConfig()
        self._builder = LagFeatureBuilder()
        self._model: Any = None
        self._feature_cols: list[str] = self._builder.get_feature_columns()
        self._residual_p90: float = 0.0
        self._pnu: str = ""

    def fit(self, ts: BuildingTimeSeries, weather_df: pd.DataFrame | None = None) -> "LagXGBoostPredictor":
        """단일 건물 시계열로 모델 학습.

        Parameters
        ----------
        ts:
            BuildingTimeSeries (최소 365일 이력 권장).
        weather_df:
            날씨 데이터. None이면 TMY 폴백.
        """
        try:
            import xgboost as xgb
        except ImportError as e:
            raise ImportError("pip install xgboost>=2.0") from e

        df = ts.to_dataframe()
        df = self._builder.build(df, weather_df)

        # lag 피처 생성 전 NaN 행 제거 (초반 window 크기만큼)
        train_df = df.dropna(subset=self._feature_cols + ["y"]).copy()
        if len(train_df) < 30:
            raise ValueError(
                f"학습 데이터 부족 ({len(train_df)}행 < 30). "
                "최소 30일 유효 이력이 필요합니다."
            )

        X = train_df[self._feature_cols].values
        y = train_df["y"].values

        cfg = self._config
        self._model = xgb.XGBRegressor(
            n_estimators=cfg.n_estimators,
            max_depth=cfg.max_depth,
            learning_rate=cfg.learning_rate,
            subsample=cfg.subsample,
            colsample_bytree=cfg.colsample_bytree,
            min_child_weight=cfg.min_child_weight,
            reg_alpha=cfg.reg_alpha,
            reg_lambda=cfg.reg_lambda,
            objective="reg:squarederror",
            n_jobs=cfg.n_jobs,
            random_state=cfg.random_state,
            verbosity=0,
        )
        self._model.fit(X, y, verbose=False)

        # 훈련 잔차 기반 예측 구간 추정
        train_preds = self._model.predict(X).clip(min=0.0)
        residuals = np.abs(y - train_preds)
        self._residual_p90 = float(np.percentile(residuals, 90))
        self._pnu = ts.pnu

        logger.info(
            "[%s] lag-XGBoost 학습 완료: %d행, 잔차P90=%.1f kWh",
            ts.pnu, len(train_df), self._residual_p90,
        )
        return self

    def predict(
        self,
        ts: BuildingTimeSeries,
        horizon_days: int = 7,
        weather_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """향후 horizon_days 일 예측.

        재귀 예측(recursive multi-step)을 사용한다:
        각 일(day)의 예측값을 다음 일의 lag 피처로 활용한다.
        이는 직접 다단계 예측(direct multi-step) 대비 단순하지만
        오차 누적이 발생한다. 7일 이하 단기 예측에서 실용적.
        """
        if self._model is None:
            raise RuntimeError("fit() 먼저 호출 필요")

        # 역사 시계열로 초기 피처 행렬 구성
        df = ts.to_dataframe()
        df = self._builder.build(df, weather_df)

        last_known = df.dropna(subset=self._feature_cols).copy()
        if last_known.empty:
            raise ValueError("피처 생성에 충분한 이력 없음")

        # 재귀 예측: 마지막 이력 상태에서 horizon_days 스텝 진행
        history = last_known.copy()
        preds: list[dict[str, Any]] = []
        last_date = pd.Timestamp(history["ds"].max())

        for step in range(horizon_days):
            pred_date = last_date + timedelta(days=step + 1)
            row = self._build_future_row(history, pred_date, weather_df)
            x_vec = np.array([[row[c] for c in self._feature_cols]])
            y_hat = float(self._model.predict(x_vec)[0])
            y_hat = max(y_hat, 0.0)

            preds.append({
                "ds": pred_date,
                "y_pred": y_hat,
                "y_lower": max(0.0, y_hat - self._residual_p90),
                "y_upper": y_hat + self._residual_p90,
            })

            # 예측값을 이력에 추가하여 다음 스텝 lag 계산에 사용
            new_row = row.copy()
            new_row["ds"] = pred_date
            new_row["y"] = y_hat
            history = pd.concat(
                [history, pd.DataFrame([new_row])],
                ignore_index=True,
            )

        return pd.DataFrame(preds)

    def _build_future_row(
        self,
        history: pd.DataFrame,
        pred_date: pd.Timestamp,
        weather_df: pd.DataFrame | None,
    ) -> dict[str, float]:
        """미래 날짜에 대한 피처 벡터를 현재 history 기반으로 생성."""
        row: dict[str, float] = {
            "day_of_week":    float(pred_date.dayofweek),
            "day_of_year":    float(pred_date.dayofyear),
            "month":          float(pred_date.month),
            "is_weekend":     float(pred_date.dayofweek >= 5),
            "is_holiday":     float(pred_date.date() in KOREA_HOLIDAYS),
            "is_work_day":    float(
                pred_date.dayofweek < 5 and pred_date.date() not in KOREA_HOLIDAYS
            ),
        }

        # Lag 피처 (history의 y 열 사용)
        y_series = history["y"].values
        for lag in LagFeatureBuilder.LAGS:
            idx = -(lag)
            row[f"lag_{lag}d"] = float(y_series[idx]) if len(y_series) >= lag else 0.0

        # Rolling 통계
        # 학습 시 pandas .rolling().std()는 ddof=1(표본 표준편차)을 사용한다.
        # 추론 시에도 ddof=1을 맞춰야 피처 분포가 일치한다.
        for window in LagFeatureBuilder.ROLLING_WINDOWS:
            if len(y_series) >= window:
                slice_ = y_series[-window:]
                row[f"roll_mean_{window}d"] = float(np.mean(slice_))
                # ddof=1: pandas .rolling().std() 기본값과 일치
                row[f"roll_std_{window}d"]  = float(np.std(slice_, ddof=1)) if len(slice_) > 1 else 0.0
            else:
                row[f"roll_mean_{window}d"] = float(np.mean(y_series)) if len(y_series) > 0 else 0.0
                row[f"roll_std_{window}d"]  = 0.0

        # 최단 rolling window 기준으로 편차 계산 (현재 ROLLING_WINDOWS[0] = 7)
        _shortest_window = LagFeatureBuilder.ROLLING_WINDOWS[0]
        roll_mean_key = f"roll_mean_{_shortest_window}d"
        row["dev_from_7d_avg"] = row["lag_1d"] - row[roll_mean_key]

        # 날씨
        from src.simulation.ml_xgboost import SEOUL_TMY_MONTHLY
        month = int(pred_date.month)
        if weather_df is not None:
            wrow = weather_df[weather_df["ds"] == pred_date]
            if not wrow.empty:
                row["HDD18"]    = float(wrow["HDD18"].iloc[0])
                row["CDD24"]    = float(wrow["CDD24"].iloc[0])
                row["avg_temp"] = float(wrow["avg_temp"].iloc[0])
                return row
        row["HDD18"]    = SEOUL_TMY_MONTHLY[month]["HDD18"] / 30
        row["CDD24"]    = SEOUL_TMY_MONTHLY[month]["CDD24"] / 30
        row["avg_temp"] = SEOUL_TMY_MONTHLY[month]["avg_temp"]
        return row

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model": self._model,
                "residual_p90": self._residual_p90,
                "config": self._config,
                "pnu": self._pnu,
                "feature_cols": self._feature_cols,
            }, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("[%s] 시계열 모델 저장: %s", self._pnu, path)

    @classmethod
    def load(cls, path: str | Path) -> "LagXGBoostPredictor":
        with open(path, "rb") as f:
            d = pickle.load(f)
        obj = cls(config=d["config"])
        obj._model = d["model"]
        obj._residual_p90 = d["residual_p90"]
        obj._pnu = d["pnu"]
        obj._feature_cols = d["feature_cols"]
        return obj


# ---------------------------------------------------------------------------
# 옵션 B: Prophet 래퍼 (설치 선택적)
# ---------------------------------------------------------------------------

class ProphetPredictor(TimeSeriesPredictor):
    """Facebook Prophet 기반 계절성 분해 예측기.

    사용 시 추가 설치 필요:
        pip install prophet

    Prophet이 XGBoost lag 모델보다 유리한 경우
    ------------------------------------------
    1. 12개월 이상 이력이 있고 뚜렷한 연간 계절성이 있는 건물
       (예: 학교 — 방학/학기 패턴, 상업시설 — 연말 성수기)
    2. 예측 결과를 그래프로 시각화하여 운영자에게 설명해야 할 때
       (Prophet은 추세/계절성/잔차를 분해하여 시각화 제공)
    3. 공휴일 효과를 명시적으로 분리하고 싶을 때

    주의사항
    --------
    - 건물별 개별 모델이 필요하여 건물 수가 많으면 배치 학습 비용이 큼
    - 1분 단위 데이터에는 부적합; 일별/주별 데이터에 최적화됨
    - Stan(C++) 의존성으로 컨테이너 이미지 크기 증가 (약 200MB)
    """

    def __init__(
        self,
        yearly_seasonality: bool = True,
        weekly_seasonality: bool = True,
        daily_seasonality: bool = False,
        changepoint_prior_scale: float = 0.05,
    ) -> None:
        self._yearly = yearly_seasonality
        self._weekly = weekly_seasonality
        self._daily  = daily_seasonality
        self._cp_scale = changepoint_prior_scale
        self._model: Any = None
        self._pnu: str = ""

    def fit(
        self,
        ts: BuildingTimeSeries,
        weather_df: pd.DataFrame | None = None,
    ) -> "ProphetPredictor":
        try:
            from prophet import Prophet
        except ImportError as e:
            raise ImportError(
                "Prophet 패키지 필요: pip install prophet\n"
                "대안: LagXGBoostPredictor 사용"
            ) from e

        df = ts.to_dataframe()[["ds", "y"]].copy()
        df["ds"] = pd.to_datetime(df["ds"])

        # 공휴일 DataFrame 구성
        holiday_list = []
        for h in sorted(KOREA_HOLIDAYS):
            holiday_list.append({
                "holiday": "kr_holiday",
                "ds": pd.Timestamp(h),
                "lower_window": 0,
                "upper_window": 0,
            })
        holidays_df = pd.DataFrame(holiday_list) if holiday_list else None

        m = Prophet(
            yearly_seasonality=self._yearly,
            weekly_seasonality=self._weekly,
            daily_seasonality=self._daily,
            changepoint_prior_scale=self._cp_scale,
            holidays=holidays_df,
        )

        # 날씨 외부 회귀 변수 추가
        if weather_df is not None:
            m.add_regressor("HDD18")
            m.add_regressor("CDD24")
            weather_df = weather_df.copy()
            weather_df["ds"] = pd.to_datetime(weather_df["ds"])
            df = df.merge(weather_df[["ds", "HDD18", "CDD24"]], on="ds", how="left")
            df[["HDD18", "CDD24"]] = df[["HDD18", "CDD24"]].fillna(0.0)

        m.fit(df)
        self._model = m
        self._pnu = ts.pnu
        self._has_weather_regressors = weather_df is not None
        logger.info("[%s] Prophet 모델 학습 완료", ts.pnu)
        return self

    def predict(
        self,
        ts: BuildingTimeSeries,
        horizon_days: int = 7,
        weather_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        if self._model is None:
            raise RuntimeError("fit() 먼저 호출 필요")

        future = self._model.make_future_dataframe(periods=horizon_days)
        # 날씨 회귀 변수가 있으면 미래 날씨 값 필요
        if self._has_weather_regressors:
            if weather_df is not None:
                weather_df = weather_df.copy()
                weather_df["ds"] = pd.to_datetime(weather_df["ds"])
                future = future.merge(
                    weather_df[["ds", "HDD18", "CDD24"]], on="ds", how="left"
                )
                future[["HDD18", "CDD24"]] = future[["HDD18", "CDD24"]].fillna(0.0)
            else:
                # TMY 폴백
                from src.simulation.ml_xgboost import SEOUL_TMY_MONTHLY
                future["month"] = future["ds"].dt.month
                future["HDD18"] = future["month"].map(
                    lambda m: SEOUL_TMY_MONTHLY[m]["HDD18"] / 30
                )
                future["CDD24"] = future["month"].map(
                    lambda m: SEOUL_TMY_MONTHLY[m]["CDD24"] / 30
                )

        forecast = self._model.predict(future)
        result = forecast.tail(horizon_days)[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        result = result.rename(columns={
            "yhat": "y_pred", "yhat_lower": "y_lower", "yhat_upper": "y_upper"
        })
        result["y_pred"]  = result["y_pred"].clip(lower=0.0)
        result["y_lower"] = result["y_lower"].clip(lower=0.0)
        return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 옵션 C: LightGBM 래퍼 (설치 선택적)
# ---------------------------------------------------------------------------

class LightGBMPredictor(TimeSeriesPredictor):
    """LightGBM + Lag 피처 기반 예측기.

    XGBoost 대비 LightGBM이 유리한 경우
    -----------------------------------
    1. 건물 수가 수천 개 이상이고 배치 재학습 속도가 병목인 경우
       (LightGBM의 leaf-wise 성장이 level-wise XGBoost보다 3~5x 빠름)
    2. 메모리 제약 환경 (GPU 메모리 효율 우수)
    3. 범주형 피처(day_of_week, month)를 정수 그대로 처리하고 싶을 때
       (LightGBM 네이티브 categorical 지원)

    주의: 학습 속도가 빠른 대신 과적합에 민감하다.
    min_child_samples(=20 이상) 를 반드시 설정할 것.
    """

    def __init__(
        self,
        n_estimators: int = 300,
        max_depth: int = -1,   # LightGBM 기본: 무제한 (num_leaves로 제어)
        num_leaves: int = 31,
        learning_rate: float = 0.08,
        min_child_samples: int = 20,
        n_jobs: int = -1,
        random_state: int = 42,
    ) -> None:
        self._params = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "num_leaves": num_leaves,
            "learning_rate": learning_rate,
            "min_child_samples": min_child_samples,
            "n_jobs": n_jobs,
            "random_state": random_state,
            "verbose": -1,
        }
        self._builder = LagFeatureBuilder()
        self._model: Any = None
        self._residual_p90: float = 0.0
        self._pnu: str = ""

    def fit(
        self,
        ts: BuildingTimeSeries,
        weather_df: pd.DataFrame | None = None,
    ) -> "LightGBMPredictor":
        try:
            import lightgbm as lgb
        except ImportError as e:
            raise ImportError(
                "LightGBM 패키지 필요: pip install lightgbm\n"
                "대안: LagXGBoostPredictor 사용"
            ) from e

        feature_cols = self._builder.get_feature_columns()
        df = ts.to_dataframe()
        df = self._builder.build(df, weather_df)
        train_df = df.dropna(subset=feature_cols + ["y"]).copy()

        if len(train_df) < 30:
            raise ValueError(f"학습 데이터 부족: {len(train_df)}행")

        X = train_df[feature_cols].values
        y = train_df["y"].values

        self._model = lgb.LGBMRegressor(**self._params)
        self._model.fit(X, y)

        train_preds = self._model.predict(X)
        residuals = np.abs(y - train_preds)
        self._residual_p90 = float(np.percentile(residuals, 90))
        self._pnu = ts.pnu

        logger.info(
            "[%s] LightGBM 학습 완료: %d행, 잔차P90=%.1f",
            ts.pnu, len(train_df), self._residual_p90,
        )
        return self

    def predict(
        self,
        ts: BuildingTimeSeries,
        horizon_days: int = 7,
        weather_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """LagXGBoostPredictor와 동일한 재귀 예측 로직."""
        if self._model is None:
            raise RuntimeError("fit() 먼저 호출 필요")

        # LagXGBoostPredictor의 재귀 예측 로직을 재사용하기 위해
        # 동일한 LagFeatureBuilder 기반으로 구현
        lag_predictor = LagXGBoostPredictor()
        lag_predictor._model = self._model        # 모델만 교체
        lag_predictor._residual_p90 = self._residual_p90
        lag_predictor._pnu = self._pnu
        lag_predictor._feature_cols = self._builder.get_feature_columns()
        return lag_predictor.predict(ts, horizon_days, weather_df)


# ---------------------------------------------------------------------------
# 건물별 모델 레지스트리 (model_versions 연동)
# ---------------------------------------------------------------------------

@dataclass
class PerBuildingModelRegistry:
    """건물별 시계열 모델을 model_versions 테이블과 연동 관리한다.

    model_versions 레코드 구조 (Tier C 모델)
    -----------------------------------------
    model_name: "ts_lag_xgb_{pnu}"
    model_type: "xgboost"
    temporal_scale: "daily"
    target_variable: "elec_kwh" | "gas_kwh" | "eui"
    artifact_path: "/models/ts/{pnu}/v{version_tag}.pkl"
    hyperparams: LagXGBoostConfig.__dict__
    train_rows: 학습 이력 일수
    """

    base_model_dir: Path = Path("/models/ts")

    def model_path(self, pnu: str, version_tag: str = "latest") -> Path:
        return self.base_model_dir / pnu / f"{version_tag}.pkl"

    def save_model(
        self,
        predictor: LagXGBoostPredictor,
        pnu: str,
        version_tag: str = "latest",
    ) -> Path:
        path = self.model_path(pnu, version_tag)
        predictor.save(path)
        return path

    def load_model(self, pnu: str, version_tag: str = "latest") -> LagXGBoostPredictor:
        path = self.model_path(pnu, version_tag)
        if not path.exists():
            raise FileNotFoundError(f"모델 없음: {path}")
        return LagXGBoostPredictor.load(path)

    def has_model(self, pnu: str, version_tag: str = "latest") -> bool:
        return self.model_path(pnu, version_tag).exists()


# ---------------------------------------------------------------------------
# 통합 예측 진입점
# ---------------------------------------------------------------------------

def predict_timeseries(
    ts: BuildingTimeSeries,
    horizon_days: int = 7,
    model_registry: PerBuildingModelRegistry | None = None,
    weather_df: pd.DataFrame | None = None,
    predictor_type: str = "xgboost",
) -> pd.DataFrame:
    """건물 시계열 예측의 통합 진입점.

    모델 선택 우선순위
    -----------------
    1. model_registry에 기존 학습 모델이 있으면 로드 후 예측
    2. 없으면 현재 이력으로 새 모델 학습 후 예측
    3. 이력이 부족하면 ValueError 발생

    Parameters
    ----------
    ts:
        대상 건물 시계열.
    horizon_days:
        예측 일수 (1~30일 권장).
    model_registry:
        건물별 모델 저장소. None이면 매 호출마다 재학습.
    weather_df:
        날씨 예보 데이터 (ds, HDD18, CDD24, avg_temp 컬럼).
    predictor_type:
        "xgboost" | "prophet" | "lightgbm"

    Returns
    -------
    pd.DataFrame
        컬럼: ds, y_pred, y_lower, y_upper, eui_pred, eui_lower, eui_upper
    """
    if not ts.has_sufficient_history(min_days=180):
        raise ValueError(
            f"[{ts.pnu}] 예측에 필요한 최소 이력 부족 (180일 미만)"
        )

    predictor: TimeSeriesPredictor

    if model_registry is not None and model_registry.has_model(ts.pnu):
        predictor = model_registry.load_model(ts.pnu)
        logger.info("[%s] 기존 모델 로드", ts.pnu)
    else:
        predictor = _create_predictor(predictor_type)
        predictor.fit(ts, weather_df)
        if model_registry is not None:
            model_registry.save_model(predictor, ts.pnu)  # type: ignore[arg-type]
        logger.info("[%s] 새 모델 학습 완료", ts.pnu)

    result = predictor.predict_daily_eui(ts, horizon_days, weather_df)
    return result


def _create_predictor(predictor_type: str) -> TimeSeriesPredictor:
    if predictor_type == "xgboost":
        return LagXGBoostPredictor()
    elif predictor_type == "prophet":
        return ProphetPredictor()
    elif predictor_type == "lightgbm":
        return LightGBMPredictor()
    else:
        raise ValueError(
            f"알 수 없는 predictor_type: {predictor_type!r}. "
            "'xgboost' | 'prophet' | 'lightgbm' 중 선택."
        )
