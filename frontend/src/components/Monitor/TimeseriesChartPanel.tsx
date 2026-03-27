import { useEffect, useRef } from "react";
import { useMonitorStore } from "../../store/monitorStore";
import { getTimeseries } from "../../api/monitorClient";
import MultiLineChart from "./MultiLineChart";
import PeriodSelector from "./PeriodSelector";
import type { Period } from "../../types/monitor";

export default function TimeseriesChartPanel() {
  const {
    buildings,
    selectedIds,
    period,
    timeseriesCache,
    setPeriod,
    cacheTimeseries,
  } = useMonitorStore();

  // AbortController를 useRef로 관리 — 언마운트 또는 deps 변경 시 진행 중인 요청 취소
  const abortRef = useRef<AbortController | null>(null);

  // 선택된 건물의 시계열 데이터 로드 (캐시 미스 시)
  // timeseriesCache를 deps에 포함하면 fetch 완료 때마다 effect 재실행 → 불필요.
  // 캐시 히트 여부는 effect 내부에서 최신 값으로 직접 확인한다.
  useEffect(() => {
    // 이전 요청 일괄 취소
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const signal = abortRef.current.signal;

    // Zustand 스토어에서 최신 캐시 스냅샷을 직접 가져와 캐시 히트 여부 판단
    const currentCache = useMonitorStore.getState().timeseriesCache;

    for (const ts_id of selectedIds) {
      const key = `${ts_id}_${period}`;
      if (currentCache[key]) continue;
      getTimeseries(ts_id, period, signal)
        .then((data) => cacheTimeseries(key, data))
        .catch((e: unknown) => {
          if ((e as Error).name !== "AbortError") {
            console.error("시계열 로드 실패:", e);
          }
        });
    }

    return () => {
      abortRef.current?.abort();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds, period, cacheTimeseries]);

  // 언마운트 시 진행 중 요청 취소
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  const datasets = selectedIds
    .map((ts_id) => {
      const key = `${ts_id}_${period}`;
      const data = timeseriesCache[key];
      if (!data) return null;
      const b = buildings.find((b) => b.ts_id === ts_id);
      return { ts_id, label: b?.alias ?? `건물 #${ts_id}`, data };
    })
    .filter((d): d is NonNullable<typeof d> => d !== null);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-3 shrink-0">
        <h2 className="text-sm font-semibold text-gray-200">에너지 소비 시계열</h2>
        <div className="flex items-center gap-3">
          {selectedIds.length > 1 && (
            <span className="text-xs text-gray-500">
              {selectedIds.length}개 건물 비교
            </span>
          )}
          <PeriodSelector value={period} onChange={(p: Period) => setPeriod(p)} />
        </div>
      </div>
      <div className="flex-1 min-h-0">
        <MultiLineChart datasets={datasets} />
      </div>
    </div>
  );
}
