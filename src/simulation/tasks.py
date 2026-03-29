"""Phase 4-D: EnergyPlus on-demand 시뮬레이션 Celery 태스크"""
import logging

from src.shared.celery_app import celery
from src.shared.config import get_settings

logger = logging.getLogger(__name__)


@celery.task(
    bind=True,
    name="src.simulation.tasks.simulate_building_task",
    time_limit=700,      # EnergyPlus 실행 최대 700초 (runner timeout=600 + 여유)
    soft_time_limit=660,
    max_retries=0,       # 시뮬레이션은 결정적 — 재시도 없음
)
def simulate_building_task(self, pnu: str, scenario: str = "baseline") -> dict:
    """EnergyPlus on-demand 시뮬레이션.

    Args:
        pnu: 건물 PNU (19자리)
        scenario: 'baseline' | 'insulation' | 'window' | 'hvac'
                  (현재 baseline만 구현, 추후 retrofit 시나리오 확장)

    Returns:
        {
            "pnu": ...,
            "status": "ok" | "error",
            "eui_total": ...,
            "heating": ..., "cooling": ..., "lighting": ...,
            "simulation_type": "energyplus",
            "scenario": ...,
            "saved": True/False,   # Tier 3 DB 저장 여부
        }
    """
    settings = get_settings()

    self.update_state(state="STARTED", meta={"pnu": pnu, "step": "init"})
    logger.info("[EnergyPlus] 태스크 시작: pnu=%s scenario=%s", pnu, scenario)

    try:
        from src.simulation.energyplus_runner import simulate_building, save_tier3

        self.update_state(state="PROGRESS", meta={"pnu": pnu, "step": "simulate"})
        result = simulate_building(pnu, settings.DATABASE_URL)

        if "error" in result:
            logger.warning("[EnergyPlus] 시뮬 실패: pnu=%s error=%s", pnu, result["error"])
            return {**result, "scenario": scenario, "saved": False}

        self.update_state(state="PROGRESS", meta={"pnu": pnu, "step": "save"})
        saved = save_tier3(result, settings.DATABASE_URL)

        logger.info(
            "[EnergyPlus] 완료: pnu=%s eui=%.1f saved=%s",
            pnu, result.get("eui_total", 0), saved,
        )
        return {**result, "scenario": scenario, "saved": saved}

    except Exception as exc:
        logger.error("[EnergyPlus] 예외: pnu=%s %s", pnu, exc, exc_info=True)
        return {
            "pnu": pnu,
            "status": "error",
            "error": str(exc),
            "scenario": scenario,
            "saved": False,
        }
