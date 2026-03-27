"""
Phase 4-A: 건물 에너지 데이터 수집 → energy_results 업데이트

우선순위:
  1. 국토교통부 건축HUB 건물에너지정보 (ID: 15135963)
     지번별 월별 전기+가스 실소비량 → 연간 EUI 계산
  2. 한국에너지공단 건축물 에너지효율등급 (ID: 15100521)
     인증 건물 1차에너지소비량 (kWh/m²·년)
  3. 건축물대장 enrgy_eff_rate → EUI 변환 (DB에 이미 있음)
  4. archetype 룩업 폴백 (기존 로직)

실행:
    python -m src.data_ingestion.collect_energy
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# ── 국토교통부 건축HUB 건물에너지정보 서비스 (ID: 15135963) ──────────────────
# 지번별 월별 전기·가스 실소비량
BLDG_ENERGY_ELEC_URL = (
    "https://apis.data.go.kr/1613000/BldEngyHubService/getBeElctyUsgInfo"
)
BLDG_ENERGY_GAS_URL = (
    "https://apis.data.go.kr/1613000/BldEngyHubService/getBeGasUsgInfo"
)

# ── 한국에너지공단 건축물 에너지효율등급 (ID: 15100521) ──────────────────────
KEA_CERT_URL = "https://apis.data.go.kr/B553530/BEEC/BEEC_01_LIST"

# ── 1. 에너지효율등급 → 1차에너지 소요량 (kWh/m²·년) 매핑 ─────────────────────
# 건물에너지 효율등급 인증 기준 (2025.1.1 시행, ZEB 통합 기준)
# 각 등급 구간 중앙값 사용
GRADE_TO_EUI: dict[str, float] = {
    "1+++": 45.0,   # ≤ 60
    "1++"  : 75.0,   # 60 ~ 90
    "1+"   : 105.0,  # 90 ~ 120
    "1"    : 135.0,  # 120 ~ 150
    "2"    : 170.0,  # 150 ~ 190
    "3"    : 210.0,  # 190 ~ 230
    "4"    : 250.0,  # 230 ~ 270
    "5"    : 295.0,  # 270 ~ 320
    "6"    : 345.0,  # 320 ~ 370
    "7"    : 420.0,  # > 370
}

# 등급 문자열 정규화 (건축물대장에서 다양한 형식으로 올 수 있음)
_GRADE_ALIASES: dict[str, str] = {
    "1+++등급": "1+++", "1++등급": "1++", "1+등급": "1+",
    "1등급": "1", "2등급": "2", "3등급": "3",
    "4등급": "4", "5등급": "5", "6등급": "6", "7등급": "7",
    "A": "1+++", "B": "1++", "C": "1+",  # 구 표기
}

# 서울 열린데이터광장 녹색건축인증 서비스명
SEOUL_GREEN_BLDG_SERVICE = "GreenBuildingInfo"
SEOUL_API_BASE = "http://openapi.seoul.go.kr:8088"


def normalize_grade(raw: Optional[str]) -> Optional[str]:
    """건축물대장 등급 문자열 정규화 → GRADE_TO_EUI 키로 변환."""
    if not raw:
        return None
    s = raw.strip()
    return _GRADE_ALIASES.get(s, s if s in GRADE_TO_EUI else None)


def grade_to_eui(grade: str) -> Optional[float]:
    """정규화된 등급 → EUI (kWh/m²·년). 미매칭 시 None."""
    norm = normalize_grade(grade)
    return GRADE_TO_EUI.get(norm) if norm else None


# ── 2. 서울 열린데이터광장 녹색건축인증 정보 ───────────────────────────────────

def fetch_seoul_green_building(api_key: str, page: int = 1, size: int = 1000) -> list[dict]:
    """
    서울 열린데이터광장 녹색건축인증 정보 조회.
    반환: [{"address": ..., "eui": ..., "grade": ...}, ...]
    """
    url = f"{SEOUL_API_BASE}/{api_key}/json/{SEOUL_GREEN_BLDG_SERVICE}/{(page-1)*size+1}/{page*size}/"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        svc = data.get(SEOUL_GREEN_BLDG_SERVICE, {})
        result_info = svc.get("RESULT") or svc.get("result") or {}
        code = result_info.get("CODE") or result_info.get("code", "")
        if code and code != "INFO-000":
            logger.warning("Seoul API non-success: %s", result_info)
            return []
        if not svc:
            logger.warning("Seoul API: service '%s' not found in response keys: %s",
                           SEOUL_GREEN_BLDG_SERVICE, list(data.keys()))
            return []
        rows = svc.get("row", [])
        results = []
        for r in rows:
            eui_raw = r.get("ENGY_EFF_RATE") or r.get("EUI") or r.get("ENERGY_USE_INTENSITY")
            grade_raw = r.get("ENGY_GRD") or r.get("GRADE") or r.get("CERT_GRADE")
            addr = r.get("ADDR") or r.get("ADRES") or r.get("BLDG_ADDR", "")
            if eui_raw:
                try:
                    results.append({"address": addr, "eui": float(eui_raw), "grade": grade_raw})
                except (ValueError, TypeError):
                    pass
        logger.info("Seoul green building API: %d rows (page %d)", len(results), page)
        return results
    except Exception as e:
        logger.warning("Seoul green building API failed: %s", e)
        return []


# ── 3. 한국에너지공단 건물에너지 소비정보 ──────────────────────────────────────

KEA_ENERGY_URL = "https://apis.data.go.kr/B552584/EnergyDataService/getBuildingEnergyInfo"


def fetch_kea_building_energy(api_key: str, addr_keyword: str) -> Optional[float]:
    """
    한국에너지공단 건물에너지 소비정보 → 연간 EUI (kWh/m²·년).
    addr_keyword: 도로명주소 일부 (예: "서울 마포구")
    """
    try:
        resp = requests.get(
            KEA_ENERGY_URL,
            params={
                "serviceKey": api_key,
                "pageNo": 1,
                "numOfRows": 10,
                "dataType": "JSON",
                "addr": addr_keyword,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = (
            resp.json()
            .get("response", {}).get("body", {})
            .get("items", {}).get("item", [])
        )
        if not items:
            return None
        item = items[0] if isinstance(items, list) else items
        total_kwh = float(item.get("totElecUseQnt", 0) or 0)
        area = float(item.get("bldArea", 0) or 0)
        if total_kwh > 0 and area > 0:
            return round(total_kwh / area, 1)
    except Exception as e:
        logger.debug("KEA energy API failed addr=%s: %s", addr_keyword, e)
    return None


# ── 4. 메인: energy_results 일괄 업데이트 ─────────────────────────────────────

def backfill_from_energy_grade(conn: Connection, batch_size: int = 2000) -> int:
    """
    buildings_enriched.energy_grade → energy_results 업데이트.

    이미 DB에 있는 enrgy_eff_rate를 활용하므로 API 호출 불필요.
    커버리지: energy_grade가 있는 건물 (~수만 건 예상).

    반환: 업서트된 행 수
    """
    logger.info("Backfill energy_results from energy_grade ...")

    # 등급 있는 건물 전체 조회
    rows = conn.execute(text("""
        SELECT pnu, energy_grade, total_area, usage_type
        FROM buildings_enriched
        WHERE energy_grade IS NOT NULL
          AND energy_grade != ''
          AND pnu IS NOT NULL
        ORDER BY pnu
    """)).fetchall()

    logger.info("Found %d buildings with energy_grade", len(rows))

    upserted = 0
    batch = []
    for r in rows:
        eui = grade_to_eui(r.energy_grade)
        if eui is None:
            continue
        area = float(r.total_area or 0)
        total_kwh = eui * area if area > 0 else None

        # 5개 항목 비율은 usage_type 기반으로 간단 분할
        fracs = _usage_fracs(r.usage_type)
        batch.append({
            "pnu":         r.pnu,
            "total_energy": eui,
            "heating":     round(eui * fracs["heating"], 2),
            "cooling":     round(eui * fracs["cooling"], 2),
            "hot_water":   round(eui * fracs["hot_water"], 2),
            "lighting":    round(eui * fracs["lighting"], 2),
            "ventilation": round(eui * fracs["ventilation"], 2),
            "source":      "energy_grade",
            "grade":       r.energy_grade,
        })

        if len(batch) >= batch_size:
            upserted += _upsert_batch(conn, batch)
            batch = []

    if batch:
        upserted += _upsert_batch(conn, batch)

    conn.commit()
    logger.info("Backfill done: %d rows upserted", upserted)
    return upserted


def _usage_fracs(usage_type: str) -> dict:
    """용도별 에너지 항목 비율 (합계 = 1.0)."""
    _FRACS = {
        "아파트":    {"heating": 0.42, "cooling": 0.10, "hot_water": 0.28, "lighting": 0.10, "ventilation": 0.10},
        "공동주택":  {"heating": 0.42, "cooling": 0.10, "hot_water": 0.28, "lighting": 0.10, "ventilation": 0.10},
        "업무시설":  {"heating": 0.30, "cooling": 0.25, "hot_water": 0.10, "lighting": 0.20, "ventilation": 0.15},
        "판매시설":  {"heating": 0.22, "cooling": 0.30, "hot_water": 0.05, "lighting": 0.25, "ventilation": 0.18},
        "교육연구":  {"heating": 0.38, "cooling": 0.15, "hot_water": 0.15, "lighting": 0.18, "ventilation": 0.14},
        "의료시설":  {"heating": 0.32, "cooling": 0.22, "hot_water": 0.20, "lighting": 0.14, "ventilation": 0.12},
    }
    for key, fracs in _FRACS.items():
        if key in (usage_type or ""):
            return fracs
    return {"heating": 0.35, "cooling": 0.18, "hot_water": 0.18, "lighting": 0.16, "ventilation": 0.13}


_SOURCE_TIER: dict[str, int] = {
    "bldg_energy_hub": 1,
    "kea_cert":        2,
    "energy_grade":    2,
    "seoul_green_cert": 2,
    "energyplus":      3,
    "archetype":       4,
}


def _upsert_batch(conn: Connection, batch: list[dict]) -> int:
    """energy_results 배치 업서트 (data_tier 자동 설정)."""
    for row in batch:
        row.setdefault("data_tier", _SOURCE_TIER.get(row.get("source", "archetype"), 4))

    sql = text("""
        INSERT INTO energy_results
            (pnu, total_energy, heating, cooling, hot_water, lighting, ventilation,
             simulation_type, archetype_id, data_tier)
        VALUES
            (:pnu, :total_energy, :heating, :cooling, :hot_water, :lighting, :ventilation,
             :source, NULL, :data_tier)
        ON CONFLICT (pnu) DO UPDATE
          SET total_energy    = EXCLUDED.total_energy,
              heating         = EXCLUDED.heating,
              cooling         = EXCLUDED.cooling,
              hot_water       = EXCLUDED.hot_water,
              lighting        = EXCLUDED.lighting,
              ventilation     = EXCLUDED.ventilation,
              simulation_type = EXCLUDED.simulation_type,
              data_tier       = EXCLUDED.data_tier
          WHERE energy_results.data_tier >= EXCLUDED.data_tier
    """)
    result = conn.execute(sql, batch)
    return result.rowcount


def collect_all(
    conn: Connection,
    data_go_kr_key: str = "",
    seoul_data_key: str = "",
    hub_limit: int = 2000,
) -> dict:
    """
    전체 수집 파이프라인.
      1단계: 건축물대장 등급 → 즉시 변환
      2단계: 건축HUB 건물에너지 API (실소비량)
      3단계: KEA 에너지효율등급 인증 API (인증 건물)
    반환: {"grade_upserted": N, "hub_upserted": M, "kea_upserted": K}
    """
    result = {}

    # 1단계: 등급 기반 (API 키 불필요)
    result["grade_upserted"] = backfill_from_energy_grade(conn)

    if not data_go_kr_key:
        logger.warning("DATA_GO_KR_API_KEY 없음 — 실소비량 API 생략")
        result["hub_upserted"] = 0
        result["kea_upserted"] = 0
        return result

    # 2단계: 건축HUB 건물에너지 (실소비량)
    result["hub_upserted"] = collect_bldg_energy_hub(
        conn, data_go_kr_key, limit=hub_limit
    )

    # 3단계: KEA 에너지효율등급 인증
    result["kea_upserted"] = collect_kea_cert(conn, data_go_kr_key)

    return result


def _pnu_to_jibun(pnu: str) -> dict:
    """
    PNU(19자리) → 건축HUB API 쿼리 파라미터 분해.
    PNU: SSSGG(5) + EEEELL(5) + TYPE(1) + BBBB(4) + JJJJ(4)
    """
    if not pnu or len(pnu) < 19:
        return {}
    return {
        "sigunguCd": pnu[0:5],
        "bjdongCd":  pnu[5:10],
        "bun":       pnu[11:15],   # 4자리 0패딩 유지 (예: 0012)
        "ji":        pnu[15:19],   # 4자리 0패딩 유지 (예: 0000)
    }


def _fetch_one_energy(url: str, api_key: str, jibun: dict, ym: str) -> float:
    """전기 또는 가스 단일 월 조회 → kWh (없으면 0.0)."""
    resp = requests.get(
        url,
        params={
            "serviceKey":  api_key,
            "pageNo": 1, "numOfRows": 10,
            "_type":       "json",
            "sigunguCd":   jibun["sigunguCd"],
            "bjdongCd":    jibun["bjdongCd"],
            "bun":         jibun["bun"],
            "ji":          jibun["ji"],
            "useYm":       ym,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = (
        resp.json()
        .get("response", {}).get("body", {})
        .get("items", {})
    )
    if not items or not isinstance(items, dict):
        return 0.0
    item_list = items.get("item", [])
    if isinstance(item_list, dict):
        item_list = [item_list]
    total = 0.0
    for item in item_list:
        # 전기: elecUseQnt / 가스: gasUseQnt (필드명은 서비스마다 다를 수 있음)
        val = item.get("useQty") or item.get("elecUseQnt") or item.get("gasUseQnt") or 0
        total += float(val)
    return total


def fetch_bldg_energy_hub(api_key: str, pnu: str,
                          months: int = 12) -> Optional[float]:
    """
    건축HUB 전기+가스 조회 → 연간 총소비량 kWh.

    최근 months개월 데이터 합산 후 연간 환산.
    데이터 없으면 None 반환 (단독주택·200세대 미만 등).

    조기종료 최적화: 가장 최근 월에 데이터가 없으면 즉시 스킵.
    데이터 없는 건물(~99%)을 2 API calls만에 처리 → ~10x 속도 향상.
    """
    jibun = _pnu_to_jibun(pnu)
    if not jibun:
        return None

    # 건축HUB 데이터는 수개월~1년 지연됨 → end_dt를 6개월 전으로 설정
    now = datetime.now()
    end_dt   = (now.replace(day=1) - timedelta(days=6*30)).replace(day=1)
    start_dt = (end_dt - timedelta(days=30 * (months - 1))).replace(day=1)

    use_yms = []
    cur = start_dt
    while cur <= end_dt:
        use_yms.append(cur.strftime("%Y%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    # 조기종료: 가장 최근 월 먼저 확인 (최신 데이터가 있는 건물만 전체 조회)
    latest_ym = use_yms[-1]
    try:
        elec_probe = _fetch_one_energy(BLDG_ENERGY_ELEC_URL, api_key, jibun, latest_ym)
        gas_probe  = _fetch_one_energy(BLDG_ENERGY_GAS_URL,  api_key, jibun, latest_ym)
    except Exception as e:
        logger.debug("BldEnergyHub probe pnu=%s ym=%s: %s", pnu, latest_ym, e)
        return None
    time.sleep(0.05)

    if elec_probe + gas_probe == 0:
        return None   # 데이터 없는 건물 → 전체 조회 스킵

    # 데이터 있는 건물만 나머지 months-1개월 전체 조회
    total_kwh = elec_probe + gas_probe
    found = 1
    for ym in use_yms[:-1]:
        try:
            elec = _fetch_one_energy(BLDG_ENERGY_ELEC_URL, api_key, jibun, ym)
            gas  = _fetch_one_energy(BLDG_ENERGY_GAS_URL,  api_key, jibun, ym)
            if elec + gas > 0:
                total_kwh += elec + gas
                found += 1
        except Exception as e:
            logger.debug("BldEnergyHub pnu=%s ym=%s: %s", pnu, ym, e)
        time.sleep(0.05)   # rate limit 준수

    return total_kwh * (12 / found)   # 연간 환산


def collect_bldg_energy_hub(conn: Connection, api_key: str,
                            limit: int = 5000) -> int:
    """
    건축HUB 건물에너지 API → energy_results 업데이트.

    energy_grade/archetype 기준으로 정렬 후 energy_grade 없는 건물부터 조회.
    limit: 최대 조회 건물 수 (API 호출 비용 제한).
    """
    logger.info("BldEnergyHub 수집 시작 (limit=%d)", limit)

    # energy_grade 없고 면적>0인 건물 우선 (대형·상업 건물)
    rows = conn.execute(text("""
        SELECT DISTINCT ON (b.pnu) b.pnu, b.total_area, b.usage_type
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu
        WHERE b.pnu IS NOT NULL
          AND b.total_area > 500
          AND (er.simulation_type = 'archetype' OR er.pnu IS NULL)
        ORDER BY b.pnu, b.total_area DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    logger.info("조회 대상: %d 건물", len(rows))
    upserted, skipped = 0, 0
    batch = []

    for r in rows:
        annual_kwh = fetch_bldg_energy_hub(api_key, r.pnu)
        if annual_kwh is None or annual_kwh <= 0:
            skipped += 1
            continue
        area = float(r.total_area or 1)
        eui  = round(annual_kwh / area, 1)
        fracs = _usage_fracs(r.usage_type)
        batch.append({
            "pnu":         r.pnu,
            "total_energy": eui,
            "heating":     round(eui * fracs["heating"], 2),
            "cooling":     round(eui * fracs["cooling"], 2),
            "hot_water":   round(eui * fracs["hot_water"], 2),
            "lighting":    round(eui * fracs["lighting"], 2),
            "ventilation": round(eui * fracs["ventilation"], 2),
            "source":      "bldg_energy_hub",
            "grade":       "",
        })
        if len(batch) >= 100:
            upserted += _upsert_batch(conn, batch)
            conn.commit()
            batch = []
            logger.info("  진행: upserted=%d skipped=%d", upserted, skipped)

    if batch:
        upserted += _upsert_batch(conn, batch)
        conn.commit()

    logger.info("BldEnergyHub 완료: upserted=%d skipped=%d", upserted, skipped)
    return upserted


def _parse_kea_addr_to_pnu_prefix(addr: str) -> Optional[str]:
    """
    KEA LOC_ADDR → 서울 시군구코드(5자리) 추출.
    예: "서울특별시 강남구 ..." → "11680"
    """
    _GU_TO_CD: dict[str, str] = {
        "종로구": "11110", "중구":    "11140", "용산구": "11170",
        "성동구": "11200", "광진구":  "11215", "동대문구": "11230",
        "중랑구": "11260", "성북구":  "11290", "강북구": "11305",
        "도봉구": "11320", "노원구":  "11350", "은평구": "11380",
        "서대문구": "11410", "마포구": "11440", "양천구": "11470",
        "강서구": "11500", "구로구":  "11530", "금천구": "11545",
        "영등포구": "11560", "동작구": "11590", "관악구": "11620",
        "서초구": "11650", "강남구":  "11680", "송파구": "11710",
        "강동구": "11740",
    }
    for gu, cd in _GU_TO_CD.items():
        if gu in addr:
            return cd
    return None


def _kea_addr_to_pnu(conn: Connection, addr: str, bld_nm: str) -> Optional[str]:
    """
    KEA LOC_ADDR → PNU 매핑.
    전략:
      1. 지번주소 파싱: 구+동+번지 → PNU 직접 구성 시도 (건물 centroid BBox)
      2. 건물명 매칭: bld_nm으로 building_footprints 조회
      3. 도로명주소: 구코드만 추출 후 건물명 조합
    """
    import re
    sigungu_cd = _parse_kea_addr_to_pnu_prefix(addr)
    if not sigungu_cd:
        return None

    # 전략 1: 지번 파싱 "동이름 본번-부번" or "동이름 본번번지"
    # 예: "강서구 오곡동 1-6번지" → dong="오곡동", bun=1, ji=6
    # 전략 1: 지번 파싱 "동이름 본번-부번" or "동이름 본번번지"
    # dong_nm은 도로명이므로 사용하지 않고 sigungu + bun + ji만으로 매칭
    jibun_match = re.search(r'(\S+동|\S+가)\s+(\d+)[-–](\d+)', addr)
    if not jibun_match:
        jibun_match = re.search(r'(\S+동|\S+가)\s+(\d+)번지', addr)
    if jibun_match:
        bun = int(jibun_match.group(2))
        ji  = int(jibun_match.group(3)) if len(jibun_match.groups()) >= 3 else 0
        row = conn.execute(text("""
            SELECT pnu FROM building_footprints
            WHERE pnu LIKE :prefix
              AND CAST(SUBSTRING(pnu, 12, 4) AS INTEGER) = :bun
              AND CAST(SUBSTRING(pnu, 16, 4) AS INTEGER) = :ji
            LIMIT 1
        """), {"prefix": f"{sigungu_cd}%", "bun": bun, "ji": ji}).fetchone()
        if row:
            return row.pnu

    # 전략 2: pg_trgm 유사도 기반 건물명 매칭 (ILIKE보다 강건)
    if bld_nm:
        row = conn.execute(text("""
            SELECT pnu FROM building_footprints
            WHERE pnu LIKE :prefix
              AND bld_nm % :bld_nm
            ORDER BY similarity(bld_nm, :bld_nm) DESC
            LIMIT 1
        """), {"prefix": f"{sigungu_cd}%", "bld_nm": bld_nm[:20]}).fetchone()
        if row:
            return row.pnu

    return None


def collect_kea_cert(conn: Connection, api_key: str) -> int:
    """
    한국에너지공단 건축물 에너지효율등급 인증 정보 → energy_results 업데이트.
    인증 건물의 실제 1차에너지소비량(kWh/m²·년) 수집.

    전국 조회(q5 생략) 후 RGN_SCT_NAME='서울' 필터링.
    총 ~3,970건, 서울 약 500~1,000건 예상.
    """
    # q4=0(전체)은 API 미지원 → 주거(1)/비주거(2) 분리 조회
    usage_codes = ["1", "2"]
    upserted, skipped = 0, 0
    cur_year = datetime.now().year

    for usage_code in usage_codes:
        page = 1
        while True:
            try:
                resp = requests.get(
                    KEA_CERT_URL,
                    params={
                        "serviceKey": api_key,
                        "pageNo":     page,
                        "numOfRows":  100,
                        "apiType":    "json",
                        "q1": "2010",          # 인증 시작연도
                        "q2": str(cur_year),   # 인증 종료연도
                        "q3": "2",             # 본인증
                        "q4": usage_code,      # 1=주거, 2=비주거
                        # q5 생략 → 전국 조회, RGN_SCT_NAME으로 필터
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                body = resp.json().get("response", {}).get("body", {})
                items = body.get("items", {})
                if not items:
                    break
                item_list = items.get("item", [])
                if isinstance(item_list, dict):
                    item_list = [item_list]
                if not item_list:
                    break

                batch = []
                for item in item_list:
                    # 서울만 처리
                    if item.get("RGN_SCT_NAME", "") != "서울":
                        continue

                    addr    = item.get("LOC_ADDR", "")
                    eui_raw = item.get("W_1ST_ENERGY_REQUIRE6")
                    bld_nm  = item.get("BLD_NM", "")
                    grade   = item.get("GRD_NAME", "")

                    if not eui_raw or not addr:
                        skipped += 1
                        continue
                    try:
                        eui = float(eui_raw)
                    except (ValueError, TypeError):
                        skipped += 1
                        continue

                    pnu = _kea_addr_to_pnu(conn, addr, bld_nm)
                    if not pnu:
                        skipped += 1
                        logger.debug("KEA PNU 미매칭: %s / %s", addr, bld_nm)
                        continue

                    fracs = _usage_fracs(bld_nm)
                    batch.append({
                        "pnu":          pnu,
                        "total_energy": eui,
                        "heating":      round(eui * fracs["heating"], 2),
                        "cooling":      round(eui * fracs["cooling"], 2),
                        "hot_water":    round(eui * fracs["hot_water"], 2),
                        "lighting":     round(eui * fracs["lighting"], 2),
                        "ventilation":  round(eui * fracs["ventilation"], 2),
                        "source":       "kea_cert",
                        "grade":        grade,
                    })

                if batch:
                    upserted += _upsert_batch(conn, batch)
                    conn.commit()

                total_count = int(body.get("totalCount", 0))
                logger.info("KEA usage=%s page=%d/%d upserted=%d skipped=%d",
                            usage_code, page, -(-total_count // 100), upserted, skipped)
                if page * 100 >= total_count:
                    break
                page += 1
                time.sleep(0.2)

            except Exception as e:
                logger.warning("KEA cert API usage=%s page=%d: %s", usage_code, page, e)
                break

    logger.info("KEA 인증 등급 수집 완료: %d 건 upserted, %d 건 미매칭", upserted, skipped)
    return upserted


def _collect_seoul_api(conn: Connection, api_key: str) -> int:
    """서울 열린데이터 녹색건축인증 → energy_results 실측 EUI로 업데이트."""
    page, upserted = 1, 0
    while True:
        rows = fetch_seoul_green_building(api_key, page=page)
        if not rows:
            break
        # 주소 → PNU 매핑 (buildings_enriched road_addr 사용)
        batch = []
        for r in rows:
            addr = r["address"]
            if not addr:
                continue
            pnu_row = conn.execute(text("""
                SELECT pnu FROM buildings_enriched
                WHERE road_addr ILIKE :addr OR jibun_addr ILIKE :addr
                LIMIT 1
            """), {"addr": f"%{addr[:15]}%"}).fetchone()
            if not pnu_row:
                continue
            eui = r["eui"]
            fracs = {"heating": 0.35, "cooling": 0.18, "hot_water": 0.18,
                     "lighting": 0.16, "ventilation": 0.13}
            batch.append({
                "pnu":         pnu_row.pnu,
                "total_energy": eui,
                "heating":     round(eui * fracs["heating"], 2),
                "cooling":     round(eui * fracs["cooling"], 2),
                "hot_water":   round(eui * fracs["hot_water"], 2),
                "lighting":    round(eui * fracs["lighting"], 2),
                "ventilation": round(eui * fracs["ventilation"], 2),
                "source":      "seoul_green_cert",
                "grade":       r.get("grade", ""),
            })
        if batch:
            upserted += _upsert_batch(conn, batch)
            conn.commit()
        if len(rows) < 1000:
            break
        page += 1
        time.sleep(0.3)
    logger.info("Seoul green building API: %d rows upserted", upserted)
    return upserted


# ── 5. 현황 조회 ───────────────────────────────────────────────────────────────

def get_coverage_stats(conn: Connection) -> dict:
    """energy_results 커버리지 통계."""
    row = conn.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE er.pnu IS NOT NULL)     AS with_energy,
            COUNT(*)                                        AS total_buildings,
            COUNT(*) FILTER (WHERE er.simulation_type = 'energy_grade')    AS from_grade,
            COUNT(*) FILTER (WHERE er.simulation_type = 'seoul_green_cert') AS from_seoul,
            COUNT(*) FILTER (WHERE er.simulation_type = 'archetype')        AS from_archetype,
            ROUND(AVG(er.total_energy)::NUMERIC, 1)        AS avg_eui
        FROM buildings_enriched b
        LEFT JOIN energy_results er ON b.pnu = er.pnu
    """)).fetchone()
    return dict(row._mapping) if row else {}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from src.shared.config import get_settings
    from src.shared.database import engine

    settings = get_settings()

    with engine.connect() as conn:
        before = get_coverage_stats(conn)
    print(f"\n[현재 상태]")
    print(f"  전체 건물:       {before.get('total_buildings', 0):,}")
    print(f"  에너지 데이터:   {before.get('with_energy', 0):,}")
    print(f"  평균 EUI:        {before.get('avg_eui', '-')} kWh/m²·년")

    print("\n[수집 시작]")
    result = {"grade_upserted": 0, "hub_upserted": 0, "kea_upserted": 0}

    # 각 단계별 독립 트랜잭션 (중간 커밋 충돌 방지)
    with engine.begin() as conn:
        result["grade_upserted"] = backfill_from_energy_grade(conn)

    if settings.DATA_GO_KR_API_KEY:
        with engine.begin() as conn:
            result["hub_upserted"] = collect_bldg_energy_hub(
                conn, settings.DATA_GO_KR_API_KEY, limit=2000
            )
        with engine.connect() as conn:
            result["kea_upserted"] = collect_kea_cert(
                conn, settings.DATA_GO_KR_API_KEY
            )

    with engine.connect() as conn:
        after = get_coverage_stats(conn)
    print(f"\n[결과]")
    print(f"  등급 변환 (DB):  {result['grade_upserted']:,} 건")
    print(f"  건축HUB 실소비:  {result.get('hub_upserted', 0):,} 건")
    print(f"  KEA 인증등급:    {result.get('kea_upserted', 0):,} 건")
    print(f"  에너지 데이터:   {after.get('with_energy', 0):,} 건")
    coverage = (after.get('with_energy', 0) / max(after.get('total_buildings', 1), 1) * 100)
    print(f"  커버리지:        {coverage:.1f}%")
    # simulation_type별 집계
    rows = []
    from src.shared.database import engine as _eng
    with _eng.connect() as _c:
        rows = _c.execute(text(
            "SELECT simulation_type, COUNT(*) AS cnt, ROUND(AVG(total_energy)::NUMERIC,1) AS avg_eui "
            "FROM energy_results GROUP BY simulation_type ORDER BY cnt DESC"
        )).fetchall()
    for r in rows:
        print(f"  [{r.simulation_type}] {r.cnt:,} 건, 평균 {r.avg_eui} kWh/m²·년")
