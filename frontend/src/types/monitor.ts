export type MeterType = "electricity" | "gas" | "district_heating" | "heat" | "water";

// ── 건물 목록 항목 (GET /api/v1/monitor/buildings) ────────────────────────────
// 백엔드 MonitorBuildingListItem 필드와 1:1 일치.
export interface MonitorBuilding {
  ts_id: number;
  pnu: string;
  /** 건물 별칭 (alias). 미설정 시 null. UI에서 ts_id 문자열로 폴백. */
  alias: string | null;
  meter_types: MeterType[];
  total_area: number | null;
  usage_type: string | null;
  built_year: number | null;
  /** 최근 365일 EUI (kWh/m²·yr). 데이터 부족 시 null. */
  eui_kwh_m2: number | null;
  lat: number | null;
  lng: number | null;
}

// ── 건물 목록 응답 (GET /api/v1/monitor/buildings) ───────────────────────────
export interface MonitorBuildingListResponse {
  /** 백엔드는 total이 아닌 count를 반환한다. */
  count: number;
  buildings: MonitorBuilding[];
}

// ── 시계열 포인트 (GET /api/v1/monitor/timeseries/{ts_id}) ───────────────────
// 백엔드 TimeseriesPoint: { ts, value, unit }
export interface TimeseriesPoint {
  ts: string;
  value: number | null;
  unit: string;
}

// ── 시계열 응답 ──────────────────────────────────────────────────────────────
// 백엔드 TimeseriesResponse: { ts_id, meter, resolution, start, end, count, points }
export interface TimeseriesResponse {
  ts_id: number;
  meter: string;
  resolution: string;
  start: string;
  end: string;
  count: number;
  points: TimeseriesPoint[];
}

// ── 모니터 요약 (GET /api/v1/monitor/summary) ────────────────────────────────
export interface MonitorSummary {
  total_monitored: number;
  anomaly_count: number;
  avg_eui: number;
}

// ── 기간 선택기 ──────────────────────────────────────────────────────────────
// "1y" → 365일. getTimeseries()에서 start/end 날짜로 변환한다.
export type Period = "7d" | "30d" | "1y";

export type UsageFilter = "all" | string;
export type MeterFilter = "all" | MeterType;
