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
