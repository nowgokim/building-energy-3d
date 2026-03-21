"""
Building archetype matching and energy estimation.

Provides archetype parameter lookup based on building usage, vintage, and
structure type, along with thermal degradation modelling and simplified
energy demand estimation calibrated to Korean building stock benchmarks.
"""

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Archetype parameter database
# ---------------------------------------------------------------------------
# Keys: (usage, vintage_class, structure_type)
#   usage:          "apartment" | "office" | "retail" | "education" | "hospital"
#   vintage_class:  "pre-1980" | "1980-2000" | "2001-2010" | "post-2010"
#   structure_type: "RC" (reinforced concrete) | "steel" | "masonry"
#
# Values (all U-values in W/m2K, energy in kWh/m2/yr):
#   wall_uvalue, roof_uvalue, window_uvalue  — envelope thermal transmittance
#   wwr               — window-to-wall ratio (0-1)
#   ref_heating       — reference heating demand   (kWh/m2/yr)
#   ref_cooling       — reference cooling demand    (kWh/m2/yr)
#   ref_total         — reference total primary energy (kWh/m2/yr)
# ---------------------------------------------------------------------------

ARCHETYPE_PARAMS: dict[tuple[str, str, str], dict[str, float]] = {
    # === Apartments ===
    ("apartment", "pre-1980", "RC"):       {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.25, "ref_heating": 120.0, "ref_cooling": 18.0, "ref_total": 185.0},
    ("apartment", "pre-1980", "masonry"):  {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.20, "ref_heating": 140.0, "ref_cooling": 16.0, "ref_total": 205.0},
    ("apartment", "1980-2000", "RC"):      {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 85.0,  "ref_cooling": 20.0, "ref_total": 150.0},
    ("apartment", "1980-2000", "masonry"): {"wall_uvalue": 0.90, "roof_uvalue": 0.70, "window_uvalue": 3.40, "wwr": 0.25, "ref_heating": 95.0,  "ref_cooling": 18.0, "ref_total": 160.0},
    ("apartment", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 55.0,  "ref_cooling": 22.0, "ref_total": 120.0},
    ("apartment", "2001-2010", "steel"):   {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.35, "ref_heating": 52.0,  "ref_cooling": 23.0, "ref_total": 118.0},
    ("apartment", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 30.0,  "ref_cooling": 24.0, "ref_total": 90.0},
    ("apartment", "post-2010", "steel"):   {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.35, "ref_heating": 28.0,  "ref_cooling": 25.0, "ref_total": 88.0},

    # === Offices ===
    ("office", "pre-1980", "RC"):          {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.40, "ref_heating": 100.0, "ref_cooling": 45.0, "ref_total": 210.0},
    ("office", "pre-1980", "steel"):       {"wall_uvalue": 1.70, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.45, "ref_heating": 110.0, "ref_cooling": 48.0, "ref_total": 225.0},
    ("office", "1980-2000", "RC"):         {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.45, "ref_heating": 65.0,  "ref_cooling": 40.0, "ref_total": 170.0},
    ("office", "1980-2000", "steel"):      {"wall_uvalue": 0.85, "roof_uvalue": 0.65, "window_uvalue": 3.40, "wwr": 0.50, "ref_heating": 70.0,  "ref_cooling": 42.0, "ref_total": 178.0},
    ("office", "2001-2010", "RC"):         {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.50, "ref_heating": 40.0,  "ref_cooling": 38.0, "ref_total": 140.0},
    ("office", "2001-2010", "steel"):      {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.55, "ref_heating": 38.0,  "ref_cooling": 40.0, "ref_total": 138.0},
    ("office", "post-2010", "RC"):         {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.50, "ref_heating": 22.0,  "ref_cooling": 35.0, "ref_total": 105.0},
    ("office", "post-2010", "steel"):      {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.55, "ref_heating": 20.0,  "ref_cooling": 36.0, "ref_total": 102.0},

    # === Retail ===
    ("retail", "pre-1980", "RC"):          {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.50, "ref_heating": 80.0,  "ref_cooling": 55.0, "ref_total": 220.0},
    ("retail", "1980-2000", "RC"):         {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.55, "ref_heating": 55.0,  "ref_cooling": 50.0, "ref_total": 175.0},
    ("retail", "2001-2010", "RC"):         {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.55, "ref_heating": 35.0,  "ref_cooling": 45.0, "ref_total": 140.0},
    ("retail", "post-2010", "RC"):         {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.55, "ref_heating": 20.0,  "ref_cooling": 40.0, "ref_total": 105.0},

    # === Education ===
    ("education", "pre-1980", "RC"):       {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.35, "ref_heating": 110.0, "ref_cooling": 30.0, "ref_total": 195.0},
    ("education", "1980-2000", "RC"):      {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.35, "ref_heating": 75.0,  "ref_cooling": 28.0, "ref_total": 155.0},
    ("education", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.40, "ref_heating": 45.0,  "ref_cooling": 30.0, "ref_total": 125.0},
    ("education", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.40, "ref_heating": 25.0,  "ref_cooling": 28.0, "ref_total": 95.0},

    # === Hospital ===
    ("hospital", "pre-1980", "RC"):        {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.30, "ref_heating": 130.0, "ref_cooling": 50.0, "ref_total": 260.0},
    ("hospital", "1980-2000", "RC"):       {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 90.0,  "ref_cooling": 45.0, "ref_total": 210.0},
    ("hospital", "2001-2010", "RC"):       {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 55.0,  "ref_cooling": 42.0, "ref_total": 165.0},
    ("hospital", "post-2010", "RC"):       {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 30.0,  "ref_cooling": 38.0, "ref_total": 120.0},
}

# ---------------------------------------------------------------------------
# End-use intensity ratios (fraction of ref_total)
# Used to break down total energy into component demands.
# ---------------------------------------------------------------------------

_END_USE_RATIOS: dict[str, dict[str, float]] = {
    "apartment":  {"heating": 0.42, "cooling": 0.10, "hot_water": 0.28, "lighting": 0.10, "ventilation": 0.10},
    "office":     {"heating": 0.30, "cooling": 0.25, "hot_water": 0.10, "lighting": 0.20, "ventilation": 0.15},
    "retail":     {"heating": 0.22, "cooling": 0.30, "hot_water": 0.05, "lighting": 0.25, "ventilation": 0.18},
    "education":  {"heating": 0.38, "cooling": 0.15, "hot_water": 0.15, "lighting": 0.18, "ventilation": 0.14},
    "hospital":   {"heating": 0.32, "cooling": 0.22, "hot_water": 0.20, "lighting": 0.14, "ventilation": 0.12},
}

_DEFAULT_END_USE: dict[str, float] = {
    "heating": 0.35, "cooling": 0.18, "hot_water": 0.18, "lighting": 0.16, "ventilation": 0.13,
}


# ---------------------------------------------------------------------------
# Vintage classification
# ---------------------------------------------------------------------------

_USAGE_KR_TO_EN: dict[str, str] = {
    "공동주택": "apartment",
    "아파트": "apartment",
    "연립주택": "apartment",
    "다세대주택": "apartment",
    "업무시설": "office",
    "사무소": "office",
    "판매시설": "retail",
    "근린생활시설": "retail",
    "제1종근린생활시설": "retail",
    "제2종근린생활시설": "retail",
    "교육연구시설": "education",
    "학교": "education",
    "의료시설": "hospital",
    "병원": "hospital",
}


def _normalize_usage(usage_type: str) -> str:
    """Translate Korean usage types to English archetype keys.

    Returns the English key if a mapping exists, otherwise returns the
    input unchanged (will fall through to archetype fallback logic).
    """
    if not usage_type:
        return "apartment"  # default for empty/None
    usage = usage_type.lower().strip()
    return _USAGE_KR_TO_EN.get(usage, usage)


def _classify_vintage(built_year: int) -> str:
    """Map a construction year to a vintage class label."""
    if built_year is None:
        return "1980-2000"  # Unknown → assume common vintage
    if built_year < 1980:
        return "pre-1980"
    if built_year <= 2000:
        return "1980-2000"
    if built_year <= 2010:
        return "2001-2010"
    return "post-2010"


# ---------------------------------------------------------------------------
# Degradation factor
# ---------------------------------------------------------------------------

def apply_degradation(base_uvalue: float, built_year: int) -> float:
    """Apply age-based thermal degradation to a U-value.

    Parameters
    ----------
    base_uvalue:
        Original envelope U-value in W/(m2 K).
    built_year:
        Year the building was constructed.

    Returns
    -------
    float
        Degraded U-value accounting for material aging.

    Degradation schedule (building age in years):
        0-10   -> factor 1.0
        10-20  -> factor 1.3
        20-30  -> factor 1.7
        30+    -> factor 2.0
    """
    current_year = datetime.now().year
    if built_year is None:
        age = 40  # Unknown age → assume worst case
    else:
        age = max(0, current_year - built_year)

    if age <= 10:
        factor = 1.0
    elif age <= 20:
        factor = 1.3
    elif age <= 30:
        factor = 1.7
    else:
        factor = 2.0

    degraded = base_uvalue * factor
    logger.debug(
        "Degradation: base=%.3f, age=%d, factor=%.1f, result=%.3f",
        base_uvalue, age, factor, degraded,
    )
    return degraded


# ---------------------------------------------------------------------------
# Archetype matching
# ---------------------------------------------------------------------------

def match_archetype(
    usage_type: str,
    built_year: int,
    total_area: float,
    structure_type: str,
) -> dict[str, Any]:
    """Look up archetype parameters and apply degradation.

    Parameters
    ----------
    usage_type:
        Building usage category (e.g. ``"apartment"``, ``"office"``).
    built_year:
        Construction year.
    total_area:
        Gross floor area in m2 (carried through for downstream use).
    structure_type:
        Structural system (``"RC"``, ``"steel"``, ``"masonry"``).

    Returns
    -------
    dict
        Archetype parameters with degraded U-values, plus metadata fields
        ``usage_type``, ``vintage_class``, ``structure_type``, ``total_area``,
        and ``built_year``.
    """
    usage = _normalize_usage(usage_type)
    structure = structure_type.upper().strip() if structure_type else "RC"
    vintage = _classify_vintage(built_year)

    # Direct lookup
    key = (usage, vintage, structure)
    params = ARCHETYPE_PARAMS.get(key)

    # Fallback: try RC for the same usage/vintage
    if params is None:
        fallback_key = (usage, vintage, "RC")
        params = ARCHETYPE_PARAMS.get(fallback_key)

    # Fallback: generic apartment RC for the vintage
    if params is None:
        fallback_key = ("apartment", vintage, "RC")
        params = ARCHETYPE_PARAMS.get(fallback_key)

    if params is None:
        # Last resort: mid-range defaults
        logger.warning(
            "No archetype found for (%s, %s, %s) — using hardcoded defaults",
            usage, vintage, structure,
        )
        params = {
            "wall_uvalue": 0.80,
            "roof_uvalue": 0.60,
            "window_uvalue": 3.40,
            "wwr": 0.35,
            "ref_heating": 80.0,
            "ref_cooling": 30.0,
            "ref_total": 160.0,
        }

    # Deep copy and apply degradation to envelope U-values
    result = dict(params)
    result["wall_uvalue"] = apply_degradation(result["wall_uvalue"], built_year)
    result["roof_uvalue"] = apply_degradation(result["roof_uvalue"], built_year)
    result["window_uvalue"] = apply_degradation(result["window_uvalue"], built_year)

    # Attach metadata
    result["usage_type"] = usage
    result["vintage_class"] = vintage
    result["structure_type"] = structure
    result["total_area"] = total_area
    result["built_year"] = built_year

    logger.info(
        "Matched archetype: usage=%s, vintage=%s, structure=%s, ref_total=%.1f kWh/m2",
        usage, vintage, structure, result["ref_total"],
    )

    return result


# ---------------------------------------------------------------------------
# Energy estimation
# ---------------------------------------------------------------------------

def estimate_energy(archetype_params: dict[str, Any]) -> dict[str, float]:
    """Estimate end-use energy breakdown from archetype parameters.

    The total reference energy (``ref_total``) is disaggregated into five
    end uses using usage-specific intensity ratios calibrated to Korean
    building stock benchmarks:

    - Apartments:  ~136 kWh/m2/yr average
    - Offices:     ~159 kWh/m2/yr average
    - Retail:      ~160 kWh/m2/yr average
    - Education:   ~140 kWh/m2/yr average
    - Hospitals:   ~190 kWh/m2/yr average

    Parameters
    ----------
    archetype_params:
        Dictionary returned by :func:`match_archetype`.

    Returns
    -------
    dict[str, float]
        Energy breakdown in kWh/m2/yr with keys: ``heating``, ``cooling``,
        ``hot_water``, ``lighting``, ``ventilation``, ``total_energy``.
    """
    ref_total = archetype_params.get("ref_total", 160.0)
    usage = archetype_params.get("usage_type", "").lower()

    # Scale ref_total by degradation ratio (degraded wall U / original wall U)
    # as a proxy for overall envelope performance decline
    original_params_key = (
        usage,
        archetype_params.get("vintage_class", "1980-2000"),
        archetype_params.get("structure_type", "RC"),
    )
    original = ARCHETYPE_PARAMS.get(original_params_key)
    if original and original["wall_uvalue"] > 0:
        degradation_ratio = archetype_params["wall_uvalue"] / original["wall_uvalue"]
        # Only heating and cooling are affected by envelope degradation
        heating_scaling = degradation_ratio
        cooling_scaling = 1.0 + (degradation_ratio - 1.0) * 0.3  # partial effect
    else:
        heating_scaling = 1.0
        cooling_scaling = 1.0

    ratios = _END_USE_RATIOS.get(usage, _DEFAULT_END_USE)

    heating = ref_total * ratios["heating"] * heating_scaling
    cooling = ref_total * ratios["cooling"] * cooling_scaling
    hot_water = ref_total * ratios["hot_water"]
    lighting = ref_total * ratios["lighting"]
    ventilation = ref_total * ratios["ventilation"]

    total_energy = heating + cooling + hot_water + lighting + ventilation

    result = {
        "heating": round(heating, 2),
        "cooling": round(cooling, 2),
        "hot_water": round(hot_water, 2),
        "lighting": round(lighting, 2),
        "ventilation": round(ventilation, 2),
        "total_energy": round(total_energy, 2),
    }

    logger.info(
        "Energy estimate: total=%.1f kWh/m2 (H=%.1f C=%.1f HW=%.1f L=%.1f V=%.1f)",
        total_energy, heating, cooling, hot_water, lighting, ventilation,
    )

    return result
