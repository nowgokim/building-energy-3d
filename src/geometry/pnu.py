"""PNU (필지고유번호) code utilities.

PNU (Parcel Number Unique) is a 19-digit code that uniquely identifies
a land parcel in Korea. This module provides functions to generate,
parse, and work with PNU codes and related building management numbers.

PNU structure (19 digits):
    시군구코드(5) + 법정동코드(5) + 대지구분코드(1) + 번(4) + 지(4)

Building management number (건축물관리번호) structure (25 digits):
    PNU(19) + 건물일련번호(6)
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

PNU_LENGTH = 19
BUILDING_MGT_SN_LENGTH = 25

# 대지구분코드 mapping: raw API response -> PNU value
DAEJI_GUBUN_MAP: Dict[str, str] = {
    "0": "1",  # 대지
    "1": "2",  # 산
    "2": "3",  # 블록
}

# Reverse mapping for parse
DAEJI_GUBUN_REVERSE: Dict[str, str] = {
    "1": "대지",
    "2": "산",
    "3": "블록",
}


def generate_pnu(
    sigungu_code: str,
    bdong_code: str,
    daeji_gubun: str,
    bon: str,
    ji: str,
) -> str:
    """Generate a 19-digit PNU code.

    Args:
        sigungu_code: 5-digit 시군구코드 (e.g. "11440" for 마포구).
        bdong_code: 5-digit 법정동코드 (e.g. "10100").
        daeji_gubun: 대지구분코드. Accepts either raw API values ("0","1","2")
                     or PNU values ("1","2","3"). Raw values are mapped
                     automatically.
        bon: 본번 (lot main number). Will be zero-padded to 4 digits.
        ji: 부번 (lot sub number). Will be zero-padded to 4 digits.

    Returns:
        19-digit PNU string.

    Raises:
        ValueError: If inputs produce an invalid PNU length.

    Examples:
        >>> generate_pnu("11440", "10100", "0", "123", "4")
        '1144010100101230004'
    """
    sigungu = str(sigungu_code).strip()
    bdong = str(bdong_code).strip()
    bon_str = str(bon).strip().zfill(4)
    ji_str = str(ji).strip().zfill(4)

    # Map raw API daeji_gubun values to PNU values
    daeji_str = str(daeji_gubun).strip()
    if daeji_str in DAEJI_GUBUN_MAP:
        daeji_str = DAEJI_GUBUN_MAP[daeji_str]

    pnu = f"{sigungu}{bdong}{daeji_str}{bon_str}{ji_str}"

    if len(pnu) != PNU_LENGTH:
        raise ValueError(
            f"Generated PNU '{pnu}' has length {len(pnu)}, expected {PNU_LENGTH}. "
            f"Inputs: sigungu={sigungu_code}, bdong={bdong_code}, "
            f"daeji={daeji_gubun}, bon={bon}, ji={ji}"
        )

    return pnu


def parse_pnu(pnu: str) -> Dict[str, str]:
    """Parse a 19-digit PNU code into its components.

    Args:
        pnu: 19-digit PNU string.

    Returns:
        Dictionary with keys:
            - sigungu_code (str): 5-digit 시군구코드
            - bdong_code (str): 5-digit 법정동코드
            - daeji_gubun (str): 1-digit 대지구분코드
            - daeji_gubun_name (str): Human-readable 대지구분 name
            - bon (str): 4-digit 본번
            - ji (str): 4-digit 부번

    Raises:
        ValueError: If PNU is not exactly 19 digits.

    Examples:
        >>> parse_pnu("1144010100101230004")
        {'sigungu_code': '11440', 'bdong_code': '10100', ...}
    """
    pnu = str(pnu).strip()

    if len(pnu) != PNU_LENGTH:
        raise ValueError(
            f"PNU '{pnu}' has length {len(pnu)}, expected {PNU_LENGTH}"
        )

    daeji_code = pnu[10]

    return {
        "sigungu_code": pnu[0:5],
        "bdong_code": pnu[5:10],
        "daeji_gubun": daeji_code,
        "daeji_gubun_name": DAEJI_GUBUN_REVERSE.get(daeji_code, "알수없음"),
        "bon": pnu[11:15],
        "ji": pnu[15:19],
    }


def generate_building_mgt_sn(pnu: str, bld_seq: str) -> str:
    """Generate a 25-digit building management number (건축물관리번호).

    The building management number uniquely identifies a building and is
    composed of the 19-digit PNU plus a 6-digit building sequence number.

    Args:
        pnu: 19-digit PNU code.
        bld_seq: Building sequence number. Will be zero-padded to 6 digits.

    Returns:
        25-digit building management number string.

    Raises:
        ValueError: If the PNU is invalid or the result is not 25 digits.

    Examples:
        >>> generate_building_mgt_sn("1144010100101230004", "1")
        '1144010100101230004000001'
    """
    pnu = str(pnu).strip()

    if len(pnu) != PNU_LENGTH:
        raise ValueError(
            f"PNU '{pnu}' has length {len(pnu)}, expected {PNU_LENGTH}"
        )

    seq_padded = str(bld_seq).strip().zfill(6)
    mgt_sn = f"{pnu}{seq_padded}"

    if len(mgt_sn) != BUILDING_MGT_SN_LENGTH:
        raise ValueError(
            f"Building management number '{mgt_sn}' has length {len(mgt_sn)}, "
            f"expected {BUILDING_MGT_SN_LENGTH}"
        )

    return mgt_sn
