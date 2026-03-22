import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { pickBuilding, getBuildingDetail, getBuildings } from "../../api/client";
import { useAppStore } from "../../store/appStore";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const loadedPnus = useRef(new Set<string>());
  const loadingRef = useRef(false);

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
      maximumRenderTimeChange: Infinity,
    });

    viewerRef.current = viewer;
    viewer.scene.globe.depthTestAgainstTerrain = true;

    // Cesium World Terrain
    viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain());

    // Create data source for our 3D buildings
    const buildingDS = new Cesium.CustomDataSource("buildings-3d");
    viewer.dataSources.add(buildingDS);

    // Initial load
    loadBuildingsInView(viewer, buildingDS, loadedPnus.current, loadingRef);

    // Reload on camera move
    viewer.camera.moveEnd.addEventListener(() => {
      loadBuildingsInView(viewer, buildingDS, loadedPnus.current, loadingRef);
      viewer.scene.requestRender();
    });

    // Fly to 마포구
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
      useAppStore.getState().setError("3D 렌더링이 중단되었습니다. 페이지를 새로고침하세요.");
    };
    canvas.addEventListener("webglcontextlost", handleContextLost);

    return () => {
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

// --- Viewport-based 3D building loading ---
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

// Energy consumption → color
function energyToColor(total: number | null): Cesium.Color {
  if (total == null) return Cesium.Color.fromCssColorString("#8899aa").withAlpha(0.8);
  const clamped = Math.max(50, Math.min(300, total));
  const ratio = (clamped - 50) / 250;
  if (ratio < 0.33) {
    return Cesium.Color.fromCssColorString("#4cb848").withAlpha(0.8);
  } else if (ratio < 0.66) {
    return Cesium.Color.fromCssColorString("#fdd835").withAlpha(0.8);
  } else {
    return Cesium.Color.fromCssColorString("#fb8c00").withAlpha(0.8);
  }
}

async function loadBuildingsInView(
  viewer: Cesium.Viewer,
  ds: Cesium.CustomDataSource,
  loadedPnus: Set<string>,
  loadingRef: React.MutableRefObject<boolean>,
) {
  if (loadingRef.current || viewer.isDestroyed()) return;

  const bbox = getViewerBbox(viewer);
  if (!bbox) return;

  // Skip if zoomed out too far
  const spanLng = bbox.east - bbox.west;
  const spanLat = bbox.north - bbox.south;
  if (spanLng > 0.05 || spanLat > 0.05) return;

  loadingRef.current = true;

  try {
    const data = await getBuildings(bbox);
    if (viewer.isDestroyed()) return;

    let added = 0;
    for (const feature of data.features) {
      const props = feature.properties;
      const geom = feature.geometry;
      if (!geom || geom.type !== "MultiPolygon" || !props.pnu) continue;
      if (loadedPnus.has(props.pnu)) continue;

      const height = props.height ?? 10;
      const color = energyToColor(props.total_energy);

      const coords = (geom as GeoJSON.MultiPolygon).coordinates[0][0];
      const positions = coords.flatMap(([lng, lat]) => [lng, lat]);

      ds.entities.add({
        polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(positions),
          height: 0,
          extrudedHeight: height,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          extrudedHeightReference: Cesium.HeightReference.RELATIVE_TO_GROUND,
          material: new Cesium.ColorMaterialProperty(color),
          outline: true,
          outlineColor: color.darken(0.3, new Cesium.Color()),
          outlineWidth: 1,
        },
      });

      loadedPnus.add(props.pnu);
      added++;
    }

    if (added > 0) {
      console.log(`Added ${added} buildings (total: ${loadedPnus.size})`);
      viewer.scene.requestRender();
    }
  } catch {
    // silently fail viewport loads
  } finally {
    loadingRef.current = false;
  }
}
