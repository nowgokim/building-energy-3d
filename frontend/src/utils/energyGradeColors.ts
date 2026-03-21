export const ENERGY_GRADE_COLORS: Record<string, string> = {
  "1+++": "#00a651",
  "1++": "#4cb848",
  "1+": "#8dc63f",
  "1": "#d4e157",
  "2": "#fdd835",
  "3": "#ffb300",
  "4": "#fb8c00",
  "5": "#f4511e",
  "6": "#d32f2f",
  "7": "#880e4f",
  unknown: "#9e9e9e",
};

export function getGradeColor(grade: string | null | undefined): string {
  if (!grade || grade.trim() === "") return ENERGY_GRADE_COLORS.unknown;
  return ENERGY_GRADE_COLORS[grade.trim()] ?? ENERGY_GRADE_COLORS.unknown;
}

/**
 * Map total energy consumption (kWh/m²/yr) to a color.
 * Low energy = green, high energy = red.
 */
export function getEnergyColor(totalEnergy: number | null | undefined): string {
  if (totalEnergy == null) return "#8899aa"; // muted blue-gray for unknown

  // Clamp to 50~300 range
  const clamped = Math.max(50, Math.min(300, totalEnergy));
  const ratio = (clamped - 50) / 250; // 0 = efficient, 1 = inefficient

  // Green → Yellow → Orange → Red gradient
  if (ratio < 0.33) {
    // Green to Yellow
    const t = ratio / 0.33;
    const r = Math.round(76 + t * 179);
    const g = Math.round(175 - t * 30);
    const b = Math.round(80 - t * 60);
    return `rgb(${r},${g},${b})`;
  } else if (ratio < 0.66) {
    // Yellow to Orange
    const t = (ratio - 0.33) / 0.33;
    const r = Math.round(255);
    const g = Math.round(145 - t * 75);
    const b = Math.round(20);
    return `rgb(${r},${g},${b})`;
  } else {
    // Orange to Red
    const t = (ratio - 0.66) / 0.34;
    const r = Math.round(255 - t * 55);
    const g = Math.round(70 - t * 50);
    const b = Math.round(20);
    return `rgb(${r},${g},${b})`;
  }
}
