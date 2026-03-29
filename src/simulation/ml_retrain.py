"""
XGBoost EUI 모델 재학습 파이프라인 (ml_retrain.py).

책임
----
1. DB에서 Tier1(실측) + Tier2(KEA 인증) 훈련 데이터를 수집한다.
2. TrainingDataset을 구성하여 XGBoostEUIPredictor를 학습한다.
3. model_registry / model_versions 테이블에 결과를 등록한다.
4. 검증 지표가 현재 프로덕션 모델 대비 2% 이상 향상되면 자동 프로모션한다.
5. Celery task로 노출하여 정기 재학습 또는 수동 트리거를 지원한다.
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Connection

from src.simulation.ml_xgboost import (
    XGBoostConfig,
    XGBoostEUIPredictor,
    FeaturePipeline,
    TrainingDataset,
    FEATURE_NAMES,
    _dict_to_building_record,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

MODEL_NAME = "xgb_eui_annual_global"
MODEL_TYPE = "xgboost"
TEMPORAL_SCALE = "annual"
TARGET_VARIABLE = "eui"
MODEL_DESCRIPTION = (
    "XGBoost 글로벌 EUI 연간 예측 모델 (Tier B). "
    "Tier1 실측 + Tier2 KEA 인증 데이터로 학습. "
    "서울 전역 766K건 배치 추론용."
)


# ---------------------------------------------------------------------------
# 1. DB 학습 데이터 수집
# ---------------------------------------------------------------------------

def fetch_training_data(
    engine: Engine,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """buildings_enriched JOIN energy_results 에서 Tier1/Tier2 행을 가져온다.

    Parameters
    ----------
    engine:
        SQLAlchemy engine 인스턴스.

    Returns
    -------
    tier1_rows:
        data_tier=1 (실측) 행 목록. 각 dict: pnu, usage_type, vintage_class,
        structure_type, built_year, total_area, floors, eui.
    tier2_rows:
        data_tier=2 (KEA 인증) 행 목록. 동일 구조.
    """
    query = text(
        """
        SELECT
            be.pnu,
            be.usage_type,
            be.vintage_class,
            be.structure_type,
            be.built_year,
            be.total_area::float          AS total_area,
            be.floors_above               AS floors,
            er.total_energy               AS eui,
            er.data_tier
        FROM buildings_enriched be
        JOIN energy_results er
            ON be.pnu = er.pnu
           AND er.is_current = TRUE
        WHERE er.data_tier IN (1, 2)
          AND er.total_energy BETWEEN 10 AND 2000
          AND be.total_area > 0
        ORDER BY er.data_tier, be.pnu
        """
    )

    tier1_rows: list[dict[str, Any]] = []
    tier2_rows: list[dict[str, Any]] = []

    with engine.connect() as conn:
        result = conn.execute(query)
        for row in result.mappings():
            record: dict[str, Any] = {
                "pnu": row["pnu"],
                "usage_type": row["usage_type"],
                "vintage_class": row["vintage_class"],
                "structure_type": row["structure_type"],
                "built_year": row["built_year"],
                "total_area": float(row["total_area"]),
                "floors": int(row["floors"]) if row["floors"] else 5,
                "eui": float(row["eui"]),
            }
            if row["data_tier"] == 1:
                tier1_rows.append(record)
            else:
                tier2_rows.append(record)

    logger.info(
        "DB 학습 데이터 수집 완료: Tier1=%d건, Tier2=%d건",
        len(tier1_rows),
        len(tier2_rows),
    )
    return tier1_rows, tier2_rows


# ---------------------------------------------------------------------------
# 2. TrainingDataset 구성
# ---------------------------------------------------------------------------

def build_dataset_from_db(
    tier1_rows: list[dict[str, Any]],
    tier2_rows: list[dict[str, Any]],
    tier1_weight: float = 5.0,
    tier2_weight: float = 1.0,
) -> TrainingDataset:
    """DB 행 목록을 TrainingDataset으로 변환한다.

    fetch_training_data()가 이미 EUI를 최종에너지로 반환하므로
    build_training_dataset()의 1차에너지 환산 로직을 거치지 않고
    직접 TrainingDataset을 구성한다.

    Parameters
    ----------
    tier1_rows:
        Tier1 실측 행 목록 (eui 키 포함).
    tier2_rows:
        Tier2 KEA 인증 행 목록 (eui 키 포함, total_energy 이미 최종 EUI).
    tier1_weight:
        Tier1 샘플 가중치. 기본 5.0.
    tier2_weight:
        Tier2 샘플 가중치. 기본 1.0.

    Returns
    -------
    TrainingDataset
    """
    pipeline = FeaturePipeline()

    all_records = []
    all_eui: list[float] = []
    all_weights: list[float] = []
    all_tiers: list[int] = []
    all_pnus: list[str] = []

    tier1_pnu_set: set[str] = set()

    for row in tier1_rows:
        # _dict_to_building_record은 "floors" 키를 참조하므로 dict에 반드시 포함
        br = _dict_to_building_record(row)
        all_records.append(br)
        all_eui.append(float(row["eui"]))
        all_weights.append(tier1_weight)
        all_tiers.append(1)
        all_pnus.append(row["pnu"])
        tier1_pnu_set.add(row["pnu"])

    for row in tier2_rows:
        if row["pnu"] in tier1_pnu_set:
            # 동일 건물은 Tier1 우선
            continue
        br = _dict_to_building_record(row)
        all_records.append(br)
        all_eui.append(float(row["eui"]))
        all_weights.append(tier2_weight)
        all_tiers.append(2)
        all_pnus.append(row["pnu"])

    X = pipeline.transform(all_records)
    y = np.array(all_eui, dtype=np.float64)
    weights = np.array(all_weights, dtype=np.float64)
    source_tier = np.array(all_tiers, dtype=np.int8)

    logger.info(
        "TrainingDataset 구성 완료: Tier1=%d건, Tier2=%d건 → 총 %d건",
        len(tier1_rows),
        len(all_tiers) - len(tier1_rows),
        len(y),
    )
    return TrainingDataset(
        X=X,
        y=y,
        weights=weights,
        source_tier=source_tier,
        pnu_index=all_pnus,
    )


# ---------------------------------------------------------------------------
# 3. model_registry 조회/생성
# ---------------------------------------------------------------------------

def get_or_create_model_registry(
    conn: Connection,
    model_name: str,
    model_type: str,
    temporal_scale: str,
    target_variable: str,
    description: str,
) -> int:
    """model_registry 테이블에서 레코드를 찾거나 새로 삽입한다.

    UNIQUE 제약 (model_name, model_type, temporal_scale, target_variable) 기준으로
    ON CONFLICT DO UPDATE를 사용하여 upsert한다.

    Returns
    -------
    int
        model_registry.id
    """
    upsert_sql = text(
        """
        INSERT INTO model_registry
            (model_name, model_type, temporal_scale, target_variable, description,
             stage, created_at, updated_at)
        VALUES
            (:model_name, CAST(:model_type AS model_type_enum),
             CAST(:temporal_scale AS temporal_scale_enum),
             CAST(:target_variable AS target_variable_enum),
             :description, 'dev', now(), now())
        ON CONFLICT (model_name, model_type, temporal_scale, target_variable)
        DO UPDATE SET updated_at = now()
        RETURNING id
        """
    )
    result = conn.execute(
        upsert_sql,
        {
            "model_name": model_name,
            "model_type": model_type,
            "temporal_scale": temporal_scale,
            "target_variable": target_variable,
            "description": description,
        },
    )
    row = result.fetchone()
    model_id: int = row[0]
    logger.debug("model_registry id=%d (model_name=%s)", model_id, model_name)
    return model_id


# ---------------------------------------------------------------------------
# 4. model_versions 등록
# ---------------------------------------------------------------------------

def register_model_version(
    conn: Connection,
    model_id: int,
    version_tag: str,
    artifact_path: str,
    metrics: dict[str, float],
    hyperparams: dict[str, Any],
    feature_names: list[str],
    train_rows: int,
    val_rows: int,
) -> int:
    """model_versions 테이블에 새 버전을 삽입하고 version id를 반환한다.

    Parameters
    ----------
    metrics:
        필수 키: val_rmse, val_mape, val_r2, val_mae.
    """
    import json

    insert_sql = text(
        """
        INSERT INTO model_versions
            (model_id, version_tag, training_data_snapshot, val_rmse, val_mape,
             val_r2, val_mae, artifact_path, hyperparams, feature_names,
             train_rows, val_rows, created_at)
        VALUES
            (:model_id, :version_tag, CURRENT_DATE,
             :val_rmse, :val_mape, :val_r2, :val_mae,
             :artifact_path, CAST(:hyperparams AS jsonb), :feature_names,
             :train_rows, :val_rows, now())
        RETURNING id
        """
    )
    result = conn.execute(
        insert_sql,
        {
            "model_id": model_id,
            "version_tag": version_tag,
            "val_rmse": float(metrics["val_rmse"]),
            "val_mape": float(metrics["val_mape"]),
            "val_r2": float(metrics["val_r2"]),
            "val_mae": float(metrics["val_mae"]),
            "artifact_path": artifact_path,
            "hyperparams": json.dumps(hyperparams),
            "feature_names": feature_names,
            "train_rows": train_rows,
            "val_rows": val_rows,
        },
    )
    row = result.fetchone()
    version_id: int = row[0]
    logger.info(
        "model_versions 등록 완료: version_id=%d, version_tag=%s, val_mape=%.2f%%",
        version_id,
        version_tag,
        metrics["val_mape"],
    )
    return version_id


# ---------------------------------------------------------------------------
# 5. 현재 프로덕션 버전 조회
# ---------------------------------------------------------------------------

def get_production_version(engine: Engine) -> dict[str, Any] | None:
    """현재 프로덕션 상태인 model_versions 행을 반환한다.

    Returns
    -------
    dict with keys {version_id, artifact_path, val_mape, model_id} or None
    """
    query = text(
        """
        SELECT
            mv.id            AS version_id,
            mv.artifact_path,
            mv.val_mape,
            mv.model_id
        FROM model_versions mv
        JOIN model_registry mr ON mr.id = mv.model_id
        WHERE mr.model_name = :model_name
          AND mv.promoted_to_production IS NOT NULL
          AND mv.archived_at IS NULL
        ORDER BY mv.promoted_to_production DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        result = conn.execute(query, {"model_name": MODEL_NAME})
        row = result.fetchone()

    if row is None:
        logger.info("현재 프로덕션 버전 없음 (신규 모델)")
        return None

    production_info: dict[str, Any] = {
        "version_id": row[0],
        "artifact_path": row[1],
        "val_mape": float(row[2]),
        "model_id": row[3],
    }
    logger.info(
        "현재 프로덕션 버전: version_id=%d, val_mape=%.2f%%",
        production_info["version_id"],
        production_info["val_mape"],
    )
    return production_info


# ---------------------------------------------------------------------------
# 6. 프로덕션 프로모션
# ---------------------------------------------------------------------------

def promote_version(conn: Connection, new_version_id: int, model_id: int) -> None:
    """새 버전을 프로덕션으로 승격하고 이전 버전을 아카이브한다.

    순서:
    1. 해당 model_id의 현재 프로덕션 버전을 archived_at=now()로 아카이브.
    2. 새 버전의 promoted_to_production=now()로 설정.
    3. model_registry stage를 'production'으로 업데이트.
    """
    archive_sql = text(
        """
        UPDATE model_versions
        SET archived_at = now()
        WHERE model_id = :model_id
          AND promoted_to_production IS NOT NULL
          AND archived_at IS NULL
        """
    )
    conn.execute(archive_sql, {"model_id": model_id})

    promote_sql = text(
        """
        UPDATE model_versions
        SET promoted_to_production = now()
        WHERE id = :version_id
        """
    )
    conn.execute(promote_sql, {"version_id": new_version_id})

    stage_sql = text(
        """
        UPDATE model_registry
        SET stage = 'production', updated_at = now()
        WHERE id = :model_id
        """
    )
    conn.execute(stage_sql, {"model_id": model_id})

    logger.info(
        "프로덕션 프로모션 완료: new_version_id=%d, model_id=%d",
        new_version_id,
        model_id,
    )


# ---------------------------------------------------------------------------
# 7. 핵심 재학습 파이프라인
# ---------------------------------------------------------------------------

def _compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """회귀 지표를 계산한다."""
    residuals = y_pred - y_true
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    mape = float(np.mean(np.abs(residuals) / np.clip(y_true, 1.0, None)) * 100.0)
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 0.0
    mae = float(np.mean(np.abs(residuals)))
    return {
        "val_rmse": rmse,
        "val_mape": mape,
        "val_r2": r2,
        "val_mae": mae,
    }


def retrain_xgboost(
    db_url: str,
    models_dir: str = "/models/xgb",
) -> dict[str, Any]:
    """전체 재학습 파이프라인을 실행한다.

    단계:
    a. DB 연결, Tier1/Tier2 학습 데이터 수집.
    b. TrainingDataset 구성.
    c. Tier1은 항상 훈련셋, Tier2의 20%를 검증셋으로 분리.
    d. XGBoostEUIPredictor 학습 (n_estimators=500, quantile 모델 포함).
    e. 검증셋 지표 계산 (RMSE, MAPE, R², MAE).
    f. 아티팩트를 {models_dir}/xgb_eui_{version_tag}.pkl 에 저장.
    g. model_registry + model_versions 등록.
    h. 프로덕션 버전 없거나 val_mape가 2% 이상 개선되면 프로모션.

    Parameters
    ----------
    db_url:
        PostgreSQL 연결 문자열.
    models_dir:
        아티팩트 저장 디렉토리.

    Returns
    -------
    dict with keys: version_tag, val_rmse, val_mape, val_r2, train_rows,
                    val_rows, promoted, artifact_path
    """
    version_tag = f"v{datetime.now().strftime('%Y%m%d-%H%M')}"
    logger.info("재학습 시작: version_tag=%s", version_tag)

    # --- a. DB 연결 및 데이터 수집 ---
    engine = create_engine(db_url, pool_pre_ping=True)
    tier1_rows, tier2_rows = fetch_training_data(engine)

    if not tier1_rows and not tier2_rows:
        raise RuntimeError("훈련 데이터가 없습니다. DB 데이터를 확인하세요.")

    # --- b. TrainingDataset 구성 ---
    dataset = build_dataset_from_db(
        tier1_rows=tier1_rows,
        tier2_rows=tier2_rows,
        tier1_weight=5.0,
        tier2_weight=1.0,
    )

    # --- c. Train/Val 분리 (Tier1 항상 train, Tier2 80/20) ---
    tier1_mask = dataset.source_tier == 1
    tier2_idx = np.where(~tier1_mask)[0]

    rng = np.random.default_rng(42)
    if len(tier2_idx) >= 5:
        shuffled = rng.permutation(tier2_idx)
        val_size = max(1, int(len(shuffled) * 0.2))
        val_idx = shuffled[:val_size]
        train_tier2_idx = shuffled[val_size:]
    else:
        # Tier2 데이터가 너무 적으면 전체를 훈련에 사용
        val_idx = tier2_idx
        train_tier2_idx = tier2_idx

    train_idx = np.concatenate([np.where(tier1_mask)[0], train_tier2_idx])

    def _subset(ds: TrainingDataset, idx: np.ndarray) -> TrainingDataset:
        return TrainingDataset(
            X=ds.X.iloc[idx].reset_index(drop=True),
            y=ds.y[idx],
            weights=ds.weights[idx],
            source_tier=ds.source_tier[idx],
            pnu_index=[ds.pnu_index[i] for i in idx],
        )

    train_ds = _subset(dataset, train_idx)
    val_ds = _subset(dataset, val_idx)

    logger.info(
        "Train/Val 분리: train=%d건 (Tier1=%d, Tier2=%d), val=%d건",
        len(train_idx),
        int(tier1_mask.sum()),
        len(train_tier2_idx),
        len(val_idx),
    )

    # --- d. 학습 ---
    config = XGBoostConfig(n_estimators=500, fit_quantile_models=True)
    predictor = XGBoostEUIPredictor(config=config)
    predictor.fit(dataset=train_ds)

    # --- e. 검증 지표 계산 ---
    X_val = val_ds.X[FEATURE_NAMES]
    y_val = val_ds.y
    y_pred = predictor.predict(X_val)
    metrics = _compute_metrics(y_val, y_pred)

    logger.info(
        "검증 지표: RMSE=%.2f, MAPE=%.2f%%, R²=%.4f, MAE=%.2f",
        metrics["val_rmse"],
        metrics["val_mape"],
        metrics["val_r2"],
        metrics["val_mae"],
    )

    # --- f. 아티팩트 저장 ---
    artifact_dir = Path(models_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = str(artifact_dir / f"xgb_eui_{version_tag}.pkl")
    predictor.save(artifact_path)
    logger.info("아티팩트 저장 완료: %s", artifact_path)

    # --- g. model_registry + model_versions 등록 ---
    hyperparams = {
        "n_estimators": config.n_estimators,
        "max_depth": config.max_depth,
        "learning_rate": config.learning_rate,
        "subsample": config.subsample,
        "colsample_bytree": config.colsample_bytree,
        "min_child_weight": config.min_child_weight,
        "gamma": config.gamma,
        "reg_alpha": config.reg_alpha,
        "reg_lambda": config.reg_lambda,
        "objective": config.objective,
        "fit_quantile_models": config.fit_quantile_models,
        "tier1_weight": 5.0,
        "tier2_weight": 1.0,
    }

    with engine.begin() as conn:
        model_id = get_or_create_model_registry(
            conn=conn,
            model_name=MODEL_NAME,
            model_type=MODEL_TYPE,
            temporal_scale=TEMPORAL_SCALE,
            target_variable=TARGET_VARIABLE,
            description=MODEL_DESCRIPTION,
        )
        version_id = register_model_version(
            conn=conn,
            model_id=model_id,
            version_tag=version_tag,
            artifact_path=artifact_path,
            metrics=metrics,
            hyperparams=hyperparams,
            feature_names=FEATURE_NAMES,
            train_rows=len(train_idx),
            val_rows=len(val_idx),
        )

    # --- h. 프로모션 판단 ---
    current_prod = get_production_version(engine)
    promoted = False

    should_promote = (
        current_prod is None
        or metrics["val_mape"] < current_prod["val_mape"] * 0.98
    )

    if should_promote:
        with engine.begin() as conn:
            promote_version(
                conn=conn,
                new_version_id=version_id,
                model_id=model_id,
            )
        promoted = True
        if current_prod is None:
            logger.info("최초 프로덕션 버전 프로모션: version_id=%d", version_id)
        else:
            logger.info(
                "프로덕션 업그레이드: %.2f%% → %.2f%% (%.1f%% 개선), version_id=%d",
                current_prod["val_mape"],
                metrics["val_mape"],
                (current_prod["val_mape"] - metrics["val_mape"])
                / current_prod["val_mape"]
                * 100.0,
                version_id,
            )
    else:
        logger.info(
            "프로모션 기준 미달 (현재 %.2f%% vs 신규 %.2f%%). 프로모션 생략.",
            current_prod["val_mape"],  # type: ignore[index]
            metrics["val_mape"],
        )

    result: dict[str, Any] = {
        "version_tag": version_tag,
        "version_id": version_id,
        "val_rmse": metrics["val_rmse"],
        "val_mape": metrics["val_mape"],
        "val_r2": metrics["val_r2"],
        "val_mae": metrics["val_mae"],
        "train_rows": int(len(train_idx)),
        "val_rows": int(len(val_idx)),
        "promoted": promoted,
        "artifact_path": artifact_path,
    }
    logger.info("재학습 완료: %s", result)
    return result


# ---------------------------------------------------------------------------
# 8. Celery Task
# ---------------------------------------------------------------------------

from src.shared.celery_app import celery  # noqa: E402  (순환 import 방지용 지연 임포트)


@celery.task(
    bind=True,
    name="src.simulation.ml_retrain.retrain_xgboost_task",
    time_limit=3600,
    soft_time_limit=3300,
    max_retries=1,
)
def retrain_xgboost_task(self, models_dir: str = "/models/xgb") -> dict[str, Any]:
    """XGBoost EUI 모델 재학습 Celery task.

    DATABASE_URL은 settings에서 읽는다.
    학습 실패 시 최대 1회 재시도한다.
    """
    from src.shared.config import get_settings
    from celery.exceptions import SoftTimeLimitExceeded

    settings = get_settings()

    try:
        result = retrain_xgboost(
            db_url=settings.DATABASE_URL,
            models_dir=models_dir,
        )
        logger.info(
            "retrain_xgboost_task 완료: version_tag=%s, val_mape=%.2f%%, promoted=%s",
            result["version_tag"],
            result["val_mape"],
            result["promoted"],
        )
        return result

    except SoftTimeLimitExceeded:
        logger.error("재학습 soft time limit 초과 (3300s). 작업 중단.")
        raise

    except Exception as exc:
        logger.exception("재학습 실패: %s", exc)
        raise self.retry(exc=exc, countdown=60)
