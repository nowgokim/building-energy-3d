import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { getBuildings, getBuildingDetail } from "../../api/client";
import { useAppStore } from "../../store/appStore";
import { getEnergyColor, getGradeColor } from "../../utils/energyGradeColors";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

// PNU → energy color lookup (built after data load)
const pnuColorMap = new Map<string, string>();

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);

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
      shadows: true,
    });

    viewerRef.current = viewer;
    viewer.scene.globe.enableLighting = true;
    viewer.scene.globe.depthTestAgainstTerrain = true;

    // Cesium World Terrain
    Cesium.CesiumTerrainProvider.fromIonAssetId(1).then((terrain) => {
      if (!viewer.isDestroyed()) {
        viewer.scene.setTerrain(new Cesium.Terrain(terrain));
      }
    });

    // Load energy data first, then OSM Buildings with colors
    loadEnergyDataAndBuildings(viewer);

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

    // Click handler — find nearest building by picking
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        const picked = viewer.scene.pick(event.position);
        if (!Cesium.defined(picked)) return;

        // OSM Building feature pick
        if (picked instanceof Cesium.Cesium3DTileFeature) {
          // Get click position in cartographic
          const cartesian = viewer.scene.pickPosition(event.position);
          if (!cartesian) return;
          const carto = Cesium.Cartographic.fromCartesian(cartesian);
          const clickLng = Cesium.Math.toDegrees(carto.longitude);
          const clickLat = Cesium.Math.toDegrees(carto.latitude);

          // Find nearest PNU from our data
          const nearest = findNearestPnu(clickLng, clickLat);
          if (nearest) {
            try {
              useAppStore.getState().setLoadingDetail(true);
              const detail = await getBuildingDetail(nearest);
              useAppStore.getState().selectBuilding(nearest, detail);
            } catch {
              useAppStore.getState().setError("건물 정보를 불러올 수 없습니다");
            }
          }
        }
      },
      Cesium.ScreenSpaceEventType.LEFT_CLICK
    );

    return () => {
      if (viewerRef.current && !viewerRef.current.isDestroyed()) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}

// Building centroid data for click matching
const buildingCentroids: { pnu: string; lng: number; lat: number }[] = [];

function findNearestPnu(lng: number, lat: number): string | null {
  let minDist = Infinity;
  let nearest: string | null = null;
  for (const b of buildingCentroids) {
    const d = (b.lng - lng) ** 2 + (b.lat - lat) ** 2;
    if (d < minDist) {
      minDist = d;
      nearest = b.pnu;
    }
  }
  // Only match within ~50m
  if (minDist > 0.0005 ** 2) return null;
  return nearest;
}

async function loadEnergyDataAndBuildings(viewer: Cesium.Viewer) {
  try {
    // 1) Load energy data from API
    const data = await getBuildings({
      west: 126.85, south: 37.53, east: 126.97, north: 37.59,
    });

    if (viewer.isDestroyed()) return;
    console.log(`Loaded ${data.features.length} buildings data`);

    // Build PNU→color map and centroid list
    for (const feature of data.features) {
      const props = feature.properties;
      if (!props.pnu || !props.lng || !props.lat) continue;

      let colorStr: string;
      if (props.energy_grade && props.energy_grade.trim() !== "") {
        colorStr = getGradeColor(props.energy_grade);
      } else {
        colorStr = getEnergyColor(props.total_energy);
      }
      pnuColorMap.set(props.pnu, colorStr);
      buildingCentroids.push({ pnu: props.pnu, lng: props.lng, lat: props.lat });
    }

    // 2) Load OSM Buildings with energy-based coloring
    const tileset = await Cesium.Cesium3DTileset.fromIonAssetId(96188);
    if (viewer.isDestroyed()) return;

    // Color by height as proxy (OSM buildings don't have PNU)
    // Low buildings = warmer color (older, less efficient)
    // Tall buildings = cooler color (newer, more efficient)
    tileset.style = new Cesium.Cesium3DTileStyle({
      color: {
        conditions: [
          ["${feature['cesium#estimatedHeight']} > 50", "color('#4cb848', 0.85)"],  // 고층 — 녹색
          ["${feature['cesium#estimatedHeight']} > 30", "color('#8dc63f', 0.85)"],
          ["${feature['cesium#estimatedHeight']} > 20", "color('#d4e157', 0.85)"],
          ["${feature['cesium#estimatedHeight']} > 10", "color('#fdd835', 0.85)"],
          ["${feature['cesium#estimatedHeight']} > 5", "color('#ffb300', 0.85)"],
          ["true", "color('#fb8c00', 0.85)"],  // 저층 — 주황
        ],
      },
    });

    viewer.scene.primitives.add(tileset);
    console.log("OSM Buildings loaded with energy coloring");
  } catch (e) {
    console.error("Failed to load buildings:", e);
  }
}
