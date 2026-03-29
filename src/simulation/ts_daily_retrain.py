"""
Tier C 시계열 모델 일별 재학습 파이프라인.

대상
----
monitored_buildings + metered_readings_daily 테이블에 충분한 실계량 이력이
있는 건물만 처리한다 (MIN_TRAINING_DAYS 이상의 유효 측정일 필요).

처리 흐름
---------
1. get_monitored_buildings_with_data(): 충분한 이력을 보유한 건물 목록 조회
2. fetch_daily_readings(): 건물별 일별 전기·가스 소비량 집계
3. train_ts_model_for_building(): LagXGBoostPredictor 학습 + model_versions 등록
4. predict_and_store_ts(): 30일 예측 후 energy_predictions 삽입

Celery beat 연동
----------------
ts_daily_retrain_task 를 매일 00:30 UTC에 실행하도록 celery beat schedule에 등록.
time_limit=7200 (건물당 최대 120초 × 최대 60동 여유).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

MIN_TRAINING_DAYS: int = 180
"""학습에 필요한 최소 유효 측정일 수."""

MIN_COVERAGE_PCT: float = 0.5
"""일별 데이터 유효 기준: 계량기 커버리지 50% 이상인 날만 유효 일수로 계산."""


# ---------------------------------------------------------------------------
# DB 헬퍼: model_registry / model_versions 조작
# ---------------------------------------------------------------------------

def get_or_create_model_registry(
    conn: Any,
    model_name: str,
    model_type: str,
    temporal_scale: str,
    target_variable: str,
    description: str = "",
) -> int:
    """model_registry에서 행을 찾거나 새로 삽입하고 id를 반환한다.

    Parameters
    ----------
    conn:
        SQLAlchemy Connection (with engine.connect() as conn 블록 내에서 사용).
    model_name:
        모델 식별자. 예: "ts_lag_xgb_1101010100000001"
    model_type:
        model_type_enum 값. 현재 'xgboost' | 'lightgbm' | 'lstm' | 'transformer' | 'patchtst'.
    temporal_scale:
        temporal_scale_enum 값. 'annual' | 'daily' | 'hourly'.
    target_variable:
        target_variable_enum 값. 'eui' | 'elec_kwh' | 'gas_kwh'.
    description:
        자유 기술 텍스트 (선택).

    Returns
    -------
    int
        model_registry.id
    """
    row = conn.execute(
        text(
            """
            SELECT id FROM model_registry
            WHERE model_name = :model_name
              AND model_type = :model_type::model_type_enum
              AND temporal_scale = :temporal_scale::temporal_scale_enum
              AND target_variable = :target_variable::target_variable_enum
            """
        ),
        {
            "model_name": model_name,
            "model_type": model_type,
            "temporal_scale": temporal_scale,
            "target_variable": target_variable,
        },
    ).fetchone()

    if row is not None:
        return int(row[0])

    new_row = conn.execute(
        text(
            """
            INSERT INTO model_registry
                (model_name, model_type, temporal_scale, target_variable, description, stage)
            VALUES
                (:model_name, :model_type::model_type_enum,
                 :temporal_scale::temporal_scale_enum,
                 :target_variable::target_variable_enum,
                 :description, 'dev')
            RETURNING id
            """
        ),
        {
            "model_name": model_name,
            "model_type": model_type,
            "temporal_scale": temporal_scale,
            "target_variable": target_variable,
            "description": description,
        },
    ).fetchone()

    conn.commit()
    logger.debug("model_registry 신규 등록: model_name=%s id=%d", model_name, new_row[0])
    return int(new_row[0])


def register_model_version(
    conn: Any,
    model_id: int,
    version_tag: str,
    artifact_path: str,
    metrics: dict[str, float],
    hyperparams: dict[str, Any],
    feature_names: list[str],
    train_rows: int,
    val_rows: int,
) -> int:
    """model_versions에 새 버전 행을 삽입하고 id를 반환한다.

    동일 (model_id, version_tag)이 이미 존재하면 artifact_path와
    metrics 컬럼을 UPDATE하고 기존 id를 반환한다.

    Parameters
    ----------
    conn:
        SQLAlchemy Connection.
    model_id:
        model_registry.id.
    version_tag:
        버전 문자열. 예: "20260329-143000"
    artifact_path:
        모델 파일 절대경로. 예: "/models/ts/1101.../ts_daily_v20260329.pkl"
    metrics:
        검증 지표 딕셔너리. 예: {"val_rmse": 12.5, "val_mape": 8.2, "val_r2": 0.87, "val_mae": 9.1}
    hyperparams:
        학습 하이퍼파라미터.
    feature_names:
        입력 피처 이름 목록.
    train_rows:
        학습 데이터 행 수.
    val_rows:
        검증 데이터 행 수 (현재 0 허용).

    Returns
    -------
    int
        model_versions.id
    """
    existing = conn.execute(
        text(
            "SELECT id FROM model_versions WHERE model_id = :model_id AND version_tag = :version_tag"
        ),
        {"model_id": model_id, "version_tag": version_tag},
    ).fetchone()

    if existing is not None:
        conn.execute(
            text(
                """
                UPDATE model_versions
                SET artifact_path = :artifact_path,
                    val_rmse = :val_rmse,
                    val_mape = :val_mape,
                    val_r2 = :val_r2,
                    val_mae = :val_mae,
                    train_rows = :train_rows,
                    val_rows = :val_rows
                WHERE id = :id
                """
            ),
            {
                "artifact_path": artifact_path,
                "val_rmse": metrics.get("val_rmse"),
                "val_mape": metrics.get("val_mape"),
                "val_r2": metrics.get("val_r2"),
                "val_mae": metrics.get("val_mae"),
                "train_rows": train_rows,
                "val_rows": val_rows,
                "id": existing[0],
            },
        )
        conn.commit()
        return int(existing[0])

    new_row = conn.execute(
        text(
            """
            INSERT INTO model_versions
                (model_id, version_tag, artifact_path,
                 val_rmse, val_mape, val_r2, val_mae,
                 hyperparams, feature_names, train_rows, val_rows,
                 training_data_snapshot)
            VALUES
                (:model_id, :version_tag, :artifact_path,
                 :val_rmse, :val_mape, :val_r2, :val_mae,
                 :hyperparams::jsonb, :feature_names, :train_rows, :val_rows,
                 CURRENT_DATE)
            RETURNING id
            """
        ),
        {
            "model_id": model_id,
            "version_tag": version_tag,
            "artifact_path": artifact_path,
            "val_rmse": metrics.get("val_rmse"),
            "val_mape": metrics.get("val_mape"),
            "val_r2": metrics.get("val_r2"),
            "val_mae": metrics.get("val_mae"),
            "hyperparams": json.dumps(hyperparams, ensure_ascii=False),
            "feature_names": feature_names,
            "train_rows": train_rows,
            "val_rows": val_rows,
        },
    ).fetchone()

    conn.commit()
    logger.debug(
        "model_versions 등록: model_id=%d version_tag=%s id=%d",
        model_id, version_tag, new_row[0],
    )
    return int(new_row[0])


# ---------------------------------------------------------------------------
# 데이터 조회
# ---------------------------------------------------------------------------

def fetch_daily_readings(
    engine: Engine,
    ts_id: int,
    days: int = 365,
) -> list[dict]:
    """metered_readings_daily에서 ts_id의 최근 `days`일 일별 소비량을 집계한다.

    전기와 가스를 일별로 합산하여 total_kwh를 계산한다.
    coverage_pct는 해당 일자의 계량기 전체 평균을 반환한다.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine.
    ts_id:
        monitored_buildings.ts_id.
    days:
        조회 기간 (일). 기본 365일.

    Returns
    -------
    list of dict with keys: day (date), elec_kwh, gas_kwh, total_kwh, coverage
    """
    sql = text(
        """
        SELECT
            day,
            SUM(CASE WHEN meter_type = 'electricity' THEN total_kwh ELSE 0 END) AS elec_kwh,
            SUM(CASE WHEN meter_type = 'gas'         THEN total_kwh ELSE 0 END) AS gas_kwh,
            SUM(total_kwh)                                                       AS total_kwh,
            AVG(coverage_pct)                                                    AS coverage
        FROM metered_readings_daily
        WHERE ts_id = :ts_id
          AND day >= CURRENT_DATE - :days
        GROUP BY day
        ORDER BY day
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"ts_id": ts_id, "days": days}).fetchall()

    result: list[dict] = []
    for row in rows:
        result.append(
            {
                "day": row[0],
                "elec_kwh": float(row[1]) if row[1] is not None else 0.0,
                "gas_kwh": float(row[2]) if row[2] is not None else 0.0,
                "total_kwh": float(row[3]) if row[3] is not None else 0.0,
                "coverage": float(row[4]) if row[4] is not None else 0.0,
            }
        )
    return result


def get_monitored_buildings_with_data(engine: Engine) -> list[dict]:
    """실계량 이력이 MIN_TRAINING_DAYS 이상인 monitored_buildings를 반환한다.

    유효 측정일 기준: metered_readings_daily.coverage_pct >= MIN_COVERAGE_PCT.

    Returns
    -------
    list of dict with keys: ts_id, pnu, alias, total_area, usage_type
    """
    sql = text(
        """
        SELECT
            mb.ts_id,
            mb.pnu,
            mb.alias,
            mb.total_area,
            mb.usage_type
        FROM monitored_buildings mb
        WHERE (
            SELECT COUNT(DISTINCT mrd.day)
            FROM metered_readings_daily mrd
            WHERE mrd.ts_id = mb.ts_id
              AND mrd.coverage_pct >= :min_coverage
        ) >= :min_days
        ORDER BY mb.ts_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {"min_coverage": MIN_COVERAGE_PCT, "min_days": MIN_TRAINING_DAYS},
        ).fetchall()

    return [
        {
            "ts_id": row[0],
            "pnu": row[1],
            "alias": row[2],
            "total_area": float(row[3]) if row[3] is not None else 1.0,
            "usage_type": row[4] or "unknown",
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 학습
# ---------------------------------------------------------------------------

def train_ts_model_for_building(
    engine: Engine,
    ts_id: int,
    pnu: str,
    total_area: float,
    usage_type: str,
    models_dir: str = "/models/ts",
) -> dict | None:
    """단일 건물에 대해 LagXGBoostPredictor를 학습하고 model_versions에 등록한다.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine.
    ts_id:
        monitored_buildings.ts_id.
    pnu:
        건물 PNU (19자리).
    total_area:
        연면적 m².
    usage_type:
        영문 archetype key.
    models_dir:
        아티팩트 저장 루트 디렉토리.

    Returns
    -------
    dict | None
        성공 시 {ts_id, pnu, version_id, artifact_path, train_days},
        유효 데이터 부족 시 None.
    """
    from src.simulation.ml_timeseries import (
        BuildingTimeSeries,
        DailyConsumption,
        LagXGBoostPredictor,
    )

    readings = fetch_daily_readings(engine, ts_id, days=365)

    valid_readings = [r for r in readings if r["coverage"] >= MIN_COVERAGE_PCT]
    if len(valid_readings) < MIN_TRAINING_DAYS:
        logger.warning(
            "[ts_id=%d pnu=%s] 유효 측정일 부족: %d일 < %d일 (학습 건너뜀)",
            ts_id, pnu, len(valid_readings), MIN_TRAINING_DAYS,
        )
        return None

    records = [
        DailyConsumption(
            consumption_date=r["day"],
            elec_kwh=r["elec_kwh"],
            gas_kwh=r["gas_kwh"],
            total_kwh=r["total_kwh"],
        )
        for r in valid_readings
    ]

    ts = BuildingTimeSeries(
        pnu=pnu,
        floor_area=max(total_area, 1.0),
        usage_type=usage_type,
        records=records,
    )

    predictor = LagXGBoostPredictor()
    try:
        predictor.fit(ts)
    except (ValueError, RuntimeError) as exc:
        logger.warning("[pnu=%s] 학습 실패: %s", pnu, exc)
        return None

    version_tag = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_path = str(
        Path(models_dir) / pnu / f"ts_daily_v{version_tag}.pkl"
    )

    try:
        predictor.save(artifact_path)
    except OSError as exc:
        logger.error("[pnu=%s] 아티팩트 저장 실패: %s", pnu, exc)
        return None

    model_name = f"ts_lag_xgb_{pnu}"
    hyperparams: dict[str, Any] = {
        "n_estimators": predictor._config.n_estimators,
        "max_depth": predictor._config.max_depth,
        "learning_rate": predictor._config.learning_rate,
        "subsample": predictor._config.subsample,
        "colsample_bytree": predictor._config.colsample_bytree,
        "min_child_weight": predictor._config.min_child_weight,
        "reg_alpha": predictor._config.reg_alpha,
        "reg_lambda": predictor._config.reg_lambda,
        "random_state": predictor._config.random_state,
    }
    feature_names: list[str] = predictor._feature_cols
    metrics: dict[str, float] = {
        "val_rmse": float(predictor._residual_p90),
        "val_mape": 0.0,
        "val_r2": 0.0,
        "val_mae": 0.0,
    }

    with engine.connect() as conn:
        model_id = get_or_create_model_registry(
            conn,
            model_name=model_name,
            model_type="xgboost",
            temporal_scale="daily",
            target_variable="elec_kwh",
            description=f"Tier C 일별 LagXGBoost 예측 모델 — pnu={pnu}",
        )
        version_id = register_model_version(
            conn,
            model_id=model_id,
            version_tag=version_tag,
            artifact_path=artifact_path,
            metrics=metrics,
            hyperparams=hyperparams,
            feature_names=feature_names,
            train_rows=len(valid_readings),
            val_rows=0,
        )

    logger.info(
        "[pnu=%s] 학습 완료: train_days=%d version_id=%d artifact=%s",
        pnu, len(valid_readings), version_id, artifact_path,
    )

    return {
        "ts_id": ts_id,
        "pnu": pnu,
        "version_id": version_id,
        "artifact_path": artifact_path,
        "train_days": len(valid_readings),
    }


# ---------------------------------------------------------------------------
# 예측 및 저장
# ---------------------------------------------------------------------------

def predict_and_store_ts(
    engine: Engine,
    ts_id: int,
    pnu: str,
    total_area: float,
    usage_type: str,
    model_version_id: int,
    artifact_path: str,
    horizon_days: int = 30,
) -> int:
    """저장된 모델을 로드하여 horizon_days 일 예측 후 energy_predictions에 삽입한다.

    각 예측 스텝(1~horizon_days)을 1행씩 삽입한다.
    predicted_eui 컬럼에는 kWh/day 값을 저장하며, eui_pred (kWh/m²·일) 컬럼은
    energy_predictions 스키마의 predicted_eui에 매핑된다.

    Parameters
    ----------
    engine:
        SQLAlchemy Engine.
    ts_id:
        monitored_buildings.ts_id (맥락 이력 조회에 사용).
    pnu:
        건물 PNU.
    total_area:
        연면적 m².
    usage_type:
        영문 archetype key.
    model_version_id:
        model_versions.id.
    artifact_path:
        모델 pickle 파일 절대경로.
    horizon_days:
        예측 기간 (일). 기본 30일.

    Returns
    -------
    int
        삽입된 energy_predictions 행 수.
    """
    from src.simulation.ml_timeseries import (
        BuildingTimeSeries,
        DailyConsumption,
        LagXGBoostPredictor,
    )

    try:
        predictor = LagXGBoostPredictor.load(artifact_path)
    except (OSError, KeyError, Exception) as exc:
        logger.error("[pnu=%s] 모델 로드 실패 (%s): %s", pnu, artifact_path, exc)
        return 0

    context_readings = fetch_daily_readings(engine, ts_id, days=60)
    valid_context = [r for r in context_readings if r["coverage"] >= MIN_COVERAGE_PCT]

    if not valid_context:
        logger.warning("[pnu=%s] 예측 컨텍스트 데이터 없음 (최근 60일)", pnu)
        return 0

    records = [
        DailyConsumption(
            consumption_date=r["day"],
            elec_kwh=r["elec_kwh"],
            gas_kwh=r["gas_kwh"],
            total_kwh=r["total_kwh"],
        )
        for r in valid_context
    ]

    ts = BuildingTimeSeries(
        pnu=pnu,
        floor_area=max(total_area, 1.0),
        usage_type=usage_type,
        records=records,
    )

    # PerBuildingModelRegistry를 통하지 않고 이미 로드한 predictor를 직접 사용한다.
    # predict_timeseries는 model_registry=None이면 ts 이력으로 재학습하므로
    # 로드된 predictor의 predict_daily_eui를 직접 호출한다.
    try:
        pred_df = predictor.predict_daily_eui(ts, horizon_days=horizon_days)
    except (ValueError, RuntimeError) as exc:
        logger.warning("[pnu=%s] 예측 실패: %s", pnu, exc)
        return 0

    if pred_df.empty:
        logger.warning("[pnu=%s] 예측 결과 없음", pnu)
        return 0

    now_utc = datetime.now(tz=timezone.utc)

    insert_sql = text(
        """
        INSERT INTO energy_predictions
            (pnu, model_version_id, predicted_at, temporal_scale, horizon_days,
             target_variable, predicted_eui, confidence_low, confidence_high,
             actual_eui, error_abs, error_pct, features_used)
        VALUES
            (:pnu, :model_version_id, :predicted_at, 'daily', :horizon_days,
             'elec_kwh', :predicted_eui, :confidence_low, :confidence_high,
             NULL, NULL, NULL, NULL)
        """
    )

    inserted = 0
    with engine.connect() as conn:
        for step_idx, row in pred_df.iterrows():
            step_horizon = int(step_idx) + 1
            try:
                conn.execute(
                    insert_sql,
                    {
                        "pnu": pnu,
                        "model_version_id": model_version_id,
                        "predicted_at": now_utc,
                        "horizon_days": step_horizon,
                        "predicted_eui": float(row["eui_pred"]),
                        "confidence_low": float(row["eui_lower"]),
                        "confidence_high": float(row["eui_upper"]),
                    },
                )
                inserted += 1
            except Exception as exc:
                logger.warning(
                    "[pnu=%s] energy_predictions 삽입 오류 (step=%d): %s",
                    pnu, step_horizon, exc,
                )
        if inserted > 0:
            conn.commit()

    logger.info("[pnu=%s] energy_predictions 삽입: %d행", pnu, inserted)
    return inserted


# ---------------------------------------------------------------------------
# 파이프라인 진입점
# ---------------------------------------------------------------------------

def run_ts_daily_retrain(
    db_url: str,
    models_dir: str = "/models/ts",
) -> dict:
    """Tier C 건물 전체에 대해 재학습 + 예측 파이프라인을 실행한다.

    Parameters
    ----------
    db_url:
        SQLAlchemy DATABASE_URL.
    models_dir:
        모델 아티팩트 루트 디렉토리.

    Returns
    -------
    dict
        {trained_count, predicted_count, errors: list[str]}
    """
    engine = create_engine(db_url, pool_pre_ping=True)

    buildings = get_monitored_buildings_with_data(engine)
    logger.info("Tier C 재학습 대상 건물: %d동", len(buildings))

    trained_count = 0
    predicted_count = 0
    errors: list[str] = []

    for bldg in buildings:
        ts_id: int = bldg["ts_id"]
        pnu: str = bldg["pnu"]
        total_area: float = bldg["total_area"]
        usage_type: str = bldg["usage_type"]

        try:
            train_result = train_ts_model_for_building(
                engine=engine,
                ts_id=ts_id,
                pnu=pnu,
                total_area=total_area,
                usage_type=usage_type,
                models_dir=models_dir,
            )
        except Exception as exc:
            msg = f"[pnu={pnu}] train 예외: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            continue

        if train_result is None:
            continue

        trained_count += 1

        try:
            n_rows = predict_and_store_ts(
                engine=engine,
                ts_id=ts_id,
                pnu=pnu,
                total_area=total_area,
                usage_type=usage_type,
                model_version_id=train_result["version_id"],
                artifact_path=train_result["artifact_path"],
                horizon_days=30,
            )
            predicted_count += n_rows
        except Exception as exc:
            msg = f"[pnu={pnu}] predict 예외: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    engine.dispose()

    logger.info(
        "Tier C 재학습 완료: trained=%d predicted_rows=%d errors=%d",
        trained_count, predicted_count, len(errors),
    )
    return {
        "trained_count": trained_count,
        "predicted_count": predicted_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Celery 태스크
# ---------------------------------------------------------------------------

try:
    from src.shared.celery_app import celery as _celery_app

    @_celery_app.task(
        bind=True,
        name="src.simulation.ts_daily_retrain.ts_daily_retrain_task",
        time_limit=7200,
        soft_time_limit=6900,
        max_retries=0,
    )
    def ts_daily_retrain_task(self) -> dict:  # type: ignore[misc]
        """Celery beat에서 일별 호출되는 Tier C 시계열 재학습 태스크.

        celery beat schedule 예시 (src/shared/celery_app.py)::

            app.conf.beat_schedule["ts_daily_retrain"] = {
                "task": "src.simulation.ts_daily_retrain.ts_daily_retrain_task",
                "schedule": crontab(hour=0, minute=30),
            }

        Raises
        ------
        SoftTimeLimitExceeded
            6900초(1h55m) 이내에 완료되지 않으면 Celery가 SoftTimeLimitExceeded를
            발생시킨다. time_limit=7200에서 강제 종료.
        """
        from src.shared.config import get_settings

        settings = get_settings()
        self.update_state(state="STARTED", meta={"step": "init"})
        logger.info("ts_daily_retrain_task 시작")

        try:
            result = run_ts_daily_retrain(db_url=settings.DATABASE_URL)
        except Exception as exc:
            logger.error("ts_daily_retrain_task 실패: %s", exc, exc_info=True)
            return {
                "status": "error",
                "error": str(exc),
                "trained_count": 0,
                "predicted_count": 0,
                "errors": [str(exc)],
            }

        logger.info(
            "ts_daily_retrain_task 완료: trained=%d predicted=%d errors=%d",
            result["trained_count"],
            result["predicted_count"],
            len(result["errors"]),
        )
        return {"status": "ok", **result}

except ImportError:
    # 단독 스크립트 실행 또는 테스트 환경에서 celery가 없을 경우 스텁을 제공한다.
    def ts_daily_retrain_task() -> dict:  # type: ignore[misc]
        """celery가 없는 환경에서의 스텁."""
        raise RuntimeError("Celery가 설치/설정되지 않았습니다.")
