"""
3D tile generation using trimesh.

Reads building footprints and heights from PostGIS, extrudes them into
3D meshes colored by energy grade, and exports a combined GLB file.
"""

import logging
import os
from pathlib import Path

import numpy as np
import trimesh
from shapely import wkb
from shapely.geometry import Polygon
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Energy grade color mapping (RGBA 0-255)
# Grades run from 1+++ (best, deep green) to 7 (worst, red).
# Unknown grades are rendered in neutral gray.
# ---------------------------------------------------------------------------

_GRADE_COLORS: dict[str, list[int]] = {
    "1+++": [0, 100, 0, 255],       # dark green
    "1++":  [0, 140, 0, 255],       # green
    "1+":   [0, 180, 0, 255],       # medium green
    "1":    [50, 205, 50, 255],      # lime green
    "2":    [154, 205, 50, 255],     # yellow-green
    "3":    [255, 255, 0, 255],      # yellow
    "4":    [255, 200, 0, 255],      # amber
    "5":    [255, 140, 0, 255],      # orange
    "6":    [255, 69, 0, 255],       # orange-red
    "7":    [200, 0, 0, 255],        # red
}

_UNKNOWN_COLOR: list[int] = [160, 160, 160, 255]  # gray


def energy_grade_to_color(grade: str | None) -> list[int]:
    """Map an energy grade string to an RGBA color list.

    Parameters
    ----------
    grade:
        Energy grade such as ``"1+++"`` through ``"7"``, or ``None``.

    Returns
    -------
    list[int]
        Four-element RGBA list with values in 0-255.
    """
    if grade is None:
        return list(_UNKNOWN_COLOR)
    return list(_GRADE_COLORS.get(grade, _UNKNOWN_COLOR))


def generate_buildings_glb(
    db_url: str,
    output_dir: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> int:
    """Generate a GLB file containing extruded 3D buildings.

    Parameters
    ----------
    db_url:
        SQLAlchemy database URL pointing to a PostGIS database.
    output_dir:
        Directory where the output ``buildings.glb`` will be written.
    bbox:
        Optional bounding box ``(west, south, east, north)`` to limit
        the spatial extent of queried buildings.

    Returns
    -------
    int
        Number of buildings successfully processed and included in the GLB.
    """
    engine = create_engine(db_url, pool_pre_ping=True)

    conditions: list[str] = []
    params: dict = {}

    if bbox is not None:
        west, south, east, north = bbox
        conditions.append(
            "ST_Intersects(geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    sql = text(f"""
        SELECT
            pnu,
            ST_AsBinary(geom) AS geom_wkb,
            height,
            energy_grade
        FROM buildings_enriched
        {where_clause}
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    logger.info("Queried %d buildings from database", len(rows))

    meshes: list[trimesh.Trimesh] = []
    processed = 0

    for row in rows:
        try:
            geom_wkb_data = row.geom_wkb
            height = float(row.height) if row.height else 3.0  # default 3m
            grade = row.energy_grade

            # Parse WKB to shapely geometry
            shape = wkb.loads(bytes(geom_wkb_data))

            # Handle MultiPolygon — take the largest polygon
            if shape.geom_type == "MultiPolygon":
                shape = max(shape.geoms, key=lambda g: g.area)

            if not isinstance(shape, Polygon) or shape.is_empty:
                continue

            # Ensure minimum height
            if height <= 0:
                height = 3.0

            # Extrude the polygon to create a 3D mesh
            mesh = trimesh.creation.extrude_polygon(shape, height)

            # Assign face colors based on energy grade
            color = energy_grade_to_color(grade)
            face_colors = np.tile(color, (len(mesh.faces), 1)).astype(np.uint8)
            mesh.visual.face_colors = face_colors

            meshes.append(mesh)
            processed += 1

        except Exception:
            logger.warning(
                "Failed to process building pnu=%s", row.pnu, exc_info=True
            )
            continue

    if not meshes:
        logger.warning("No meshes generated — GLB file will not be created")
        return 0

    # Combine all meshes into a scene and export
    scene = trimesh.Scene(meshes)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    glb_file = output_path / "buildings.glb"

    scene.export(str(glb_file), file_type="glb")
    file_size_mb = os.path.getsize(glb_file) / (1024 * 1024)
    logger.info(
        "Exported %d buildings to %s (%.1f MB)",
        processed, glb_file, file_size_mb,
    )

    return processed
