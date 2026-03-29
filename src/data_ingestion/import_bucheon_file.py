"""부천시 건축물대장 파일 데이터 임포트

data.go.kr/data/15144153 에서 다운로드한 CSV/Excel 파일을 building_ledger에 적재.

부천시 API(sigunguCd=41190)는 국토부 DB 마이그레이션 불완전으로 영구 0건 반환.
파일 다운로드 방식만 가능.

지원 파일 형식:
  - API 형식: 컬럼명이 sigunguCd, bjdongCd 등 (영문/한글 코드)
  - 원문 파일 형식: 컬럼명이 '대지위치', '시군구', '법정동' 등 (세움터 추출)

Usage:
    # 파일 다운로드: https://www.data.go.kr/data/15144153/fileData.do
    python -m src.data_ingestion.import_bucheon_file --file scratch/bucheon_ledger.csv
    python -m src.data_ingestion.import_bucheon_file --file scratch/bucheon_ledger.xlsx
"""
import sys
import os
import re
import argparse
import logging
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 부천시 법정동 코드 매핑 (행안부 법정동 코드 41190 계열)
BUCHEON_SIGUNGU_CD = "41190"

BUCHEON_DONG_CODE: dict[str, str] = {
    "계수동":   "10100",
    "괴안동":   "10200",
    "범박동":   "10300",
    "범안동":   "10400",
    "소사동":   "10500",
    "소사본동": "10600",
    "송내동":   "10700",
    "심곡본동": "10800",
    "고강동":   "10900",
    "내동":     "11000",
    "도당동":   "11100",
    "삼정동":   "11200",
    "오정동":   "11300",
    "여월동":   "11400",
    "중동":     "11500",
    "상동":     "11600",
    "약대동":   "11700",
    "심곡동":   "11800",
    "원미동":   "11900",
    "춘의동":   "12000",
    "역곡동":   "12100",
    "성곡동":   "12200",
    "원종동":   "12300",
    "대장동":   "12400",
    "옥길동":   "12500",
    "작동":     "12600",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """컬럼명을 building_ledger 스키마에 맞게 정규화."""
    col_map = {
        # 한글 컬럼명 (API 포맷)
        "대지위치":         "plat_plc",
        "시군구코드":       "sigungu_cd",
        "법정동코드":       "bjdong_cd",
        "대지구분코드":     "plat_gb_cd",
        "번":               "bun",
        "지":               "ji",
        "관리건축물대장PK": "mgm_bldrgst_pk",
        "건물명":           "bld_nm",
        "주용도코드":       "main_purps_cd",
        "주용도코드명":     "main_purps_nm",
        "기타용도":         "etc_purps",
        "지상층수":         "grnd_flr_cnt",
        "지하층수":         "ugrnd_flr_cnt",
        "승강기수":         "rideuse_elvt_cnt",
        "비상용승강기수":   "emgen_elvt_cnt",
        "건축면적(㎡)":     "bld_area",
        "연면적(㎡)":       "tot_area",
        "대지면적(㎡)":     "plat_area",
        "건폐율(%)":        "bcrat",
        "용적률산정연면적(㎡)": "vlrat_fltarea",
        "용적률(%)":        "vlrat",
        "주구조코드":       "strct_cd",
        "주구조코드명":     "strct_nm",
        "지붕코드":         "roof_cd",
        "지붕코드명":       "roof_nm",
        "사용승인일":       "use_apr_day",
        "허가일":           "pmsday",
        "착공일":           "stcnsday",
        "에너지효율등급":   "enrgy_eff_rate",
        "EPI점수":          "epi_score",
        # 영문 컬럼명 (API 포맷)
        "platPlc":          "plat_plc",
        "sigunguCd":        "sigungu_cd",
        "bjdongCd":         "bjdong_cd",
        "platGbCd":         "plat_gb_cd",
        "bldNm":            "bld_nm",
        "mainPurpsCd":      "main_purps_cd",
        "mainPurpsNm":      "main_purps_nm",
        "grndFlrCnt":       "grnd_flr_cnt",
        "ugndFlrCnt":       "ugrnd_flr_cnt",
        "bldArea":          "bld_area",
        "totArea":          "tot_area",
        "platArea":         "plat_area",
        "strctCd":          "strct_cd",
        "strctNm":          "strct_nm",
        "useAprDay":        "use_apr_day",
        "enrgyEffRate":     "enrgy_eff_rate",
        "epiScore":         "epi_score",
        # 세움터 원문 파일 컬럼명
        "건축면적(제곱미터)":     "bld_area",
        "연면적(제곱미터)":       "tot_area",
        "대지면적(제곱미터)":     "plat_area",
        "건폐율(퍼센트)":         "bcrat",
        "용적율(퍼센트)":         "vlrat",
        "용적율_산정_연면적(제곱미터)": "vlrat_fltarea",
        "주구조":                  "strct_nm",
        "주용도":                  "main_purps_nm",
        "사용승인일":              "use_apr_day",
        "승용승강기 수":           "rideuse_elvt_cnt",
        "비상용승강기 수":         "emgen_elvt_cnt",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return df


def _parse_jibun_address(addr: str) -> tuple[str | None, int | None, int | None]:
    """지번 주소(대지위치)에서 법정동명, 번, 지를 파싱.

    예: '경기도 부천시 소사구 계수동 67-5'     → ('계수동', 67, 5)
        '경기도 부천시 원미구 중동 100'         → ('중동', 100, 0)
        '경기도 부천시 오정구 대장동 산 1-2'   → ('대장동', 1, 2)   # 산 번지
        '경기도 부천시 소사구 계수동 67-5번지'  → ('계수동', 67, 5)  # 번지 접미사
    """
    if not isinstance(addr, str):
        return None, None, None
    parts = addr.strip().split()
    if len(parts) < 2:
        return None, None, None

    # '번지' 접미사 제거
    last = parts[-1].rstrip("번지").strip()

    # 산 번지: '... 동명 산 1-2' 형태
    if parts[-1] == "산" or (len(parts) >= 2 and parts[-2] == "산"):
        # 번지 부분이 별도 토큰 '산'인 경우 → 번지 없이 파싱 불가, dong만 반환
        dong_idx = -3 if parts[-2] == "산" else -2
        dong = parts[dong_idx] if len(parts) >= abs(dong_idx) else None
        # 산 번지는 plat_gb_cd=2 이므로 PNU 생성 로직 별도 처리 필요
        # 여기서는 번호만 파싱 시도
        bun_ji_str = last if parts[-1] != "산" else ""
        if not bun_ji_str:
            return dong, None, None
    else:
        dong = parts[-2] if len(parts) >= 2 else None
        bun_ji_str = last

    m = re.match(r"^(\d+)(?:-(\d+))?$", bun_ji_str)
    if not m:
        return dong, None, None
    bun = int(m.group(1))
    ji = int(m.group(2)) if m.group(2) else 0
    return dong, bun, ji


def _derive_pnu_from_address(df: pd.DataFrame) -> pd.DataFrame:
    """'대지위치' 컬럼에서 PNU를 생성한다 (원문 파일 형식 전용)."""
    rows = []
    unknown_dongs: set[str] = set()
    for _, row in df.iterrows():
        plat_plc = row.get("plat_plc", "")
        dong, bun, ji = _parse_jibun_address(plat_plc)
        if dong is None or bun is None:
            rows.append(None)
            continue
        bjdong_cd = BUCHEON_DONG_CODE.get(dong)
        if bjdong_cd is None:
            unknown_dongs.add(dong)
            rows.append(None)
            continue
        pnu = f"{BUCHEON_SIGUNGU_CD}{bjdong_cd}1{str(bun).zfill(4)}{str(ji).zfill(4)}"
        rows.append(pnu)
    if unknown_dongs:
        logger.warning("알 수 없는 법정동 (PNU 생성 불가): %s", unknown_dongs)
    df = df.copy()
    df["pnu"] = rows
    df["sigungu_cd"] = BUCHEON_SIGUNGU_CD
    return df


def _generate_pnu(row: pd.Series) -> str | None:
    """행 데이터에서 PNU 19자리 생성 (API 포맷 전용)."""
    sigungu = str(row.get("sigungu_cd", BUCHEON_SIGUNGU_CD)).zfill(5)
    bdong = str(row.get("bjdong_cd", "")).zfill(5)
    plat_gb = str(row.get("plat_gb_cd", "0")).zfill(1)
    bun = str(row.get("bun", "0")).zfill(4)
    ji = str(row.get("ji", "0")).zfill(4)
    if not bdong or bdong == "00000":
        return None
    return f"{sigungu}{bdong}{plat_gb}{bun}{ji}"


def _detect_format(df: pd.DataFrame) -> str:
    """CSV 컬럼명으로 파일 형식을 감지한다."""
    if "대지위치" in df.columns and "시군구" in df.columns and "법정동" in df.columns:
        return "raw"   # 세움터 원문 파일
    return "api"       # API 포맷


def load_bucheon_file(filepath: str) -> pd.DataFrame:
    """CSV 또는 Excel 파일 로드 (CP949/UTF-8 자동 감지)."""
    path = Path(filepath)
    logger.info("파일 로드: %s", path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        for enc in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                df = pd.read_csv(path, dtype=str, encoding=enc)
                logger.info("인코딩 감지: %s", enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        else:
            raise ValueError(f"파일 인코딩 감지 실패: {filepath}")
    logger.info("원본 행수: %d, 컬럼: %s", len(df), list(df.columns[:8]))
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="다운로드한 CSV/Excel 경로")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    parser.add_argument("--db-url", default=db_url)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df = load_bucheon_file(args.file)
    fmt = _detect_format(df)
    logger.info("파일 형식 감지: %s", fmt)

    df = _normalize_columns(df)

    if fmt == "raw":
        # 세움터 원문 파일: 대지위치에서 PNU 파생
        df = _derive_pnu_from_address(df)
    else:
        # API 포맷: sigungu_cd + bjdong_cd + bun + ji 로 PNU 생성
        df["pnu"] = df.apply(_generate_pnu, axis=1)
        if "sigungu_cd" not in df.columns:
            df["sigungu_cd"] = BUCHEON_SIGUNGU_CD

    before = len(df)
    df = df[df["pnu"].notna()].copy()
    logger.info("PNU 생성: %d건 / 원본 %d건", len(df), before)

    # building_ledger 실제 컬럼에 맞게 유지 (sigungu_cd 등 없음)
    keep = [
        "pnu", "bld_nm", "main_purps_cd", "main_purps_nm",
        "strct_cd", "strct_nm", "grnd_flr_cnt", "ugrnd_flr_cnt",
        "bld_area", "tot_area", "use_apr_day", "enrgy_eff_rate", "epi_score",
    ]
    df = df[[c for c in keep if c in df.columns]]

    # use_apr_day: 'YYYY-MM-DD' → 'YYYYMMDD' (VARCHAR(8))
    if "use_apr_day" in df.columns:
        df["use_apr_day"] = df["use_apr_day"].str.replace("-", "", regex=False).str[:8]

    # PNU 중복 제거 (같은 필지에 여러 동 → 연면적 합산 없이 첫 행 유지)
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["pnu"], keep="first")
    if len(df) < before_dedup:
        logger.info("PNU 중복 제거: %d건 → %d건", before_dedup, len(df))

    logger.info("최종 적재 대상: %d건", len(df))

    if args.dry_run:
        logger.info("[DRY RUN] DB 변경 없음")
        print(df.head(10).to_string())
        return

    from sqlalchemy import create_engine, text
    engine = create_engine(args.db_url)

    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM building_ledger WHERE LEFT(pnu, 5) = :sgg"),
            {"sgg": BUCHEON_SIGUNGU_CD}
        ).rowcount
        logger.info("기존 부천시 레코드 삭제: %d건", deleted)
        df.to_sql("building_ledger", conn, if_exists="append", index=False,
                  method="multi", chunksize=500)
    logger.info("적재 완료 (DELETE+INSERT 단일 트랜잭션): %d건", len(df))

    engine.dispose()

    logger.info("MV REFRESH 필요:")
    logger.info("  docker compose exec db psql -U postgres -d buildings -c "
                "'REFRESH MATERIALIZED VIEW buildings_enriched;'")


if __name__ == "__main__":
    main()
