# ADR-001: VWorld WebGL 3D API 채택 (Google Photorealistic 3D Tiles 대신)

**상태**: 확정
**결정일**: 2026-03-21
**결정자**: 프로젝트 팀

---

## 결정

3D 건물 렌더링에 **VWorld WebGL 3D API 3.0**을 사용한다. Google Photorealistic 3D Tiles 및 Cesium OSM Buildings는 사용하지 않는다.

## 근거

| 옵션 | 검토 결과 |
|------|----------|
| Google Photorealistic 3D Tiles | ❌ 한국 미지원 확인 (2026-03) |
| Cesium OSM Buildings (Ion 96188) | ❌ 건물 높이 부정확, 텍스처 없음 |
| VWorld WebGL 3D API 3.0 | ✅ 서울 LoD3-4 사진 텍스처, 정부 공식 데이터 |

- `ws3d.viewer`가 내부적으로 `Cesium.Viewer`이므로 Cesium API 직접 사용 가능
- 한국 정부 공식 공간정보 플랫폼 → 지속성 보장

## 영향 파일

- `frontend/vworld.html` — VWorld SDK 직접 사용
- `docs/ARCHITECTURE.md §2.1` — 컴포넌트 다이어그램
- `CLAUDE.md 주요 기술 결정 사항 #1`

## 변경 조건

VWorld API 정책 변경 또는 서비스 종료 시 재검토. 대안: Cesium Ion 자체 3D Tiles 생성.
