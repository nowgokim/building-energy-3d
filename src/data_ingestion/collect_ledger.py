"""Korean building ledger (건축물대장) API collector.

Collects building ledger data for 마포구 (Mapo-gu) by directly calling
the 국토교통부 건축HUB 건축물대장 API (HTTPS).

Usage:
    stats = collect_mapo_ledger(api_key="YOUR_KEY", db_url="postgresql://...")
"""

import logging
import time
from typing import Any, Dict, List
from xml.etree import ElementTree

import httpx
import pandas as pd
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1613000/BldRgstHubService"
MAPO_SIGUNGU_CODE = "11440"

# 서울시 전체 25개 자치구 시군구코드
SEOUL_SIGUNGU_CODES: List[str] = [
    "11110",  # 종로구
    "11140",  # 중구
    "11170",  # 용산구
    "11200",  # 성동구
    "11215",  # 광진구
    "11230",  # 동대문구
    "11260",  # 중랑구
    "11290",  # 성북구
    "11305",  # 강북구
    "11320",  # 도봉구
    "11350",  # 노원구
    "11380",  # 은평구
    "11410",  # 서대문구
    "11440",  # 마포구
    "11470",  # 양천구
    "11500",  # 강서구
    "11530",  # 구로구
    "11545",  # 금천구
    "11560",  # 영등포구
    "11590",  # 동작구
    "11620",  # 관악구
    "11650",  # 서초구
    "11680",  # 강남구
    "11710",  # 송파구
    "11740",  # 강동구
]

# 마포구 법정동 코드 목록 (현존하는 동)
MAPO_BDONG_CODES: List[str] = [
    "10100",  # 아현동
    "10200",  # 공덕동
    "10300",  # 신공덕동
    "10400",  # 도화동
    "10500",  # 용강동
    "10600",  # 토정동
    "10700",  # 마포동
    "10800",  # 대흥동
    "10900",  # 염리동
    "11000",  # 노고산동
    "11100",  # 신수동
    "11200",  # 현석동
    "11300",  # 구수동
    "11400",  # 창전동
    "11500",  # 상수동
    "11600",  # 하중동
    "11700",  # 합정동
    "11800",  # 망원동
    "11900",  # 연남동
    "12000",  # 서교동
    "12100",  # 동교동
    "12200",  # 성산동
    "12300",  # 상암동
    "12400",  # 중동
]


def _fetch_ledger_page(
    api_key: str,
    sigungu_cd: str,
    bdong_cd: str,
    page: int = 1,
    num_rows: int = 100,
) -> tuple[list[dict], int]:
    """Fetch one page of 총괄표제부 data from the API.

    Returns:
        Tuple of (list of item dicts, total count).
    """
    resp = httpx.get(
        f"{BASE_URL}/getBrRecapTitleInfo",
        params={
            "serviceKey": api_key,
            "sigunguCd": sigungu_cd,
            "bjdongCd": bdong_cd,
            "numOfRows": str(num_rows),
            "pageNo": str(page),
        },
        timeout=30.0,
    )

    if resp.status_code != 200:
        logger.warning("API returned status %d for bdong %s page %d", resp.status_code, bdong_cd, page)
        return [], 0

    # Parse XML response
    root = ElementTree.fromstring(resp.text)

    result_code = root.findtext(".//resultCode", "")
    if result_code != "00":
        msg = root.findtext(".//resultMsg", "")
        logger.warning("API error: code=%s msg=%s (bdong=%s)", result_code, msg, bdong_cd)
        return [], 0

    total_count = int(root.findtext(".//totalCount", "0"))
    items = []
    for item_el in root.findall(".//item"):
        item = {}
        for child in item_el:
            item[child.tag] = child.text.strip() if child.text else ""
        items.append(item)

    return items, total_count


def _generate_pnu(row: dict) -> str:
    """Generate a 19-digit PNU code from a ledger item dict."""
    sigungu = row.get("sigunguCd", "")
    bdong = row.get("bjdongCd", "")
    plat_gb = row.get("platGbCd", "0")
    bun = row.get("bun", "0").zfill(4)
    ji = row.get("ji", "0").zfill(4)

    # 대지구분코드: 0→1(대지), 1→2(산)
    daeji_map = {"0": "1", "1": "2", "2": "3"}
    daeji = daeji_map.get(plat_gb, "1")

    return f"{sigungu}{bdong}{daeji}{bun}{ji}"


def _items_to_dataframe(items: list[dict]) -> pd.DataFrame:
    """Convert raw API items to a DataFrame matching building_ledger schema."""
    records = []
    for item in items:
        pnu = _generate_pnu(item)
        records.append({
            "pnu": pnu,
            "bld_mgt_sn": item.get("mgmBldrgstPk", ""),
            "bld_nm": item.get("bldNm", "").strip(),
            "dong_nm": "",
            "main_purps_cd": item.get("mainPurpsCd", ""),
            "main_purps_nm": item.get("mainPurpsCdNm", "").strip(),
            "strct_cd": "",
            "strct_nm": "",
            "grnd_flr_cnt": None,
            "ugrnd_flr_cnt": None,
            "bld_ht": None,
            "tot_area": float(item["totArea"]) if item.get("totArea") else None,
            "bld_area": float(item["archArea"]) if item.get("archArea") else None,
            "use_apr_day": item.get("useAprDay", "").strip(),
            "enrgy_eff_rate": item.get("engrGrade", "").strip() or None,
            "epi_score": float(item["engrEpi"]) if item.get("engrEpi") and item["engrEpi"].strip() != "0" else None,
        })
    return pd.DataFrame(records)


def collect_mapo_ledger(api_key: str, db_url: str) -> Dict[str, Any]:
    """Collect building ledger data for all 법정동 in 마포구.

    Directly calls the 건축HUB API (HTTPS) to fetch 총괄표제부 data
    for each 법정동, generates PNU codes, and stores in PostgreSQL.

    Args:
        api_key: data.go.kr API service key.
        db_url: SQLAlchemy PostgreSQL connection URL.

    Returns:
        Dictionary with total_records and failed_bdongs.
    """
    logger.info("Starting 마포구 building ledger collection (%d 법정동)", len(MAPO_BDONG_CODES))

    engine = create_engine(db_url)
    total_records = 0
    failed_bdongs: list[str] = []

    for idx, bdong_code in enumerate(MAPO_BDONG_CODES, start=1):
        logger.info("[%d/%d] Collecting 법정동 %s", idx, len(MAPO_BDONG_CODES), bdong_code)

        try:
            all_items: list[dict] = []
            page = 1

            while True:
                items, total_count = _fetch_ledger_page(
                    api_key, MAPO_SIGUNGU_CODE, bdong_code, page=page,
                )

                if not items:
                    break

                all_items.extend(items)

                if len(all_items) >= total_count:
                    break

                page += 1
                time.sleep(0.3)

            if not all_items:
                logger.warning("No data for 법정동 %s", bdong_code)
                time.sleep(0.3)
                continue

            df = _items_to_dataframe(all_items)

            df.to_sql(
                name="building_ledger",
                con=engine,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500,
            )

            total_records += len(df)
            logger.info("Saved %d records for 법정동 %s (total: %d)", len(df), bdong_code, total_records)

        except Exception:
            logger.exception("Failed for 법정동 %s", bdong_code)
            failed_bdongs.append(bdong_code)

        time.sleep(0.3)

    engine.dispose()

    stats = {
        "total_records": total_records,
        "failed_bdongs": failed_bdongs,
    }
    logger.info("Ledger collection complete. Total: %d, Failed: %d", total_records, len(failed_bdongs))
    return stats


def collect_seoul_ledger(api_key: str, db_url: str, ledger_type: str = "recap") -> Dict[str, Any]:
    """Collect building ledger data for all 25 districts of Seoul.

    Args:
        api_key: data.go.kr API service key.
        db_url: SQLAlchemy PostgreSQL connection URL.
        ledger_type: "recap" for 총괄표제부, "title" for 표제부.

    Returns:
        Dictionary with total_records and failed items.
    """
    api_func = _fetch_ledger_page if ledger_type == "recap" else _fetch_title_page
    items_func = _items_to_dataframe if ledger_type == "recap" else _title_items_to_updates
    table_name = "building_ledger"

    logger.info("Starting Seoul-wide %s collection (%d 자치구)", ledger_type, len(SEOUL_SIGUNGU_CODES))

    engine = create_engine(db_url)
    total_records = 0
    failed: list[str] = []

    for gu_idx, sigungu_code in enumerate(SEOUL_SIGUNGU_CODES, start=1):
        logger.info("=== [%d/%d] 자치구 %s ===", gu_idx, len(SEOUL_SIGUNGU_CODES), sigungu_code)

        # Get 법정동 codes for this 구
        try:
            import PublicDataReader as pdr
            bdong_codes_df = pdr.code_bdong()
            gu_bdongs = bdong_codes_df[bdong_codes_df["시군구코드"] == sigungu_code]
            if "삭제일자" in gu_bdongs.columns:
                gu_bdongs = gu_bdongs[gu_bdongs["삭제일자"].isna()]
            bdong_list = gu_bdongs["법정동코드"].unique().tolist()
        except Exception:
            logger.warning("Failed to get 법정동 codes for %s, skipping", sigungu_code)
            failed.append(sigungu_code)
            continue

        logger.info("Found %d 법정동 in 자치구 %s", len(bdong_list), sigungu_code)

        for bdong_idx, bdong_code in enumerate(bdong_list, start=1):
            try:
                all_items: list[dict] = []
                page = 1
                while True:
                    items, total_count = api_func(api_key, sigungu_code, bdong_code, page=page)
                    if not items:
                        break
                    all_items.extend(items)
                    if len(all_items) >= total_count:
                        break
                    page += 1
                    time.sleep(0.3)

                if not all_items:
                    time.sleep(0.2)
                    continue

                if ledger_type == "recap":
                    df = items_func(all_items)
                else:
                    records = items_func(all_items)
                    df = pd.DataFrame(records)

                df.to_sql(name=table_name, con=engine, if_exists="append",
                          index=False, method="multi", chunksize=500)

                total_records += len(df)
                if bdong_idx % 5 == 0 or bdong_idx == len(bdong_list):
                    logger.info("[%s] %d/%d 법정동 완료 (누적: %d건)",
                                sigungu_code, bdong_idx, len(bdong_list), total_records)

            except Exception:
                logger.exception("Failed %s/%s", sigungu_code, bdong_code)
                failed.append(f"{sigungu_code}/{bdong_code}")

            time.sleep(0.3)

    engine.dispose()
    logger.info("Seoul-wide %s complete. Total: %d, Failed: %d", ledger_type, total_records, len(failed))
    return {"total_records": total_records, "failed": failed}


# ---------------------------------------------------------------------------
# 표제부 (동별 상세) 수집
# ---------------------------------------------------------------------------

def _fetch_title_page(
    api_key: str,
    sigungu_cd: str,
    bdong_cd: str,
    page: int = 1,
    num_rows: int = 100,
) -> tuple[list[dict], int]:
    """Fetch one page of 표제부 (building title) data."""
    resp = httpx.get(
        f"{BASE_URL}/getBrTitleInfo",
        params={
            "serviceKey": api_key,
            "sigunguCd": sigungu_cd,
            "bjdongCd": bdong_cd,
            "numOfRows": str(num_rows),
            "pageNo": str(page),
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        return [], 0

    root = ElementTree.fromstring(resp.text)
    result_code = root.findtext(".//resultCode", "")
    if result_code != "00":
        return [], 0

    total_count = int(root.findtext(".//totalCount", "0"))
    items = []
    for item_el in root.findall(".//item"):
        item = {}
        for child in item_el:
            item[child.tag] = child.text.strip() if child.text else ""
        items.append(item)
    return items, total_count


def _title_items_to_updates(items: list[dict]) -> list[dict]:
    """Convert 표제부 items to update records for building_ledger."""
    records = []
    for item in items:
        pnu = _generate_pnu(item)
        grnd = None
        ugrnd = None
        try:
            grnd = int(item["grndFlrCnt"]) if item.get("grndFlrCnt") else None
        except (ValueError, TypeError):
            pass
        try:
            ugrnd = int(item["ugrndFlrCnt"]) if item.get("ugrndFlrCnt") else None
        except (ValueError, TypeError):
            pass

        records.append({
            "pnu": pnu,
            "bld_mgt_sn": item.get("mgmBldrgstPk", ""),
            "dong_nm": item.get("dongNm", "").strip(),
            "main_purps_cd": item.get("mainPurpsCd", ""),
            "main_purps_nm": item.get("mainPurpsCdNm", "").strip(),
            "strct_cd": item.get("strctCd", ""),
            "strct_nm": item.get("strctCdNm", "").strip(),
            "grnd_flr_cnt": grnd,
            "ugrnd_flr_cnt": ugrnd,
            "bld_ht": float(item["ht"]) if item.get("ht") and item["ht"].strip() not in ("", "0") else None,
            "tot_area": float(item["totArea"]) if item.get("totArea") else None,
            "bld_area": float(item["archArea"]) if item.get("archArea") else None,
            "use_apr_day": item.get("useAprDay", "").strip(),
            "enrgy_eff_rate": item.get("engrGrade", "").strip() or None,
            "epi_score": float(item["engrEpi"]) if item.get("engrEpi") and item["engrEpi"].strip() != "0" else None,
        })
    return records


def collect_mapo_title(api_key: str, db_url: str) -> Dict[str, Any]:
    """Collect 표제부 (동별 상세) data for all 법정동 in 마포구.

    Updates building_ledger with per-dong details: floor counts,
    construction year, structure type, height.
    """
    logger.info("Starting 마포구 표제부 collection (%d 법정동)", len(MAPO_BDONG_CODES))

    engine = create_engine(db_url)
    total_records = 0
    failed_bdongs: list[str] = []

    for idx, bdong_code in enumerate(MAPO_BDONG_CODES, start=1):
        logger.info("[%d/%d] Collecting 표제부 법정동 %s", idx, len(MAPO_BDONG_CODES), bdong_code)

        try:
            all_items: list[dict] = []
            page = 1

            while True:
                items, total_count = _fetch_title_page(
                    api_key, MAPO_SIGUNGU_CODE, bdong_code, page=page,
                )
                if not items:
                    break
                all_items.extend(items)
                if len(all_items) >= total_count:
                    break
                page += 1
                time.sleep(0.3)

            if not all_items:
                time.sleep(0.3)
                continue

            records = _title_items_to_updates(all_items)
            df = pd.DataFrame(records)

            # Update existing ledger records with 표제부 data
            # Use INSERT with ON CONFLICT would be ideal but simpler to append
            # since building_ledger has no unique constraint on (pnu, bld_mgt_sn)
            df.to_sql(
                name="building_ledger",
                con=engine,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500,
            )

            total_records += len(df)
            logger.info("Saved %d 표제부 records for 법정동 %s (total: %d)",
                        len(df), bdong_code, total_records)

        except Exception:
            logger.exception("Failed 표제부 for 법정동 %s", bdong_code)
            failed_bdongs.append(bdong_code)

        time.sleep(0.3)

    engine.dispose()

    stats = {
        "total_records": total_records,
        "failed_bdongs": failed_bdongs,
    }
    logger.info("표제부 collection complete. Total: %d, Failed: %d", total_records, len(failed_bdongs))
    return stats
