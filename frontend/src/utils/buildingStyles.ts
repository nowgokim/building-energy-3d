/**
 * Building usage type → base color mapping.
 * Colors inspired by typical urban zoning maps.
 */
const USAGE_COLORS: Record<string, string> = {
  // 주거
  "공동주택": "#d4a574",
  "아파트": "#d4a574",
  "연립주택": "#c9956a",
  "다세대주택": "#c9956a",
  "단독주택": "#dbb890",

  // 상업
  "판매시설": "#7facd6",
  "근린생활시설": "#8cb8de",
  "제1종근린생활시설": "#8cb8de",
  "제2종근린생활시설": "#8cb8de",

  // 업무
  "업무시설": "#9aa8b8",
  "사무소": "#9aa8b8",

  // 교육
  "교육연구시설": "#a8c99a",
  "학교": "#a8c99a",

  // 의료
  "의료시설": "#e8a8a8",
  "병원": "#e8a8a8",

  // 기타
  "종교시설": "#c8b8d0",
  "운동시설": "#88c4a8",
  "공장": "#b0b0b0",
  "창고시설": "#a0a0a0",
};

const DEFAULT_COLOR = "#b8b0a8"; // 미분류 — 베이지/회색

export function getUsageColor(usageType: string | null | undefined): string {
  if (!usageType) return DEFAULT_COLOR;
  return USAGE_COLORS[usageType.trim()] ?? DEFAULT_COLOR;
}

/**
 * Create a stripe pattern canvas for simulating windows/floors.
 * Returns a canvas element that can be used as a Cesium material image.
 */
export function createWindowPatternCanvas(
  floors: number,
  color: string,
): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  const width = 64;
  const height = Math.max(32, Math.min(256, floors * 24));
  canvas.width = width;
  canvas.height = height;

  const ctx = canvas.getContext("2d")!;

  // Wall base color
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, width, height);

  // Darken slightly for depth
  ctx.fillStyle = "rgba(0,0,0,0.08)";
  ctx.fillRect(0, 0, width, height);

  // Floor lines (horizontal)
  const floorHeight = height / Math.max(floors, 1);
  ctx.strokeStyle = "rgba(0,0,0,0.15)";
  ctx.lineWidth = 1;
  for (let i = 1; i < floors; i++) {
    const y = i * floorHeight;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  // Windows (dark rectangles)
  const windowWidth = 8;
  const windowHeight = floorHeight * 0.5;
  const windowGap = 4;
  ctx.fillStyle = "rgba(40, 60, 80, 0.5)";

  for (let floor = 0; floor < Math.min(floors, 20); floor++) {
    const y = floor * floorHeight + floorHeight * 0.25;
    for (let wx = windowGap; wx + windowWidth < width; wx += windowWidth + windowGap) {
      ctx.fillRect(wx, y, windowWidth, windowHeight);
    }
  }

  // Window reflections (lighter spots)
  ctx.fillStyle = "rgba(180, 210, 240, 0.2)";
  for (let floor = 0; floor < Math.min(floors, 20); floor++) {
    const y = floor * floorHeight + floorHeight * 0.25;
    for (let wx = windowGap; wx + windowWidth < width; wx += windowWidth + windowGap) {
      ctx.fillRect(wx + 1, y + 1, windowWidth * 0.4, windowHeight * 0.3);
    }
  }

  return canvas;
}
