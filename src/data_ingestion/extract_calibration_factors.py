"""Phase A': Tier 1 실측 vs Korean_BB 시뮬 → 보정계수 산출

한국 건물 실측 데이터(Tier 1, 79건 유효)와 Korean_BB 시뮬레이션 EUI 중앙값을
비교하여 용도별 보정계수(correction_factor)를 계산한다.

보정계수 정의:
    correction_factor = median(tier1_eui) / median(korean_bb_eui)
    보정된 EUI = korean_bb_eui × correction_factor

사용 조건:
    - Phase A(extract_eui_lookup.py)를 먼저 실행하여 eui_lookup.py를 생성할 것
    - DB에 Tier 1 데이터가 적재되어 있을 것

이상치 기준 (실측):
    - EUI < 10 kWh/m²/yr: 미보고 또는 오기입
    - EUI > 400 kWh/m²/yr: 특수용도(교정/군사) 또는 개인정보 캡값(500.1)
    - 동일 PNU 중복: is_current=true + DISTINCT ON으로 처리

Usage:
    python -m src.data_ingestion.extract_calibration_factors
    python -m src.data_ingestion.extract_calibration_factors --output-csv
"""
import sys
import os
import argparse
import json
import numpy as np
import pandas as pd
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 경로 ───────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PY = PROJECT_ROOT / "src" / "simulation" / "calibration_factors.py"

# ── 한국 usage_type → Korean_BB archetype 매핑 ────────────────────────────
# 매핑 불가 용도(공동주택, 숙박 등)는 None → 해당 용도는 보정계수 1.0(미보정)
USAGE_TO_ARCHETYPE: dict[str, str | None] = {
    # 근린생활시설 → small_office (소규모 상업/서비스)
    "제1종근린생활시설":  "small_office",
    "제2종근린생활시설":  "small_office",
    "근린생활시설":       "small_office",
    # 업무시설 → office
    "업무시설":           "office",
    # 교육 → school
    "교육연구시설":       "school",
    # 의료 → hospital
    "의료시설":           "hospital",
    "노유자시설":         "hospital",   # 노인/어린이 시설: 유사 부하 패턴
    # 공동주택 → apartment_midrise (고층 구분 불가한 경우 midrise 기본)
    "공동주택":           "apartment_midrise",
    "아파트":             "apartment_highrise",
    # 문화/집회 → retail (복합 상업)
    "문화및집회시설":     "retail",
    "판매시설":           "retail",
    # 종교시설, 숙박, 기타 → 보정 불가
    "종교시설":           None,
    "숙박시설":           None,
    "위락시설":           None,
    "공장":               None,
    "창고시설":           "warehouse",
    "운동시설":           None,
    "관광휴게시설":       None,
}

# ── vintage 매핑: built_year → Korean_BB vintage ──────────────────────────
def year_to_vintage(built_year: int | None) -> str:
    if built_year is None:
        return "2001_2010"   # 중간값으로 폴백
    if built_year < 1990:
        return "pre1990"
    if built_year < 2001:
        return "1991_2000"
    if built_year < 2011:
        return "2001_2010"
    if built_year < 2018:
        return "2011_2017"
    return "2018_plus"


TIER1_CACHE_CSV = PROJECT_ROOT / "scratch" / "tier1_eui.csv"


def fetch_tier1_data(db_url: str) -> pd.DataFrame:
    """DB에서 Tier 1 유효 데이터 조회.

    psycopg2 미설치 환경에서는 scratch/tier1_eui.csv 캐시를 우선 사용한다.
    캐시 생성 명령:
        docker compose exec db psql -U postgres -d buildings \\
          -c "SELECT DISTINCT ON (er.pnu) er.pnu, er.total_energy AS eui,
              be.usage_type, be.built_year
              FROM energy_results er JOIN buildings_enriched be ON er.pnu = be.pnu
              WHERE er.data_tier=1 AND er.is_current=true
              AND er.total_energy BETWEEN 10 AND 400
              ORDER BY er.pnu, er.total_energy;" --csv > scratch/tier1_eui.csv
    """
    if TIER1_CACHE_CSV.exists():
        print(f"  캐시 사용: {TIER1_CACHE_CSV}")
        return pd.read_csv(TIER1_CACHE_CSV)

    try:
        import sqlalchemy as sa
    except ImportError:
        raise RuntimeError(
            "psycopg2/sqlalchemy 미설치. scratch/tier1_eui.csv 캐시를 먼저 생성하세요.\n"
            "docker compose exec db psql ... --csv > scratch/tier1_eui.csv"
        )

    query = """
        SELECT DISTINCT ON (er.pnu)
            er.pnu,
            er.total_energy AS eui,
            be.usage_type,
            be.built_year
        FROM energy_results er
        JOIN buildings_enriched be ON er.pnu = be.pnu
        WHERE er.data_tier = 1
          AND er.is_current = true
          AND er.total_energy BETWEEN 10 AND 400
        ORDER BY er.pnu, er.total_energy
    """
    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        df = pd.read_sql_query(sa.text(query), conn)
    return df


def load_eui_lookup() -> dict:
    """생성된 eui_lookup.py에서 EUI_LOOKUP 딕셔너리 로드."""
    lookup_path = PROJECT_ROOT / "src" / "simulation" / "eui_lookup.py"
    if not lookup_path.exists():
        raise FileNotFoundError(
            f"eui_lookup.py가 없습니다: {lookup_path}\n"
            "먼저 extract_eui_lookup.py를 실행하세요."
        )
    # exec로 동적 로드
    ns: dict = {}
    exec(lookup_path.read_text(encoding="utf-8"), ns)
    return ns["EUI_LOOKUP"]


def compute_correction_factors(
    tier1_df: pd.DataFrame,
    eui_lookup: dict,
    city: str = "seoul",
) -> pd.DataFrame:
    """
    usage_type별 보정계수 계산.

    Returns:
        DataFrame with columns: usage_category, archetype, n_tier1,
        median_tier1, median_simul, correction_factor
    """
    rows = []

    # usage_type별 그룹
    for usage_type, grp in tier1_df.groupby("usage_type"):
        archetype = USAGE_TO_ARCHETYPE.get(usage_type)
        if archetype is None:
            print(f"  ⚠️  매핑 없음: {usage_type} ({len(grp)}건) → 보정계수 1.0")
            rows.append({
                "usage_type":        usage_type,
                "archetype":         "N/A",
                "n_tier1":           len(grp),
                "median_tier1":      round(float(grp["eui"].median()), 1),
                "median_simul":      None,
                "correction_factor": 1.0,
                "note":              "매핑 불가 → 미보정",
            })
            continue

        # Tier 1 중앙값
        tier1_median = float(grp["eui"].median())

        # Korean_BB EUI: vintage별 가중 중앙값
        # vintage 분포 계산
        vintage_counts: dict[str, int] = {}
        for _, row in grp.iterrows():
            v = year_to_vintage(row.get("built_year"))
            vintage_counts[v] = vintage_counts.get(v, 0) + 1

        # vintage별 Korean_BB median 수집
        simul_euis = []
        for vintage, cnt in vintage_counts.items():
            stats = eui_lookup.get((archetype, vintage, city))
            if stats is None:
                # city 폴백 → 모든 도시 평균
                all_cities = ["seoul", "busan", "daegu", "gangneung", "jeju"]
                for c in all_cities:
                    stats = eui_lookup.get((archetype, vintage, c))
                    if stats:
                        break
            if stats:
                simul_euis.extend([stats["median"]] * cnt)

        if not simul_euis:
            print(f"  ⚠️  Korean_BB EUI 없음: {archetype} → 보정계수 1.0")
            rows.append({
                "usage_type":        usage_type,
                "archetype":         archetype,
                "n_tier1":           len(grp),
                "median_tier1":      round(tier1_median, 1),
                "median_simul":      None,
                "correction_factor": 1.0,
                "note":              "시뮬 데이터 없음 → 미보정",
            })
            continue

        simul_median = float(np.median(simul_euis))
        if simul_median <= 0:
            cf = 1.0
        else:
            cf = round(tier1_median / simul_median, 4)

        rows.append({
            "usage_type":        usage_type,
            "archetype":         archetype,
            "n_tier1":           len(grp),
            "median_tier1":      round(tier1_median, 1),
            "median_simul":      round(simul_median, 1),
            "correction_factor": cf,
            "note":              "",
        })

    return pd.DataFrame(rows)


def write_calibration_py(result_df: pd.DataFrame) -> None:
    """보정계수 Python 모듈로 저장."""
    lines = [
        '"""보정계수 — Tier 1 실측 vs Korean_BB 시뮬 비교 (Phase A\' 자동 생성)',
        '',
        'correction_factor = median(tier1_eui) / median(korean_bb_eui)',
        '보정된 EUI = korean_bb_eui × correction_factor',
        '',
        'n_tier1이 작은 용도(≤5건)는 신뢰도 낮음 — 주석 참조.',
        '"""',
        '',
        '# usage_type → correction_factor',
        '# (archetype, n_tier1, tier1_median, simul_median 은 참고용)',
        'CORRECTION_FACTORS: dict[str, float] = {',
    ]

    for _, row in result_df.iterrows():
        note = f"  # n={row['n_tier1']}, tier1={row['median_tier1']}, simul={row['median_simul']}"
        if row["note"]:
            note += f", {row['note']}"
        lines.append(f'    "{row["usage_type"]}": {row["correction_factor"]},{note}')

    lines += [
        '}',
        '',
        'DEFAULT_FACTOR = 1.0  # 매핑 없는 용도',
        '',
        '',
        'def get_correction_factor(usage_type: str) -> float:',
        '    """usage_type에 대한 보정계수 반환. 미등록 용도는 1.0."""',
        '    return CORRECTION_FACTORS.get(usage_type, DEFAULT_FACTOR)',
    ]

    OUTPUT_PY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  저장: {OUTPUT_PY}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:password@localhost:5434/buildings",
    ))
    parser.add_argument("--city", default="seoul",
                        help="Korean_BB 참조 도시 (기본: seoul)")
    parser.add_argument("--output-csv", action="store_true",
                        help="비교 결과를 scratch/에 CSV로 저장")
    args = parser.parse_args()

    print("=== Phase A': Tier 1 실측 vs Korean_BB → 보정계수 산출 ===")

    print("\n[1] eui_lookup.py 로드 중...")
    eui_lookup = load_eui_lookup()
    print(f"  EUI 조합 수: {len(eui_lookup)}")

    print("\n[2] DB Tier 1 데이터 조회 중...")
    tier1_df = fetch_tier1_data(args.db_url)
    print(f"  유효 건물 수: {len(tier1_df)}")
    print(f"  용도 분포:\n{tier1_df['usage_type'].value_counts().to_string()}")

    print("\n[3] 보정계수 계산 중...")
    result_df = compute_correction_factors(tier1_df, eui_lookup, city=args.city)

    print("\n[결과]")
    print(result_df[["usage_type", "archetype", "n_tier1",
                      "median_tier1", "median_simul", "correction_factor"]].to_string(index=False))

    if args.output_csv:
        scratch = PROJECT_ROOT / "scratch"
        scratch.mkdir(exist_ok=True)
        csv_path = scratch / "calibration_comparison.csv"
        result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"\n  CSV 저장: {csv_path}")

    print("\n[4] calibration_factors.py 생성 중...")
    write_calibration_py(result_df)
    print("\n완료.")


if __name__ == "__main__":
    main()
