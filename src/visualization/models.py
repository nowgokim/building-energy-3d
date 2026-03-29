"""
ML 모델 레지스트리 및 정확도 API 라우터.

model_registry / model_versions / energy_predictions / model_accuracy_summary MV를
조회하고, 재학습·배치 예측 Celery 태스크를 트리거한다.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.database import get_db_dependency
from src.shared.limiter import limiter
from src.shared.utils import optional_float as _float

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/models", tags=["models"])


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@router.get("/accuracy")
def get_model_accuracy(
    model_type: Optional[str] = Query(None, description="xgboost / lstm 등"),
    temporal_scale: Optional[str] = Query(None, description="annual / monthly / daily"),
    stage: Optional[str] = Query(None, description="production / staging / archived"),
    db: Session = Depends(get_db_dependency),
):
    """
    model_accuracy_summary MV에서 모델 정확도 목록을 반환한다.

    선택적 필터(model_type, temporal_scale, stage)를 적용하며
    mape 오름차순으로 정렬한다.
    """
    conditions = ["1=1"]
    params: dict = {}

    if model_type:
        conditions.append("model_type = :model_type")
        params["model_type"] = model_type
    if temporal_scale:
        conditions.append("temporal_scale = :temporal_scale")
        params["temporal_scale"] = temporal_scale
    if stage:
        conditions.append("stage = :stage")
        params["stage"] = stage

    where_clause = " AND ".join(conditions)

    sql = text(f"""
        SELECT
            model_version_id,
            model_name,
            model_type,
            version_tag,
            stage,
            temporal_scale,
            target_variable,
            prediction_count,
            evaluated_count,
            rmse,
            mape,
            r_squared,
            avg_predicted,
            avg_actual,
            max_error_abs,
            first_prediction_at,
            last_prediction_at,
            refreshed_at
        FROM model_accuracy_summary
        WHERE {where_clause}
        ORDER BY mape ASC NULLS LAST
    """)

    rows = db.execute(sql, params).fetchall()

    models = [
        {
            "model_version_id": row.model_version_id,
            "model_name": row.model_name,
            "model_type": row.model_type,
            "version_tag": row.version_tag,
            "stage": row.stage,
            "temporal_scale": row.temporal_scale,
            "target_variable": row.target_variable,
            "prediction_count": row.prediction_count,
            "evaluated_count": row.evaluated_count,
            "rmse": _float(row.rmse),
            "mape": _float(row.mape),
            "r_squared": _float(row.r_squared),
            "avg_predicted": _float(row.avg_predicted),
            "avg_actual": _float(row.avg_actual),
            "max_error_abs": _float(row.max_error_abs),
            "first_prediction_at": row.first_prediction_at.isoformat() if row.first_prediction_at else None,
            "last_prediction_at": row.last_prediction_at.isoformat() if row.last_prediction_at else None,
            "refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None,
        }
        for row in rows
    ]

    refreshed_at = models[0]["refreshed_at"] if models else None

    return {"models": models, "refreshed_at": refreshed_at}


@router.get("/list")
def list_models(db: Session = Depends(get_db_dependency)):
    """
    model_registry를 조회하고 각 모델의 최신 버전 정보를 LEFT JOIN으로 붙여 반환한다.

    최신 버전은 model_versions.created_at DESC 기준으로 선택한다.
    """
    sql = text("""
        SELECT
            r.id                        AS model_id,
            r.model_name,
            r.model_type,
            r.temporal_scale,
            r.target_variable,
            r.stage,
            r.description,
            r.created_at,
            lv.version_tag              AS latest_version_tag,
            lv.val_mape                 AS latest_val_mape,
            lv.val_rmse                 AS latest_val_rmse,
            lv.promoted_to_production
        FROM model_registry r
        LEFT JOIN LATERAL (
            SELECT version_tag, val_mape, val_rmse, promoted_to_production
            FROM model_versions
            WHERE model_id = r.id
            ORDER BY created_at DESC
            LIMIT 1
        ) lv ON TRUE
        ORDER BY r.created_at DESC
    """)

    rows = db.execute(sql).fetchall()

    return [
        {
            "model_id": row.model_id,
            "model_name": row.model_name,
            "model_type": row.model_type,
            "temporal_scale": row.temporal_scale,
            "target_variable": row.target_variable,
            "stage": row.stage,
            "description": row.description,
            "latest_version_tag": row.latest_version_tag,
            "latest_val_mape": _float(row.latest_val_mape),
            "latest_val_rmse": _float(row.latest_val_rmse),
            "promoted_to_production": (
                row.promoted_to_production.isoformat()
                if row.promoted_to_production else None
            ),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.get("/production")
def get_production_model(db: Session = Depends(get_db_dependency)):
    """
    현재 운영 중인 XGBoost EUI 모델 정보를 반환한다.

    model_name='xgb_eui_annual_global', promoted_to_production IS NOT NULL,
    archived_at IS NULL 조건에서 가장 최근에 승격된 버전을 반환한다.
    """
    sql = text("""
        SELECT
            mv.id                   AS version_id,
            mv.version_tag,
            mv.val_rmse,
            mv.val_mape,
            mv.val_r2,
            mv.train_rows,
            mv.val_rows,
            mv.promoted_to_production,
            mv.feature_names,
            mv.artifact_path
        FROM model_registry r
        JOIN model_versions mv ON mv.model_id = r.id
        WHERE r.model_name = 'xgb_eui_annual_global'
          AND mv.promoted_to_production IS NOT NULL
          AND mv.archived_at IS NULL
        ORDER BY mv.promoted_to_production DESC
        LIMIT 1
    """)

    row = db.execute(sql).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="운영 중인 xgb_eui_annual_global 모델 버전이 없습니다.",
        )

    return {
        "version_id": row.version_id,
        "version_tag": row.version_tag,
        "val_rmse": _float(row.val_rmse),
        "val_mape": _float(row.val_mape),
        "val_r2": _float(row.val_r2),
        "train_rows": row.train_rows,
        "val_rows": row.val_rows,
        "promoted_to_production": (
            row.promoted_to_production.isoformat()
            if row.promoted_to_production else None
        ),
        "feature_names": row.feature_names,
        "artifact_path": row.artifact_path,
    }


@router.get("/accuracy/building/{pnu}")
def get_building_prediction_history(
    pnu: str,
    db: Session = Depends(get_db_dependency),
):
    """
    특정 건물(PNU)의 에너지 예측 이력을 최대 90건 반환한다.

    predicted_at DESC 정렬이므로 최근 예측이 먼저 나온다.
    """
    sql = text("""
        SELECT
            predicted_at,
            temporal_scale,
            horizon_days,
            predicted_eui,
            actual_eui,
            error_pct,
            confidence_low,
            confidence_high,
            model_version_id
        FROM energy_predictions
        WHERE pnu = :pnu
        ORDER BY predicted_at DESC
        LIMIT 90
    """)

    rows = db.execute(sql, {"pnu": pnu}).fetchall()

    return [
        {
            "predicted_at": row.predicted_at.isoformat() if row.predicted_at else None,
            "temporal_scale": row.temporal_scale,
            "horizon_days": row.horizon_days,
            "predicted_eui": _float(row.predicted_eui),
            "actual_eui": _float(row.actual_eui),
            "error_pct": _float(row.error_pct),
            "confidence_low": _float(row.confidence_low),
            "confidence_high": _float(row.confidence_high),
            "model_version_id": row.model_version_id,
        }
        for row in rows
    ]


@router.post("/retrain")
@limiter.limit("3/minute")  # 재학습은 GPU/CPU 집약적 — 엄격히 제한
def trigger_retrain(request: Request):
    """
    XGBoost EUI 재학습 Celery 태스크를 큐에 등록한다.

    태스크 ID와 상태 메시지를 즉시 반환하며, 실제 학습은 비동기로 진행된다.
    """
    from src.simulation.ml_retrain import retrain_xgboost_task  # noqa: PLC0415

    result = retrain_xgboost_task.delay()
    logger.info("retrain_xgboost_task 등록: task_id=%s", result.id)

    return {
        "task_id": result.id,
        "message": "XGBoost EUI 재학습 태스크가 큐에 등록되었습니다.",
    }


@router.post("/predict-batch")
def trigger_predict_batch():
    """
    전체 건물 배치 예측 Celery 태스크를 큐에 등록한다.

    태스크 ID와 상태 메시지를 즉시 반환하며, 실제 예측은 비동기로 진행된다.
    """
    from src.simulation.ml_predict_batch import predict_batch_task  # noqa: PLC0415

    result = predict_batch_task.delay()
    logger.info("predict_batch_task 등록: task_id=%s", result.id)

    return {
        "task_id": result.id,
        "message": "배치 예측 태스크가 큐에 등록되었습니다.",
    }
