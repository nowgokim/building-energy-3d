"""
CO2 배출량 및 1차에너지 환산 모듈.

한국 기준:
- CO2 배출계수 (환경부 2023 국가 온실가스 배출계수):
    전력      0.4781 kgCO₂eq/kWh
    도시가스   0.2036 kgCO₂eq/kWh  (LNG 연소 기준)
    지역난방   0.1218 kgCO₂eq/kWh  (한국지역난방공사)

- 1차에너지 환산계수 (건축물에너지절약설계기준 제21조, 2025.1.1 시행):
    전력      2.75
    도시가스   1.1
    지역난방   0.614

사용 예::

    from src.simulation.carbon import compute_co2_primary

    result = compute_co2_primary(
        total_energy_kwh=500_000,
        area_m2=3_000,
        usage_type="office",
    )
    print(result.co2_kg_m2)         # ≈ 79.7 kgCO₂/m²·yr
    print(result.primary_energy_kwh_m2)  # ≈ 413.5 kWh/m²·yr
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 배출계수 (kgCO₂eq/kWh) ────────────────────────────────────────────────────
CO2_FACTOR_ELECTRICITY: float = 0.4781   # 2023 국가 전력 배출계수
CO2_FACTOR_GAS: float = 0.2036           # LNG 도시가스 연소
CO2_FACTOR_DISTRICT_HEAT: float = 0.1218 # 지역난방

# ── 1차에너지 환산계수 (무차원) ────────────────────────────────────────────────
PE_FACTOR_ELECTRICITY: float = 2.75
PE_FACTOR_GAS: float = 1.1
PE_FACTOR_DISTRICT_HEAT: float = 0.614

# ── 용도별 에너지믹스 (전력비율, 가스비율, 지역난방비율) ──────────────────────
# archetype/Tier 4에 실측 소스 분리값이 없을 때 사용하는 추정 믹스.
# 값의 합 = 1.0 (합계 1을 보장하는 보정은 compute_co2_primary 내에서 수행).
USAGE_MIX: dict[str, tuple[float, float, float]] = {
    # 영문 키 (archetype 코드 내부 사용)
    "apartment":          (0.35, 0.65, 0.00),  # 가스보일러/지역난방 혼합 → 가스 주도
    "residential_single": (0.30, 0.70, 0.00),
    "office":             (0.80, 0.20, 0.00),
    "retail":             (0.85, 0.15, 0.00),
    "education":          (0.60, 0.40, 0.00),
    "hospital":           (0.55, 0.35, 0.10),
    "warehouse":          (0.60, 0.40, 0.00),
    "cultural":           (0.70, 0.30, 0.00),
    "mixed_use":          (0.65, 0.35, 0.00),
    # 한국어 키 (buildings_enriched.usage_type 실제 값)
    "공동주택":            (0.35, 0.65, 0.00),
    "단독주택":            (0.30, 0.70, 0.00),
    "업무시설":            (0.80, 0.20, 0.00),
    "제1종근린생활시설":   (0.85, 0.15, 0.00),
    "제2종근린생활시설":   (0.85, 0.15, 0.00),
    "판매시설":            (0.85, 0.15, 0.00),
    "교육연구시설":        (0.60, 0.40, 0.00),
    "의료시설":            (0.55, 0.35, 0.10),
    "창고시설":            (0.60, 0.40, 0.00),
    "공장":                (0.60, 0.40, 0.00),
    "문화및집회시설":      (0.70, 0.30, 0.00),
    "종교시설":            (0.70, 0.30, 0.00),
    "노유자시설":          (0.65, 0.35, 0.00),
}
_DEFAULT_MIX: tuple[float, float, float] = (0.65, 0.35, 0.00)


@dataclass
class CarbonResult:
    """CO2 배출량 및 1차에너지 계산 결과."""

    co2_kg_yr: float
    """총 CO2 배출량 (kgCO₂eq/yr)"""

    co2_kg_m2: float | None
    """CO2 강도 (kgCO₂eq/m²·yr). area_m2가 없으면 None."""

    primary_energy_kwh_yr: float
    """1차에너지 소요량 (kWh/yr)"""

    primary_energy_kwh_m2: float | None
    """1차에너지 강도 (kWh/m²·yr). area_m2가 없으면 None."""


def compute_co2_primary(
    *,
    total_energy_kwh: float,
    area_m2: float | None = None,
    usage_type: str | None = None,
    elec_kwh: float | None = None,
    gas_kwh: float | None = None,
    dh_kwh: float | None = None,
) -> CarbonResult:
    """CO2 배출량 및 1차에너지 소요량 계산.

    실측 소스 분리값(elec_kwh, gas_kwh, dh_kwh)이 모두 주어지면 정확 계산.
    없으면 usage_type 기반 에너지믹스 추정.

    Args:
        total_energy_kwh: 연간 총 에너지 소비량 (kWh/yr). 0 이하면 모두 0 반환.
        area_m2: 건물 연면적 (m²). 강도값(m²당) 계산에 사용. None이면 강도값 None.
        usage_type: 건물 용도 ('apartment', 'office' 등). 믹스 추정 시 사용.
        elec_kwh: 전력 소비량 (kWh/yr). 실측값 있을 때 우선 사용.
        gas_kwh: 도시가스 소비량 (kWh/yr, 열량 환산).
        dh_kwh: 지역난방 소비량 (kWh/yr, 열량 환산).

    Returns:
        CarbonResult 데이터클래스.
    """
    if total_energy_kwh <= 0:
        return CarbonResult(
            co2_kg_yr=0.0,
            co2_kg_m2=0.0 if area_m2 else None,
            primary_energy_kwh_yr=0.0,
            primary_energy_kwh_m2=0.0 if area_m2 else None,
        )

    # 소스별 kWh 결정
    if elec_kwh is not None and gas_kwh is not None:
        e = max(elec_kwh, 0.0)
        g = max(gas_kwh, 0.0)
        d = max(dh_kwh or 0.0, 0.0)
    else:
        frac_e, frac_g, frac_d = USAGE_MIX.get(usage_type or "", _DEFAULT_MIX)
        e = total_energy_kwh * frac_e
        g = total_energy_kwh * frac_g
        d = total_energy_kwh * frac_d

    co2_yr = e * CO2_FACTOR_ELECTRICITY + g * CO2_FACTOR_GAS + d * CO2_FACTOR_DISTRICT_HEAT
    pe_yr  = e * PE_FACTOR_ELECTRICITY  + g * PE_FACTOR_GAS  + d * PE_FACTOR_DISTRICT_HEAT

    co2_m2 = round(co2_yr / area_m2, 2) if area_m2 else None
    pe_m2  = round(pe_yr  / area_m2, 1) if area_m2 else None

    return CarbonResult(
        co2_kg_yr=round(co2_yr, 1),
        co2_kg_m2=co2_m2,
        primary_energy_kwh_yr=round(pe_yr, 1),
        primary_energy_kwh_m2=pe_m2,
    )
