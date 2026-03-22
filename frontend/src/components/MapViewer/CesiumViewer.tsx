import { useRef, useEffect } from "react";
import * as Cesium from "cesium";
import { MAPO_CENTER } from "../../utils/constants";
import { pickBuilding, getBuildingDetail } from "../../api/client";
import { useAppStore } from "../../store/appStore";

Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_ION_TOKEN ?? "";

export default function CesiumViewerComponent() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
      // Performance: no shadows, render only on change
      shadows: false,
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity,
    });

    viewerRef.current = viewer;
    viewer.scene.globe.depthTestAgainstTerrain = true;

    // Cesium World Terrain
    viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain());

    // Load OSM Buildings (3D shapes + shader)
    loadOSMBuildings(viewer);

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

    // Click handler — server-side pick (no client-side centroid loading)
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        if (viewer.isDestroyed()) return;

        // Cancel previous request
        abortRef.current?.abort();
        const controller = new AbortController();
        abortRef.current = controller;

        // Get click position
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
          // Server-side nearest building lookup
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

    // WebGL context loss handler
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

// --- OSM Buildings with shader ---
async function loadOSMBuildings(viewer: Cesium.Viewer) {
  try {
    const tileset = await Cesium.Cesium3DTileset.fromIonAssetId(96188, {
      maximumScreenSpaceError: 16,
      maximumMemoryUsage: 256, // MB
    });
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
            float isWindow = step(0.4, horizFrac) * (1.0 - step(2.0, horizFrac))
                           * step(0.2, vertFrac) * (1.0 - step(0.75, vertFrac));
            vec3 wallBase = material.diffuse * 0.75;
            float rnd = hash(floor(posWC.xy * 0.5));
            vec3 glassColor = mix(vec3(0.12, 0.18, 0.28), vec3(0.25, 0.35, 0.50), rnd * 0.5 + 0.25);
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
