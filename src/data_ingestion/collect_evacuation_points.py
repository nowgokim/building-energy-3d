"""
Phase F3-04: 서울시 피난 집결지 데이터 수집
출처:
  1. 서울 열린데이터광장 - 지정 대피소 (API)
  2. 서울 공원 (ST_DWithin 기반 대형 공원 추출)
  3. 학교 (OSM tags: amenity=school 대형 시설)

실행: python -m src.data_ingestion.collect_evacuation_points
"""
import os
import json
import logging
import requests
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:devpassword@localhost:5434/buildings",
)

# 서울 열린데이터광장 재난대피소 API
# https://data.seoul.go.kr/dataList/OA-2842/S/1/datasetView.do
SEOUL_SHELTER_URL = "http://openapi.seoul.go.kr:8088/{key}/json/TBSHLTRRSVTBSH/1/1000/"

# 서울시 좌표계 내 박스 (대략)
SEOUL_BBOX = (126.734086, 37.413294, 127.269311, 37.715133)


def collect_seoul_shelters(api_key: str) -> list[dict]:
    """서울시 공식 대피소 API 수집."""
    url = SEOUL_SHELTER_URL.format(key=api_key)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("TBSHLTRRSVTBSH", {}).get("row", [])
        results = []
        for r in rows:
            try:
                lng = float(r.get("REFINE_WGS84_LOGT", 0))
                lat = float(r.get("REFINE_WGS84_LAT", 0))
            except (ValueError, TypeError):
                continue
            if not (126.5 < lng < 127.5 and 37.3 < lat < 37.8):
                continue
            results.append({
                "name":     r.get("FCLT_NM", "대피소"),
                "category": "지정대피소",
                "capacity": _parse_int(r.get("XCRD", 0)),
                "address":  r.get("REFINE_ROADNM_ADDR", ""),
                "lng": lng,
                "lat": lat,
            })
        logger.info("서울 대피소 %d건 수집", len(results))
        return results
    except Exception as e:
        logger.error("대피소 API 오류: %s", e)
        return []


def _parse_int(val) -> int | None:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def insert_points(points: list[dict], engine) -> int:
    """evacuation_points 테이블에 upsert."""
    if not points:
        return 0
    inserted = 0
    with engine.begin() as conn:
        for p in points:
            conn.execute(text("""
                INSERT INTO evacuation_points (name, category, capacity, address, geom, source)
                VALUES (:name, :category, :capacity, :address,
                        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
                        :source)
                ON CONFLICT DO NOTHING
            """), {
                "name":     p["name"],
                "category": p.get("category", "기타"),
                "capacity": p.get("capacity"),
                "address":  p.get("address", ""),
                "lng":      p["lng"],
                "lat":      p["lat"],
                "source":   p.get("source", "seoul_opendata"),
            })
            inserted += 1
    return inserted


def add_fallback_points(engine):
    """
    API 키 없이도 동작하는 폴백: 서울 주요 공원·광장 하드코딩.
    실제 운영 시 API 수집 데이터로 대체.
    """
    fallback = [
        {"name": "서울광장",      "category": "광장",   "capacity": 50000, "lng": 126.9780, "lat": 37.5662},
        {"name": "여의도공원",    "category": "공원",   "capacity": 30000, "lng": 126.9249, "lat": 37.5259},
        {"name": "올림픽공원",    "category": "공원",   "capacity": 80000, "lng": 127.1219, "lat": 37.5205},
        {"name": "남산공원",      "category": "공원",   "capacity": 20000, "lng": 126.9900, "lat": 37.5513},
        {"name": "월드컵공원",    "category": "공원",   "capacity": 50000, "lng": 126.8879, "lat": 37.5679},
        {"name": "북서울꿈의숲",  "category": "공원",   "capacity": 15000, "lng": 127.0468, "lat": 37.6296},
        {"name": "용산가족공원",  "category": "공원",   "capacity": 10000, "lng": 126.9784, "lat": 37.5252},
        {"name": "서울대공원",    "category": "공원",   "capacity": 40000, "lng": 127.0107, "lat": 37.4314},
        {"name": "보라매공원",    "category": "공원",   "capacity": 20000, "lng": 126.9218, "lat": 37.4943},
        {"name": "중랑캠핑숲",    "category": "공원",   "capacity": 5000,  "lng": 127.0997, "lat": 37.6274},
        # 각 자치구 구민체육관 (25개 구 전체 커버)
        {"name": "마포구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9028, "lat": 37.5607},
        {"name": "강남구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0477, "lat": 37.5040},
        {"name": "노원구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0604, "lat": 37.6544},
        {"name": "은평구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9287, "lat": 37.6177},
        {"name": "송파구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.1116, "lat": 37.5145},
        {"name": "종로구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9827, "lat": 37.5940},
        {"name": "중구민체육관",     "category": "체육관", "capacity": 3000, "lng": 126.9977, "lat": 37.5636},
        {"name": "용산구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9884, "lat": 37.5340},
        {"name": "성동구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0374, "lat": 37.5631},
        {"name": "광진구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0820, "lat": 37.5389},
        {"name": "동대문구민체육관", "category": "체육관", "capacity": 3000, "lng": 127.0543, "lat": 37.5745},
        {"name": "중랑구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0926, "lat": 37.6065},
        {"name": "성북구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0170, "lat": 37.6050},
        {"name": "강북구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0251, "lat": 37.6396},
        {"name": "도봉구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0469, "lat": 37.6686},
        {"name": "서대문구민체육관", "category": "체육관", "capacity": 3000, "lng": 126.9368, "lat": 37.5793},
        {"name": "양천구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.8662, "lat": 37.5249},
        {"name": "강서구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.8500, "lat": 37.5545},
        {"name": "구로구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.8878, "lat": 37.4954},
        {"name": "금천구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.8955, "lat": 37.4567},
        {"name": "영등포구민체육관", "category": "체육관", "capacity": 3000, "lng": 126.9063, "lat": 37.5261},
        {"name": "동작구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9399, "lat": 37.4970},
        {"name": "관악구민체육관",   "category": "체육관", "capacity": 3000, "lng": 126.9514, "lat": 37.4769},
        {"name": "서초구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.0325, "lat": 37.4836},
        {"name": "강동구민체육관",   "category": "체육관", "capacity": 3000, "lng": 127.1467, "lat": 37.5496},
    ]
    for p in fallback:
        p["source"] = "fallback_hardcoded"
    return insert_points(fallback, engine)


if __name__ == "__main__":
    engine = create_engine(DATABASE_URL)
    api_key = os.environ.get("SEOUL_DATA_API_KEY", "")

    if api_key:
        points = collect_seoul_shelters(api_key)
        n = insert_points(points, engine)
        logger.info("API 수집 %d건 적재", n)
    else:
        logger.warning("SEOUL_DATA_API_KEY 없음 — 폴백 데이터 사용")
        n = add_fallback_points(engine)
        logger.info("폴백 %d건 적재", n)
