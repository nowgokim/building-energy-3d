import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { getBuildings, getBuildingDetail } from "../../api/client";
import { useAppStore } from "../../store/appStore";
import { getEnergyColor, getGradeColor } from "../../utils/energyGradeColors";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";
const GOOGLE_API_KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY ?? "";

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);
  const selectBuilding = useAppStore((s) => s.selectBuilding);
  const setLoadingDetail = useAppStore((s) => s.setLoadingDetail);
  const setError = useAppStore((s) => s.setError);

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
    });

    viewerRef.current = viewer;

    // Load Google 3D Tiles (photorealistic buildings + terrain)
    loadGoogle3DTiles(viewer).catch(() => {});

    // Load energy data markers on top of Google buildings
    loadEnergyMarkers(viewer).catch(() => {});

    // Fly to 마포구
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(
        MAPO_CENTER.lng,
        MAPO_CENTER.lat,
        800
      ),
      orientation: {
        heading: Cesium.Math.toRadians(20),
        pitch: Cesium.Math.toRadians(-40),
        roll: 0,
      },
      duration: 2,
    });

    // Click handler
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        const picked = viewer.scene.pick(event.position);
        if (Cesium.defined(picked) && picked.id) {
          const entity = picked.id as Cesium.Entity;
          const pnu = entity.properties?.pnu?.getValue(Cesium.JulianDate.now());
          if (pnu) {
            try {
              setLoadingDetail(true);
              const detail = await getBuildingDetail(pnu);
              selectBuilding(pnu, detail);
            } catch {
              setError("건물 정보를 불러올 수 없습니다");
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

// --- Google Photorealistic 3D Tiles ---
async function loadGoogle3DTiles(viewer: Cesium.Viewer) {
  const tileset = await Cesium.createGooglePhotorealistic3DTileset(
    GOOGLE_API_KEY,
    { showCreditsOnScreen: true }
  );
  if (viewer.isDestroyed()) return;

  viewer.scene.primitives.add(tileset);
  viewer.scene.globe.show = false;
  console.log("Google Photorealistic 3D Tiles loaded");
}

// --- Energy markers (colored circles on building rooftops) ---
async function loadEnergyMarkers(viewer: Cesium.Viewer) {
  const data = await getBuildings({
    west: 126.89, south: 37.53, east: 126.96, north: 37.58,
  });

  if (viewer.isDestroyed()) return;
  console.log(`Loading ${data.features.length} energy markers`);

  const ds = new Cesium.CustomDataSource("energy-markers");

  for (const feature of data.features) {
    const props = feature.properties;
    if (!props.pnu || !props.lng || !props.lat) continue;

    const buildingHeight = props.height ?? 10;
    // 마포구 해발 + 건물 높이 + 약간의 오프셋
    const markerHeight = 30 + buildingHeight + 3;

    // Color by energy grade or consumption
    let colorStr: string;
    if (props.energy_grade && props.energy_grade.trim() !== "") {
      colorStr = getGradeColor(props.energy_grade);
    } else {
      colorStr = getEnergyColor(props.total_energy);
    }
    const color = Cesium.Color.fromCssColorString(colorStr);

    ds.entities.add({
      position: Cesium.Cartesian3.fromDegrees(props.lng, props.lat, markerHeight),
      point: {
        pixelSize: 8,
        color: color,
        outlineColor: Cesium.Color.BLACK.withAlpha(0.5),
        outlineWidth: 1,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: new Cesium.NearFarScalar(200, 1.5, 3000, 0.5),
      },
      properties: {
        pnu: props.pnu,
        building_name: props.building_name,
        usage_type: props.usage_type,
        energy_grade: props.energy_grade,
        total_energy: props.total_energy,
      } as any,
    });
  }

  if (viewer.isDestroyed()) return;
  await viewer.dataSources.add(ds);
  console.log(`${ds.entities.values.length} energy markers added`);
}
