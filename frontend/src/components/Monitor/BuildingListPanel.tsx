import { useEffect, useMemo } from "react";
import { useMonitorStore } from "../../store/monitorStore";
import BuildingListItem from "./BuildingListItem";
import MonitorFilterBar from "./MonitorFilterBar";

export default function BuildingListPanel() {
  const { buildings, isLoading, error, filters, selectedIds, clearSelected } =
    useMonitorStore();

  // 필터 변경으로 건물 목록이 바뀌면 현재 selectedIds 중 존재하지 않는 항목을 정리한다.
  // 이렇게 하지 않으면 필터 후 차트/미니맵에 이미 사라진 건물의 데이터가 유령처럼 남는다.
  useEffect(() => {
    if (selectedIds.length === 0) return;
    const buildingIds = new Set(buildings.map((b) => b.ts_id));
    const allPresent = selectedIds.every((id) => buildingIds.has(id));
    if (!allPresent) {
      clearSelected();
    }
  }, [buildings, selectedIds, clearSelected]);

  const filtered = useMemo(() => {
    return buildings.filter((b) => {
      // usageType 필터는 서버에서 처리됨. 클라이언트에서는 meterType + search만 적용.
      if (filters.meterType !== "all") {
        if (
          !b.meter_types.includes(
            filters.meterType as "electricity" | "gas" | "district_heating" | "heat" | "water"
          )
        )
          return false;
      }
      if (filters.search) {
        const q = filters.search.toLowerCase();
        const name = (b.alias ?? String(b.ts_id)).toLowerCase();
        const addr = (b.usage_type ?? "").toLowerCase();
        if (!name.includes(q) && !addr.includes(q)) return false;
      }
      return true;
    });
  }, [buildings, filters]);

  return (
    <div className="flex flex-col h-full">
      <MonitorFilterBar />

      {/* 요약 바 */}
      <div className="flex items-center justify-between px-4 py-2 text-xs text-gray-400 border-b border-gray-800 shrink-0">
        <span>{filtered.length}개 건물</span>
        {selectedIds.length > 0 && (
          <span className="text-blue-400">{selectedIds.length}개 선택</span>
        )}
      </div>

      {/* 목록 */}
      <div className="flex-1 overflow-y-auto" role="list" aria-label="건물 목록">
        {isLoading && buildings.length === 0 && (
          <div
            className="flex items-center justify-center h-32 text-gray-500 text-sm"
            role="status"
            aria-live="polite"
          >
            불러오는 중...
          </div>
        )}
        {error && (
          <div className="px-4 py-3 text-red-400 text-sm" role="alert">
            {error}
          </div>
        )}
        {!isLoading && !error && filtered.length === 0 && (
          <div className="flex items-center justify-center h-32 text-gray-500 text-sm">
            조건에 맞는 건물이 없습니다
          </div>
        )}
        {filtered.map((b, i) => (
          <BuildingListItem key={b.ts_id} index={i} building={b} />
        ))}
      </div>
    </div>
  );
}
