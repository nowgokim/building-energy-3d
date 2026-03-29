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
#   usage:          "apartment" | "residential_single" | "office" | "retail"
#                   "education" | "hospital" | "warehouse" | "cultural"
#                   "mixed_use"
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
    # =========================================================================
    # Apartments (공동주택: 아파트, 연립, 다세대)
    # =========================================================================
    ("apartment", "pre-1980", "RC"):       {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.25, "ref_heating": 120.0, "ref_cooling": 18.0, "ref_total": 185.0},
    ("apartment", "pre-1980", "masonry"):  {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.20, "ref_heating": 140.0, "ref_cooling": 16.0, "ref_total": 205.0},
    ("apartment", "pre-1980", "steel"):    {"wall_uvalue": 2.20, "roof_uvalue": 2.50, "window_uvalue": 5.80, "wwr": 0.22, "ref_heating": 130.0, "ref_cooling": 17.0, "ref_total": 198.0},
    ("apartment", "1980-2000", "RC"):      {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 85.0,  "ref_cooling": 20.0, "ref_total": 150.0},
    ("apartment", "1980-2000", "masonry"): {"wall_uvalue": 0.90, "roof_uvalue": 0.70, "window_uvalue": 3.40, "wwr": 0.25, "ref_heating": 95.0,  "ref_cooling": 18.0, "ref_total": 160.0},
    ("apartment", "1980-2000", "steel"):   {"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.28, "ref_heating": 90.0,  "ref_cooling": 19.0, "ref_total": 155.0},
    ("apartment", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 55.0,  "ref_cooling": 22.0, "ref_total": 120.0},
    ("apartment", "2001-2010", "steel"):   {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.35, "ref_heating": 52.0,  "ref_cooling": 23.0, "ref_total": 118.0},
    ("apartment", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 30.0,  "ref_cooling": 24.0, "ref_total": 90.0},
    ("apartment", "post-2010", "steel"):   {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.35, "ref_heating": 28.0,  "ref_cooling": 25.0, "ref_total": 88.0},

    # =========================================================================
    # Residential Single (단독주택, 다가구주택, 다중주택)
    # Worse envelope performance than apartments; masonry dominant for pre-1980
    # =========================================================================
    ("residential_single", "pre-1980", "RC"):      {"wall_uvalue": 1.80, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.20, "ref_heating": 135.0, "ref_cooling": 13.0, "ref_total": 195.0},
    ("residential_single", "pre-1980", "masonry"): {"wall_uvalue": 2.50, "roof_uvalue": 2.50, "window_uvalue": 5.80, "wwr": 0.18, "ref_heating": 150.0, "ref_cooling": 12.0, "ref_total": 210.0},
    ("residential_single", "1980-2000", "RC"):      {"wall_uvalue": 0.90, "roof_uvalue": 0.70, "window_uvalue": 3.40, "wwr": 0.25, "ref_heating": 90.0,  "ref_cooling": 15.0, "ref_total": 155.0},
    ("residential_single", "1980-2000", "masonry"): {"wall_uvalue": 1.20, "roof_uvalue": 0.90, "window_uvalue": 3.40, "wwr": 0.22, "ref_heating": 100.0, "ref_cooling": 14.0, "ref_total": 165.0},
    ("residential_single", "2001-2010", "RC"):      {"wall_uvalue": 0.52, "roof_uvalue": 0.35, "window_uvalue": 2.40, "wwr": 0.28, "ref_heating": 58.0,  "ref_cooling": 18.0, "ref_total": 122.0},
    ("residential_single", "2001-2010", "masonry"): {"wall_uvalue": 0.65, "roof_uvalue": 0.45, "window_uvalue": 2.40, "wwr": 0.26, "ref_heating": 65.0,  "ref_cooling": 17.0, "ref_total": 130.0},
    ("residential_single", "post-2010", "RC"):      {"wall_uvalue": 0.32, "roof_uvalue": 0.20, "window_uvalue": 1.50, "wwr": 0.30, "ref_heating": 33.0,  "ref_cooling": 21.0, "ref_total": 96.0},
    ("residential_single", "post-2010", "masonry"): {"wall_uvalue": 0.40, "roof_uvalue": 0.25, "window_uvalue": 1.50, "wwr": 0.28, "ref_heating": 38.0,  "ref_cooling": 20.0, "ref_total": 100.0},

    # =========================================================================
    # Offices (업무시설, 사무소)
    # =========================================================================
    ("office", "pre-1980", "RC"):          {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.40, "ref_heating": 100.0, "ref_cooling": 45.0, "ref_total": 210.0},
    ("office", "pre-1980", "steel"):       {"wall_uvalue": 1.70, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.45, "ref_heating": 110.0, "ref_cooling": 48.0, "ref_total": 225.0},
    ("office", "pre-1980", "masonry"):     {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.35, "ref_heating": 105.0, "ref_cooling": 42.0, "ref_total": 215.0},
    ("office", "1980-2000", "RC"):         {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.45, "ref_heating": 65.0,  "ref_cooling": 40.0, "ref_total": 170.0},
    ("office", "1980-2000", "steel"):      {"wall_uvalue": 0.85, "roof_uvalue": 0.65, "window_uvalue": 3.40, "wwr": 0.50, "ref_heating": 70.0,  "ref_cooling": 42.0, "ref_total": 178.0},
    ("office", "1980-2000", "masonry"):    {"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.40, "ref_heating": 68.0,  "ref_cooling": 38.0, "ref_total": 168.0},
    ("office", "2001-2010", "RC"):         {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.50, "ref_heating": 40.0,  "ref_cooling": 38.0, "ref_total": 140.0},
    ("office", "2001-2010", "steel"):      {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.55, "ref_heating": 38.0,  "ref_cooling": 40.0, "ref_total": 138.0},
    ("office", "post-2010", "RC"):         {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.50, "ref_heating": 22.0,  "ref_cooling": 35.0, "ref_total": 105.0},
    ("office", "post-2010", "steel"):      {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.55, "ref_heating": 20.0,  "ref_cooling": 36.0, "ref_total": 102.0},

    # =========================================================================
    # Retail (판매시설, 근린생활시설)
    # =========================================================================
    ("retail", "pre-1980", "RC"):          {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.50, "ref_heating": 80.0,  "ref_cooling": 55.0, "ref_total": 220.0},
    ("retail", "pre-1980", "masonry"):     {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.45, "ref_heating": 85.0,  "ref_cooling": 50.0, "ref_total": 220.0},
    ("retail", "1980-2000", "RC"):         {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.55, "ref_heating": 55.0,  "ref_cooling": 50.0, "ref_total": 175.0},
    ("retail", "1980-2000", "steel"):      {"wall_uvalue": 0.85, "roof_uvalue": 0.65, "window_uvalue": 3.40, "wwr": 0.60, "ref_heating": 48.0,  "ref_cooling": 52.0, "ref_total": 178.0},
    ("retail", "1980-2000", "masonry"):    {"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.50, "ref_heating": 58.0,  "ref_cooling": 46.0, "ref_total": 172.0},
    ("retail", "2001-2010", "RC"):         {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.55, "ref_heating": 35.0,  "ref_cooling": 45.0, "ref_total": 140.0},
    ("retail", "2001-2010", "steel"):      {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.60, "ref_heating": 30.0,  "ref_cooling": 48.0, "ref_total": 138.0},
    ("retail", "post-2010", "RC"):         {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.55, "ref_heating": 20.0,  "ref_cooling": 40.0, "ref_total": 105.0},
    ("retail", "post-2010", "steel"):      {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.60, "ref_heating": 16.0,  "ref_cooling": 42.0, "ref_total": 104.0},

    # =========================================================================
    # Education (교육연구시설, 학교, 수련시설)
    # =========================================================================
    ("education", "pre-1980", "RC"):       {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.35, "ref_heating": 110.0, "ref_cooling": 30.0, "ref_total": 195.0},
    ("education", "pre-1980", "steel"):    {"wall_uvalue": 1.70, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.38, "ref_heating": 105.0, "ref_cooling": 28.0, "ref_total": 192.0},
    ("education", "pre-1980", "masonry"):  {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.30, "ref_heating": 115.0, "ref_cooling": 27.0, "ref_total": 198.0},
    ("education", "1980-2000", "RC"):      {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.35, "ref_heating": 75.0,  "ref_cooling": 28.0, "ref_total": 155.0},
    ("education", "1980-2000", "steel"):   {"wall_uvalue": 0.85, "roof_uvalue": 0.65, "window_uvalue": 3.40, "wwr": 0.38, "ref_heating": 70.0,  "ref_cooling": 26.0, "ref_total": 152.0},
    ("education", "1980-2000", "masonry"): {"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.32, "ref_heating": 78.0,  "ref_cooling": 26.0, "ref_total": 158.0},
    ("education", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.40, "ref_heating": 45.0,  "ref_cooling": 30.0, "ref_total": 125.0},
    ("education", "2001-2010", "steel"):   {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.42, "ref_heating": 42.0,  "ref_cooling": 28.0, "ref_total": 122.0},
    ("education", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.40, "ref_heating": 25.0,  "ref_cooling": 28.0, "ref_total": 95.0},
    ("education", "post-2010", "steel"):   {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.42, "ref_heating": 22.0,  "ref_cooling": 26.0, "ref_total": 92.0},

    # =========================================================================
    # Hospital (의료시설, 병원, 노유자시설)
    # 24/7 operation; high hot water demand
    # =========================================================================
    ("hospital", "pre-1980", "RC"):        {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.30, "ref_heating": 130.0, "ref_cooling": 50.0, "ref_total": 260.0},
    ("hospital", "pre-1980", "steel"):     {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.32, "ref_heating": 140.0, "ref_cooling": 52.0, "ref_total": 270.0},
    ("hospital", "1980-2000", "RC"):       {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 90.0,  "ref_cooling": 45.0, "ref_total": 210.0},
    ("hospital", "1980-2000", "steel"):    {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.32, "ref_heating": 95.0,  "ref_cooling": 46.0, "ref_total": 215.0},
    ("hospital", "2001-2010", "RC"):       {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 55.0,  "ref_cooling": 42.0, "ref_total": 165.0},
    ("hospital", "2001-2010", "steel"):    {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.38, "ref_heating": 58.0,  "ref_cooling": 44.0, "ref_total": 168.0},
    ("hospital", "post-2010", "RC"):       {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 30.0,  "ref_cooling": 38.0, "ref_total": 120.0},
    ("hospital", "post-2010", "steel"):    {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.38, "ref_heating": 32.0,  "ref_cooling": 40.0, "ref_total": 124.0},

    # =========================================================================
    # Warehouse / Industrial (창고시설, 공장, 산업시설)
    # Low wwr; steel frame dominant for modern builds
    # =========================================================================
    ("warehouse", "pre-1980", "RC"):       {"wall_uvalue": 1.80, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.10, "ref_heating": 55.0,  "ref_cooling": 10.0, "ref_total": 95.0},
    ("warehouse", "pre-1980", "steel"):    {"wall_uvalue": 3.00, "roof_uvalue": 3.20, "window_uvalue": 5.80, "wwr": 0.12, "ref_heating": 70.0,  "ref_cooling": 12.0, "ref_total": 115.0},
    ("warehouse", "pre-1980", "masonry"):  {"wall_uvalue": 2.50, "roof_uvalue": 2.80, "window_uvalue": 5.80, "wwr": 0.10, "ref_heating": 60.0,  "ref_cooling": 10.0, "ref_total": 100.0},
    ("warehouse", "1980-2000", "RC"):      {"wall_uvalue": 1.00, "roof_uvalue": 1.10, "window_uvalue": 3.40, "wwr": 0.12, "ref_heating": 40.0,  "ref_cooling": 12.0, "ref_total": 75.0},
    ("warehouse", "1980-2000", "steel"):   {"wall_uvalue": 1.80, "roof_uvalue": 2.00, "window_uvalue": 3.40, "wwr": 0.15, "ref_heating": 50.0,  "ref_cooling": 14.0, "ref_total": 88.0},
    ("warehouse", "1980-2000", "masonry"): {"wall_uvalue": 1.50, "roof_uvalue": 1.60, "window_uvalue": 3.40, "wwr": 0.12, "ref_heating": 45.0,  "ref_cooling": 12.0, "ref_total": 80.0},
    ("warehouse", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.50, "window_uvalue": 2.40, "wwr": 0.15, "ref_heating": 28.0,  "ref_cooling": 13.0, "ref_total": 58.0},
    ("warehouse", "2001-2010", "steel"):   {"wall_uvalue": 0.55, "roof_uvalue": 0.60, "window_uvalue": 2.40, "wwr": 0.18, "ref_heating": 32.0,  "ref_cooling": 15.0, "ref_total": 62.0},
    ("warehouse", "2001-2010", "masonry"): {"wall_uvalue": 0.65, "roof_uvalue": 0.70, "window_uvalue": 2.40, "wwr": 0.15, "ref_heating": 30.0,  "ref_cooling": 14.0, "ref_total": 60.0},
    ("warehouse", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.30, "window_uvalue": 1.50, "wwr": 0.15, "ref_heating": 16.0,  "ref_cooling": 13.0, "ref_total": 40.0},
    ("warehouse", "post-2010", "steel"):   {"wall_uvalue": 0.30, "roof_uvalue": 0.35, "window_uvalue": 1.50, "wwr": 0.20, "ref_heating": 20.0,  "ref_cooling": 15.0, "ref_total": 45.0},
    ("warehouse", "post-2010", "masonry"): {"wall_uvalue": 0.35, "roof_uvalue": 0.40, "window_uvalue": 1.50, "wwr": 0.15, "ref_heating": 18.0,  "ref_cooling": 14.0, "ref_total": 42.0},

    # =========================================================================
    # Cultural / Assembly (문화및집회시설, 종교시설, 집회시설, 장례시설)
    # Intermittent occupancy; high ceiling volumes; moderate wwr
    # =========================================================================
    ("cultural", "pre-1980", "RC"):       {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.30, "ref_heating": 90.0,  "ref_cooling": 28.0, "ref_total": 175.0},
    ("cultural", "pre-1980", "masonry"):  {"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.25, "ref_heating": 100.0, "ref_cooling": 25.0, "ref_total": 180.0},
    ("cultural", "1980-2000", "RC"):      {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 65.0,  "ref_cooling": 26.0, "ref_total": 140.0},
    ("cultural", "1980-2000", "masonry"): {"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.28, "ref_heating": 70.0,  "ref_cooling": 24.0, "ref_total": 145.0},
    ("cultural", "2001-2010", "RC"):      {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 38.0,  "ref_cooling": 28.0, "ref_total": 110.0},
    ("cultural", "2001-2010", "masonry"): {"wall_uvalue": 0.55, "roof_uvalue": 0.40, "window_uvalue": 2.40, "wwr": 0.32, "ref_heating": 42.0,  "ref_cooling": 26.0, "ref_total": 115.0},
    ("cultural", "post-2010", "RC"):      {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 20.0,  "ref_cooling": 26.0, "ref_total": 75.0},
    ("cultural", "post-2010", "masonry"): {"wall_uvalue": 0.35, "roof_uvalue": 0.25, "window_uvalue": 1.50, "wwr": 0.32, "ref_heating": 24.0,  "ref_cooling": 24.0, "ref_total": 80.0},

    # =========================================================================
    # Mixed Use / Lodging (숙박시설, 복합건축물, 운동시설)
    # Hotels and mixed-commercial: high hot water; high internal gains
    # =========================================================================
    ("mixed_use", "pre-1980", "RC"):      {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.35, "ref_heating": 100.0, "ref_cooling": 48.0, "ref_total": 230.0},
    ("mixed_use", "pre-1980", "steel"):   {"wall_uvalue": 1.60, "roof_uvalue": 1.90, "window_uvalue": 5.80, "wwr": 0.40, "ref_heating": 110.0, "ref_cooling": 50.0, "ref_total": 240.0},
    ("mixed_use", "1980-2000", "RC"):     {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.40, "ref_heating": 70.0,  "ref_cooling": 42.0, "ref_total": 190.0},
    ("mixed_use", "1980-2000", "steel"):  {"wall_uvalue": 0.80, "roof_uvalue": 0.62, "window_uvalue": 3.40, "wwr": 0.45, "ref_heating": 75.0,  "ref_cooling": 44.0, "ref_total": 198.0},
    ("mixed_use", "2001-2010", "RC"):     {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.45, "ref_heating": 42.0,  "ref_cooling": 38.0, "ref_total": 148.0},
    ("mixed_use", "2001-2010", "steel"):  {"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.50, "ref_heating": 40.0,  "ref_cooling": 40.0, "ref_total": 145.0},
    ("mixed_use", "post-2010", "RC"):     {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.45, "ref_heating": 24.0,  "ref_cooling": 34.0, "ref_total": 110.0},
    ("mixed_use", "post-2010", "steel"):  {"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.50, "ref_heating": 22.0,  "ref_cooling": 36.0, "ref_total": 108.0},

    # =========================================================================
    # Apartment — District Heating (지역난방 공동주택)
    # 서울·인천 고층 아파트: 지역열공급망 → 건물 내 난방·온수 에너지 ~25% 절감.
    # 기존 apartment RC 대비 ref_heating ×0.73, ref_total ×0.85.
    # =========================================================================
    ("apartment_district_heating", "pre-1980", "RC"):    {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.25, "ref_heating": 88.0,  "ref_cooling": 18.0, "ref_total": 158.0},
    ("apartment_district_heating", "pre-1980", "steel"): {"wall_uvalue": 2.20, "roof_uvalue": 2.50, "window_uvalue": 5.80, "wwr": 0.22, "ref_heating": 95.0,  "ref_cooling": 17.0, "ref_total": 168.0},
    ("apartment_district_heating", "1980-2000", "RC"):   {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.30, "ref_heating": 62.0,  "ref_cooling": 20.0, "ref_total": 125.0},
    ("apartment_district_heating", "1980-2000", "steel"):{"wall_uvalue": 1.00, "roof_uvalue": 0.80, "window_uvalue": 3.40, "wwr": 0.28, "ref_heating": 66.0,  "ref_cooling": 19.0, "ref_total": 130.0},
    ("apartment_district_heating", "2001-2010", "RC"):   {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.35, "ref_heating": 40.0,  "ref_cooling": 22.0, "ref_total": 100.0},
    ("apartment_district_heating", "2001-2010", "steel"):{"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.35, "ref_heating": 38.0,  "ref_cooling": 23.0, "ref_total": 98.0},
    ("apartment_district_heating", "post-2010", "RC"):   {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.35, "ref_heating": 22.0,  "ref_cooling": 24.0, "ref_total": 76.0},
    ("apartment_district_heating", "post-2010", "steel"):{"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.35, "ref_heating": 20.0,  "ref_cooling": 25.0, "ref_total": 74.0},

    # =========================================================================
    # Apartment — Ondol (온돌 단독주택·다세대)
    # 바닥복사난방: 낮은 설정온도로 동일 쾌적도 → ref_heating residential_single × 0.90.
    # masonry/RC 구조 우세. 외피 성능은 residential_single과 동일.
    # =========================================================================
    ("apartment_ondol", "pre-1980", "masonry"): {"wall_uvalue": 2.50, "roof_uvalue": 2.50, "window_uvalue": 5.80, "wwr": 0.18, "ref_heating": 135.0, "ref_cooling": 12.0, "ref_total": 192.0},
    ("apartment_ondol", "pre-1980", "RC"):      {"wall_uvalue": 1.80, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.20, "ref_heating": 122.0, "ref_cooling": 13.0, "ref_total": 178.0},
    ("apartment_ondol", "1980-2000", "masonry"):{"wall_uvalue": 1.20, "roof_uvalue": 0.90, "window_uvalue": 3.40, "wwr": 0.22, "ref_heating": 90.0,  "ref_cooling": 14.0, "ref_total": 150.0},
    ("apartment_ondol", "1980-2000", "RC"):     {"wall_uvalue": 0.90, "roof_uvalue": 0.70, "window_uvalue": 3.40, "wwr": 0.25, "ref_heating": 81.0,  "ref_cooling": 15.0, "ref_total": 140.0},
    ("apartment_ondol", "2001-2010", "masonry"):{"wall_uvalue": 0.65, "roof_uvalue": 0.45, "window_uvalue": 2.40, "wwr": 0.26, "ref_heating": 58.0,  "ref_cooling": 17.0, "ref_total": 117.0},
    ("apartment_ondol", "2001-2010", "RC"):     {"wall_uvalue": 0.52, "roof_uvalue": 0.35, "window_uvalue": 2.40, "wwr": 0.28, "ref_heating": 52.0,  "ref_cooling": 18.0, "ref_total": 110.0},
    ("apartment_ondol", "post-2010", "masonry"):{"wall_uvalue": 0.40, "roof_uvalue": 0.25, "window_uvalue": 1.50, "wwr": 0.28, "ref_heating": 34.0,  "ref_cooling": 20.0, "ref_total": 90.0},
    ("apartment_ondol", "post-2010", "RC"):     {"wall_uvalue": 0.32, "roof_uvalue": 0.20, "window_uvalue": 1.50, "wwr": 0.30, "ref_heating": 30.0,  "ref_cooling": 21.0, "ref_total": 86.0},

    # =========================================================================
    # Data Center (데이터센터, IDC, 전산센터)
    # 24/7 냉방 지배. PUE 기반 EUI: pre-1980=legacy machine room, post-2010=modern IDC.
    # 난방=0, 온수=0. 냉방+UPS+조명+환기가 전부.
    # ref_total 기준: post-2010 ~330 kWh/m²/yr (PUE 1.4), 2001-2010 ~450 (PUE 1.8).
    # =========================================================================
    ("datacenter", "pre-1980", "RC"):    {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.10, "ref_heating": 0.0,  "ref_cooling": 620.0, "ref_total": 650.0},
    ("datacenter", "pre-1980", "steel"): {"wall_uvalue": 1.70, "roof_uvalue": 2.00, "window_uvalue": 5.80, "wwr": 0.12, "ref_heating": 0.0,  "ref_cooling": 640.0, "ref_total": 670.0},
    ("datacenter", "1980-2000", "RC"):   {"wall_uvalue": 0.80, "roof_uvalue": 0.60, "window_uvalue": 3.40, "wwr": 0.10, "ref_heating": 0.0,  "ref_cooling": 520.0, "ref_total": 545.0},
    ("datacenter", "1980-2000", "steel"):{"wall_uvalue": 0.85, "roof_uvalue": 0.65, "window_uvalue": 3.40, "wwr": 0.12, "ref_heating": 0.0,  "ref_cooling": 540.0, "ref_total": 565.0},
    ("datacenter", "2001-2010", "RC"):   {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.10, "ref_heating": 0.0,  "ref_cooling": 430.0, "ref_total": 450.0},
    ("datacenter", "2001-2010", "steel"):{"wall_uvalue": 0.45, "roof_uvalue": 0.27, "window_uvalue": 2.20, "wwr": 0.12, "ref_heating": 0.0,  "ref_cooling": 445.0, "ref_total": 465.0},
    ("datacenter", "post-2010", "RC"):   {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.10, "ref_heating": 0.0,  "ref_cooling": 308.0, "ref_total": 330.0},
    ("datacenter", "post-2010", "steel"):{"wall_uvalue": 0.25, "roof_uvalue": 0.16, "window_uvalue": 1.40, "wwr": 0.12, "ref_heating": 0.0,  "ref_cooling": 295.0, "ref_total": 315.0},

    # =========================================================================
    # Mixed Residential-Commercial (주거+상업 복합용도)
    # 서울 다세대주거+근린생활시설 복합 건물. apartment와 retail의 중간 특성.
    # ref_total ≈ (apartment + retail) / 2 × 1.05 (복합 오버헤드).
    # =========================================================================
    ("mixed_residential_commercial", "pre-1980", "RC"):    {"wall_uvalue": 1.50, "roof_uvalue": 1.80, "window_uvalue": 5.80, "wwr": 0.35, "ref_heating": 100.0, "ref_cooling": 35.0, "ref_total": 198.0},
    ("mixed_residential_commercial", "pre-1980", "masonry"):{"wall_uvalue": 2.00, "roof_uvalue": 2.20, "window_uvalue": 5.80, "wwr": 0.30, "ref_heating": 110.0, "ref_cooling": 32.0, "ref_total": 205.0},
    ("mixed_residential_commercial", "1980-2000", "RC"):   {"wall_uvalue": 0.76, "roof_uvalue": 0.58, "window_uvalue": 3.40, "wwr": 0.40, "ref_heating": 70.0,  "ref_cooling": 30.0, "ref_total": 162.0},
    ("mixed_residential_commercial", "1980-2000", "masonry"):{"wall_uvalue": 0.90, "roof_uvalue": 0.70, "window_uvalue": 3.40, "wwr": 0.35, "ref_heating": 75.0,  "ref_cooling": 28.0, "ref_total": 168.0},
    ("mixed_residential_commercial", "2001-2010", "RC"):   {"wall_uvalue": 0.47, "roof_uvalue": 0.29, "window_uvalue": 2.40, "wwr": 0.45, "ref_heating": 45.0,  "ref_cooling": 34.0, "ref_total": 130.0},
    ("mixed_residential_commercial", "2001-2010", "masonry"):{"wall_uvalue": 0.55, "roof_uvalue": 0.40, "window_uvalue": 2.40, "wwr": 0.40, "ref_heating": 48.0,  "ref_cooling": 32.0, "ref_total": 135.0},
    ("mixed_residential_commercial", "post-2010", "RC"):   {"wall_uvalue": 0.27, "roof_uvalue": 0.18, "window_uvalue": 1.50, "wwr": 0.45, "ref_heating": 25.0,  "ref_cooling": 32.0, "ref_total": 98.0},
    ("mixed_residential_commercial", "post-2010", "masonry"):{"wall_uvalue": 0.35, "roof_uvalue": 0.25, "window_uvalue": 1.50, "wwr": 0.40, "ref_heating": 28.0,  "ref_cooling": 30.0, "ref_total": 103.0},
}

# ---------------------------------------------------------------------------
# End-use intensity ratios (fraction of ref_total)
# Used to break down total energy into component demands.
# ---------------------------------------------------------------------------

_END_USE_RATIOS: dict[str, dict[str, float]] = {
    "apartment":         {"heating": 0.42, "cooling": 0.10, "hot_water": 0.28, "lighting": 0.10, "ventilation": 0.10},
    "residential_single":{"heating": 0.45, "cooling": 0.08, "hot_water": 0.30, "lighting": 0.08, "ventilation": 0.09},
    "office":            {"heating": 0.30, "cooling": 0.25, "hot_water": 0.10, "lighting": 0.20, "ventilation": 0.15},
    "retail":            {"heating": 0.22, "cooling": 0.30, "hot_water": 0.05, "lighting": 0.25, "ventilation": 0.18},
    "education":         {"heating": 0.38, "cooling": 0.15, "hot_water": 0.15, "lighting": 0.18, "ventilation": 0.14},
    "hospital":          {"heating": 0.32, "cooling": 0.22, "hot_water": 0.20, "lighting": 0.14, "ventilation": 0.12},
    "warehouse":         {"heating": 0.35, "cooling": 0.12, "hot_water": 0.05, "lighting": 0.30, "ventilation": 0.18},
    "cultural":          {"heating": 0.35, "cooling": 0.18, "hot_water": 0.08, "lighting": 0.22, "ventilation": 0.17},
    "mixed_use":         {"heating": 0.28, "cooling": 0.22, "hot_water": 0.25, "lighting": 0.14, "ventilation": 0.11},
    # Phase 4-C 신규 아키타입
    "apartment_district_heating":      {"heating": 0.32, "cooling": 0.12, "hot_water": 0.28, "lighting": 0.15, "ventilation": 0.13},
    "apartment_ondol":                 {"heating": 0.42, "cooling": 0.08, "hot_water": 0.30, "lighting": 0.11, "ventilation": 0.09},
    "datacenter":                      {"heating": 0.01, "cooling": 0.75, "hot_water": 0.00, "lighting": 0.10, "ventilation": 0.14},
    "mixed_residential_commercial":    {"heating": 0.34, "cooling": 0.20, "hot_water": 0.20, "lighting": 0.14, "ventilation": 0.12},
}

_DEFAULT_END_USE: dict[str, float] = {
    "heating": 0.35, "cooling": 0.18, "hot_water": 0.18, "lighting": 0.16, "ventilation": 0.13,
}


# ---------------------------------------------------------------------------
# Vintage classification
# ---------------------------------------------------------------------------

_USAGE_KR_TO_EN: dict[str, str] = {
    # 공동주택
    "공동주택": "apartment",
    "아파트": "apartment",
    "연립주택": "apartment",
    "다세대주택": "apartment",
    # 단독주택
    "단독주택": "residential_single",
    "다가구주택": "residential_single",
    "다중주택": "residential_single",
    # 업무시설
    "업무시설": "office",
    "사무소": "office",
    # 판매·근린
    "판매시설": "retail",
    "근린생활시설": "retail",
    "제1종근린생활시설": "retail",
    "제2종근린생활시설": "retail",
    # 교육
    "교육연구시설": "education",
    "학교": "education",
    "수련시설": "education",
    "도서관": "education",
    # 의료·노유자
    "의료시설": "hospital",
    "병원": "hospital",
    "노유자시설": "hospital",
    "사회복지시설": "hospital",
    # 창고·공장
    "창고시설": "warehouse",
    "공장": "warehouse",
    "산업시설": "warehouse",
    "위험물저장및처리시설": "warehouse",
    "자동차관련시설": "warehouse",
    "운수시설": "warehouse",
    # 문화·종교·집회
    "문화및집회시설": "cultural",
    "종교시설": "cultural",
    "집회시설": "cultural",
    "관람집회시설": "cultural",
    "전시시설": "cultural",
    "장례시설": "cultural",
    # 숙박·복합·운동
    "숙박시설": "mixed_use",
    "운동시설": "mixed_use",
    "복합건축물": "mixed_use",
    "관광휴게시설": "mixed_use",
    # Phase 4-C 신규 아키타입
    "지역난방공동주택": "apartment_district_heating",
    "지역난방아파트":   "apartment_district_heating",
    "온돌주택":         "apartment_ondol",
    "데이터센터":       "datacenter",
    "정보통신시설":     "datacenter",
    "전산센터":         "datacenter",
    "통신시설":         "datacenter",
    "주거복합":         "mixed_residential_commercial",
    "복합용도":         "mixed_residential_commercial",
}


def _normalize_usage(usage_type: str) -> str:
    """Translate Korean usage types to English archetype keys.

    Returns the English key if a mapping exists, otherwise returns the
    input unchanged (will fall through to archetype fallback logic).
    """
    if not usage_type:
        return "apartment"  # default for empty/None
    usage = usage_type.lower().strip()
    # Try exact match first, then normalised
    result = _USAGE_KR_TO_EN.get(usage_type.strip(), None)
    if result is None:
        result = _USAGE_KR_TO_EN.get(usage, usage)
    return result


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
        Building usage category (Korean or English).
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

    - Apartments:          ~136 kWh/m2/yr average
    - Residential single:  ~145 kWh/m2/yr average
    - Offices:             ~159 kWh/m2/yr average
    - Retail:              ~160 kWh/m2/yr average
    - Education:           ~140 kWh/m2/yr average
    - Hospitals:           ~190 kWh/m2/yr average
    - Warehouse:           ~70  kWh/m2/yr average
    - Cultural:            ~120 kWh/m2/yr average
    - Mixed use:           ~165 kWh/m2/yr average

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


# ---------------------------------------------------------------------------
# Korean_BB lookup integration (Phase A / A')
# ---------------------------------------------------------------------------

# archetypes.py usage key → Korean_BB archetype ID
_USAGE_TO_KBB: dict[str, str] = {
    "apartment":                       "apartment_highrise",  # floors_above ≤ 5 → midrise (런타임 판별)
    "residential_single":              "apartment_midrise",
    "office":                          "office",
    "retail":                          "small_office",         # 근린생활시설 → small_office
    "education":                       "school",
    "hospital":                        "hospital",
    "warehouse":                       "warehouse",
    "cultural":                        "retail",               # 문화/집회 → retail 유사
    "mixed_use":                       "hotel",                # 숙박/복합 → hotel 유사
    # Phase 4-C
    "apartment_district_heating":      "apartment_highrise",   # 지역난방 고층 = highrise
    "apartment_ondol":                 "apartment_midrise",    # 온돌 단독 = midrise 유사
    "datacenter":                      "warehouse",            # Korean_BB 내 가장 유사
    "mixed_residential_commercial":    "small_office",         # 복합용도 → small_office 유사
}


def _built_year_to_kbb_vintage(built_year: int | None) -> str:
    """Map built_year to Korean_BB vintage label (5-구간)."""
    if built_year is None:
        return "2001_2010"
    if built_year < 1990:
        return "pre1990"
    if built_year < 2001:
        return "1991_2000"
    if built_year < 2011:
        return "2001_2010"
    if built_year < 2018:
        return "2011_2017"
    return "2018_plus"


def get_korean_bb_eui(
    usage_type: str,
    built_year: int | None,
    floors_above: int | None = None,
    city: str = "seoul",
) -> float | None:
    """Calibrated EUI (kWh/m²/yr) from Korean_BB lookup + Tier 1 correction.

    Korean_BB 시뮬레이션 EUI 중앙값에 Tier 1 실측 기반 보정계수를 곱한다.

    Parameters
    ----------
    usage_type:
        한국어 용도 (예: "업무시설", "제2종근린생활시설")
    built_year:
        준공 연도. None 이면 2001-2010 vintage로 처리.
    floors_above:
        지상 층수. 아파트 고층/중층 구분에 사용.
    city:
        도시 소문자 영문. Korean_BB 지원: seoul/busan/daegu/gangneung/jeju.
        미지원 도시(incheon/gwangju/daejeon/cheongju/ulsan)는 ems_transformer
        E0 보정계수를 자동 적용. 기본: 'seoul'.

    Returns
    -------
    float | None
        보정된 EUI (kWh/m²/yr). 룩업 실패 시 None.
    """
    try:
        from src.simulation.eui_lookup import lookup_eui
        from src.simulation.calibration_factors import get_correction_factor
    except ImportError:
        logger.debug("eui_lookup / calibration_factors 모듈 없음 → Korean_BB 룩업 생략")
        return None

    usage = _normalize_usage(usage_type)

    # datacenter: Korean_BB에 대응 아키타입 없음 → fallback(ARCHETYPE_PARAMS)에 위임
    if usage == "datacenter":
        return None

    # 아파트: 층수 기반 고층/중층 구분
    if usage == "apartment":
        kbb_arch = (
            "apartment_midrise"
            if (floors_above is not None and floors_above <= 5)
            else "apartment_highrise"
        )
    else:
        kbb_arch = _USAGE_TO_KBB.get(usage, "small_office")

    vintage = _built_year_to_kbb_vintage(built_year)

    stats = lookup_eui(kbb_arch, vintage, city)
    if stats is None:
        logger.debug("Korean_BB 룩업 실패: arch=%s vintage=%s city=%s", kbb_arch, vintage, city)
        return None

    cf = get_correction_factor(usage_type)
    calibrated = stats["median"] * cf
    logger.debug(
        "Korean_BB EUI: arch=%s vintage=%s median=%.1f cf=%.3f → %.1f kWh/m²/yr",
        kbb_arch, vintage, stats["median"], cf, calibrated,
    )
    return round(calibrated, 1)
