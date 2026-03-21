"""3D Tiles 생성 테스트 — trimesh 기반 건물 메시 생성"""
import pytest
from src.tile_generation.generate import energy_grade_to_color


class TestEnergyGradeColor:
    def test_top_grade_green(self):
        """최고등급(1+++) = 녹색 계열"""
        color = energy_grade_to_color("1+++")
        assert color[1] > color[0]  # G > R (녹색)
        assert len(color) == 4  # RGBA

    def test_worst_grade_red(self):
        """최저등급(7) = 적색 계열"""
        color = energy_grade_to_color("7")
        assert color[0] > color[1]  # R > G (적색)

    def test_unknown_grade_gray(self):
        """등급 없음 = 회색"""
        color = energy_grade_to_color(None)
        assert color[0] == color[1] == color[2]  # R=G=B (회색)

    def test_all_grades_unique(self):
        """모든 등급이 고유한 색상"""
        grades = ["1+++", "1++", "1+", "1", "2", "3", "4", "5", "6", "7"]
        colors = [tuple(energy_grade_to_color(g)) for g in grades]
        assert len(set(colors)) == len(grades)
