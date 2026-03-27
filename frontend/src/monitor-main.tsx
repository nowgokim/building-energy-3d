// StrictMode를 사용하지 않음:
// Leaflet의 L.map()이 동일 DOM 요소에 두 번 호출되면 "Map container is already initialized"
// 오류가 발생한다. React StrictMode는 개발 모드에서 effect를 이중 실행하므로 비활성화.
// (Cesium 뷰어와 동일한 이유 — CLAUDE.md 기술 결정 #5 참조)
import { createRoot } from "react-dom/client";
import "./index.css";
import MonitorPage from "./pages/MonitorPage";

const el = document.getElementById("monitor-root");
if (!el) throw new Error("monitor-root element not found");

createRoot(el).render(<MonitorPage />);
