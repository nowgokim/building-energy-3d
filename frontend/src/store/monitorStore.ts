import { create } from "zustand";
import type {
  MonitorBuilding,
  TimeseriesResponse,
  Period,
  MeterFilter,
  UsageFilter,
} from "../types/monitor";

const MAX_COMPARE = 3;

interface MonitorFilters {
  usageType: UsageFilter;
  meterType: MeterFilter;
  search: string;
}

interface MonitorState {
  buildings: MonitorBuilding[];
  isLoading: boolean;
  error: string | null;
  // 비교 선택 건물 ts_id 목록 (최대 3개)
  selectedIds: number[];
  period: Period;
  filters: MonitorFilters;
  // 시계열 캐시: `${ts_id}_${period}`
  timeseriesCache: Record<string, TimeseriesResponse>;

  setBuildings: (buildings: MonitorBuilding[]) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  toggleSelected: (ts_id: number) => void;
  clearSelected: () => void;
  setPeriod: (p: Period) => void;
  setFilters: (f: Partial<MonitorFilters>) => void;
  cacheTimeseries: (key: string, data: TimeseriesResponse) => void;
}

export const useMonitorStore = create<MonitorState>((set) => ({
  buildings: [],
  isLoading: false,
  error: null,
  selectedIds: [],
  period: "30d",
  filters: { usageType: "all", meterType: "all", search: "" },
  timeseriesCache: {},

  setBuildings: (buildings) => set({ buildings }),
  setLoading: (v) => set({ isLoading: v }),
  setError: (e) => set({ error: e }),

  toggleSelected: (ts_id) =>
    set((state) => {
      const exists = state.selectedIds.includes(ts_id);
      if (exists) {
        return { selectedIds: state.selectedIds.filter((id) => id !== ts_id) };
      }
      if (state.selectedIds.length >= MAX_COMPARE) {
        // 가장 오래된 선택 제거 후 새 항목 추가
        return { selectedIds: [...state.selectedIds.slice(1), ts_id] };
      }
      return { selectedIds: [...state.selectedIds, ts_id] };
    }),

  clearSelected: () => set({ selectedIds: [] }),
  setPeriod: (period) => set({ period }),
  setFilters: (f) =>
    set((state) => ({ filters: { ...state.filters, ...f } })),
  cacheTimeseries: (key, data) =>
    set((state) => ({
      timeseriesCache: { ...state.timeseriesCache, [key]: data },
    })),
}));
