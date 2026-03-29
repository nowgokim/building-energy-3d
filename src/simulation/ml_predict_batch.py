"""
배치 EUI 예측 파이프라인 (Tier B — XGBoost 프로덕션 모델 적용).

전체 흐름
---------
1. model_versions 테이블에서 프로덕션 버전 조회
2. 아티팩트 pickle 로드
3. buildings_enriched 테이블에서 배치 단위로 건물 페치
4. FeaturePipeline → predict_with_interval() → energy_predictions 벌크 삽입
5. Tier1 실측치 대상으로 actual_eui / 오차 역계산(UPDATE)
6. Celery beat 연동 가능한 태스크 노출

파티션 주의사항
--------------
energy_predictions 테이블은 predicted_at 컬럼으로 파티션된다.
INSERT 시 predicted_at 값을 반드시 포함해야 파티션 라우팅이 정상 동작한다.
executemany 방식(행별 INSERT)은 파티션 경계를 런타임에 안전하게 처리한다.
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.shared.celery_app import celery
from src.shared.config import get_settings
from src.simulation.ml_xgboost import (
    BuildingRecord,
    FeaturePipeline,
    XGBoostEUIPredictor,
    _dict_to_building_record,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_BUILDINGS_QUERY = text(
    """
    SELECT
        pnu,
        usage_type,
        vintage_class,
        structure_type,
        built_year,
        total_area,
        floors_above AS floors
    FROM buildings_enriched
    ORDER BY pnu
    LIMIT :limit OFFSET :offset
    """
)

_COUNT_QUERY = text("SELECT COUNT(*) FROM buildings_enriched")

_INSERT_PREDICTION = text(
    """
    INSERT INTO energy_predictions (
        pnu,
        model_version_id,
        predicted_at,
        temporal_scale,
        horizon_days,
        target_variable,
        predicted_eui,
        confidence_low,
        confidence_high,
        features_used
    ) VALUES (
        :pnu,
        :model_version_id,
        :predicted_at,
        :temporal_scale,
        :horizon_days,
        :target_variable,
        :predicted_eui,
        :confidence_low,
        :confidence_high,
        :features_used
    )
    """
)

_UPDATE_ACTUAL_EUI = text(
    """
    UPDATE energy_predictions ep
    SET
        actual_eui  = er.total_energy,
        error_abs   = ABS(ep.predicted_eui - er.total_energy),
        error_pct   = ABS(ep.predicted_eui - er.total_energy)
                      / NULLIF(er.total_energy, 0) * 100
    FROM energy_results er
    WHERE ep.pnu             = er.pnu
      AND ep.model_version_id = :model_version_id
      AND ep.actual_eui      IS NULL
      AND er.data_tier        = 1
      AND er.is_current       = TRUE
      AND er.total_energy     > 0
    """
)

_ACCURACY_QUERY = text(
    """
    SELECT
        COUNT(*)                    AS n,
        AVG(error_pct)              AS mean_mape,
        SQRT(AVG(error_abs * error_abs)) AS rmse
    FROM energy_predictions
    WHERE model_version_id = :model_version_id
      AND actual_eui IS NOT NULL
    """
)


# ---------------------------------------------------------------------------
# 1. 모델 로드
# ---------------------------------------------------------------------------

def load_model(artifact_path: str) -> XGBoostEUIPredictor:
    """pickle 아티팩트에서 학습 완료된 XGBoostEUIPredictor를 복원한다.

    Parameters
    ----------
    artifact_path:
        pickle 파일 경로 (절대 또는 상대).

    Returns
    -------
    XGBoostEUIPredictor
        즉시 predict() 호출 가능한 상태의 인스턴스.

    Raises
    ------
    FileNotFoundError
        artifact_path 파일이 존재하지 않을 때.
    TypeError
        pickle 내용이 XGBoostEUIPredictor가 아닐 때.
    """
    path = Path(artifact_path)
    if not path.exists():
        raise FileNotFoundError(f"모델 아티팩트 없음: {artifact_path}")

    # predictor.save()는 dict payload로 저장하므로 cls.load()로 복원
    obj = XGBoostEUIPredictor.load(path)
    logger.info("모델 로드 완료: %s", artifact_path)
    return obj


# ---------------------------------------------------------------------------
# 2. 배치 예측 및 energy_predictions 적재
# ---------------------------------------------------------------------------

def batch_predict_buildings(
    engine: Engine,
    model_version_id: int,
    predictor: XGBoostEUIPredictor,
    pipeline: FeaturePipeline,
    batch_size: int = 5000,
    limit: int | None = None,
) -> int:
    """buildings_enriched 전체를 배치 단위로 예측하여 energy_predictions에 삽입한다.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine (PostGIS 연결).
    model_version_id:
        model_versions.id — energy_predictions.model_version_id 외래키 값.
    predictor:
        load_model()로 복원된 XGBoostEUIPredictor.
    pipeline:
        FeaturePipeline 인스턴스.
    batch_size:
        한 번에 처리할 건물 수.
    limit:
        전체 처리 건물 수 상한. None이면 전체 처리.

    Returns
    -------
    int
        실제로 삽입된 행 수.
    """
    predicted_at = datetime.now(tz=timezone.utc)

    with engine.connect() as conn:
        total_in_db: int = conn.execute(_COUNT_QUERY).scalar_one()

    effective_total = min(total_in_db, limit) if limit is not None else total_in_db
    num_batches = math.ceil(effective_total / batch_size)

    logger.info(
        "배치 예측 시작: 대상=%d건, batch_size=%d, 배치수=%d",
        effective_total,
        batch_size,
        num_batches,
    )

    total_inserted = 0

    for batch_idx in range(num_batches):
        offset = batch_idx * batch_size
        current_limit = min(batch_size, effective_total - offset)

        with engine.connect() as conn:
            rows = conn.execute(
                _BUILDINGS_QUERY,
                {"limit": current_limit, "offset": offset},
            ).mappings().all()

        if not rows:
            break

        records: list[BuildingRecord] = []
        valid_rows: list[Any] = []
        for row in rows:
            try:
                br = _dict_to_building_record(dict(row))
                records.append(br)
                valid_rows.append(row)
            except Exception as exc:
                logger.warning("BuildingRecord 변환 실패 pnu=%s: %s", row.get("pnu"), exc)

        if not records:
            logger.warning("배치 %d: 유효 레코드 없음, 건너뜀", batch_idx)
            continue

        try:
            X: pd.DataFrame = pipeline.transform(records)
            means, lowers, uppers = predictor.predict_with_interval(X)
        except Exception as exc:
            logger.error("배치 %d 예측 실패: %s", batch_idx, exc)
            continue

        insert_params: list[dict[str, Any]] = []
        for i, row in enumerate(valid_rows):
            pnu: str = row["pnu"] or ""
            predicted_eui = float(means[i]) if not math.isnan(means[i]) else None
            confidence_low = float(lowers[i]) if not math.isnan(lowers[i]) else None
            confidence_high = float(uppers[i]) if not math.isnan(uppers[i]) else None

            features_snapshot: dict[str, float] = {
                col: float(X.iloc[i][col]) for col in X.columns
            }

            insert_params.append(
                {
                    "pnu": pnu,
                    "model_version_id": model_version_id,
                    "predicted_at": predicted_at,
                    "temporal_scale": "annual",
                    "horizon_days": 365,
                    "target_variable": "eui",
                    "predicted_eui": predicted_eui,
                    "confidence_low": confidence_low,
                    "confidence_high": confidence_high,
                    "features_used": json.dumps(features_snapshot),
                }
            )

        with engine.begin() as conn:
            conn.execute(_INSERT_PREDICTION, insert_params)

        batch_count = len(insert_params)
        total_inserted += batch_count
        logger.info(
            "배치 %d/%d 완료: +%d건 삽입 (누계 %d건)",
            batch_idx + 1,
            num_batches,
            batch_count,
            total_inserted,
        )

    logger.info("배치 예측 완료: 총 %d건 삽입", total_inserted)
    return total_inserted


# ---------------------------------------------------------------------------
# 3. Tier1 정확도 평가
# ---------------------------------------------------------------------------

def evaluate_tier1_accuracy(engine: Engine, model_version_id: int) -> dict[str, Any]:
    """energy_results(data_tier=1)과 energy_predictions를 조인하여 오차 지표를 계산한다.

    실행 순서
    ---------
    1. energy_predictions.actual_eui 가 NULL인 행 중 Tier1 실측치가 있는 건물 UPDATE.
    2. 해당 model_version_id의 전체 정확도(MAPE, RMSE) 집계.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine.
    model_version_id:
        평가 대상 모델 버전 ID.

    Returns
    -------
    dict with keys: updated_count (int), mean_mape (float | None), mean_rmse (float | None)
    """
    with engine.begin() as conn:
        result = conn.execute(
            _UPDATE_ACTUAL_EUI, {"model_version_id": model_version_id}
        )
        updated_count: int = result.rowcount

    logger.info(
        "Tier1 actual_eui 업데이트 완료: model_version_id=%d, %d건",
        model_version_id,
        updated_count,
    )

    with engine.connect() as conn:
        acc_row = conn.execute(
            _ACCURACY_QUERY, {"model_version_id": model_version_id}
        ).mappings().one_or_none()

    mean_mape: float | None = None
    mean_rmse: float | None = None

    if acc_row and acc_row["n"] and acc_row["n"] > 0:
        mean_mape = float(acc_row["mean_mape"]) if acc_row["mean_mape"] is not None else None
        mean_rmse = float(acc_row["rmse"]) if acc_row["rmse"] is not None else None
        logger.info(
            "Tier1 정확도: n=%d, MAPE=%.2f%%, RMSE=%.2f kWh/m²·년",
            acc_row["n"],
            mean_mape or 0.0,
            mean_rmse or 0.0,
        )
    else:
        logger.warning("Tier1 정확도 집계 대상 없음 (model_version_id=%d)", model_version_id)

    return {
        "updated_count": updated_count,
        "mean_mape": mean_mape,
        "mean_rmse": mean_rmse,
    }


# ---------------------------------------------------------------------------
# 4. 전체 파이프라인 진입점
# ---------------------------------------------------------------------------

def _get_production_version(engine: Engine) -> dict[str, Any] | None:
    """model_versions 테이블에서 프로덕션 버전을 조회한다.

    ml_retrain 모듈이 존재하면 그 함수를 사용하고,
    없으면 직접 SQL로 폴백하여 외부 의존성 결합도를 낮춘다.
    """
    try:
        from src.simulation.ml_retrain import get_production_version  # type: ignore
        return get_production_version(engine)
    except ImportError:
        logger.debug("ml_retrain 모듈 미존재 — SQL 직접 조회")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    mv.id          AS version_id,
                    mv.artifact_path,
                    mv.val_mape,
                    mv.model_id,
                    mv.version_tag
                FROM model_versions mv
                JOIN model_registry mr ON mr.id = mv.model_id
                WHERE mv.is_production = TRUE
                ORDER BY mv.created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one_or_none()

    if row is None:
        return None

    return dict(row)


def run_batch_predict(
    db_url: str,
    models_dir: str = "/models/xgb",
    limit: int | None = None,
) -> dict[str, Any]:
    """전체 배치 예측 파이프라인을 실행하고 결과 요약을 반환한다.

    Parameters
    ----------
    db_url:
        SQLAlchemy 호환 PostgreSQL 연결 문자열.
    models_dir:
        아티팩트 기본 디렉토리 (artifact_path가 상대 경로일 때 prefix로 사용).
    limit:
        예측 대상 건물 수 상한. None이면 전체.

    Returns
    -------
    dict with keys:
        - version_id / version_tag: 사용된 모델 버전
        - predictions_inserted: 삽입된 예측 행 수
        - tier1_evaluated: Tier1 업데이트 건수
        - mean_mape: 평균 MAPE (%)
        - mean_rmse: 평균 RMSE (kWh/m²·년)
        - error: 오류 메시지 (정상 실행 시 키 없음)
    """
    engine = create_engine(db_url, pool_pre_ping=True)

    # (a) 프로덕션 버전 조회
    prod = _get_production_version(engine)
    if prod is None:
        logger.warning("프로덕션 모델 버전 없음 — 배치 예측 건너뜀")
        return {"error": "no production model"}

    version_id: int = prod["version_id"]
    artifact_path: str = prod["artifact_path"]
    version_tag: str | None = prod.get("version_tag")

    # artifact_path 가 상대 경로이면 models_dir 로 보완
    if not Path(artifact_path).is_absolute():
        artifact_path = str(Path(models_dir) / artifact_path)

    logger.info(
        "프로덕션 모델: version_id=%d, tag=%s, path=%s",
        version_id,
        version_tag,
        artifact_path,
    )

    # (b) 모델 로드
    try:
        predictor = load_model(artifact_path)
    except (FileNotFoundError, TypeError) as exc:
        logger.error("모델 로드 실패: %s", exc)
        return {"error": str(exc)}

    pipeline = FeaturePipeline()

    # (c) 배치 예측
    predictions_inserted = batch_predict_buildings(
        engine=engine,
        model_version_id=version_id,
        predictor=predictor,
        pipeline=pipeline,
        batch_size=5000,
        limit=limit,
    )

    # (d) Tier1 정확도 평가
    acc = evaluate_tier1_accuracy(engine=engine, model_version_id=version_id)

    result: dict[str, Any] = {
        "version_id": version_id,
        "predictions_inserted": predictions_inserted,
        "tier1_evaluated": acc["updated_count"],
        "mean_mape": acc["mean_mape"],
        "mean_rmse": acc["mean_rmse"],
    }
    if version_tag:
        result["version_tag"] = version_tag

    logger.info("배치 예측 파이프라인 완료: %s", result)
    return result


# ---------------------------------------------------------------------------
# 5. Celery 태스크
# ---------------------------------------------------------------------------

@celery.task(
    bind=True,
    name="src.simulation.ml_predict_batch.predict_batch_task",
    time_limit=7200,
    soft_time_limit=6900,
    max_retries=0,
)
def predict_batch_task(self, limit: int | None = None) -> dict[str, Any]:
    """배치 EUI 예측 Celery 태스크.

    Celery beat 또는 수동 트리거로 실행된다.
    소프트 타임 리밋(6900s) 도달 시 진행 중인 배치를 완료한 후 종료한다.

    Parameters
    ----------
    limit:
        예측 대상 건물 수 상한. None이면 전체 (약 766K건, ~90분 소요).

    Returns
    -------
    dict
        run_batch_predict() 반환값과 동일.
    """
    settings = get_settings()
    db_url: str = settings.DATABASE_URL

    logger.info(
        "predict_batch_task 시작: task_id=%s, limit=%s",
        self.request.id,
        limit,
    )

    result = run_batch_predict(db_url=db_url, limit=limit)

    logger.info("predict_batch_task 완료: %s", result)
    return result
