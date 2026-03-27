"""
건물 에너지 리트로핏(개선) 비용·효과 추정 모듈.

시나리오별 EUI 절감률 및 비용 단가 기반 단순 추정.
실제 공사비는 현장 조건에 따라 달라질 수 있으므로 참고용으로만 사용.

배경:
  - 리트로핏 비용 단가: 국토부·에너지공단 2024 참고 단가
  - 에너지 단가: 전력 120원/kWh, 도시가스 80원/kWh (2024 상업용 기준)
  - EUI 절감 범위: vintage_class·용도별 차등 적용
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# ── 에너지 단가 (원/kWh) ──────────────────────────────────────────────────────
PRICE_ELECTRICITY_KRW: Final[float] = 120.0  # 한전 일반용 중간구간
PRICE_GAS_KRW: Final[float] = 80.0           # 도시가스 열량 기준 (LNG)

# ── 리트로핏 조치 정의 ────────────────────────────────────────────────────────
# EUI 절감 비율은 (vintage_class별) 최솟값~최댓값 범위로 지정.
# 노후 건물(pre-1980)은 개선 폭이 크고, 신축(post-2010)은 작음.
# cost_per_m2_floor: 시공비 (원/m², 연면적 기준)

MEASURES: dict[str, dict] = {
    "window": {
        "label": "창호 교체 (저방사 이중창 → 삼중창)",
        "description": "기존 단창/이중창을 저방사(Low-e) 이중창 이상으로 교체",
        "heating_saving": {
            "pre-1980":    0.22,  # 22% 난방에너지 절감
            "1980-2000":   0.18,
            "2001-2010":   0.10,
            "post-2010":   0.05,
            "default":     0.12,
        },
        "cooling_saving": {
            "pre-1980":    0.08,
            "1980-2000":   0.06,
            "2001-2010":   0.04,
            "post-2010":   0.02,
            "default":     0.05,
        },
        "cost_per_m2_floor": 35_000,   # 원/m²·연면적
    },
    "wall_insulation": {
        "label": "외벽 단열 강화 (외단열 추가 시공)",
        "description": "외벽에 고성능 단열재(EPS/XPS/PF) 외단열 추가",
        "heating_saving": {
            "pre-1980":    0.20,
            "1980-2000":   0.15,
            "2001-2010":   0.08,
            "post-2010":   0.03,
            "default":     0.11,
        },
        "cooling_saving": {
            "pre-1980":    0.05,
            "1980-2000":   0.04,
            "2001-2010":   0.02,
            "post-2010":   0.01,
            "default":     0.03,
        },
        "cost_per_m2_floor": 45_000,
    },
    "roof_insulation": {
        "label": "지붕·옥상 단열 강화",
        "description": "옥상 단열재 두께 증가 또는 재시공",
        "heating_saving": {
            "pre-1980":    0.12,
            "1980-2000":   0.09,
            "2001-2010":   0.05,
            "post-2010":   0.02,
            "default":     0.07,
        },
        "cooling_saving": {
            "pre-1980":    0.05,
            "1980-2000":   0.04,
            "2001-2010":   0.02,
            "post-2010":   0.01,
            "default":     0.03,
        },
        "cost_per_m2_floor": 20_000,
    },
    "led_lighting": {
        "label": "LED 조명 전면 교체",
        "description": "형광등·메탈할라이드 등 기존 조명을 LED로 전면 교체",
        "lighting_saving": {
            "pre-1980":    0.55,
            "1980-2000":   0.50,
            "2001-2010":   0.40,
            "post-2010":   0.20,
            "default":     0.40,
        },
        "cost_per_m2_floor": 18_000,
    },
    "hvac_upgrade": {
        "label": "고효율 냉난방설비 교체 (EHP/GHP)",
        "description": "노후 보일러·패키지에어컨을 고효율 히트펌프·인버터 시스템으로 교체",
        "heating_saving": {
            "pre-1980":    0.25,
            "1980-2000":   0.20,
            "2001-2010":   0.12,
            "post-2010":   0.05,
            "default":     0.15,
        },
        "cooling_saving": {
            "pre-1980":    0.25,
            "1980-2000":   0.20,
            "2001-2010":   0.12,
            "post-2010":   0.05,
            "default":     0.15,
        },
        "cost_per_m2_floor": 65_000,
    },
}

ALL_MEASURE_IDS: tuple[str, ...] = tuple(MEASURES.keys())


@dataclass
class MeasureResult:
    """단일 리트로핏 조치 결과."""

    id: str
    label: str
    description: str
    eui_saving_kwh_m2: float           # EUI 절감량 (kWh/m²/yr)
    saving_pct: float                  # 전체 EUI 대비 절감 비율 (0-1)
    co2_saving_kg_m2: float            # CO2 절감 (kgCO₂/m²/yr)
    cost_per_m2: int                   # 시공비 단가 (원/m²)
    cost_total_krw: int                # 총 시공비 (원)
    annual_saving_krw: int             # 연간 에너지비 절감액 (원/yr)
    payback_years: float | None        # 단순회수기간 (yr). area=None이면 None


@dataclass
class RetrofitResult:
    """건물 전체 리트로핏 시뮬레이션 결과."""

    pnu: str
    eui_before: float                  # 현재 EUI (kWh/m²/yr)
    co2_before_kg_m2: float | None     # 현재 CO2 강도
    total_area_m2: float | None
    vintage_class: str | None

    measures: list[MeasureResult] = field(default_factory=list)

    # 선택된 조치 복합 적용 결과
    eui_after: float = 0.0
    eui_saving_kwh_m2: float = 0.0
    saving_pct: float = 0.0
    co2_after_kg_m2: float | None = None
    co2_saving_kg_m2: float | None = None
    cost_total_krw: int = 0
    annual_saving_krw: int = 0
    payback_years: float | None = None


def _get_saving(spec: dict, key: str, vintage_class: str | None) -> float:
    """vintage_class별 절감 비율 조회. key: 'heating_saving'|'cooling_saving' etc."""
    table = spec.get(key, {})
    return table.get(vintage_class or "default", table.get("default", 0.0))


def _weighted_energy_price(usage_type: str | None) -> float:
    """용도별 에너지믹스 가중평균 단가 (원/kWh).

    전력비중이 높은 업무시설은 단가 높음, 가스 주도 주택은 낮음.
    """
    # (전력비율, 가스비율) — carbon.py USAGE_MIX와 동일 한국어 키
    mix: dict[str, tuple[float, float]] = {
        "공동주택":            (0.35, 0.65),
        "단독주택":            (0.30, 0.70),
        "업무시설":            (0.80, 0.20),
        "제1종근린생활시설":   (0.85, 0.15),
        "제2종근린생활시설":   (0.85, 0.15),
        "판매시설":            (0.85, 0.15),
        "교육연구시설":        (0.60, 0.40),
        "의료시설":            (0.55, 0.45),
        "창고시설":            (0.60, 0.40),
        "공장":                (0.60, 0.40),
        "문화및집회시설":      (0.70, 0.30),
        "종교시설":            (0.70, 0.30),
    }
    frac_e, frac_g = mix.get(usage_type or "", (0.65, 0.35))
    return frac_e * PRICE_ELECTRICITY_KRW + frac_g * PRICE_GAS_KRW


def simulate_retrofit(
    *,
    pnu: str,
    eui_kwh_m2: float,
    co2_kg_m2: float | None,
    total_area_m2: float | None,
    vintage_class: str | None,
    usage_type: str | None,
    selected_measures: list[str] | None = None,
) -> RetrofitResult:
    """리트로핏 시뮬레이션 실행.

    Args:
        pnu: 건물 PNU.
        eui_kwh_m2: 현재 EUI (kWh/m²/yr).
        co2_kg_m2: 현재 CO2 강도. None이면 CO2 절감량 미계산.
        total_area_m2: 건물 연면적. None이면 총 비용/회수기간 미계산.
        vintage_class: 건물 연대 ('pre-1980'|'1980-2000'|'2001-2010'|'post-2010').
        usage_type: 건물 용도 (한국어 또는 영문).
        selected_measures: 적용할 조치 ID 목록. None이면 전체.

    Returns:
        RetrofitResult (개별 조치 + 복합 결과 포함).
    """
    if eui_kwh_m2 <= 0:
        raise ValueError("eui_kwh_m2 must be positive")

    ids = selected_measures if selected_measures is not None else list(MEASURES.keys())
    unit_price = _weighted_energy_price(usage_type)
    result = RetrofitResult(
        pnu=pnu,
        eui_before=eui_kwh_m2,
        co2_before_kg_m2=co2_kg_m2,
        total_area_m2=total_area_m2,
        vintage_class=vintage_class,
    )

    # 복합 효과: 중복 절감 방지를 위해 남은 EUI에 순차 적용
    remaining_eui = eui_kwh_m2
    total_cost_per_m2 = 0
    total_saving_kwh_m2 = 0.0

    for mid in ids:
        if mid not in MEASURES:
            continue
        spec = MEASURES[mid]

        # 이 조치로 인한 EUI 절감량 계산
        if mid == "led_lighting":
            # 조명만 독립적으로 절감 (난방/냉방과 별개)
            lighting_frac = 0.15  # 전체 EUI 중 조명 비율 추정
            ls = _get_saving(spec, "lighting_saving", vintage_class)
            saving_kwh_m2 = eui_kwh_m2 * lighting_frac * ls
        else:
            hs = _get_saving(spec, "heating_saving", vintage_class)
            cs = _get_saving(spec, "cooling_saving", vintage_class)
            heating_frac = 0.45  # 전체 EUI 중 난방 비율 추정
            cooling_frac = 0.20  # 전체 EUI 중 냉방 비율 추정
            saving_kwh_m2 = remaining_eui * (heating_frac * hs + cooling_frac * cs)

        saving_kwh_m2 = max(saving_kwh_m2, 0.0)
        saving_pct = saving_kwh_m2 / eui_kwh_m2 if eui_kwh_m2 > 0 else 0.0
        co2_saving = saving_kwh_m2 * (co2_kg_m2 / eui_kwh_m2) if (co2_kg_m2 and eui_kwh_m2) else None
        cost_per_m2 = spec["cost_per_m2_floor"]
        cost_total = int(cost_per_m2 * total_area_m2) if total_area_m2 else 0
        annual_saving = int(saving_kwh_m2 * total_area_m2 * unit_price) if total_area_m2 else 0
        payback = round(cost_total / annual_saving, 1) if annual_saving > 0 else None

        result.measures.append(MeasureResult(
            id=mid,
            label=spec["label"],
            description=spec["description"],
            eui_saving_kwh_m2=round(saving_kwh_m2, 2),
            saving_pct=round(saving_pct, 4),
            co2_saving_kg_m2=round(co2_saving, 2) if co2_saving is not None else 0.0,
            cost_per_m2=cost_per_m2,
            cost_total_krw=cost_total,
            annual_saving_krw=annual_saving,
            payback_years=payback,
        ))

        remaining_eui = max(remaining_eui - saving_kwh_m2, 0.0)
        total_saving_kwh_m2 += saving_kwh_m2
        total_cost_per_m2 += cost_per_m2

    # 복합 결과
    eui_after = max(eui_kwh_m2 - total_saving_kwh_m2, 0.0)
    co2_after = None
    co2_saving_total = None
    if co2_kg_m2 is not None and eui_kwh_m2 > 0:
        co2_ratio = eui_after / eui_kwh_m2
        co2_after = round(co2_kg_m2 * co2_ratio, 2)
        co2_saving_total = round(co2_kg_m2 - co2_after, 2)

    cost_total = int(total_cost_per_m2 * total_area_m2) if total_area_m2 else 0
    annual_saving = int(total_saving_kwh_m2 * (total_area_m2 or 0) * unit_price)
    payback = round(cost_total / annual_saving, 1) if annual_saving > 0 else None

    result.eui_after = round(eui_after, 1)
    result.eui_saving_kwh_m2 = round(total_saving_kwh_m2, 2)
    result.saving_pct = round(total_saving_kwh_m2 / eui_kwh_m2, 4) if eui_kwh_m2 > 0 else 0.0
    result.co2_after_kg_m2 = co2_after
    result.co2_saving_kg_m2 = co2_saving_total
    result.cost_total_krw = cost_total
    result.annual_saving_krw = annual_saving
    result.payback_years = payback

    return result
