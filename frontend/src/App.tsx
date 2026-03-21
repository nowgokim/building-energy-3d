import { Suspense, lazy } from "react";
import SearchBar from "./components/Controls/SearchBar";
import BuildingDetailPanel from "./components/Panel/BuildingDetailPanel";
import StatsBar from "./components/Dashboard/StatsBar";
import Legend from "./components/Dashboard/Legend";
import ErrorToast from "./components/Dashboard/ErrorToast";
import { getBuildingDetail } from "./api/client";
import { useAppStore } from "./store/appStore";
import type { SearchResult } from "./types/building";

const CesiumViewer = lazy(
  () => import("./components/MapViewer/CesiumViewer")
);

export default function App() {
  const selectBuilding = useAppStore((s) => s.selectBuilding);
  const setError = useAppStore((s) => s.setError);

  const handleSearchSelect = async (result: SearchResult) => {
    if (!result.pnu) return;
    try {
      const detail = await getBuildingDetail(result.pnu);
      selectBuilding(result.pnu, detail);
    } catch {
      setError("건물 정보를 불러올 수 없습니다");
    }
  };

  return (
    <div className="relative w-full h-full">
      <Suspense
        fallback={
          <div className="w-full h-full flex items-center justify-center bg-gray-900 text-white text-sm">
            3D 뷰어 로딩 중...
          </div>
        }
      >
        <CesiumViewer />
      </Suspense>
      <SearchBar onSelect={handleSearchSelect} />
      <BuildingDetailPanel />
      <StatsBar />
      <Legend />
      <ErrorToast />
    </div>
  );
}
