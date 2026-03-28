"""Phase A: Korean_BB npy → EUI 룩업테이블 추출

Korean_BB 시뮬레이션 결과에서 (archetype, vintage, city)별 연간 EUI를 계산하여
src/simulation/eui_lookup.py로 저장한다.

에너지 합산 방식:
  total_kwh = hourly_electricity + hourly_gas + hourly_water_gas (NaN=0)
  hourly_electricity = Electricity:Facility (전기 전체, 냉방/조명/장비/팬/펌프 포함)
  hourly_gas         = Heating:NaturalGas (가스 난방)
  hourly_water_gas   = WaterSystems:NaturalGas (가스 급탕)
  * hourly_cooling / hourly_heating은 hourly_electricity의 부분계량 → 합산 제외

바닥면적:
  gfa_range 중간값을 사용 (각 시뮬레이션의 실제 연면적이 metadata에 없으므로)
  Tier 1 보정계수(Phase A')가 이 오차를 흡수한다.

Usage:
    python -m src.data_ingestion.extract_eui_lookup
    python -m src.data_ingestion.extract_eui_lookup --max-samples 100 --output-csv
"""
import sys
import os
import re
import json
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 경로 ───────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KOREAN_BB_ROOT = Path("C:/Users/User/Desktop/myjob/8.simulation/Korean_BB")
NPY_DIR = KOREAN_BB_ROOT / "simulations" / "npy_tier_a"
OUTPUT_PY = PROJECT_ROOT / "src" / "simulation" / "eui_lookup.py"

# ── 아키타입별 ASHRAE 90.1-2019 기준 연면적 (m²) ──────────────────────────
# Korean_BB는 ASHRAE 프로토타입 IDF 기하를 그대로 사용 (HVAC/외피/스케줄만 교체).
# 출처: idf_mapping.yaml ASHRAE 원본 + ASHRAE 90.1-2019 Commercial Reference Buildings
GFA_MIDPOINT: dict[str, float] = {
    "apartment_highrise": 17471,   # ASHRAE901_ApartmentHighRise: 17,471 m²
    "apartment_midrise":   3135,   # ASHRAE901_ApartmentMidRise:   3,135 m²
    "office":              4982,   # ASHRAE901_OfficeMedium:        4,982 m²
    "school":              6871,   # ASHRAE901_SchoolPrimary:       6,871 m²
    "university":          6871,   # Primary School 기반 (동일 IDF)
    "retail":              2294,   # ASHRAE901_RetailStandalone:    2,294 m²
    "hospital":           22422,   # ASHRAE901_Hospital:           22,422 m²
    "hotel":               4013,   # ASHRAE901_HotelSmall:          4,013 m²
    "small_office":         511,   # ASHRAE901_OfficeSmall:           511 m²
    "large_office":       46320,   # ASHRAE901_OfficeLarge:        46,320 m²
    "warehouse":           4835,   # ASHRAE901_Warehouse:           4,835 m²
    "restaurant_full":      511,   # ASHRAE901_RestaurantSitDown:     511 m²
    "restaurant_quick":     232,   # ASHRAE901_RestaurantFastFood:    232 m²
    "strip_mall":          2090,   # ASHRAE901_RetailStripmall:     2,090 m²
}

# ── vintage 레이블 정규화 ───────────────────────────────────────────────────
VINTAGE_LABEL: dict[str, str] = {
    "v1_pre1990":   "pre1990",
    "v2_1991_2000": "1991_2000",
    "v3_2001_2010": "2001_2010",
    "v4_2011_2017": "2011_2017",
    "v5_2018_plus": "2018_plus",
}


def compute_annual_eui(npy_dir: Path, gfa_m2: float) -> float | None:
    """단일 시뮬레이션 디렉토리 → EUI (kWh/m²/yr). 실패 시 None 반환."""
    try:
        elec = np.load(npy_dir / "hourly_electricity.npy")
        gas = np.load(npy_dir / "hourly_gas.npy")
        wg_path = npy_dir / "hourly_water_gas.npy"
        wg = np.load(wg_path) if wg_path.exists() else np.zeros(8760)

        total_kwh = float(np.nansum(elec) + np.nansum(gas) + np.nansum(wg))
        if total_kwh <= 0 or gfa_m2 <= 0:
            return None
        return total_kwh / gfa_m2
    except Exception:
        return None


def _parse_dir_name(name: str) -> tuple[str, str, str] | None:
    """디렉토리명 → (archetype, vintage_raw, city) 파싱.

    패턴: {archetype}_{vintage}_{city}_tmy_p{NNNN}
    예: apartment_highrise_v1_pre1990_seoul_tmy_p0042
    """
    # _tmy_p 기준으로 앞부분만 추출
    m = re.match(r'^(.+)_tmy_p\d+$', name)
    if not m:
        return None
    prefix = m.group(1)   # e.g. apartment_highrise_v1_pre1990_seoul

    cities = {"seoul", "busan", "daegu", "gangneung", "jeju",
              "chuncheon", "wonju", "incheon", "daejeon", "sejong", "gwangju", "ulsan"}
    vintages = {"v1_pre1990", "v2_1991_2000", "v3_2001_2010", "v4_2011_2017", "v5_2018_plus"}

    # city는 마지막 토큰
    parts = prefix.split("_")
    for ci in range(len(parts) - 1, 0, -1):
        city = "_".join(parts[ci:])
        if city in cities:
            rest = "_".join(parts[:ci])
            # vintage는 rest 뒤 부분 (v1_pre1990 등)
            for vi in range(len(parts[:ci]) - 1, 0, -1):
                vintage = "_".join(parts[vi:ci])
                if vintage in vintages:
                    archetype = "_".join(parts[:vi])
                    return archetype, vintage, city
    return None


def collect_eui_samples(max_samples: int = 200) -> pd.DataFrame:
    """npy_tier_a 디렉토리명 파싱 → 고유 combo 목록 구성 → EUI 계산.

    catalog.csv 대신 디렉토리명을 직접 파싱하므로 catalog에 없는 시뮬도 포함한다.
    디렉토리 목록은 os.scandir 1회로 수집하여 metadata.json 읽기 불필요.
    """
    import os
    from collections import defaultdict

    # combo → building_id 목록 (순서 유지)
    combos: dict[tuple, list[str]] = defaultdict(list)

    with os.scandir(NPY_DIR) as it:
        for entry in it:
            if not entry.is_dir():
                continue
            parsed = _parse_dir_name(entry.name)
            if parsed is None:
                continue
            arch, vintage, city = parsed
            combos[(arch, vintage, city)].append(entry.name)

    total = len(combos)
    print(f"  고유 (archetype, vintage, city) 조합: {total}")

    rows = []
    for idx, ((arch, vintage, city), all_ids) in enumerate(sorted(combos.items())):
        gfa = GFA_MIDPOINT.get(arch)
        if gfa is None:
            continue

        vintage_label = VINTAGE_LABEL.get(vintage, vintage)
        sample_ids = sorted(all_ids)[:max_samples]

        for bid in sample_ids:
            eui_val = compute_annual_eui(NPY_DIR / bid, gfa)
            if eui_val is not None and 5 < eui_val < 5000:
                rows.append({
                    "archetype": arch,
                    "vintage":   vintage_label,
                    "city":      city,
                    "eui":       round(eui_val, 2),
                })

        if (idx + 1) % 50 == 0:
            print(f"  진행: {idx+1}/{total}")

    return pd.DataFrame(rows)


def aggregate(df: pd.DataFrame) -> dict:
    """(archetype, vintage, city)별 중앙값/p10/p90 집계."""
    result = {}
    for (arch, vintage, city), grp in df.groupby(["archetype", "vintage", "city"]):
        euis = grp["eui"].values
        result[(arch, vintage, city)] = {
            "median": round(float(np.median(euis)), 1),
            "p10":    round(float(np.percentile(euis, 10)), 1),
            "p90":    round(float(np.percentile(euis, 90)), 1),
            "n":      len(euis),
        }
    return result


def write_lookup_py(agg: dict) -> None:
    """집계 결과를 Python 모듈로 저장."""
    lines = [
        '"""EUI 룩업테이블 — Korean_BB 시뮬레이션 기반 (Phase A 자동 생성)',
        '',
        '(archetype, vintage, city) → {median, p10, p90, n}',
        '',
        '에너지 단위: kWh/m²/yr (연면적 = GFA 중간값 기준)',
        '보정계수 적용 전 원시 시뮬레이션 값. 실사용 시 calibration_factors.py 참조.',
        '"""',
        '',
        'from typing import TypedDict',
        '',
        'class EUIStats(TypedDict):',
        '    median: float',
        '    p10: float',
        '    p90: float',
        '    n: int',
        '',
        '# (archetype, vintage, city) → EUIStats',
        'EUI_LOOKUP: dict[tuple[str, str, str], EUIStats] = {',
    ]

    for (arch, vintage, city), stats in sorted(agg.items()):
        lines.append(
            f'    ("{arch}", "{vintage}", "{city}"): '
            f'{{"median": {stats["median"]}, "p10": {stats["p10"]}, '
            f'"p90": {stats["p90"]}, "n": {stats["n"]}}},'
        )

    lines += [
        '}',
        '',
        '',
        'def lookup_eui(',
        '    archetype: str,',
        '    vintage: str,',
        '    city: str = "seoul",',
        ') -> EUIStats | None:',
        '    """(archetype, vintage, city) → EUIStats. city 불일치 시 seoul 폴백."""',
        '    key = (archetype, vintage, city)',
        '    if key in EUI_LOOKUP:',
        '        return EUI_LOOKUP[key]',
        '    # city 폴백: seoul',
        '    key_seoul = (archetype, vintage, "seoul")',
        '    return EUI_LOOKUP.get(key_seoul)',
    ]

    OUTPUT_PY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  저장: {OUTPUT_PY}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-samples", type=int, default=200,
                        help="조합당 최대 샘플 수 (기본 200)")
    parser.add_argument("--output-csv", action="store_true",
                        help="중간 데이터프레임을 scratch/에 CSV로 저장")
    args = parser.parse_args()

    print("=== Phase A: Korean_BB EUI 룩업테이블 추출 ===")
    print(f"  NPY 디렉토리: {NPY_DIR}")
    print(f"  조합당 최대 샘플: {args.max_samples}")

    print("\n[1] npy 파일 순회 중...")
    df = collect_eui_samples(max_samples=args.max_samples)
    print(f"  유효 샘플: {len(df)}건")

    if args.output_csv:
        scratch = PROJECT_ROOT / "scratch"
        scratch.mkdir(exist_ok=True)
        csv_path = scratch / "eui_samples_raw.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"  CSV 저장: {csv_path}")

    print("\n[2] 집계 중 (archetype × vintage × city)...")
    agg = aggregate(df)
    print(f"  집계 조합: {len(agg)}개")

    # 요약 출력
    summary = (
        df.groupby(["archetype", "vintage"])["eui"]
        .agg(["median", "mean", "std", "count"])
        .round(1)
    )
    print("\n[archetype × vintage 요약 (도시 평균)]")
    print(summary.to_string())

    print("\n[3] eui_lookup.py 생성 중...")
    write_lookup_py(agg)
    print("\n완료.")


if __name__ == "__main__":
    main()
