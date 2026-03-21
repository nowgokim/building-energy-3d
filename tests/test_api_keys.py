"""API 키 연동 테스트 - 3개 외부 API 호출 검증"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()


def test_data_go_kr():
    """공공데이터포털 - 건축물대장 API 테스트"""
    api_key = os.environ.get("DATA_GO_KR_API_KEY", "")
    if not api_key:
        print("[SKIP] DATA_GO_KR_API_KEY not set")
        return False

    url = "http://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"
    params = {
        "serviceKey": api_key,
        "sigunguCd": "11440",  # 마포구
        "bjdongCd": "10100",   # 공덕동
        "numOfRows": "3",
        "pageNo": "1",
        "_type": "json",
    }

    print("[TEST] 공공데이터포털 건축물대장 API...")
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        # 응답 구조 확인
        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode", "")
        result_msg = header.get("resultMsg", "")

        if result_code == "00":
            items = data["response"]["body"].get("items", {}).get("item", [])
            count = len(items) if isinstance(items, list) else (1 if items else 0)
            print(f"  [PASS] 성공 - {count}건 조회")
            if items:
                item = items[0] if isinstance(items, list) else items
                print(f"  건물명: {item.get('bldNm', 'N/A')}")
                print(f"  주용도: {item.get('mainPurpsCdNm', 'N/A')}")
                print(f"  층수: {item.get('grndFlrCnt', 'N/A')}층")
            return True
        else:
            print(f"  [FAIL] 코드={result_code}, 메시지={result_msg}")
            return False
    except Exception as e:
        print(f"  [FAIL] 요청 실패: {e}")
        return False


def test_vworld():
    """VWorld API 테스트"""
    api_key = os.environ.get("VWORLD_API_KEY", "")
    if not api_key:
        print("[SKIP] VWORLD_API_KEY not set")
        return False

    url = "https://api.vworld.kr/req/data"
    params = {
        "key": api_key,
        "service": "data",
        "request": "GetFeature",
        "data": "LT_C_ADSIDO_INFO",
        "geomFilter": "POINT(126.9095 37.5565)",  # 마포구
        "geometry": "false",
        "size": "1",
    }

    print("[TEST] VWorld API...")
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        status = data.get("response", {}).get("status", "")

        if status == "OK":
            features = data["response"]["result"].get("featureCollection", {}).get("features", [])
            print(f"  [PASS] 성공 - {len(features)}건 조회")
            return True
        else:
            error = data.get("response", {}).get("error", {})
            print(f"  [FAIL] status={status}, error={error}")
            return False
    except Exception as e:
        print(f"  [FAIL] 요청 실패: {e}")
        return False


def test_juso():
    """도로명주소 API 테스트"""
    api_key = os.environ.get("JUSO_API_KEY", "")
    if not api_key:
        print("[SKIP] JUSO_API_KEY not set")
        return False

    url = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
    params = {
        "confmKey": api_key,
        "keyword": "마포구 공덕동",
        "resultType": "json",
        "countPerPage": "3",
        "currentPage": "1",
    }

    print("[TEST] 도로명주소 API...")
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        common = data.get("results", {}).get("common", {})
        error_code = common.get("errorCode", "")

        if error_code == "0":
            juso_list = data["results"].get("juso", [])
            print(f"  [PASS] 성공 - {len(juso_list)}건 조회")
            if juso_list:
                j = juso_list[0]
                print(f"  도로명: {j.get('roadAddr', 'N/A')}")
                print(f"  지번: {j.get('jibunAddr', 'N/A')}")
            return True
        else:
            error_msg = common.get("errorMessage", "")
            print(f"  [FAIL] 코드={error_code}, 메시지={error_msg}")
            return False
    except Exception as e:
        print(f"  [FAIL] 요청 실패: {e}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("API 키 연동 테스트")
    print("=" * 50)

    results = {
        "공공데이터포털": test_data_go_kr(),
        "VWorld": test_vworld(),
        "도로명주소": test_juso(),
    }

    print("\n" + "=" * 50)
    print("결과 요약")
    print("=" * 50)
    all_pass = True
    for name, ok in results.items():
        status = "PASS" if ok else ("SKIP" if ok is False else "FAIL")
        print(f"  {name}: {status}")
        if not ok:
            all_pass = False

    sys.exit(0 if all_pass else 1)
