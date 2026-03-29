import { API_BASE_URL } from "../utils/constants";
import { fetchJSON } from "./client";
import type {
  MonitorBuilding,
  MonitorBuildingListResponse,
  TimeseriesResponse,
  MonitorSummary,
  Period,
  MeterFilter,
} from "../types/monitor";

function buildUrl(base: string, params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) sp.set(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `${base}?${qs}` : base;
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
  const url = buildUrl(`${API_BASE_URL}/monitor/buildings`, {
    usage_type: params.usageType && params.usageType !== "all" ? params.usageType : undefined,
    meter_type: params.meterType && params.meterType !== "all" ? params.meterType : undefined,
    q: params.search || undefined,
    page: params.page ?? 1,
    limit: params.limit ?? 100,
  });
  return fetchJSON<MonitorBuildingListResponse>(url, undefined, signal);
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
    undefined,
    signal
  );
}

export async function getMonitorSummary(signal?: AbortSignal): Promise<MonitorSummary> {
  return fetchJSON<MonitorSummary>(`${API_BASE_URL}/monitor/summary`, undefined, signal);
}
