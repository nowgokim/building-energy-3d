import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import type { EnergyBreakdown as EnergyData } from "../../types/building";

const CATEGORY_COLORS: Record<string, string> = {
  난방: "#ef4444",
  냉방: "#3b82f6",
  급탕: "#f59e0b",
  조명: "#a855f7",
  환기: "#22c55e",
};

interface Props {
  energy: EnergyData;
}

export default function EnergyBreakdown({ energy }: Props) {
  const data = [
    { name: "난방", value: energy.heating ?? 0 },
    { name: "냉방", value: energy.cooling ?? 0 },
    { name: "급탕", value: energy.hot_water ?? 0 },
    { name: "조명", value: energy.lighting ?? 0 },
    { name: "환기", value: energy.ventilation ?? 0 },
  ];

  return (
    <div>
      <div className="text-xs text-gray-600 mb-1">
        총 {energy.total_energy?.toFixed(1) ?? "-"} kWh/m²/yr
      </div>
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={data} layout="vertical" margin={{ left: 4, right: 8 }}>
          <XAxis type="number" tick={{ fontSize: 10 }} />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fontSize: 11 }}
            width={32}
          />
          <Tooltip
            formatter={(v: number) => [`${v.toFixed(1)} kWh/m²`, ""]}
            contentStyle={{ fontSize: 12 }}
          />
          <Bar dataKey="value" radius={[0, 4, 4, 0]}>
            {data.map((d) => (
              <Cell key={d.name} fill={CATEGORY_COLORS[d.name] ?? "#ccc"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
