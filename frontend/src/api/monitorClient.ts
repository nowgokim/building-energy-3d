import { API_BASE_URL } from "../utils/constants";
import type {
  MonitorBuilding,
  MonitorBuildingListResponse,
  TimeseriesResponse,
  MonitorSummary,
  Period,
  MeterFilter,
} from "../types/monitor";

async function fetchJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(url, { signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new Error("서버에 연결할 수 없습니다");
  }
  if (!resp.ok) {
    throw new Error(
      resp.status === 404 ? "데이터를 찾을 수 없습니다" : `서버 오류 (${resp.status})`
    );
  }
  return resp.json();
}

export interface BuildingListParams {
  usageType?: string;
  meterType?: MeterFilter;
  search?: string;
  page?: number;
  limit?: number;
}

export async function getMonitorBuildings(
  params: BuildingListParams = {},
  signal?: AbortSignal
): Promise<{ count: number; buildings: MonitorBuilding[] }> {
  const sp = new URLSearchParams();
  if (params.usageType && params.usageType !== "all") sp.set("usage_type", params.usageType);
  if (params.meterType && params.meterType !== "all") sp.set("meter_type", params.meterType);
  if (params.search) sp.set("q", params.search);
  sp.set("page", String(params.page ?? 1));
  sp.set("limit", String(params.limit ?? 100));
  return fetchJSON<MonitorBuildingListResponse>(
    `${API_BASE_URL}/monitor/buildings?${sp}`,
    signal
  );
}

/**
 * Period 문자열("7d", "30d", "1y")을 start/end 날짜 문자열로 변환한다.
 * 백엔드 /timeseries/{ts_id} 는 period 파라미터를 받지 않으므로
 * 클라이언트에서 현재 날짜 기준으로 직접 계산한다.
 */
function periodToDateRange(period: Period): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end);
  if (period === "7d") {
    start.setDate(end.getDate() - 7);
  } else if (period === "30d") {
    start.setDate(end.getDate() - 30);
  } else {
    // "1y" → 365일
    start.setDate(end.getDate() - 365);
  }
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  return { start: fmt(start), end: fmt(end) };
}

export async function getTimeseries(
  ts_id: number,
  period: Period,
  signal?: AbortSignal,
  meter = "electricity"
): Promise<TimeseriesResponse> {
  const { start, end } = periodToDateRange(period);
  return fetchJSON<TimeseriesResponse>(
    `${API_BASE_URL}/monitor/timeseries/${ts_id}?start=${start}&end=${end}&resolution=daily&meter=${meter}`,
    signal
  );
}

export async function getMonitorSummary(signal?: AbortSignal): Promise<MonitorSummary> {
  return fetchJSON<MonitorSummary>(`${API_BASE_URL}/monitor/summary`, signal);
}
