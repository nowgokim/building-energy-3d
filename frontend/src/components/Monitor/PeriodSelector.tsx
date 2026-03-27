import type { Period } from "../../types/monitor";

const OPTIONS: { value: Period; label: string }[] = [
  { value: "7d", label: "7일" },
  { value: "30d", label: "30일" },
  { value: "1y", label: "1년" },
];

interface Props {
  value: Period;
  onChange: (p: Period) => void;
}

export default function PeriodSelector({ value, onChange }: Props) {
  return (
    <div className="flex rounded overflow-hidden border border-gray-700">
      {OPTIONS.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
          className={[
            "px-3 py-1 text-xs transition-colors",
            value === o.value
              ? "bg-blue-700 text-white"
              : "bg-gray-800 text-gray-400 hover:bg-gray-700",
          ].join(" ")}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
