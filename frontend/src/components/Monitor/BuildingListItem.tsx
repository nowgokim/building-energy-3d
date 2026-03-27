import { useMonitorStore } from "../../store/monitorStore";
import type { MonitorBuilding, MeterType } from "../../types/monitor";

const METER_ICONS: Record<MeterType, string> = {
  electricity: "⚡",
  gas: "🔥",
  district_heating: "🏙️",
  heat: "♨️",
  water: "💧",
};

const METER_LABELS: Record<MeterType, string> = {
  electricity: "전력",
  gas: "가스",
  district_heating: "지역난방",
  heat: "열",
  water: "수도",
};

interface Props {
  index: number;
  building: MonitorBuilding;
}

export default function BuildingListItem({ index, building }: Props) {
  const { selectedIds, toggleSelected } = useMonitorStore();
  const isSelected = selectedIds.includes(building.ts_id);

  const displayName = building.alias ?? `건물 #${building.ts_id}`;
  const meterLabel = building.meter_types
    .map((m) => METER_LABELS[m] ?? m)
    .join(", ");

  return (
    <button
      type="button"
      onClick={() => toggleSelected(building.ts_id)}
      aria-pressed={isSelected}
      aria-label={`${displayName}, ${building.usage_type ?? "용도 미상"}, ${meterLabel}`}
      role="listitem"
      className={[
        "w-full text-left px-4 py-3 border-b border-gray-800",
        "hover:bg-gray-800 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500",
        isSelected ? "bg-blue-950 border-l-2 border-l-blue-500" : "",
      ].join(" ")}
    >
      <div className="flex items-start gap-2">
        <span className="text-xs text-gray-500 mt-0.5 shrink-0 w-6 text-right" aria-hidden="true">
          #{index + 1}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-sm font-medium text-gray-100 truncate">
              {displayName}
            </span>
          </div>
          <p className="text-xs text-gray-500 truncate mt-0.5">
            {building.usage_type ?? "용도 미상"}
            {building.built_year != null && ` · ${building.built_year}년`}
          </p>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-xs text-gray-400" aria-hidden="true">
              {building.meter_types.map((m) => METER_ICONS[m] ?? m).join(" ")}
            </span>
            {building.eui_kwh_m2 != null && (
              <span className="text-xs font-mono text-emerald-400">
                {building.eui_kwh_m2.toFixed(0)} kWh/m²
              </span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}
