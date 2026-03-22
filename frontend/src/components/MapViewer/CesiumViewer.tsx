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

    // 2) Load OSM Buildings
    const tileset = await Cesium.Cesium3DTileset.fromIonAssetId(96188);
    if (viewer.isDestroyed()) return;

    // Energy color by height
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

    // Procedural texture shader: windows on walls, varied rooftop colors
    tileset.customShader = new Cesium.CustomShader({
      fragmentShaderText: `
        // Simple hash for pseudo-random values
        float hash(vec2 p) {
          return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
          vec3 normalEC = fsInput.attributes.normalEC;
          vec3 upEC = normalize(czm_normal * vec3(0.0, 0.0, 1.0));
          float upDot = abs(dot(normalEC, upEC));
          vec3 posWC = (czm_inverseView * vec4(fsInput.attributes.positionEC, 1.0)).xyz;

          if (upDot < 0.35) {
            // === WALL: window pattern ===
            float floorH = 3.3;
            float vertFrac = mod(posWC.z, floorH) / floorH;
            float horizFrac = mod(posWC.x + posWC.y, 2.8);

            // Window openings
            float isWinH = step(0.4, horizFrac) * (1.0 - step(2.0, horizFrac));
            float isWinV = step(0.2, vertFrac) * (1.0 - step(0.75, vertFrac));
            float isWindow = isWinH * isWinV;

            // Concrete wall base (slightly darker than roof color)
            vec3 wallBase = material.diffuse * 0.75;

            // Glass: dark blue with subtle variation
            float rnd = hash(floor(posWC.xy * 0.5));
            vec3 glassColor = mix(
              vec3(0.12, 0.18, 0.28),
              vec3(0.25, 0.35, 0.50),
              rnd * 0.5 + 0.25
            );

            material.diffuse = mix(wallBase, glassColor, isWindow * 0.85);

            // Floor separation line
            float lineStrength = 1.0 - smoothstep(0.0, 0.05, vertFrac);
            material.diffuse = mix(material.diffuse, wallBase * 0.6, lineStrength * 0.8);

          } else {
            // === ROOF: varied color simulating different materials ===
            float roofRnd = hash(floor(posWC.xy * 0.02));

            // Mix of roof types: concrete gray, green, brown, dark
            vec3 roofColors[4];
            roofColors[0] = vec3(0.55, 0.53, 0.50); // concrete
            roofColors[1] = vec3(0.35, 0.45, 0.35); // green (trees/garden)
            roofColors[2] = vec3(0.50, 0.40, 0.32); // brown tile
            roofColors[3] = vec3(0.40, 0.40, 0.42); // dark flat

            int idx = int(floor(roofRnd * 4.0));
            vec3 roofTint;
            if (idx == 0) roofTint = roofColors[0];
            else if (idx == 1) roofTint = roofColors[1];
            else if (idx == 2) roofTint = roofColors[2];
            else roofTint = roofColors[3];

            // Blend energy color with roof material
            material.diffuse = mix(material.diffuse * 0.7, roofTint, 0.5);
          }

          // Fully opaque
          material.alpha = 1.0;
        }
      `,
    });

    // OSM Buildings are pre-clamped to Cesium World Terrain — no manual offset needed.
    // If fine-tuning is needed, use boundingSphere.center:
    // const heightOffset = -1.0;
    // const bs = tileset.boundingSphere;
    // const carto = Cesium.Cartographic.fromCartesian(bs.center);
    // const surface = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0);
    // const shifted = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, heightOffset);
    // tileset.modelMatrix = Cesium.Matrix4.fromTranslation(
    //   Cesium.Cartesian3.subtract(shifted, surface, new Cesium.Cartesian3())
    // );

    viewer.scene.primitives.add(tileset);
    console.log("OSM Buildings loaded with textures");
  } catch (e) {
    console.error("Failed to load buildings:", e);
  }
}
