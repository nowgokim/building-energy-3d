import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { getBuildings, getBuildingDetail } from "../../api/client";
import { useAppStore } from "../../store/appStore";
import { getEnergyColor, getGradeColor } from "../../utils/energyGradeColors";
import { getUsageColor, createWindowPatternCanvas } from "../../utils/buildingStyles";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

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

    // 3D buildings with window textures + energy coloring
    loadTexturedBuildings(viewer).catch((e) => console.error("Buildings:", e));

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

    // Click handler
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        const picked = viewer.scene.pick(event.position);
        if (!Cesium.defined(picked)) return;

        if (picked.id && picked.id instanceof Cesium.Entity) {
          const pnu = picked.id.properties?.pnu?.getValue(Cesium.JulianDate.now());
          if (pnu) {
            highlightEntity(picked.id);
            try {
              useAppStore.getState().setLoadingDetail(true);
              const detail = await getBuildingDetail(pnu);
              useAppStore.getState().selectBuilding(pnu, detail);
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

// --- Textured 3D buildings ---
async function loadTexturedBuildings(viewer: Cesium.Viewer) {
  const data = await getBuildings({
    west: 126.85, south: 37.53, east: 126.97, north: 37.59,
  });

  if (viewer.isDestroyed()) return;
  console.log(`Loading ${data.features.length} textured 3D buildings`);

  const ds = new Cesium.CustomDataSource("buildings-3d");

  for (const feature of data.features) {
    const props = feature.properties;
    const geom = feature.geometry;
    if (!geom || geom.type !== "MultiPolygon" || !props.pnu) continue;

    const height = props.height ?? 10;
    const floors = props.floors_above ?? Math.max(1, Math.round(height / 3.3));

    // 1) Base color from usage type
    const usageColor = getUsageColor(props.usage_type);

    // 2) Energy tint (blend usage color with energy color)
    let energyColorStr: string;
    if (props.energy_grade && props.energy_grade.trim() !== "") {
      energyColorStr = getGradeColor(props.energy_grade);
    } else {
      energyColorStr = getEnergyColor(props.total_energy);
    }

    // Blend: 60% usage color + 40% energy color
    const usageC = Cesium.Color.fromCssColorString(usageColor);
    const energyC = Cesium.Color.fromCssColorString(energyColorStr);
    const blended = new Cesium.Color(
      usageC.red * 0.6 + energyC.red * 0.4,
      usageC.green * 0.6 + energyC.green * 0.4,
      usageC.blue * 0.6 + energyC.blue * 0.4,
      0.92,
    );

    // 3) Create window pattern texture
    const patternCanvas = createWindowPatternCanvas(floors, usageColor);
    const wallMaterial = new Cesium.ImageMaterialProperty({
      image: patternCanvas,
      repeat: new Cesium.Cartesian2(4, 1),
    });

    const coords = (geom as GeoJSON.MultiPolygon).coordinates[0][0];
    const positions = coords.flatMap(([lng, lat]) => [lng, lat]);

    // Wall entity with window texture
    ds.entities.add({
      polygon: {
        hierarchy: Cesium.Cartesian3.fromDegreesArray(positions),
        height: 0,
        extrudedHeight: height,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        extrudedHeightReference: Cesium.HeightReference.RELATIVE_TO_GROUND,
        material: wallMaterial,
        outline: true,
        outlineColor: blended.darken(0.3, new Cesium.Color()),
        outlineWidth: 1,
        shadows: Cesium.ShadowMode.ENABLED,
      },
      properties: {
        pnu: props.pnu,
        building_name: props.building_name,
        usage_type: props.usage_type,
        energy_grade: props.energy_grade,
        total_energy: props.total_energy,
        _blendedColor: blended, // store for highlight restore
      } as any,
    });
  }

  if (viewer.isDestroyed()) return;
  await viewer.dataSources.add(ds);
  console.log(`${ds.entities.values.length} textured buildings added`);
}

// --- Highlight ---
let lastHighlighted: Cesium.Entity | null = null;
let lastMaterial: Cesium.MaterialProperty | null = null;

function highlightEntity(entity: Cesium.Entity) {
  if (lastHighlighted?.polygon) {
    lastHighlighted.polygon.material = lastMaterial as Cesium.MaterialProperty;
  }
  if (entity.polygon) {
    lastMaterial = entity.polygon.material;
    entity.polygon.material = new Cesium.ColorMaterialProperty(
      Cesium.Color.CYAN.withAlpha(0.9)
    );
    lastHighlighted = entity;
  }
}
