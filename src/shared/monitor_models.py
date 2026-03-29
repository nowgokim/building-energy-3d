"""
모니터링 API Pydantic 스키마.

요청/응답 모델을 src/shared/monitor_models.py에 독립적으로 정의한다.
src/shared/models.py가 없는 현재 구조에서 기존 코드(buildings.py, risk.py)는
라우터 내부에 BaseModel을 직접 정의하므로, 모니터링 모델만 별도 파일로 관리한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# 건물 목록
# ─────────────────────────────────────────────────────────────────────────────


class MonitorBuildingListItem(BaseModel):
    ts_id: int = Field(..., description="시계열 건물 고유 ID (monitored_buildings.ts_id)")
    # pnu는 DB에서 nullable (monitored_buildings.pnu VARCHAR without NOT NULL).
    # PNU 없이 등록된 건물(alias만 입력된 경우)을 허용해야 하므로 Optional로 선언.
    pnu: Optional[str] = Field(None, description="건물 PNU (19자리 표준 지번코드)")
    alias: Optional[str] = Field(None, description="건물 별칭 (예: 'A동 관리동')")
    meter_types: list[str] = Field(default_factory=list, description="설치 계량기 종류 목록")
    total_area: Optional[float] = Field(None, description="연면적 (m²)")
    usage_type: Optional[str] = Field(None, description="주용도")
    built_year: Optional[int] = Field(None, description="준공연도")
    eui_kwh_m2: Optional[float] = Field(None, description="에너지사용원단위 (kWh/m²·yr, 최근 365일)")
    lng: Optional[float] = Field(None, description="경도 (WGS84)")
    lat: Optional[float] = Field(None, description="위도 (WGS84)")


class MonitorBuildingListResponse(BaseModel):
    count: int
    buildings: list[MonitorBuildingListItem]


# ─────────────────────────────────────────────────────────────────────────────
# 건물 상세 (메타 + 최근 30일 일별 집계)
# ─────────────────────────────────────────────────────────────────────────────


class DailyAggregatePoint(BaseModel):
    day: str = Field(..., description="날짜 (YYYY-MM-DD)")
    value: float = Field(..., description="일별 합산 소비량")
    unit: str = Field(..., description="단위 (kWh, m3 등)")


class MonitorBuildingDetail(BaseModel):
    ts_id: int
    # pnu는 DB에서 nullable — MonitorBuildingListItem과 동일한 이유로 Optional.
    pnu: Optional[str] = None
    alias: Optional[str] = None
    meter_types: list[str] = Field(default_factory=list)
    total_area: Optional[float] = None
    usage_type: Optional[str] = None
    built_year: Optional[int] = None
    data_source: Optional[str] = Field(None, description="데이터 출처 (KEPCO_AMI, 가스공사, 수동 등)")
    created_at: Optional[str] = None
    lng: Optional[float] = None
    lat: Optional[float] = None
    # meter_type → 일별 포인트 목록
    daily_30d: dict[str, list[DailyAggregatePoint]] = Field(
        default_factory=dict,
        description="최근 30일 일별 집계 {meter_type: [{day, value, unit}]}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 시계열 조회
# ─────────────────────────────────────────────────────────────────────────────


class TimeseriesPoint(BaseModel):
    ts: str = Field(..., description="타임스탬프 (ISO 8601)")
    value: Optional[float] = Field(None, description="계량값 (단위는 unit 참조)")
    unit: str = Field(..., description="계량 단위 (kWh, m3, MJ 등)")


class TimeseriesResponse(BaseModel):
    ts_id: int
    meter: str
    resolution: str = Field(..., description="raw | hourly | daily | weekly")
    start: str
    end: str
    count: int
    points: list[TimeseriesPoint]


# ─────────────────────────────────────────────────────────────────────────────
# 다건 비교
# ─────────────────────────────────────────────────────────────────────────────


class CompareBuildingItem(BaseModel):
    ts_id: int
    alias: Optional[str] = None
    pnu: Optional[str] = None
    value: Optional[float] = Field(None, description="집계 지표값")
    unit: str = Field(..., description="kWh/m² (EUI) 또는 kWh (소비량)")


class MonitorCompareResponse(BaseModel):
    metric: str
    period: str
    start: str
    end: str
    buildings: list[CompareBuildingItem]


# ─────────────────────────────────────────────────────────────────────────────
# 이상치
# ─────────────────────────────────────────────────────────────────────────────


class AnomalyItem(BaseModel):
    ts_id: int
    alias: Optional[str] = None
    pnu: Optional[str] = None
    meter_type: str
    detected_at: Optional[str] = None
    window_mean: Optional[float] = Field(None, description="rolling window 평균 (24h)")
    window_std: Optional[float] = Field(None, description="rolling window 표준편차 (24h)")
    offending_value: Optional[float] = Field(None, description="이상 값")
    z_score: Optional[float] = Field(None, description="z-score (|z| > 2 = 이상치)")


class MonitorAnomalyResponse(BaseModel):
    count: int
    anomalies: list[AnomalyItem]


# ─────────────────────────────────────────────────────────────────────────────
# CSV 업로드
# ─────────────────────────────────────────────────────────────────────────────


class ReadingUploadResponse(BaseModel):
    ts_id: int
    meter: str
    rows_parsed: int = Field(..., description="CSV에서 읽은 전체 행 수")
    rows_inserted: int = Field(..., description="실제 DB에 삽입된 행 수")
    parse_errors: int = Field(..., description="파싱 실패 행 수")
    skipped_duplicates: int = Field(..., description="중복으로 무시된 행 수")


# ─────────────────────────────────────────────────────────────────────────────
# DB 테이블 스키마 참조 문서 (주석)
# ─────────────────────────────────────────────────────────────────────────────
#
# monitored_buildings
#   ts_id        SERIAL PRIMARY KEY
#   pnu          VARCHAR(19)   -- buildings_enriched.pnu FK (nullable)
#   alias        VARCHAR(200)
#   meter_types  TEXT[]        -- {'electricity','gas','heat','water'}
#   total_area   REAL
#   usage_type   VARCHAR(100)
#   built_year   INTEGER
#   data_source  VARCHAR(100)  -- 'KEPCO_AMI' | '가스공사' | '수동'
#   created_at   TIMESTAMP DEFAULT NOW()
#
# metered_readings  (LIST 파티션: recorded_at 기준 월별)
#   id           BIGSERIAL
#   ts_id        INTEGER REFERENCES monitored_buildings(ts_id)
#   meter_type   VARCHAR(20)   -- electricity | gas | heat | water
#   recorded_at  TIMESTAMP NOT NULL
#   value        REAL NOT NULL
#   unit         VARCHAR(10)   -- kWh | m3 | MJ
#   UNIQUE (ts_id, meter_type, recorded_at)
#   INDEX: (ts_id, meter_type, recorded_at) -- 시계열 쿼리 복합 인덱스
#
# anomaly_log
#   id              BIGSERIAL PRIMARY KEY
#   ts_id           INTEGER REFERENCES monitored_buildings(ts_id)
#   meter_type      VARCHAR(20)
#   detected_at     TIMESTAMP NOT NULL
#   window_mean     REAL
#   window_std      REAL
#   offending_value REAL
#   z_score         REAL
#   INDEX: (detected_at DESC), (ts_id, detected_at DESC)
