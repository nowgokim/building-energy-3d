import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { pickBuilding, getBuildingDetail, getBuildings } from "../../api/client";
import { useAppStore } from "../../store/appStore";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

const MAX_ENTITIES = 8000;

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const loadedPnus = useRef(new Set<string>());
  const loadingRef = useRef(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!containerRef.current || viewerRef.current) return;
    containerRef.current.innerHTML = "";

    const viewer = new Cesium.Viewer(containerRef.current, {
      sceneMode: Cesium.SceneMode.SCENE3D,
      animation: false,
      timeline: false,
      geocoder: false,
      homeButton: false,
      baseLayerPicker: false,
      navigationHelpButton: false,
      fullscreenButton: false,
      sceneModePicker: false,
      selectionIndicator: false,
      infoBox: false,
      shadows: false,
      requestRenderMode: true,
      maximumRenderTimeChange: 0.5,
    });

    viewerRef.current = viewer;

    // #1 FIX: 지형 없는 평면 지구 — height:0이 정확히 지면
    viewer.scene.globe.terrainProvider = new Cesium.EllipsoidTerrainProvider();

    // Data source for 3D buildings
    const buildingDS = new Cesium.CustomDataSource("buildings-3d");
    viewer.dataSources.add(buildingDS);

    // #2 FIX: flyTo 완료 후 첫 로딩 (이전에는 flyTo 전에 호출하여 빈 결과)
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(
        MAPO_CENTER.lng - 0.006,
        MAPO_CENTER.lat - 0.004,
        500
      ),
      orientation: {
        heading: Cesium.Math.toRadians(30),
        pitch: Cesium.Math.toRadians(-35),
        roll: 0,
      },
      duration: 2,
      complete: () => {
        loadBuildingsInView(viewer, buildingDS, loadedPnus.current, loadingRef);
      },
    });

    // Debounced reload on camera move
    viewer.camera.moveEnd.addEventListener(() => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        loadBuildingsInView(viewer, buildingDS, loadedPnus.current, loadingRef);
        viewer.scene.requestRender();
      }, 400);
    });

    // Click handler — server-side pick
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        if (viewer.isDestroyed()) return;

        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;

        let cartesian = viewer.scene.pickPosition(event.position);
        if (!cartesian) {
          const ray = viewer.camera.getPickRay(event.position);
          if (ray) cartesian = viewer.scene.globe.pick(ray, viewer.scene);
        }
        if (!cartesian) return;

        const carto = Cesium.Cartographic.fromCartesian(cartesian);
        const lng = Cesium.Math.toDegrees(carto.longitude);
        const lat = Cesium.Math.toDegrees(carto.latitude);

        try {
          const result = await pickBuilding(lng, lat, controller.signal);
          if (controller.signal.aborted || viewer.isDestroyed()) return;

          if (result.pnu) {
            useAppStore.getState().setLoadingDetail(true);
            const detail = await getBuildingDetail(result.pnu, controller.signal);
            if (controller.signal.aborted || viewer.isDestroyed()) return;
            useAppStore.getState().selectBuilding(result.pnu, detail);
          }
        } catch (e) {
          if ((e as Error).name === "AbortError") return;
          useAppStore.getState().setError("건물 정보를 불러올 수 없습니다");
        }
      },
      Cesium.ScreenSpaceEventType.LEFT_CLICK
    );

    // WebGL context loss
    const canvas = viewer.scene.canvas;
    const handleContextLost = () => {
      useAppStore.getState().setError("3D 렌더링이 중단되었습니다. 새로고침하세요.");
    };
    canvas.addEventListener("webglcontextlost", handleContextLost);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      canvas.removeEventListener("webglcontextlost", handleContextLost);
      abortRef.current?.abort();
      if (viewerRef.current && !viewerRef.current.isDestroyed()) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
    };
  }, []);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}

// --- Energy color (fully opaque) ---
function energyToColor(total: number | null): Cesium.Color {
  if (total == null) return new Cesium.Color(0.65, 0.67, 0.70, 1.0);
  const clamped = Math.max(50, Math.min(300, total));
  const ratio = (clamped - 50) / 250;
  if (ratio < 0.25) return new Cesium.Color(0.30, 0.72, 0.28, 1.0);
  if (ratio < 0.50) return new Cesium.Color(0.55, 0.76, 0.22, 1.0);
  if (ratio < 0.75) return new Cesium.Color(0.99, 0.85, 0.21, 1.0);
  return new Cesium.Color(0.98, 0.55, 0.00, 1.0);
}

// --- Viewport bbox ---
function getViewerBbox(viewer: Cesium.Viewer) {
  const rect = viewer.camera.computeViewRectangle();
  if (!rect) return null;
  return {
    west: Cesium.Math.toDegrees(rect.west),
    south: Cesium.Math.toDegrees(rect.south),
    east: Cesium.Math.toDegrees(rect.east),
    north: Cesium.Math.toDegrees(rect.north),
  };
}

// --- Load buildings with batching + eviction ---
async function loadBuildingsInView(
  viewer: Cesium.Viewer,
  ds: Cesium.CustomDataSource,
  loadedPnus: Set<string>,
  loadingRef: React.MutableRefObject<boolean>,
) {
  if (loadingRef.current || viewer.isDestroyed()) return;

  const bbox = getViewerBbox(viewer);
  if (!bbox) return;

  const spanLng = bbox.east - bbox.west;
  const spanLat = bbox.north - bbox.south;
  if (spanLng > 0.15 || spanLat > 0.15) return;

  loadingRef.current = true;

  try {
    const data = await getBuildings(bbox);
    if (viewer.isDestroyed()) return;

    // #6 FIX: Entity 최대 개수 제한 — 오래된 것 제거
    if (ds.entities.values.length > MAX_ENTITIES) {
      const toRemove = ds.entities.values.length - MAX_ENTITIES + 1000;
      for (let i = 0; i < toRemove; i++) {
        ds.entities.values[0] && ds.entities.remove(ds.entities.values[0]);
      }
      loadedPnus.clear(); // 재로딩 허용
    }

    // #3 FIX: suspendEvents로 배치 추가 (Entity 하나씩 → 한번에)
    ds.entities.suspendEvents();

    let added = 0;
    for (const feature of data.features) {
      const props = feature.properties;
      const geom = feature.geometry;
      if (!geom || geom.type !== "MultiPolygon" || !props.pnu) continue;
      if (loadedPnus.has(props.pnu)) continue;

      const height = props.height ?? 10;
      const color = energyToColor(props.total_energy);
      const outlineColor = color.darken(0.25, new Cesium.Color());

      // #5 FIX: MultiPolygon 전체 폴리곤 + 홀 처리
      const multiCoords = (geom as GeoJSON.MultiPolygon).coordinates;
      for (const polygon of multiCoords) {
        const outerRing = polygon[0];
        const outerPositions = Cesium.Cartesian3.fromDegreesArray(
          outerRing.flatMap(([lng, lat]) => [lng, lat])
        );

        const holes = polygon.slice(1).map(
          (ring) =>
            new Cesium.PolygonHierarchy(
              Cesium.Cartesian3.fromDegreesArray(
                ring.flatMap(([lng, lat]) => [lng, lat])
              )
            )
        );

        ds.entities.add({
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(outerPositions, holes),
            height: 0,
            extrudedHeight: height,
            material: new Cesium.ColorMaterialProperty(color),
            outline: true,
            outlineColor: new Cesium.ColorMaterialProperty(outlineColor),
            outlineWidth: 1,
          },
        });
      }

      loadedPnus.add(props.pnu);
      added++;
    }

    ds.entities.resumeEvents();

    if (added > 0) {
      console.log(`Added ${added} buildings (total entities: ${ds.entities.values.length})`);
      viewer.scene.requestRender();
    }
  } catch {
    // silently fail viewport loads
  } finally {
    loadingRef.current = false;
  }
}
