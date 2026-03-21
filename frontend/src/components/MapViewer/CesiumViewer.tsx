import { useRef, useCallback } from "react";
import { Viewer, Globe, CameraFlyTo } from "resium";
import {
  Cartesian3,
  Ion,
  SceneMode,
  Color,
  ScreenSpaceEventType,
  defined,
  Cesium3DTileset as Cesium3DTilesetClass,
  Model,
  Transforms,
  HeadingPitchRoll,
} from "cesium";
import type { CesiumComponentRef } from "resium";
import type { Viewer as CesiumViewer } from "cesium";
import { MAPO_CENTER, TILES_URL } from "../../utils/constants";
import { getBuildingDetail, getBuildings } from "../../api/client";
import { useAppStore } from "../../store/appStore";

Ion.defaultAccessToken =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI0ZjY0NjI1Mi01NWRiLTRmMjAtOTU2NS02YTcwMTk2NjZlNjEiLCJpZCI6MjU5LCJpYXQiOjE3MjY0MDk2OTZ9.demo";

// Free Cesium Ion token - replace with your own from https://ion.cesium.com/tokens
// For MVP, terrain and imagery will work without a valid token (fallback to ellipsoid)

export default function MapViewerComponent() {
  const viewerRef = useRef<CesiumComponentRef<CesiumViewer>>(null);
  const modelLoaded = useRef(false);
  const selectBuilding = useAppStore((s) => s.selectBuilding);

  const handleViewerReady = useCallback((viewer: CesiumViewer) => {
    // Load GLB model
    if (!modelLoaded.current) {
      modelLoaded.current = true;
      loadBuildingModel(viewer);
    }

    // Click handler for building selection
    viewer.screenSpaceEventHandler.setInputAction(
      async (event: { position: { x: number; y: number } }) => {
        const picked = viewer.scene.pick(event.position);
        if (defined(picked)) {
          // Try to get PNU from picked feature
          const pnu = picked?.id?.properties?.pnu?.getValue?.();
          if (pnu) {
            try {
              const detail = await getBuildingDetail(pnu);
              selectBuilding(pnu, detail);
            } catch {
              /* ignore */
            }
          }
        }
      },
      ScreenSpaceEventType.LEFT_CLICK
    );
  }, [selectBuilding]);

  return (
    <Viewer
      ref={viewerRef}
      full
      sceneMode={SceneMode.SCENE3D}
      animation={false}
      timeline={false}
      geocoder={false}
      homeButton={false}
      baseLayerPicker={false}
      navigationHelpButton={false}
      fullscreenButton={false}
      sceneModePicker={false}
      selectionIndicator={false}
      infoBox={false}
      onReady={handleViewerReady}
    >
      <Globe
        baseColor={Color.fromCssColorString("#e8e6e3")}
        enableLighting={false}
      />
      <CameraFlyTo
        destination={Cartesian3.fromDegrees(
          MAPO_CENTER.lng,
          MAPO_CENTER.lat,
          3000
        )}
        orientation={{
          heading: 0,
          pitch: -0.8,
          roll: 0,
        }}
        duration={0}
      />
    </Viewer>
  );
}

async function loadBuildingModel(viewer: CesiumViewer) {
  try {
    const position = Cartesian3.fromDegrees(MAPO_CENTER.lng, MAPO_CENTER.lat, 0);
    const modelMatrix = Transforms.headingPitchRollToFixedFrame(
      position,
      new HeadingPitchRoll(0, 0, 0)
    );

    const model = await Model.fromGltfAsync({
      url: TILES_URL,
      modelMatrix,
      scale: 1.0,
      minimumPixelSize: 64,
      maximumScale: 20000,
    });

    viewer.scene.primitives.add(model);
    console.log("Building GLB model loaded");
  } catch (e) {
    console.warn("Failed to load building model:", e);
  }
}
