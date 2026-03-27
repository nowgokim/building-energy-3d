import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import type { TimeseriesResponse } from "../../types/monitor";

const SERIES_COLORS = ["#60a5fa", "#34d399", "#f472b6"];

interface Dataset {
  ts_id: number;
  label: string;
  data: TimeseriesResponse;
}

interface Props {
  datasets: Dataset[];
}

export default function MultiLineChart({ datasets }: Props) {
  if (datasets.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-gray-500 text-sm gap-2">
        <span className="text-2xl opacity-30" aria-hidden="true">📊</span>
        <span>좌측 목록에서 건물을 선택하면 차트가 표시됩니다</span>
        <span className="text-xs text-gray-600">최대 3개 건물 비교 가능</span>
      </div>
    );
  }

  // 날짜(YYYY-MM-DD)별로 데이터 병합.
  // 백엔드 TimeseriesPoint는 { ts: ISO8601, value: number|null, unit: string }.
  // ts를 날짜 부분(앞 10자)으로 normalize하여 건물 간 날짜를 정렬 기준으로 사용한다.
  const dateMap = new Map<string, Record<string, number | null | string>>();
  for (const { ts_id, data } of datasets) {
    for (const point of data.points) {
      const dateKey = String(point.ts).slice(0, 10);
      if (!dateMap.has(dateKey)) {
        dateMap.set(dateKey, { date: dateKey });
      }
      const entry = dateMap.get(dateKey)!;
      entry[String(ts_id)] = point.value;
    }
  }
  const chartData = Array.from(dateMap.values()).sort((a, b) =>
    String(a.date).localeCompare(String(b.date))
  );

  // 단위는 첫 번째 데이터셋의 첫 포인트에서 가져온다 (없으면 "kWh" 기본값)
  const unit = datasets[0]?.data.points[0]?.unit ?? "kWh";

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={chartData} margin={{ top: 8, right: 24, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
        <XAxis
          dataKey="date"
          tick={{ fill: "#9ca3af", fontSize: 11 }}
          tickFormatter={(v: unknown) => String(v).slice(5)}
        />
        <YAxis
          tick={{ fill: "#9ca3af", fontSize: 11 }}
          unit={` ${unit}`}
          width={70}
        />
        <Tooltip
          contentStyle={{
            background: "#1f2937",
            border: "1px solid #374151",
            borderRadius: 6,
          }}
          labelStyle={{ color: "#e5e7eb", marginBottom: 4 }}
          itemStyle={{ color: "#d1d5db" }}
          formatter={(value: unknown) => {
            const n = typeof value === "number" ? value : Number(value);
            return [`${isNaN(n) ? "-" : n.toFixed(1)} ${unit}`, ""];
          }}
        />
        <Legend
          wrapperStyle={{ fontSize: 12, color: "#9ca3af" }}
        />
        {datasets.map(({ ts_id, label }, i) => (
          <Line
            key={ts_id}
            type="monotone"
            dataKey={String(ts_id)}
            name={label}
            stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
            strokeWidth={2}
            dot={false}
            connectNulls
            activeDot={{ r: 4 }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
