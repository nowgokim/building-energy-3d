"""PNU 코드 생성/파싱 테스트 — 프로젝트 최우선 테스트 대상"""
import pytest
from src.geometry.pnu import generate_pnu, parse_pnu, generate_building_mgt_sn


class TestGeneratePnu:
    def test_basic_pnu(self):
        """마포구 공덕동 일반 대지"""
        pnu = generate_pnu("11440", "10100", "0", "0123", "0004")
        assert len(pnu) == 19
        assert pnu == "1144010100101230004"

    def test_mountain_lot(self):
        """산 구분 (대지구분코드 1 → 2)"""
        pnu = generate_pnu("11440", "10100", "1", "0050", "0000")
        assert pnu[10] == "2"  # 산

    def test_zero_padding(self):
        """본번/부번 4자리 제로패딩"""
        pnu = generate_pnu("11440", "10100", "0", "5", "3")
        assert pnu.endswith("00050003")

    def test_daeji_gubun_mapping(self):
        """대지구분코드 매핑: 0→1, 1→2, 2→3"""
        assert generate_pnu("11440", "10100", "0", "1", "0")[10] == "1"
        assert generate_pnu("11440", "10100", "1", "1", "0")[10] == "2"
        assert generate_pnu("11440", "10100", "2", "1", "0")[10] == "3"


class TestParsePnu:
    def test_parse_roundtrip(self):
        """생성 → 파싱 왕복 검증"""
        pnu = generate_pnu("11440", "10100", "0", "0123", "0004")
        parsed = parse_pnu(pnu)
        assert parsed["sigungu_code"] == "11440"
        assert parsed["bdong_code"] == "10100"
        assert parsed["daeji_gubun"] == "1"  # "0" → "1" 매핑
        assert parsed["bon"] == "0123"
        assert parsed["ji"] == "0004"

    def test_invalid_length(self):
        """잘못된 길이"""
        with pytest.raises(ValueError):
            parse_pnu("12345")


class TestBuildingMgtSn:
    def test_25_digit(self):
        """건물관리번호 25자리"""
        pnu = "1144010100101230004"
        sn = generate_building_mgt_sn(pnu, "1")
        assert len(sn) == 25
        assert sn[:19] == pnu
        assert sn[19:] == "000001"
