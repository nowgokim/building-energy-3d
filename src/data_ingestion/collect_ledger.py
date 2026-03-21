"""Korean building ledger (건축물대장) API collector.

Collects building ledger data for 마포구 (Mapo-gu) using the PublicDataReader
library, which wraps the 국토교통부 건축물대장 공공데이터 API.

Usage:
    stats = collect_mapo_ledger(api_key="YOUR_KEY", db_url="postgresql://...")
"""

import logging
import time
from typing import Any, Dict

import pandas as pd
import PublicDataReader as pdr
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# 대지구분코드 mapping: API response value -> PNU value
DAEJI_GUBUN_MAP: Dict[str, str] = {
    "0": "1",  # 대지
    "1": "2",  # 산
    "2": "3",  # 블록
}

MAPO_SIGUNGU_CODE = "11440"


def _generate_pnu_from_row(row: pd.Series) -> str:
    """Generate a 19-digit PNU code from a ledger row.

    PNU structure (19 digits):
        시군구코드(5) + 법정동코드(5) + 대지구분코드(1) + 번(4) + 지(4)

    Args:
        row: A pandas Series containing 시군구코드, 법정동코드,
             대지구분코드, 번, 지 fields.

    Returns:
        19-digit PNU string.
    """
    sigungu = str(row.get("시군구코드", "")).strip()
    bdong = str(row.get("법정동코드", "")).strip()
    daeji_raw = str(row.get("대지구분코드", "0")).strip()
    bon = str(row.get("번", "0")).strip()
    ji = str(row.get("지", "0")).strip()

    daeji = DAEJI_GUBUN_MAP.get(daeji_raw, "1")
    bon_padded = bon.zfill(4)
    ji_padded = ji.zfill(4)

    return f"{sigungu}{bdong}{daeji}{bon_padded}{ji_padded}"


def collect_mapo_ledger(api_key: str, db_url: str) -> Dict[str, Any]:
    """Collect building ledger data for all 법정동 in 마포구.

    Uses PublicDataReader to fetch 총괄표제부 (general title section) data
    for every 법정동 within 마포구 (시군구코드 11440), generates PNU codes,
    and stores results in a PostgreSQL database.

    Args:
        api_key: Public data portal API service key (공공데이터포털 서비스키).
        db_url: SQLAlchemy-compatible PostgreSQL connection URL.
                e.g. "postgresql://user:pass@host:5432/dbname"

    Returns:
        Dictionary with collection statistics:
            - total_records (int): Total number of records saved.
            - failed_bdongs (list[str]): List of 법정동 codes that failed.
    """
    logger.info("Starting 마포구 building ledger collection (시군구코드: %s)", MAPO_SIGUNGU_CODE)

    # Initialize the building ledger API client
    api = pdr.BuildingLedger(api_key)

    # Retrieve 법정동 codes for 마포구
    bdong_codes = pdr.code_bdong()
    mapo_bdongs = bdong_codes[
        bdong_codes["시군구코드"] == MAPO_SIGUNGU_CODE
    ].copy()

    # Filter to active (현존) 법정동 entries only
    if "삭제일자" in mapo_bdongs.columns:
        mapo_bdongs = mapo_bdongs[mapo_bdongs["삭제일자"].isna()]

    bdong_list = mapo_bdongs["법정동코드"].unique().tolist()
    logger.info("Found %d 법정동 codes in 마포구", len(bdong_list))

    engine = create_engine(db_url)
    total_records = 0
    failed_bdongs: list[str] = []

    for idx, bdong_code in enumerate(bdong_list, start=1):
        logger.info(
            "[%d/%d] Collecting ledger for 법정동 %s",
            idx, len(bdong_list), bdong_code,
        )
        try:
            df = api.get_data(
                ledger_type="총괄표제부",
                sigungu_code=MAPO_SIGUNGU_CODE,
                bdong_code=bdong_code,
            )

            if df is None or df.empty:
                logger.warning("No data returned for 법정동 %s", bdong_code)
                time.sleep(0.5)
                continue

            # Generate PNU codes
            df["pnu"] = df.apply(_generate_pnu_from_row, axis=1)

            # Persist to database
            df.to_sql(
                name="building_ledger",
                con=engine,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=1000,
            )

            record_count = len(df)
            total_records += record_count
            logger.info(
                "Saved %d records for 법정동 %s (running total: %d)",
                record_count, bdong_code, total_records,
            )

        except Exception:
            logger.exception("Failed to collect data for 법정동 %s", bdong_code)
            failed_bdongs.append(bdong_code)

        # Rate limiting to respect API throttle
        time.sleep(0.5)

    engine.dispose()

    stats = {
        "total_records": total_records,
        "failed_bdongs": failed_bdongs,
    }
    logger.info(
        "Collection complete. Total records: %d, Failed 법정동: %d",
        total_records, len(failed_bdongs),
    )
    return stats
