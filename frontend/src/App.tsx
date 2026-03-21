import CesiumViewer from "./components/MapViewer/CesiumViewer";
import SearchBar from "./components/Controls/SearchBar";
import BuildingDetailPanel from "./components/Panel/BuildingDetailPanel";
import StatsBar from "./components/Dashboard/StatsBar";
import Legend from "./components/Dashboard/Legend";
import { getBuildingDetail } from "./api/client";
import { useAppStore } from "./store/appStore";
import type { SearchResult } from "./types/building";

export default function App() {
  const selectBuilding = useAppStore((s) => s.selectBuilding);

  const handleSearchSelect = async (result: SearchResult) => {
    if (!result.pnu) return;
    try {
      const detail = await getBuildingDetail(result.pnu);
      selectBuilding(result.pnu, detail);
      // TODO: fly camera to result.lng, result.lat
    } catch (e) {
      console.error("Failed to load building detail:", e);
    }
  };

  return (
    <div className="relative w-full h-full">
      <CesiumViewer />
      <SearchBar onSelect={handleSearchSelect} />
      <BuildingDetailPanel />
      <StatsBar />
      <Legend />
    </div>
  );
}
