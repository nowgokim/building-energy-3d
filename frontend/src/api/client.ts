import { API_BASE_URL } from "../utils/constants";
import type {
  BuildingCollection,
  BuildingDetail,
  StatsResponse,
  SearchResult,
} from "../types/building";

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new Error("서버에 연결할 수 없습니다");
  }
  if (!resp.ok) {
    throw new Error(
      resp.status === 404
        ? "데이터를 찾을 수 없습니다"
        : `서버 오류 (${resp.status})`
    );
  }
  return resp.json();
}

export async function pickBuilding(
  lng: number,
  lat: number,
  signal?: AbortSignal
): Promise<{ pnu: string | null; building_name: string | null }> {
  return fetchJSON(
    `${API_BASE_URL}/buildings/pick?lng=${lng}&lat=${lat}`,
    { signal }
  );
}

export async function getBuildingDetail(
  pnu: string,
  signal?: AbortSignal
): Promise<BuildingDetail> {
  return fetchJSON(`${API_BASE_URL}/buildings/${pnu}`, { signal });
}

export async function getStats(bbox?: {
  west: number;
  south: number;
  east: number;
  north: number;
}): Promise<StatsResponse> {
  const sp = new URLSearchParams();
  if (bbox) {
    sp.set("west", String(bbox.west));
    sp.set("south", String(bbox.south));
    sp.set("east", String(bbox.east));
    sp.set("north", String(bbox.north));
  }
  const qs = sp.toString();
  return fetchJSON(`${API_BASE_URL}/buildings/stats${qs ? "?" + qs : ""}`);
}

export async function searchBuildings(
  q: string
): Promise<{ query: string; count: number; results: SearchResult[] }> {
  return fetchJSON(
    `${API_BASE_URL}/search?q=${encodeURIComponent(q)}`
  );
}
