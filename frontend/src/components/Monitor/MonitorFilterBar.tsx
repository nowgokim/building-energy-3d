import { useMonitorStore } from "../../store/monitorStore";
import type { MeterFilter } from "../../types/monitor";

const USAGE_OPTIONS = [
  { value: "all", label: "전체 용도" },
  { value: "공동주택", label: "공동주택" },
  { value: "업무시설", label: "업무시설" },
  { value: "교육연구시설", label: "교육" },
  { value: "의료시설", label: "의료" },
  { value: "판매시설", label: "판매" },
];

const METER_OPTIONS: { value: MeterFilter; label: string }[] = [
  { value: "all", label: "전체 계량" },
  { value: "electricity", label: "⚡ 전력" },
  { value: "gas", label: "🔥 가스" },
  { value: "district_heating", label: "🏙️ 지역난방" },
];

export default function MonitorFilterBar() {
  const { filters, setFilters } = useMonitorStore();

  return (
    <div className="px-4 py-3 border-b border-gray-800 space-y-2">
      <input
        type="search"
        placeholder="건물명 또는 주소 검색..."
        value={filters.search}
        onChange={(e) => setFilters({ search: e.target.value })}
        className="w-full bg-gray-800 text-gray-200 text-sm rounded px-3 py-1.5 placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
      <div className="flex gap-2">
        <select
          value={filters.usageType}
          onChange={(e) => setFilters({ usageType: e.target.value })}
          className="flex-1 bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          {USAGE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          value={filters.meterType}
          onChange={(e) => setFilters({ meterType: e.target.value as MeterFilter })}
          className="flex-1 bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          {METER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
