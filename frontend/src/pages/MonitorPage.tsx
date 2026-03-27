import { useEffect, useRef, useCallback } from "react";
import { useMonitorStore } from "../store/monitorStore";
import { getMonitorBuildings } from "../api/monitorClient";
import BuildingListPanel from "../components/Monitor/BuildingListPanel";
import TimeseriesChartPanel from "../components/Monitor/TimeseriesChartPanel";
import BuildingMiniMap from "../components/Monitor/BuildingMiniMap";

const POLL_MS = 30_000;

export default function MonitorPage() {
  const { filters, setBuildings, setLoading, setError } = useMonitorStore();
  const abortRef = useRef<AbortController | null>(null);
  // fetchIdRef: abort 중인 요청의 finally에서 setLoading(false)가 새 요청의
  // setLoading(true)를 덮어쓰는 로딩 깜빡임을 방지하기 위한 식별자
  const fetchIdRef = useRef<number>(0);

  const fetchBuildings = useCallback(async () => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const currentFetchId = ++fetchIdRef.current;

    setLoading(true);
    setError(null);
    try {
      const res = await getMonitorBuildings(
        {
          usageType: filters.usageType,
          meterType: filters.meterType,
          search: filters.search,
        },
        abortRef.current.signal
      );
      // 이 fetch가 아직 최신인 경우에만 상태 업데이트
      if (currentFetchId === fetchIdRef.current) {
        setBuildings(res.buildings);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        if (currentFetchId === fetchIdRef.current) {
          setError((e as Error).message);
        }
      }
    } finally {
      // 취소된 요청의 finally가 새 요청의 loading 상태를 꺼버리지 않도록 guard
      if (currentFetchId === fetchIdRef.current) {
        setLoading(false);
      }
    }
  }, [filters, setBuildings, setLoading, setError]);

  // 초기 로드 + 필터 변경 시 재조회
  useEffect(() => {
    fetchBuildings();
  }, [fetchBuildings]);

  // 30초 polling (탭 비활성 시 정지)
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") fetchBuildings();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [fetchBuildings]);

  // 언마운트 시 진행 중 요청 취소
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  return (
    <div className="flex flex-col h-screen bg-gray-950 text-gray-100 overflow-hidden">
      {/* 헤더 */}
      <header className="flex items-center justify-between px-6 py-3 bg-gray-900 border-b border-gray-800 shrink-0">
        <div className="flex items-center gap-4">
          <h1 className="text-sm font-bold tracking-tight text-gray-100">
            건물 에너지 모니터링
          </h1>
        </div>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span>30초 자동 갱신</span>
          <button
            type="button"
            onClick={fetchBuildings}
            className="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
          >
            새로고침
          </button>
          <a
            href="/"
            className="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
          >
            ← 3D 지도
          </a>
        </div>
      </header>

      {/* 본문 — 데스크톱 2열 */}
      <div className="flex flex-1 overflow-hidden">
        {/* 좌측: 건물 목록 (320px 고정) */}
        <aside
          className="w-80 shrink-0 border-r border-gray-800 overflow-hidden flex flex-col"
          aria-label="건물 목록 패널"
        >
          <BuildingListPanel />
        </aside>

        {/* 우측: 차트 + 미니맵 */}
        <main className="flex-1 flex flex-col overflow-hidden min-w-0">
          {/* 차트 영역 */}
          <div className="flex-1 p-4 min-h-0">
            <TimeseriesChartPanel />
          </div>
          {/* 미니맵 + 건물 정보 */}
          <div className="h-44 shrink-0 border-t border-gray-800 p-4">
            <BuildingMiniMap />
          </div>
        </main>
      </div>
    </div>
  );
}
