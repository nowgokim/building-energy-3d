"""Phase 4-D: EnergyPlus on-demand retrofit 시뮬레이션 러너

건물 PNU → buildings_enriched 파라미터 → IDF 생성 → EnergyPlus 실행
→ 결과 파싱 → energy_results Tier 3 저장.

대상: Tier 1/2 실측/인증 건물 + 리트로핏 시나리오 분석 요청 건물.
770K 전체 시뮬이 아닌 on-demand 방식.

Usage:
    python -m src.simulation.energyplus_runner --pnu 1101010100100010001
"""
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 경로 상수 ─────────────────────────────────────────────────────────────────
ENERGYPLUS_EXE = Path("C:/EnergyPlusV24-1-0/energyplus.exe")
IDF_TEMPLATE_DIR = Path("C:/EnergyPlusV24-1-0/ExampleFiles")
EPW_DIR = Path("C:/Users/User/Desktop/myjob/8.simulation/ems_transformer/weather")

# ── IDF 템플릿: archetype → 파일명 ────────────────────────────────────────────
IDF_TEMPLATES: dict[str, str] = {
    "apartment":                       "ASHRAE901_ApartmentHighRise_STD2019_Denver.idf",
    "apartment_district_heating":      "ASHRAE901_ApartmentHighRise_STD2019_Denver.idf",
    "apartment_ondol":                 "ASHRAE901_ApartmentMidRise_STD2019_Denver.idf",
    "residential_single":              "ASHRAE901_ApartmentMidRise_STD2019_Denver.idf",
    "office":                          "ASHRAE901_OfficeLarge_STD2019_Denver.idf",
    "retail":                          "ASHRAE901_RetailStandalone_STD2019_Denver.idf",
    "education":                       "ASHRAE901_SchoolPrimary_STD2019_Denver.idf",
    "hospital":                        "ASHRAE901_Hospital_STD2019_Denver.idf",
    "warehouse":                       "ASHRAE901_Warehouse_STD2019_Denver.idf",
    "cultural":                        "ASHRAE901_RetailStandalone_STD2019_Denver.idf",
    "mixed_use":                       "ASHRAE901_HotelLarge_STD2019_Denver.idf",
    "datacenter":                      "ASHRAE901_OfficeLarge_STD2019_Denver.idf",
    "mixed_residential_commercial":    "ASHRAE901_ApartmentHighRise_STD2019_Denver.idf",
}

# ── EPW: city(소문자) → 파일명 ────────────────────────────────────────────────
EPW_FILES: dict[str, str] = {
    "seoul":     "KOR_Seoul.epw",
    "busan":     "KOR_Busan.epw",
    "daegu":     "KOR_Daegu.epw",
    "incheon":   "KOR_Incheon.epw",
    "gwangju":   "KOR_Gwangju.epw",
    "daejeon":   "KOR_Daejeon.epw",
    "ulsan":     "KOR_Ulsan.epw",
    "gangneung": "KOR_Gangneung.epw",
    "cheongju":  "KOR_Cheongju.epw",
    "jeju":      "KOR_Jeju.epw",
}

# ── 연대별 envelope_factor (ASHRAE STD2019 대비 한국 건물 외피 성능 비율) ─────
# > 1.0 = 더 낮은 단열 (더 높은 U-value) → 에너지 소비 증가
VINTAGE_ENVELOPE_FACTOR: dict[str, float] = {
    "pre-1980":  2.8,
    "1980-2000": 1.9,
    "2001-2010": 1.3,
    "post-2010": 1.0,
}

# ── 한국 표준 설정온도 (°C) ──────────────────────────────────────────────────
KOREAN_COOLING_SETPOINT = 26.0   # 냉방 (ASHRAE 기준 24°C)
KOREAN_HEATING_SETPOINT = 20.0   # 난방 (ASHRAE 기준 21°C)


# ─────────────────────────────────────────────────────────────────────────────
# IDF 텍스트 수정 헬퍼 (modify_idf_params.py 패턴)
# ─────────────────────────────────────────────────────────────────────────────

def _code_part(line: str) -> str:
    idx = line.find("!")
    return line[:idx] if idx >= 0 else line


def _find_objects(lines: list[str], obj_type: str) -> list[dict]:
    """IDF 텍스트에서 특정 타입의 객체 위치/필드를 반환."""
    results = []
    pattern = re.compile(r"^\s*" + re.escape(obj_type) + r"\s*,", re.IGNORECASE)
    i = 0
    while i < len(lines):
        if pattern.match(lines[i]):
            obj = {"start": i, "end": i, "fields": []}
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if not stripped or stripped.startswith("!"):
                    j += 1
                    continue
                code = _code_part(stripped)
                val = code.rstrip(",; \t").strip()
                obj["fields"].append({"idx": j, "val": val})
                obj["end"] = j
                if ";" in code:
                    break
                j += 1
            results.append(obj)
        i += 1
    return results


def _set_schedule_value(lines: list[str], schedule_name: str, new_value: float) -> list[str]:
    """Schedule:Compact 내 온도값 수정 (이름 기반 검색)."""
    lines = list(lines)
    in_schedule = False
    target_pat = re.compile(r"Schedule:Compact", re.IGNORECASE)
    name_pat = re.compile(re.escape(schedule_name), re.IGNORECASE)

    for i, line in enumerate(lines):
        if target_pat.match(line.strip()):
            in_schedule = True
        if in_schedule and name_pat.search(line):
            # 이 Schedule 블록에서 숫자 값들을 새 값으로 교체
            for j in range(i + 1, min(i + 30, len(lines))):
                stripped = lines[j].strip()
                if stripped.startswith("!"):
                    continue
                if ";" in _code_part(stripped) and not any(
                    kw in stripped.lower() for kw in ["through", "for", "until", "interpolate"]
                ):
                    # 순수 숫자 값 라인
                    code = _code_part(stripped)
                    val = code.rstrip(",; \t").strip()
                    try:
                        float(val)
                        indent = len(lines[j]) - len(lines[j].lstrip())
                        sep = ";" if ";" in code else ","
                        comment = ""
                        bang = stripped.find("!")
                        if bang >= 0:
                            comment = " " + stripped[bang:]
                        lines[j] = " " * indent + f"{new_value}{sep}{comment}\n"
                    except ValueError:
                        pass
            in_schedule = False
    return lines


def modify_idf(
    idf_content: str,
    envelope_factor: float = 1.0,
    cooling_setpoint: float = KOREAN_COOLING_SETPOINT,
    heating_setpoint: float = KOREAN_HEATING_SETPOINT,
) -> str:
    """IDF 텍스트에서 외피 성능 + 설정온도를 수정한다.

    Parameters
    ----------
    envelope_factor:
        > 1.0 → 단열 성능 저하 (U-value 증가). 연대별 보정.
    cooling_setpoint / heating_setpoint:
        한국 기준 설정온도 (°C).
    """
    lines = idf_content.splitlines(keepends=True)

    # 1. Material 외피 수정 (conductivity × envelope_factor)
    mat_objs = _find_objects(lines, "Material")
    for obj in mat_objs:
        if len(obj["fields"]) >= 3:  # field[2] = Conductivity
            field = obj["fields"][2]
            try:
                orig = float(field["val"])
                new_val = round(orig * envelope_factor, 4)
                lines[field["idx"]] = lines[field["idx"]].replace(field["val"], str(new_val), 1)
            except ValueError:
                pass

    # 2. Material:NoMass 외피 수정 (ThermalResistance / envelope_factor)
    nomass_objs = _find_objects(lines, "Material:NoMass")
    for obj in nomass_objs:
        if len(obj["fields"]) >= 2:  # field[1] = ThermalResistance
            field = obj["fields"][1]
            try:
                orig = float(field["val"])
                # 단열 저하 = 저항 감소 → / envelope_factor
                new_val = round(orig / envelope_factor, 4)
                lines[field["idx"]] = lines[field["idx"]].replace(field["val"], str(new_val), 1)
            except ValueError:
                pass

    # 3. WindowMaterial:SimpleGlazingSystem → U-Factor × envelope_factor
    win_objs = _find_objects(lines, "WindowMaterial:SimpleGlazingSystem")
    for obj in win_objs:
        if len(obj["fields"]) >= 2:  # field[1] = U-Factor
            field = obj["fields"][1]
            try:
                orig = float(field["val"])
                new_val = round(orig * envelope_factor, 4)
                lines[field["idx"]] = lines[field["idx"]].replace(field["val"], str(new_val), 1)
            except ValueError:
                pass

    # 4. 설정온도: ThermostatSetpoint:DualSetpoint → 모든 숫자 필드 교체
    thermo_objs = _find_objects(lines, "ThermostatSetpoint:DualSetpoint")
    for obj in thermo_objs:
        # field[1]=HeatSetTempSchedule, field[2]=CoolSetTempSchedule (이름)
        # 직접 Schedule 이름을 찾아서 그 스케쥴의 숫자 값을 수정하는 것이
        # 안전하지만, 여기서는 Schedule:Compact 내 고정값을 직접 교체
        pass  # Schedule 기반 수정은 아래에서 처리

    # 5. Thermostat Schedule 직접 수정: 숫자값 전체 교체 방식
    # ASHRAE 참조 건물의 Heating ≈ 21°C, Cooling ≈ 24°C
    # 한국 기준으로 Heating → 20°C, Cooling → 26°C
    content = "".join(lines)

    # Cooling: 24.0 → 26.0 (± 0.5 범위 내 값만)
    def replace_temp(m: re.Match, new_t: float, orig_range: tuple[float, float]) -> str:
        val = float(m.group(1))
        if orig_range[0] <= val <= orig_range[1]:
            return m.group(0).replace(m.group(1), str(new_t))
        return m.group(0)

    # 냉방 설정온도 교체 (23~25°C → 26°C)
    content = re.sub(
        r"(2[3-5]\.\d+)",
        lambda m: str(cooling_setpoint) if 23.0 <= float(m.group(1)) <= 25.5 else m.group(0),
        content,
    )
    # 난방 설정온도 교체 (21~22°C → 20°C)
    content = re.sub(
        r"(2[12]\.\d+)",
        lambda m: str(heating_setpoint) if 21.0 <= float(m.group(1)) <= 22.5 else m.group(0),
        content,
    )

    return content


# ─────────────────────────────────────────────────────────────────────────────
# 결과 파싱
# ─────────────────────────────────────────────────────────────────────────────

def parse_eui_from_html(html_path: Path, floor_area_m2: float) -> dict:
    """eplustbl.htm 에서 연간 에너지 합계를 파싱한다.

    Returns
    -------
    dict with keys: total_energy, heating, cooling, hot_water, lighting,
                    ventilation, unit (kWh/m²/yr)
    """
    try:
        content = html_path.read_text(encoding="latin-1", errors="replace")
    except Exception as e:
        logger.error("eplustbl.htm 읽기 실패: %s", e)
        return {}

    # "Site:EnergyUse" 또는 "End Uses" 테이블에서 합계 추출
    # EnergyPlus HTML table: "Total Site Energy" → GJ
    total_gj = 0.0
    heating_gj = cooling_gj = lighting_gj = equip_gj = 0.0

    # End Uses by Energy Source 테이블에서 Total 컬럼 파싱
    pat = re.compile(
        r"<td[^>]*>\s*Total\s*</td>.*?<td[^>]*>([\d.]+)\s*</td>",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pat.finditer(content):
        try:
            total_gj = float(m.group(1))
            break
        except ValueError:
            pass

    # Heating/Cooling/Lighting from specific row patterns
    def _extract_row(name: str) -> float:
        p = re.compile(
            r"<td[^>]*>\s*" + re.escape(name) + r"\s*</td>.*?<td[^>]*>([\d.]+)\s*</td>",
            re.DOTALL | re.IGNORECASE,
        )
        m = p.search(content)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return 0.0

    heating_gj  = _extract_row("Heating")
    cooling_gj  = _extract_row("Cooling")
    lighting_gj = _extract_row("Interior Lighting")
    equip_gj    = _extract_row("Interior Equipment")

    # GJ → kWh
    gj_to_kwh = 1_000_000.0 / 3600.0

    def _eui(gj: float) -> float:
        if floor_area_m2 <= 0:
            return 0.0
        return round(gj * gj_to_kwh / floor_area_m2, 1)

    total_eui = _eui(total_gj)
    return {
        "total_energy": total_eui,
        "heating":      _eui(heating_gj),
        "cooling":      _eui(cooling_gj),
        "lighting":     _eui(lighting_gj),
        "hot_water":    0.0,
        "ventilation":  0.0,
        "source":       "energyplus_tier3",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 메인 시뮬레이션 함수
# ─────────────────────────────────────────────────────────────────────────────

def simulate_building(
    pnu: str,
    db_url: str,
    output_base: Optional[Path] = None,
    timeout_s: int = 600,
) -> dict:
    """PNU → EnergyPlus 시뮬레이션 → EUI dict 반환.

    Parameters
    ----------
    pnu:
        건물 필지 고유번호 (19자리).
    db_url:
        PostgreSQL 연결 URL.
    output_base:
        시뮬레이션 출력 디렉토리. None이면 임시 디렉토리 사용.
    timeout_s:
        EnergyPlus 타임아웃 (초). 기본 10분.

    Returns
    -------
    dict
        EUI 결과 + 메타정보. 실패 시 {"error": "..."}.
    """
    from src.shared.database import get_pg_conn
    from src.simulation.archetypes import _normalize_usage, _classify_vintage
    from src.simulation.city_eui_base import sigungu_to_city

    # 1. buildings_enriched에서 건물 파라미터 조회
    try:
        conn = get_pg_conn(db_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT pnu, usage_type, built_year, floors_above, total_area,
                   LEFT(pnu, 5) AS sigungu_cd
            FROM buildings_enriched
            WHERE pnu = %s
            LIMIT 1
        """, (pnu,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return {"error": f"DB 조회 실패: {e}"}

    if not row:
        return {"error": f"PNU {pnu} 미발견"}

    pnu_db, usage_type, built_year, floors_above, total_area, sigungu_cd = row
    city = sigungu_to_city(sigungu_cd)
    usage = _normalize_usage(usage_type or "")
    vintage = _classify_vintage(built_year)
    floor_area = float(total_area or 1000.0)

    logger.info("시뮬 대상: pnu=%s usage=%s vintage=%s city=%s area=%.0fm²",
                pnu, usage, vintage, city, floor_area)

    # 2. IDF 템플릿 선택
    idf_name = IDF_TEMPLATES.get(usage, IDF_TEMPLATES["office"])
    idf_src = IDF_TEMPLATE_DIR / idf_name
    if not idf_src.exists():
        return {"error": f"IDF 템플릿 없음: {idf_src}"}

    # 3. EPW 파일 선택
    epw_name = EPW_FILES.get(city, EPW_FILES["seoul"])
    epw_path = EPW_DIR / epw_name
    if not epw_path.exists():
        return {"error": f"EPW 파일 없음: {epw_path}"}

    # 4. 출력 디렉토리 준비
    use_tmp = output_base is None
    if use_tmp:
        tmp_dir = tempfile.mkdtemp(prefix=f"ep_{pnu}_")
        work_dir = Path(tmp_dir)
    else:
        work_dir = Path(output_base) / f"ep_{pnu}"
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 5. IDF 수정
        envelope_factor = VINTAGE_ENVELOPE_FACTOR.get(vintage, 1.3)
        idf_content = idf_src.read_text(encoding="latin-1", errors="replace")
        modified = modify_idf(
            idf_content,
            envelope_factor=envelope_factor,
            cooling_setpoint=KOREAN_COOLING_SETPOINT,
            heating_setpoint=KOREAN_HEATING_SETPOINT,
        )
        idf_out = work_dir / "in.idf"
        idf_out.write_text(modified, encoding="latin-1")
        logger.info("IDF 수정 완료: template=%s envelope_factor=%.1f", idf_name, envelope_factor)

        # 6. EnergyPlus 실행
        if not ENERGYPLUS_EXE.exists():
            return {"error": f"EnergyPlus 실행 파일 없음: {ENERGYPLUS_EXE}"}

        cmd = [
            str(ENERGYPLUS_EXE),
            "-w", str(epw_path),
            "-d", str(work_dir),
            str(idf_out),
        ]
        logger.info("EnergyPlus 실행: %s", " ".join(cmd[:4]))
        t0 = time.time()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        elapsed = time.time() - t0
        logger.info("EnergyPlus 종료: exit=%d, %.1fs", result.returncode, elapsed)

        if result.returncode != 0:
            err_snippet = (result.stderr or result.stdout or "")[:400]
            return {"error": f"EnergyPlus 실패 (exit={result.returncode}): {err_snippet}"}

        # 7. 결과 파싱
        html_out = work_dir / "eplustbl.htm"
        if not html_out.exists():
            return {"error": "eplustbl.htm 미생성 — 시뮬레이션 실패 의심"}

        eui = parse_eui_from_html(html_out, floor_area)
        if not eui or eui.get("total_energy", 0) <= 0:
            return {"error": "EUI 파싱 실패 (total_energy=0)"}

        eui.update({
            "pnu":       pnu,
            "city":      city,
            "usage":     usage,
            "vintage":   vintage,
            "area_m2":   floor_area,
            "idf":       idf_name,
            "elapsed_s": round(elapsed, 1),
        })
        logger.info("시뮬 완료: pnu=%s total_eui=%.1f kWh/m²/yr", pnu, eui["total_energy"])
        return eui

    except subprocess.TimeoutExpired:
        return {"error": f"EnergyPlus 타임아웃 ({timeout_s}s)"}
    except Exception as e:
        logger.exception("시뮬 오류: %s", e)
        return {"error": str(e)}
    finally:
        if use_tmp:
            shutil.rmtree(work_dir, ignore_errors=True)


def save_tier3(result: dict, db_url: str) -> bool:
    """EnergyPlus 결과를 energy_results Tier 3으로 저장 (Tier 1/2 보호)."""
    from src.shared.database import get_pg_conn
    pnu = result.get("pnu")
    if not pnu or "error" in result:
        return False

    try:
        conn = get_pg_conn(db_url)
        cur = conn.cursor()
        # Tier 1/2 보호 확인
        cur.execute("SELECT data_tier FROM energy_results WHERE pnu = %s", (pnu,))
        row = cur.fetchone()
        if row and row[0] in (1, 2):
            logger.info("Tier %d 보호: pnu=%s, Tier3 저장 스킵", row[0], pnu)
            cur.close()
            conn.close()
            return False

        cur.execute("""
            INSERT INTO energy_results
                (pnu, data_tier, simulation_type, total_energy,
                 heating, cooling, hot_water, lighting, ventilation, created_at)
            VALUES (%s, 3, 'energyplus_on_demand',
                    %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (pnu) DO UPDATE SET
                data_tier       = 3,
                simulation_type = 'energyplus_on_demand',
                total_energy    = EXCLUDED.total_energy,
                heating         = EXCLUDED.heating,
                cooling         = EXCLUDED.cooling,
                hot_water       = EXCLUDED.hot_water,
                lighting        = EXCLUDED.lighting,
                ventilation     = EXCLUDED.ventilation,
                created_at      = NOW()
        """, (
            pnu,
            result["total_energy"],
            result.get("heating", 0),
            result.get("cooling", 0),
            result.get("hot_water", 0),
            result.get("lighting", 0),
            result.get("ventilation", 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Tier3 저장 완료: pnu=%s total=%.1f", pnu, result["total_energy"])
        return True

    except Exception as e:
        logger.error("Tier3 저장 실패: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI 실행
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, os, json

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser()
    parser.add_argument("--pnu", required=True)
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--save", action="store_true", help="결과를 DB에 저장")
    args = parser.parse_args()

    if not args.db_url:
        sys.exit("DATABASE_URL 환경변수 필요")

    out_dir = Path(args.output_dir) if args.output_dir else None
    result = simulate_building(args.pnu, args.db_url, output_base=out_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.save and "error" not in result:
        saved = save_tier3(result, args.db_url)
        print(f"DB 저장: {'성공' if saved else '실패'}")
