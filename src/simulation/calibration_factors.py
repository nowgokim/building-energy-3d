"""보정계수 — Tier 1 실측 vs Korean_BB 시뮬 비교 (Phase A' 자동 생성)

correction_factor = median(tier1_eui) / median(korean_bb_eui)
보정된 EUI = korean_bb_eui × correction_factor

n_tier1이 작은 용도(≤5건)는 신뢰도 낮음 — 주석 참조.
"""

# usage_type → correction_factor
# (archetype, n_tier1, tier1_median, simul_median 은 참고용)
CORRECTION_FACTORS: dict[str, float] = {
    "공동주택": 0.8784,  # n=1, tier1=200.8, simul=228.6
    "교육연구시설": 0.5543,  # n=9, tier1=136.9, simul=247.0
    "근린생활시설": 0.4473,  # n=1, tier1=113.3, simul=253.3
    "노유자시설": 0.1481,  # n=2, tier1=85.2, simul=574.8
    "문화및집회시설": 0.4523,  # n=1, tier1=122.3, simul=270.4
    "숙박시설": 1.0,  # n=1, tier1=222.6, simul=nan, 매핑 불가 → 미보정
    "업무시설": 0.2934,  # n=11, tier1=131.3, simul=447.5
    "제1종근린생활시설": 0.4533,  # n=16, tier1=115.2, simul=254.0
    "제2종근린생활시설": 0.4303,  # n=34, tier1=109.6, simul=254.6
    "종교시설": 1.0,  # n=3, tier1=105.0, simul=nan, 매핑 불가 → 미보정
}

DEFAULT_FACTOR = 1.0  # 매핑 없는 용도


def get_correction_factor(usage_type: str) -> float:
    """usage_type에 대한 보정계수 반환. 미등록 용도는 1.0."""
    return CORRECTION_FACTORS.get(usage_type, DEFAULT_FACTOR)
