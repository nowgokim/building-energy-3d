import { useAppStore } from "../../store/appStore";
import { getGradeColor } from "../../utils/energyGradeColors";
import EnergyBreakdown from "./EnergyBreakdown";

export default function BuildingDetailPanel() {
  const building = useAppStore((s) => s.selectedBuilding);
  const isPanelOpen = useAppStore((s) => s.isPanelOpen);
  const clearSelection = useAppStore((s) => s.clearSelection);

  if (!isPanelOpen || !building) return null;

  const p = building.properties;
  const grade = p.energy_grade?.trim() || "unknown";
  const gradeColor = getGradeColor(grade);

  return (
    <div className="absolute top-0 right-0 z-20 w-80 h-full bg-white/95 backdrop-blur shadow-2xl overflow-y-auto">
      {/* Header */}
      <div className="sticky top-0 bg-white/95 backdrop-blur px-4 py-3 border-b border-gray-200 flex items-center justify-between">
        <h2 className="text-base font-bold text-gray-800 truncate flex-1 mr-2">
          {p.building_name || "건물 정보"}
        </h2>
        <button
          onClick={clearSelection}
          className="text-gray-400 hover:text-gray-600 text-xl leading-none"
        >
          ×
        </button>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-3 gap-2 p-4">
        <div className="text-center p-2 rounded-lg bg-gray-50">
          <div
            className="text-lg font-bold"
            style={{ color: gradeColor }}
          >
            {grade === "unknown" ? "-" : grade}
          </div>
          <div className="text-[10px] text-gray-500">에너지등급</div>
        </div>
        <div className="text-center p-2 rounded-lg bg-gray-50">
          <div className="text-lg font-bold text-gray-700">
            {p.total_energy ? Math.round(p.total_energy) : "-"}
          </div>
          <div className="text-[10px] text-gray-500">kWh/m²</div>
        </div>
        <div className="text-center p-2 rounded-lg bg-gray-50">
          <div className="text-lg font-bold text-gray-700">
            {p.total_area != null ? p.total_area.toLocaleString() : "-"}
          </div>
          <div className="text-[10px] text-gray-500">m² 연면적</div>
        </div>
      </div>

      {/* Basic Info */}
      <div className="px-4 pb-3">
        <h3 className="text-xs font-semibold text-gray-500 mb-2">기본 정보</h3>
        <dl className="text-sm space-y-1">
          <InfoRow label="PNU" value={p.pnu} />
          <InfoRow label="용도" value={p.usage_type} />
          <InfoRow label="구조" value={p.structure_type} />
          <InfoRow
            label="층수"
            value={
              p.floors_above
                ? `지상 ${p.floors_above}층 / 지하 ${p.floors_below}층`
                : null
            }
          />
          <InfoRow
            label="높이"
            value={p.height ? `${p.height.toFixed(1)}m` : null}
          />
          <InfoRow label="건축년도" value={p.built_year?.toString()} />
          <InfoRow label="연대 분류" value={p.vintage_class} />
        </dl>
      </div>

      {/* Energy Breakdown */}
      {p.energy && (
        <div className="px-4 pb-4">
          <h3 className="text-xs font-semibold text-gray-500 mb-2">
            에너지 소비 분해
          </h3>
          <EnergyBreakdown energy={p.energy} />
        </div>
      )}
    </div>
  );
}

function InfoRow({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  return (
    <div className="flex justify-between">
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-gray-800 font-medium">{value || "-"}</dd>
    </div>
  );
}
