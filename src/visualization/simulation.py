"""Phase 4-D: EnergyPlus on-demand 시뮬레이션 API 엔드포인트

POST /api/v1/simulate/{pnu}          — 시뮬레이션 Celery 태스크 등록
GET  /api/v1/simulate/status/{task_id} — 태스크 상태 조회
GET  /api/v1/simulate/result/{pnu}    — 저장된 Tier 3 결과 조회
"""
import logging

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.shared.celery_app import celery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/simulate", tags=["simulation"])


class SimulateRequest(BaseModel):
    scenario: str = "baseline"


class SimulateResponse(BaseModel):
    task_id: str
    pnu: str
    status: str = "PENDING"
    message: str = ""


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str          # PENDING | STARTED | PROGRESS | SUCCESS | FAILURE
    result: dict | None = None
    meta: dict | None = None


# ── POST /api/v1/simulate/{pnu} ──────────────────────────────────────────────

@router.post("/{pnu}", response_model=SimulateResponse)
def trigger_simulation(pnu: str, body: SimulateRequest = SimulateRequest()):
    """EnergyPlus 시뮬레이션 태스크를 Celery 큐에 등록한다.

    - Tier 1/2가 이미 있는 건물도 실행 가능 (Tier 3는 별도 저장, 덮어쓰기 안 함)
    - PNU 길이 검증 (19자리)
    """
    if len(pnu) != 19 or not pnu.isdigit():
        raise HTTPException(status_code=400, detail="PNU는 19자리 숫자여야 합니다.")

    from src.simulation.tasks import simulate_building_task

    task = simulate_building_task.delay(pnu, body.scenario)
    logger.info("시뮬레이션 태스크 등록: pnu=%s task_id=%s", pnu, task.id)

    return SimulateResponse(
        task_id=task.id,
        pnu=pnu,
        status="PENDING",
        message=f"EnergyPlus 시뮬레이션이 큐에 등록되었습니다. task_id={task.id}",
    )


# ── GET /api/v1/simulate/status/{task_id} ───────────────────────────────────

@router.get("/status/{task_id}", response_model=TaskStatusResponse)
def get_simulation_status(task_id: str):
    """Celery 태스크 상태를 조회한다.

    status 값:
    - PENDING   : 큐 대기 중 (또는 task_id 미존재)
    - STARTED   : 워커가 태스크 수신
    - PROGRESS  : 시뮬레이션 진행 중 (meta에 step 정보 포함)
    - SUCCESS   : 완료 (result에 EUI 결과 포함)
    - FAILURE   : 에러 발생
    """
    task = AsyncResult(task_id, app=celery)

    result = None
    meta = None

    if task.state == "SUCCESS":
        result = task.result if isinstance(task.result, dict) else {"value": task.result}
    elif task.state == "FAILURE":
        result = {"error": str(task.result)}
    elif task.state in ("STARTED", "PROGRESS"):
        meta = task.info if isinstance(task.info, dict) else {}

    return TaskStatusResponse(
        task_id=task_id,
        status=task.state,
        result=result,
        meta=meta,
    )


# ── GET /api/v1/simulate/result/{pnu} ───────────────────────────────────────

@router.get("/result/{pnu}")
def get_simulation_result(pnu: str):
    """DB에 저장된 Tier 3 EnergyPlus 결과를 조회한다."""
    from sqlalchemy import text
    from src.shared.database import engine

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT pnu, total_energy, heating, cooling, hot_water,
                       lighting, ventilation, simulation_type, data_tier,
                       created_at
                FROM energy_results
                WHERE pnu = :pnu
                  AND data_tier = 3
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"pnu": pnu},
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"PNU {pnu}에 대한 Tier 3 시뮬레이션 결과가 없습니다.",
        )

    return {
        "pnu": row.pnu,
        "total_energy": row.total_energy,
        "heating": row.heating,
        "cooling": row.cooling,
        "hot_water": row.hot_water,
        "lighting": row.lighting,
        "ventilation": row.ventilation,
        "simulation_type": row.simulation_type,
        "data_tier": row.data_tier,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
