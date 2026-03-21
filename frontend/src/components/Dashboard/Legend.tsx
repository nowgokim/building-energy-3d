import { ENERGY_GRADE_COLORS } from "../../utils/energyGradeColors";

const LEGEND_GRADES = ["1+", "1", "2", "3", "4", "5"];

export default function Legend() {
  return (
    <div className="absolute bottom-4 right-4 z-10 bg-white/90 backdrop-blur rounded-lg shadow-lg px-3 py-2">
      <div className="text-[10px] text-gray-500 mb-1 font-semibold">
        에너지등급
      </div>
      <div className="flex gap-1">
        {LEGEND_GRADES.map((g) => (
          <div key={g} className="text-center">
            <div
              className="w-5 h-3 rounded-sm"
              style={{ backgroundColor: ENERGY_GRADE_COLORS[g] }}
            />
            <div className="text-[9px] text-gray-600 mt-0.5">{g}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
