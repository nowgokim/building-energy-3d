# RFC: 에너지 시뮬레이션 파이프라인

**문서 버전**: 1.1 (전문가 리뷰 반영: 구조유형, 열화계수, 온돌, UHI, 발코니)
**작성일**: 2026-03-21
**관련 문서**: [PRD](./PRD.md) | [Architecture](./ARCHITECTURE.md) | [RFC-Data-Pipeline](./RFC-DATA-PIPELINE.md)
**대상 지역**: 서울특별시 마포구 (기후지역: 중부2)

---

## 1. 개요

건축물대장 속성(용도, 건축년도, 구조, 면적)에서 에너지 시뮬레이션 파라미터를 추정하고, 원형(Archetype) 기반 EnergyPlus 시뮬레이션 + ML 대리모델로 마포구 전 건물의 에너지 소비를 예측하는 파이프라인.

### 1.1 핵심 과제

건축물대장이 제공하는 것과 EnergyPlus가 필요로 하는 것 사이의 갭:

```
건축물대장 제공              EnergyPlus 필요              갭 해소 방법
─────────────              ──────────────              ───────────
용도 (주거/사무/상업)    →   내부 부하 프로파일        →  용도별 기본값
건축년도 (사용승인일)    →   외피 U-value             →  국토부 고시 연도별 기준
구조 (RC/철골/조적)     →   열용량                   →  구조별 기본값
층수, 높이, 면적        →   건물 지오메트리           →  Shoebox 모델
에너지효율등급 (있으면)  →   검증용                   →  시뮬레이션 결과와 비교
                          창면적비 (WWR)            →  용도별 기본값
                          HVAC 시스템               →  용도+년도+규모별 추정
                          기상 데이터               →  서울 EPW 파일
```

---

## 2. 건물 원형(Archetype) 분류 체계

### 2.1 분류 축

| 축 | 분류 | 값 |
|-----|------|-----|
| **용도** | 건축물대장 주용도명 기반 | 공동주택, 단독주택, 사무, 상업(판매), 교육, 의료, 숙박, 문화, 공장, 기타 (10종) |
| **건축년도** | 에너지절약설계기준 개정 시점 기준 | pre-2001, 2001-2009, 2010-2016, 2017-present (4구간) |
| **규모** | 연면적 기준 | small(<500m²), medium(500~3000m²), large(>3000m²) (3등급) |
| **구조** | 건축물대장 구조코드 기반 | RC(철근콘크리트), 철골, 조적/목구조 (3종). **구조간 열성능 차이 30%+** |
| **기후** | 마포구 고정 | 중부2 (1종) |

**이론적 조합**: 10 x 4 x 3 x 3 x 1 = 360개
**실 존재 추정**: 80~120개 (마포구에 없는 조합 제외)

### 2.2 한국 외피 기준 매핑 (국토부 고시)

**근거**: 국토교통부고시 제2024-421호 「건축물의에너지절약설계기준」
**마포구 = 중부2지역**

#### 벽체 열관류율 (W/m²K) - 건축년도별 추정

| 건축년도 | 외벽 | 지붕 | 바닥 | 창호 | 근거 |
|----------|------|------|------|------|------|
| ~2000 | 0.58 | 0.41 | 0.58 | 3.40 | 2001년 이전 기준 추정 |
| 2001~2009 | 0.47 | 0.29 | 0.47 | 2.70 | 에너지절약설계기준 개정 |
| 2010~2016 | 0.35 | 0.20 | 0.35 | 1.80 | 강화 기준 |
| 2017~ | 0.24 | 0.15 | 0.24 | 1.20 | 현행 기준 근사 |

*주의: 정확한 수치는 국토부 고시 원문 [별표1]에서 확인 필요. 위 값은 리서치 기반 근사치.*

#### 열화계수 (Degradation Factor) 적용

법정 기준값(신축 시)과 현재 실제 성능 사이의 괴리를 보정:

| 경과 년수 | 열화계수 | 적용 U-value | 근거 |
|-----------|---------|-------------|------|
| 0~10년 | 1.0 | 기준값 그대로 | 신축 |
| 10~20년 | 1.3 | 기준값 × 1.3 | 10년당 30% 성능 저하 |
| 20~30년 | 1.7 | 기준값 × 1.7 | 누적 열화 |
| 30년+ | 2.0 | 기준값 × 2.0 (상한) | 20~30% 전체 효율 저하 |

열화 원인:
- **벽체**: 단열재 흡습, 물리적 손상, 열교 악화
- **창호**: 아르곤 가스 누출, Low-E 코팅 열화, 실란트 손상, 기밀 저하
- **지붕**: 방수층 손상에 의한 단열재 흡습

```python
def apply_degradation(base_uvalue: float, built_year: int) -> float:
    """열화계수 적용한 현재 추정 U-value"""
    age = 2026 - built_year
    if age <= 10:
        factor = 1.0
    elif age <= 20:
        factor = 1.3
    elif age <= 30:
        factor = 1.7
    else:
        factor = 2.0
    return base_uvalue * factor
```

#### 창면적비(WWR) 기본값

| 용도 | WWR | 근거 |
|------|-----|------|
| 공동주택 (아파트) | 0.25 | 한국 아파트 평균 |
| 단독주택 | 0.20 | 보수적 추정 |
| 사무 | 0.40 | 커튼월/창호 비율 |
| 상업 (판매) | 0.50 | 쇼윈도/유리벽 |
| 교육 | 0.30 | 교실 채광 |
| 의료 | 0.30 | 병실/복도 |
| 숙박 | 0.30 | 객실 창호 |
| 문화 | 0.20 | 벽체 비중 높음 |
| 공장 | 0.10 | 벽체 위주 |

#### HVAC 시스템 추정

| 용도 | 규모 | 건축년도 | 추정 HVAC |
|------|------|---------|----------|
| 공동주택 | 전체 | 전체 | 개별난방 (도시가스), 개별냉방 (벽걸이 에어컨) |
| 사무 | large | 2010~ | 중앙 공조 (AHU + 칠러/보일러) |
| 사무 | small/medium | 전체 | 개별 냉난방 (EHP/GHP) |
| 상업 | large | 전체 | 중앙 공조 |
| 상업 | small | 전체 | 개별 냉난방 (패키지 에어컨) |
| 교육 | 전체 | 전체 | 중앙 난방 + 개별 냉방 |

**한국 특성: 온돌(바닥복사난방) 모델링**

공동주택/단독주택 원형은 **반드시 온돌 시스템**으로 모델링:
- EnergyPlus 객체: `ZoneHVAC:LowTemperatureRadiant:VariableFlow`
- 열원: 가스보일러 (도시가스) → 온수 → 바닥배관
- ASHRAE 강제공기식과 근본적으로 다른 열전달 메커니즘 (복사 60% + 대류 40%)
- Shoebox IDF에서 바닥 Construction에 배관 레이어 포함 필수

#### 한국 아파트 발코니 확장

2005년 건축법 개정으로 발코니 확장 합법화. 에너지 영향:
- 2005년 이후 아파트: **확장 가정** (실제 확장률 90%+)
- 확장 시: 외기 접면 면적 증가, 단열 취약부 발생
- 열성능 영향: **7~15%** (창호 사양에 따라 변동)
- 원형 분류에 반영: 2005년 이후 공동주택 원형은 발코니 확장 상태로 모델링

#### 기후 보정: 도시열섬(UHI) 효과

서울 도시열섬 특성 (Phase 5 적용):
- 가을 최대 4.3°C, 봄 최소 3.6°C, 주야간 차이 3.8°C
- MVP에서는 표준 EPW 사용, Phase 5에서 UWG(Urban Weather Generator) 적용
- 마포구 내 지역별 차이: 상암(녹지 인접, 낮음) vs 홍대(상업 밀집, 높음)

### 2.3 원형 데이터베이스 스키마

```sql
INSERT INTO building_archetypes
    (name, usage_category, vintage_class, size_class, climate_zone,
     wall_uvalue, roof_uvalue, floor_uvalue, window_uvalue, default_wwr,
     occupancy_density, lighting_power, equipment_power)
VALUES
    -- 예시: 2001-2009년 중규모 사무
    ('office_2001-2009_medium_central2',
     '사무', '2001-2009', 'medium', '중부2',
     0.47, 0.29, 0.47, 2.70, 0.40,
     0.10, 12.0, 15.0),

    -- 예시: 2017+ 대규모 공동주택
    ('apartment_2017-present_large_central2',
     '공동주택', '2017-present', 'large', '중부2',
     0.24, 0.15, 0.24, 1.20, 0.25,
     0.04, 4.0, 3.0),

    -- ... 60~80개 원형
;
```

---

## 3. 에너지 시뮬레이션 엔진

### 3.1 기상 데이터

**서울 EPW 파일**:
- 출처: KIAEBS (한국건축환경설비학회) 표준기상데이터
- 표준: KIAEBS S-19:2024 (2025.02.24 채택)
- 기반: 기상청 30년 데이터 (1991-2020)
- 다운로드: https://www.kiaebs.org/html/standard_weather.vm (또는 BECube)
- 대안: climate.onebuilding.org → Korea → Seoul

### 3.2 Shoebox 모델 접근

도시 스케일에서 모든 건물을 상세 다중존 모델로 시뮬레이션하는 것은 비현실적. **Shoebox (단순화 모델)** 사용:

```
실제 건물 (복잡한 평면)          Shoebox 모델 (단순화)
┌──┐  ┌─────┐                   ┌──────────────┐
│  │  │     │                   │   Perimeter   │
│  └──┘     │         →        │  ┌────────┐  │
│           │                   │  │  Core  │  │
│     ┌──┐  │                   │  └────────┘  │
└─────┘  └──┘                   └──────────────┘

- 동일한 면적, 높이, 층수
- Perimeter/Core 2존 구성
- 4면 외벽 (실제 건물의 외피면적비 보정)
- 성능: 50~296배 빠름, RMSE ±11~20%
```

### 3.3 EnergyPlus 모델 생성 (geomeppy)

```python
# src/simulation/idf_generator.py

from geomeppy import IDF

def create_shoebox_idf(archetype: dict, epw_path: str) -> IDF:
    """원형 파라미터로 Shoebox IDF 생성"""

    # EnergyPlus IDD 경로
    IDF.setiddname('/path/to/Energy+.idd')
    idf = IDF('/path/to/Minimal.idf')
    idf.epw = epw_path

    # 건물 블록 추가 (정사각형 Shoebox)
    side_length = (archetype['building_area']) ** 0.5  # 정사각형 근사
    idf.add_block(
        name=archetype['name'],
        coordinates=[
            (0, 0),
            (side_length, 0),
            (side_length, side_length),
            (0, side_length)
        ],
        height=archetype['height'],
        num_stories=archetype['floors_above'],
        zoning='core/perim'  # Perimeter/Core 2존
    )

    # 창호 설정
    idf.set_wwr(archetype['default_wwr'])

    # 외피 재료 설정
    set_envelope_materials(idf, archetype)

    # 내부 부하 설정
    set_internal_loads(idf, archetype)

    # HVAC 시스템 (IdealLoads로 간소화)
    set_ideal_loads(idf)

    return idf

def set_envelope_materials(idf, archetype):
    """U-value 기반 외피 구성 설정"""
    # Wall construction
    wall_r = 1.0 / archetype['wall_uvalue'] - 0.17  # 표면열저항 제외
    # ... 절연재 두께 계산 및 Construction 객체 설정

def set_internal_loads(idf, archetype):
    """내부 부하 프로파일 설정"""
    for zone in idf.idfobjects['ZONE']:
        # People
        idf.newidfobject('PEOPLE',
            Name=f'{zone.Name}_People',
            Zone_or_ZoneList_or_Space_or_SpaceList_Name=zone.Name,
            Number_of_People_Calculation_Method='People/Area',
            People_per_Floor_Area=archetype['occupancy_density']
        )
        # Lights
        idf.newidfobject('LIGHTS',
            Name=f'{zone.Name}_Lights',
            Zone_or_ZoneList_or_Space_or_SpaceList_Name=zone.Name,
            Design_Level_Calculation_Method='Watts/Area',
            Watts_per_Floor_Area=archetype['lighting_power']
        )
        # Equipment
        idf.newidfobject('ELECTRICEQUIPMENT',
            Name=f'{zone.Name}_Equip',
            Zone_or_ZoneList_or_Space_or_SpaceList_Name=zone.Name,
            Design_Level_Calculation_Method='Watts/Area',
            Watts_per_Floor_Area=archetype['equipment_power']
        )

def set_ideal_loads(idf):
    """IdealLoadsAirSystem (간소화 HVAC)"""
    for zone in idf.idfobjects['ZONE']:
        idf.newidfobject('ZONEHVAC:IDEALLOADSAIRSYSTEM',
            Zone_Name=zone.Name,
            Heating_Limit='NoLimit',
            Cooling_Limit='NoLimit',
        )
```

### 3.4 시뮬레이션 실행

```python
# src/simulation/runner.py

from eppy.runner.run_functions import run as ep_run
import subprocess

def run_energyplus(idf_path: str, epw_path: str, output_dir: str):
    """EnergyPlus 시뮬레이션 실행"""
    subprocess.run([
        'energyplus',
        '--weather', epw_path,
        '--output-directory', output_dir,
        '--readvars',
        idf_path
    ], check=True)

def parse_results(output_dir: str) -> dict:
    """연간 에너지 결과 파싱"""
    # eplusout.csv 또는 eplustbl.htm 파싱
    # 단위: kWh/m²/yr
    return {
        'heating': ...,
        'cooling': ...,
        'hot_water': ...,
        'lighting': ...,
        'ventilation': ...,
        'total_energy': ...,
    }
```

### 3.5 Celery 태스크 (비동기 시뮬레이션)

```python
# src/simulation/tasks.py

@celery.task(bind=True, max_retries=2, time_limit=600)
def simulate_archetype(self, archetype_id: int):
    """단일 원형 EnergyPlus 시뮬레이션"""
    archetype = get_archetype(archetype_id)
    epw_path = get_seoul_epw()

    # IDF 생성
    idf = create_shoebox_idf(archetype, epw_path)
    idf_path = f'/tmp/archetype_{archetype_id}.idf'
    idf.save(idf_path)

    # 시뮬레이션 실행
    output_dir = f'/tmp/output_{archetype_id}'
    run_energyplus(idf_path, epw_path, output_dir)

    # 결과 파싱 & DB 저장
    results = parse_results(output_dir)
    update_archetype_results(archetype_id, results)

    return results

@celery.task
def simulate_all_archetypes():
    """전체 원형 배치 시뮬레이션"""
    archetypes = get_all_archetypes()
    group = celery.group([
        simulate_archetype.s(a.id) for a in archetypes
    ])
    group.apply_async()

@celery.task
def assign_energy_to_buildings():
    """모든 건물에 원형 시뮬레이션 결과 할당"""
    # SQL: buildings_enriched의 각 건물 →
    #       (usage_type, vintage_class, size_class) 기반 원형 매칭 →
    #       energy_results 테이블에 INSERT
    pass
```

---

## 4. ML 대리모델 (사용자 추후 대체 예정)

### 4.1 설계 원칙

- 인터페이스를 명확히 정의하여 사용자가 자체 모델로 교체 가능
- 초기 구현: XGBoost (빠른 학습, 해석 가능)
- 학습 데이터: 원형 EnergyPlus 시뮬레이션 결과

### 4.2 인터페이스 정의

```python
# src/simulation/ml_interface.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class BuildingFeatures:
    """ML 모델 입력 피처"""
    usage_type: str          # 용도 (원핫 인코딩)
    total_area: float        # 연면적 (m²)
    height: float            # 높이 (m)
    floors_above: int        # 지상 층수
    built_year: int          # 건축년도
    wall_uvalue: float       # 벽체 U-value (W/m²K)
    roof_uvalue: float       # 지붕 U-value
    window_uvalue: float     # 창호 U-value
    wwr: float               # 창면적비 (0~1)
    occupancy_density: float # 재실밀도 (인/m²)
    lighting_power: float    # 조명밀도 (W/m²)
    equipment_power: float   # 장비밀도 (W/m²)

@dataclass
class EnergyPrediction:
    """ML 모델 출력"""
    heating: float       # 난방 (kWh/m²/yr)
    cooling: float       # 냉방
    hot_water: float     # 급탕
    lighting: float      # 조명
    ventilation: float   # 환기
    total_energy: float  # 합계
    confidence: float    # 신뢰도 (0~1)

class EnergyPredictor(ABC):
    """에너지 예측 모델 인터페이스 - 사용자 교체 가능"""

    @abstractmethod
    def predict(self, features: BuildingFeatures) -> EnergyPrediction:
        """단일 건물 에너지 예측"""
        pass

    @abstractmethod
    def predict_batch(self, features_list: list[BuildingFeatures]) -> list[EnergyPrediction]:
        """배치 건물 에너지 예측"""
        pass

    @abstractmethod
    def load_model(self, model_path: str) -> None:
        """학습된 모델 로드"""
        pass
```

### 4.3 초기 XGBoost 구현

```python
# src/simulation/ml_xgboost.py

import xgboost as xgb
import numpy as np
from .ml_interface import EnergyPredictor, BuildingFeatures, EnergyPrediction

class XGBoostEnergyPredictor(EnergyPredictor):
    """XGBoost 기반 에너지 예측 (사용자 추후 대체)"""

    def __init__(self):
        self.models = {}  # target별 모델 (heating, cooling, ...)

    def train(self, X: np.ndarray, y: dict[str, np.ndarray]):
        """원형 시뮬레이션 결과로 학습"""
        targets = ['heating', 'cooling', 'hot_water', 'lighting', 'ventilation']
        for target in targets:
            self.models[target] = xgb.XGBRegressor(
                n_estimators=200,
                max_depth=6,
                learning_rate=0.1,
                objective='reg:squarederror'
            )
            self.models[target].fit(X, y[target])

    def predict(self, features: BuildingFeatures) -> EnergyPrediction:
        X = self._features_to_array(features)
        results = {}
        for target, model in self.models.items():
            results[target] = float(model.predict(X.reshape(1, -1))[0])
        results['total_energy'] = sum(results.values())
        results['confidence'] = 0.85  # 초기 고정값, 추후 교차검증 기반
        return EnergyPrediction(**results)

    def predict_batch(self, features_list):
        X = np.array([self._features_to_array(f) for f in features_list])
        predictions = []
        for i in range(len(features_list)):
            results = {}
            for target, model in self.models.items():
                results[target] = float(model.predict(X[i:i+1])[0])
            results['total_energy'] = sum(results.values())
            results['confidence'] = 0.85
            predictions.append(EnergyPrediction(**results))
        return predictions

    def _features_to_array(self, f: BuildingFeatures) -> np.ndarray:
        """피처를 numpy 배열로 변환"""
        # 용도 원핫 인코딩은 별도 처리
        return np.array([
            f.total_area, f.height, f.floors_above, f.built_year,
            f.wall_uvalue, f.roof_uvalue, f.window_uvalue, f.wwr,
            f.occupancy_density, f.lighting_power, f.equipment_power
        ])

    def load_model(self, model_path: str):
        import joblib
        self.models = joblib.load(model_path)

    def save_model(self, model_path: str):
        import joblib
        joblib.dump(self.models, model_path)
```

### 4.4 모델 교체 방법 (사용자용)

```python
# 사용자가 자체 모델로 교체 시:
# 1. EnergyPredictor 인터페이스를 상속
# 2. predict(), predict_batch(), load_model() 구현
# 3. config에서 predictor 클래스 지정

# 예시: src/simulation/my_custom_predictor.py
class MyCustomPredictor(EnergyPredictor):
    def predict(self, features):
        # 사용자 커스텀 로직
        ...

# config.py 에서:
ENERGY_PREDICTOR_CLASS = "src.simulation.my_custom_predictor.MyCustomPredictor"
```

---

## 5. 한국 에너지 벤치마크 (검증용)

### 5.1 용도별 에너지 원단위 (kWh/m²/yr)

| 용도 | 중부 지역 | 남부 지역 | 출처 |
|------|----------|----------|------|
| 공동주택 | 136 | 111 | MOCT 2024 |
| 사무 | 159 | 102 | MOCT 2024 |
| 의료 (대학병원) | 789 | - | 2014 기준 |
| 교통시설 (공항) | 1,447 | - | 2014 기준 |
| 수련시설 | 266 | - | 2014 기준 |
| 공공건축물 평균 | 672 | - | 2014 기준 |

**검증 방법**: 마포구 건물의 시뮬레이션 결과 평균을 위 벤치마크와 비교.
- 검증 지표: **CV(RMSE)** (Coefficient of Variation of RMSE) 및 **NMBE** (Normalized Mean Bias Error)
- 목표: CV(RMSE) ≤ 30%, NMBE ≤ ±15% (용도별 집단 평균 기준)
- 주의: 원형 기반 시뮬레이션은 개별 건물 수준에서 35~60% 오차 보고됨. **집단 평균** 정확도에 집중

### 5.2 실측 데이터 교차 검증

```
시뮬레이션 결과 (kWh/m²/yr)
        ↕ 비교
공공건축물 에너지소비량 데이터 (data.go.kr/data/3069931)
        ↕ 비교
서울시 에너지정보 (energyinfo.seoul.go.kr) - 마포구 필터
```

---

## 6. 전체 파이프라인 흐름

```
[Phase 4-1] 원형 정의 (1주)
──────────────────────────
  건축물대장 주용도명 분석 → 용도 10종 확정
  국토부 고시 열관류율 → 건축년도별 U-value 테이블 구축
  building_archetypes 테이블 60~80개 INSERT

[Phase 4-2] 시뮬레이션 실행 (1주)
──────────────────────────────
  서울 EPW 파일 준비
  원형별 Shoebox IDF 생성 (geomeppy)
  EnergyPlus 배치 실행 (Celery parallel)
  결과 파싱 → building_archetypes 테이블 업데이트

[Phase 4-3] 건물 매핑 (0.5주)
──────────────────────────
  buildings_enriched의 각 건물 → 최적 원형 매칭
  원형 에너지 결과 → energy_results 테이블 INSERT
  매칭 품질 검증

[Phase 4-4] ML 대리모델 (1주)
──────────────────────────
  원형 시뮬레이션 데이터로 XGBoost 학습
  교차검증 (R², RMSE)
  모델 저장 + API 엔드포인트 연동

[Phase 4-5] 3D 통합 (0.5주)
──────────────────────────
  energy_results → 3D Tiles 속성에 바인딩
  CesiumJS 에너지 색상 코딩 적용
  건물 클릭 → 에너지 분해 차트 표시
```

---

## 7. 검증 체크리스트

### 7.1 원형 검증

- [ ] 마포구 건물 용도 분포 분석 → 10종 분류 커버리지 95% 이상
- [ ] 건축년도 분포 분석 → 4구간 분류 확인
- [ ] 원형 60~80개 정의 완료

### 7.2 시뮬레이션 검증

- [ ] 서울 EPW 파일 로딩 성공
- [ ] 단일 원형 Shoebox IDF → EnergyPlus 실행 성공
- [ ] 결과 파싱: 난방/냉방/급탕/조명/환기 값 추출
- [ ] 벤치마크 비교: 공동주택 ±30% (136 kWh/m²), 사무 ±30% (159 kWh/m²)
- [ ] 전체 원형 배치 실행 완료 (60~80개, 예상 소요 ~2~6시간)

### 7.3 ML 대리모델 검증

- [ ] XGBoost 학습 데이터: 원형 x 파라미터 변동 = 2,000~5,000 샘플
- [ ] 교차검증 R² ≥ 0.90
- [ ] predict() API 응답 시간 < 100ms

### 7.4 통합 검증

- [ ] 마포구 전 건물 에너지 결과 할당 완료
- [ ] 3D Tiles에 에너지 속성 포함 확인
- [ ] CesiumJS 에너지등급 색상 코딩 정상 표시
- [ ] 건물 클릭 → 에너지 분해 차트 정상 표시
- [ ] 에너지 등급 필터 동작 확인

---

## 8. 필요 사전 작업

| 작업 | 담당 | 기한 |
|------|------|------|
| EnergyPlus 설치 (v24.1+) | 개발 | Phase 4 시작 전 |
| 서울 EPW 파일 확보 (KIAEBS) | 사용자/개발 | Phase 4 시작 전 |
| 국토부 고시 2024-421호 열관류율 수치 확인 | 개발 | Phase 4 시작 전 |
| geomeppy, eppy, openstudio 패키지 설치 | 개발 | Phase 4 시작 전 |
| 공공건축물 에너지소비량 데이터 다운로드 (검증용) | 사용자 | Phase 4-5 |

---

## 9. 리스크 & 대응

| 리스크 | 확률 | 영향 | 대응 |
|--------|------|------|------|
| 외피 U-value 추정 오차 (건축년도 기반 한계) | 높 | 중 | 실측 데이터와 지속 교정, 원형 세분화 |
| HVAC 시스템 추정 오류 | 높 | 중 | IdealLoads로 HVAC 영향 분리, 추후 세분화 |
| Shoebox 모델 정확도 한계 (±20%) | 중 | 중 | 도시 스케일에서는 수용 가능, 개별 건물 정밀 분석은 상세 모델 별도 |
| ML 대리모델 과적합 | 중 | 중 | 교차검증, 정규화, 조기 중단 |
| EPW 기상파일 접근 불가 | 낮 | 상 | climate.onebuilding.org 대안 |
