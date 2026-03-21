import { useEffect } from "react";
import { useAppStore } from "../../store/appStore";
import { getStats } from "../../api/client";
import { ENERGY_GRADE_COLORS } from "../../utils/energyGradeColors";

export default function StatsBar() {
  const stats = useAppStore((s) => s.stats);
  const setStats = useAppStore((s) => s.setStats);

  useEffect(() => {
    getStats().then(setStats).catch(console.error);
  }, [setStats]);

  if (!stats) return null;

  const gradeCounts = Object.entries(stats.grade_distribution).filter(
    ([k]) => k !== "unknown"
  );
  const totalGraded = gradeCounts.reduce((s, [, v]) => s + v, 0);

  return (
    <div className="absolute bottom-4 left-4 z-10 bg-white/90 backdrop-blur rounded-lg shadow-lg px-4 py-3 flex items-center gap-6 text-sm">
      <div>
        <span className="text-gray-500">건물 </span>
        <span className="font-bold text-gray-800">
          {stats.total_count.toLocaleString()}
        </span>
      </div>
      <div>
        <span className="text-gray-500">평균 에너지 </span>
        <span className="font-bold text-gray-800">
          {stats.avg_energy ? `${stats.avg_energy.toFixed(0)} kWh/m²` : "-"}
        </span>
      </div>
      {totalGraded > 0 && (
        <div className="flex items-center gap-1">
          <span className="text-gray-500 mr-1">등급</span>
          {gradeCounts.map(([grade, count]) => (
            <span
              key={grade}
              className="inline-block px-1.5 py-0.5 rounded text-[10px] font-bold text-white"
              style={{
                backgroundColor:
                  ENERGY_GRADE_COLORS[grade] ?? ENERGY_GRADE_COLORS.unknown,
              }}
              title={`${grade}등급: ${count}건`}
            >
              {grade}:{count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
