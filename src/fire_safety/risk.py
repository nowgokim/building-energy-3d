"""
화재 위험도 API 라우터 (Phase F0).

buildings_enriched 뷰의 구조/연령/용도/층수 데이터만으로
건물별 화재 위험 점수(0-100)를 제공한다. 추가 데이터 수집 불필요.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.shared.database import get_db_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fire", tags=["fire"])


@router.get("/risk/{pnu}")
def get_fire_risk(
    pnu: str,
    db: Session = Depends(get_db_dependency),
) -> dict:
    """단일 건물 화재 위험도 조회."""
    if not re.match(r"^\d{19,25}$", pnu):
        raise HTTPException(status_code=400, detail="Invalid PNU format")

    sql = text("""
        SELECT
            r.pnu,
            r.structure_score,
            r.age_score,
            r.usage_score,
            r.height_score,
            r.total_score,
            r.risk_grade,
            b.structure_type,
            b.built_year,
            b.usage_type,
            b.floors_above
        FROM building_fire_risk r
        JOIN buildings_enriched b ON r.pnu = b.pnu
        WHERE r.pnu = :pnu
        LIMIT 1
    """)
    row = db.execute(sql, {"pnu": pnu}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Building {pnu} not found")

    return {
        "pnu": row.pnu,
        "total_score": int(row.total_score),
        "risk_grade": row.risk_grade,
        "breakdown": {
            "structure": {"score": int(row.structure_score), "value": row.structure_type},
            "age":       {"score": int(row.age_score),       "value": row.built_year},
            "usage":     {"score": int(row.usage_score),     "value": row.usage_type},
            "height":    {"score": int(row.height_score),    "value": row.floors_above},
        },
    }


@router.get("/risk")
def list_fire_risk(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    grade: Optional[str]   = Query(None, description="HIGH | MEDIUM | LOW"),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 건물 화재 위험도 목록 (히트맵용, 최대 5000건)."""
    conditions: list[str] = []
    params: dict = {}

    if all(v is not None for v in [west, south, east, north]):
        conditions.append(
            "ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params.update({"west": west, "south": south, "east": east, "north": north})

    if grade:
        conditions.append("r.risk_grade = :grade")
        params["grade"] = grade.upper()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = text(f"""
        SELECT
            r.pnu,
            r.total_score,
            r.risk_grade,
            ST_X(ST_Centroid(b.geom)) AS lng,
            ST_Y(ST_Centroid(b.geom)) AS lat
        FROM building_fire_risk r
        JOIN buildings_enriched b ON r.pnu = b.pnu
        {where}
        ORDER BY r.total_score DESC
        LIMIT 5000
    """)
    rows = db.execute(sql, params).fetchall()
    logger.info("Fire risk list: %d buildings", len(rows))

    return {
        "count": len(rows),
        "features": [
            {
                "pnu":   r.pnu,
                "score": int(r.total_score),
                "grade": r.risk_grade,
                "lng":   float(r.lng),
                "lat":   float(r.lat),
            }
            for r in rows
        ],
    }


@router.get("/stats")
def get_fire_stats(
    west:  Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east:  Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    db: Session = Depends(get_db_dependency),
) -> dict:
    """뷰포트 내 화재 위험도 등급별 통계."""
    params: dict = {}
    join_clause = ""

    if all(v is not None for v in [west, south, east, north]):
        join_clause = (
            "JOIN buildings_enriched b ON r.pnu = b.pnu "
            "WHERE ST_Intersects(b.geom, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
        )
        params = {"west": west, "south": south, "east": east, "north": north}

    sql = text(f"""
        SELECT
            r.risk_grade,
            COUNT(*) AS cnt,
            ROUND(AVG(r.total_score)::numeric, 1) AS avg_score
        FROM building_fire_risk r
        {join_clause}
        GROUP BY r.risk_grade
        ORDER BY r.risk_grade
    """)
    rows = db.execute(sql, params).fetchall()

    return {
        "grade_distribution": {
            r.risk_grade: {"count": int(r.cnt), "avg_score": float(r.avg_score)}
            for r in rows
        }
    }
