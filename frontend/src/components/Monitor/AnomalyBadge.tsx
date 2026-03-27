// AnomalyBadge — 이상치 경고 배지.
// 현재 MonitorBuilding에 anomaly 필드가 없으므로 직접 사용되지 않지만,
// 향후 백엔드에 anomaly 정보가 추가될 때를 위해 컴포넌트를 유지한다.

interface AnomalyInfo {
  is_anomaly: boolean;
  z_score: number;
  direction: "high" | "low";
}

interface Props {
  anomaly: AnomalyInfo;
}

export default function AnomalyBadge({ anomaly }: Props) {
  if (!anomaly.is_anomaly) return null;
  const zText = isFinite(anomaly.z_score) ? anomaly.z_score.toFixed(1) : "?";
  const label =
    anomaly.direction === "high"
      ? `과소비 (z=${zText})`
      : `과소 (z=${zText})`;
  return (
    <span
      role="img"
      aria-label={`이상치 경고: ${label}`}
      title={label}
      className="text-orange-400 text-xs shrink-0"
    >
      🔔
    </span>
  );
}
