import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { getBuildings, getBuildingDetail } from "../../api/client";
import { useAppStore } from "../../store/appStore";
import { getEnergyColor, getGradeColor } from "../../utils/energyGradeColors";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

// Building centroid data for click matching
let buildingCentroids: { pnu: string; lng: number; lat: number }[] = [];
const pnuColorMap = new Map<string, string>();

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);

  useEffect(() => {
    if (!containerRef.current || viewerRef.current) return;
    containerRef.current.innerHTML = "";

    const viewer = new Cesium.Viewer(containerRef.current, {
      sceneMode: Cesium.SceneMode.SCENE3D,
      baseLayer: new Cesium.ImageryLayer(
        new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })
      ),
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

    // Cesium World Terrain (correct API)
    viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain());

    // Load OSM Buildings + energy data
    initBuildings(viewer).catch((e) => console.error("Init buildings:", e));

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
        if (viewer.isDestroyed()) return;
        const picked = viewer.scene.pick(event.position);
        if (!Cesium.defined(picked)) return;

        // OSM Building or any 3D feature clicked
        const cartesian = viewer.scene.pickPosition(event.position);
        if (!cartesian) return;
        const carto = Cesium.Cartographic.fromCartesian(cartesian);
        const clickLng = Cesium.Math.toDegrees(carto.longitude);
        const clickLat = Cesium.Math.toDegrees(carto.latitude);

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
      },
      Cesium.ScreenSpaceEventType.LEFT_CLICK
    );

    return () => {
      if (viewerRef.current && !viewerRef.current.isDestroyed()) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
    };
  }, []);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}

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
  if (minDist > 0.0005 ** 2) return null;
  return nearest;
}

async function initBuildings(viewer: Cesium.Viewer) {
  // Clear globals (safety for hot reload)
  buildingCentroids = [];
  pnuColorMap.clear();

  // 1) Fetch energy data from API
  const data = await getBuildings({
    west: 126.89, south: 37.53, east: 126.98, north: 37.60,
  });
  if (viewer.isDestroyed()) return;
  console.log(`Loaded ${data.features.length} buildings data`);

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

  // 2) Load OSM Buildings with styling + shader
  try {
    const tileset = await Cesium.Cesium3DTileset.fromIonAssetId(96188);
    if (viewer.isDestroyed()) return;

    tileset.style = new Cesium.Cesium3DTileStyle({
      color: {
        conditions: [
          ["${feature['cesium#estimatedHeight']} > 50", "color('#4cb848')"],
          ["${feature['cesium#estimatedHeight']} > 30", "color('#8dc63f')"],
          ["${feature['cesium#estimatedHeight']} > 20", "color('#d4e157')"],
          ["${feature['cesium#estimatedHeight']} > 10", "color('#fdd835')"],
          ["${feature['cesium#estimatedHeight']} > 5", "color('#ffb300')"],
          ["true", "color('#fb8c00')"],
        ],
      },
    });

    tileset.customShader = new Cesium.CustomShader({
      fragmentShaderText: `
        float hash(vec2 p) {
          return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
          vec3 normalEC = fsInput.attributes.normalEC;
          vec3 upEC = normalize(czm_normal * vec3(0.0, 0.0, 1.0));
          float upDot = abs(dot(normalEC, upEC));
          vec3 posWC = (czm_inverseView * vec4(fsInput.attributes.positionEC, 1.0)).xyz;

          if (upDot < 0.35) {
            float floorH = 3.3;
            float vertFrac = mod(posWC.z, floorH) / floorH;
            float horizFrac = mod(posWC.x + posWC.y, 2.8);

            float isWinH = step(0.4, horizFrac) * (1.0 - step(2.0, horizFrac));
            float isWinV = step(0.2, vertFrac) * (1.0 - step(0.75, vertFrac));
            float isWindow = isWinH * isWinV;

            vec3 wallBase = material.diffuse * 0.75;
            float rnd = hash(floor(posWC.xy * 0.5));
            vec3 glassColor = mix(
              vec3(0.12, 0.18, 0.28),
              vec3(0.25, 0.35, 0.50),
              rnd * 0.5 + 0.25
            );
            material.diffuse = mix(wallBase, glassColor, isWindow * 0.85);

            float lineStrength = 1.0 - smoothstep(0.0, 0.05, vertFrac);
            material.diffuse = mix(material.diffuse, wallBase * 0.6, lineStrength * 0.8);
          } else {
            float roofRnd = hash(floor(posWC.xy * 0.02));
            vec3 roofTint;
            if (roofRnd < 0.25) roofTint = vec3(0.55, 0.53, 0.50);
            else if (roofRnd < 0.45) roofTint = vec3(0.35, 0.45, 0.35);
            else if (roofRnd < 0.65) roofTint = vec3(0.50, 0.40, 0.32);
            else if (roofRnd < 0.80) roofTint = vec3(0.40, 0.40, 0.42);
            else roofTint = vec3(0.60, 0.58, 0.55);
            material.diffuse = mix(material.diffuse * 0.6, roofTint, 0.6);
          }

          material.alpha = 1.0;
        }
      `,
    });

    viewer.scene.primitives.add(tileset);
    console.log("OSM Buildings loaded");
  } catch (e) {
    console.warn("OSM Buildings failed:", e);
  }
}
