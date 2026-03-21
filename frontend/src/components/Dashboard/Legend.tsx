import { getEnergyColor } from "../../utils/energyGradeColors";

const STEPS = [50, 80, 110, 140, 170, 200, 250, 300];

export default function Legend() {
  return (
    <div className="absolute bottom-4 right-4 z-10 bg-white/90 backdrop-blur rounded-lg shadow-lg px-3 py-2">
      <div className="text-[10px] text-gray-500 mb-1 font-semibold">
        에너지 소비량 (kWh/m²/yr)
      </div>
      <div className="flex gap-0.5">
        {STEPS.map((v) => (
          <div key={v} className="text-center">
            <div
              className="w-5 h-3 rounded-sm"
              style={{ backgroundColor: getEnergyColor(v) }}
            />
            <div className="text-[8px] text-gray-600 mt-0.5">{v}</div>
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[8px] text-gray-400 mt-0.5">
        <span>효율적</span>
        <span>비효율적</span>
      </div>
    </div>
  );
}
