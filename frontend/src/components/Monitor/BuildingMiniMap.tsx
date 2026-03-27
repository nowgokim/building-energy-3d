import { useEffect, useRef } from "react";
import { useMonitorStore } from "../../store/monitorStore";

// Leaflet은 monitor.html CDN에서 로드. 타입만 선언.
declare const L: {
  map: (el: HTMLElement, opts: object) => LeafletMap;
  tileLayer: (url: string, opts: object) => { addTo: (m: LeafletMap) => void };
  marker: (latlng: [number, number], opts?: object) => LeafletMarker;
  latLngBounds: (points: [number, number][]) => LeafletBounds;
  divIcon: (opts: object) => object;
};
interface LeafletMap {
  remove: () => void;
  setView: (latlng: [number, number], zoom: number) => void;
  fitBounds: (bounds: LeafletBounds, opts?: object) => void;
}
interface LeafletMarker {
  addTo: (m: LeafletMap) => LeafletMarker;
  remove: () => void;
  bindTooltip: (text: string) => LeafletMarker;
}
interface LeafletBounds { _southWest?: unknown }

/** Leaflet CDN 로드 완료 여부를 안전하게 확인한다. */
function isLeafletReady(): boolean {
  try {
    return typeof L !== "undefined" && typeof L.map === "function";
  } catch {
    return false;
  }
}

export default function BuildingMiniMap() {
  const mapElRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<LeafletMap | null>(null);
  const markersRef = useRef<LeafletMarker[]>([]);
  const { buildings, selectedIds } = useMonitorStore();

  // 지도 초기화 (1회)
  useEffect(() => {
    if (!mapElRef.current || mapRef.current) return;
    if (!isLeafletReady()) {
      // CDN 스크립트가 아직 로드되지 않은 경우 — 짧은 폴링으로 재시도
      const timer = setTimeout(() => {
        if (!mapElRef.current || mapRef.current || !isLeafletReady()) return;
        mapRef.current = L.map(mapElRef.current, {
          center: [37.5665, 126.978],
          zoom: 11,
          zoomControl: false,
          attributionControl: false,
        });
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 18,
        }).addTo(mapRef.current);
      }, 300);
      return () => clearTimeout(timer);
    }
    mapRef.current = L.map(mapElRef.current, {
      center: [37.5665, 126.978],
      zoom: 11,
      zoomControl: false,
      attributionControl: false,
    });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
    }).addTo(mapRef.current);

    return () => {
      // 마커 먼저 정리 후 지도 제거
      markersRef.current.forEach((m) => {
        try { m.remove(); } catch { /* 이미 제거된 마커 무시 */ }
      });
      markersRef.current = [];
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, []);

  // 선택 건물 마커 갱신
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !isLeafletReady()) return;

    // 이전 마커 정리
    markersRef.current.forEach((m) => {
      try { m.remove(); } catch { /* 이미 제거된 마커 무시 */ }
    });
    markersRef.current = [];

    // lat/lng가 null인 건물은 지도에 표시하지 않는다
    const selected = buildings.filter(
      (b) => selectedIds.includes(b.ts_id) && b.lat != null && b.lng != null
    );
    if (selected.length === 0) return;

    for (const b of selected) {
      const marker = L.marker([b.lat as number, b.lng as number], {
        icon: L.divIcon({
          className: "",
          html: `<div style="width:12px;height:12px;border-radius:50%;background:#60a5fa;border:2px solid white;box-shadow:0 0 4px rgba(0,0,0,.5)"></div>`,
        }),
      })
        .addTo(map)
        .bindTooltip(b.alias ?? `건물 #${b.ts_id}`);
      markersRef.current.push(marker);
    }

    if (selected.length === 1) {
      map.setView([selected[0].lat as number, selected[0].lng as number], 15);
    } else {
      const bounds = L.latLngBounds(
        selected.map((b) => [b.lat as number, b.lng as number])
      );
      map.fitBounds(bounds, { padding: [20, 20] });
    }
  }, [buildings, selectedIds]);

  const primary = buildings.find((b) => b.ts_id === selectedIds[0]);

  return (
    <div className="flex gap-4 h-full">
      {/* 미니맵 */}
      <div
        ref={mapElRef}
        className="w-52 h-full rounded-lg overflow-hidden shrink-0 bg-gray-800"
        aria-label="선택된 건물 위치 지도"
        role="region"
      />

      {/* 건물 정보 */}
      <div className="flex-1 flex flex-col justify-between min-w-0">
        {primary ? (
          <>
            <div className="space-y-1">
              <p className="text-sm font-semibold text-gray-200 truncate">
                {primary.alias ?? `건물 #${primary.ts_id}`}
              </p>
              <p className="text-xs text-gray-400 truncate">
                {primary.usage_type ?? "용도 미상"}
                {primary.built_year != null && ` · ${primary.built_year}년`}
              </p>
              {primary.eui_kwh_m2 != null && (
                <p className="text-xs text-gray-500">
                  연간 EUI:{" "}
                  <span className="text-yellow-400 font-mono">
                    {primary.eui_kwh_m2} kWh/m²
                  </span>
                </p>
              )}
              {primary.total_area != null && (
                <p className="text-xs text-gray-500">
                  연면적:{" "}
                  <span className="text-gray-300 font-mono">
                    {primary.total_area.toLocaleString()} m²
                  </span>
                </p>
              )}
            </div>
            <a
              href={`/vworld.html?pnu=${primary.pnu}&zoom=17`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-block text-xs px-3 py-1.5 rounded bg-blue-700 hover:bg-blue-600 text-white w-fit transition-colors"
              aria-label={`${primary.alias ?? `건물 #${primary.ts_id}`} 3D 지도로 이동 (새 탭)`}
            >
              3D 지도로 이동 →
            </a>
          </>
        ) : (
          <p className="text-sm text-gray-500">
            건물을 선택하면 위치가 표시됩니다
          </p>
        )}
      </div>
    </div>
  );
}
