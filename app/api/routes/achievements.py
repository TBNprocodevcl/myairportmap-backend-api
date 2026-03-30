from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.session import get_db
from app.models.visit import Visit
from app.models.airport import Airport
from app.api.routes.auth import get_current_user

router = APIRouter(prefix="/achievements", tags=["achievements"])


def success_response(data, message="Success"):
    return {
        "success": True,
        "data": data,
        "message": message
    }


def get_phase(pct: float):
    if pct >= 100:
        return "COMPLETED"
    elif pct >= 70:
        return "APPROACH"
    elif pct >= 20:
        return "CRUISING"
    else:
        return "TAKE-OFF"


def get_icon(phase):
    return {
        "COMPLETED": "🏆",
        "APPROACH": "↘️",
        "CRUISING": "✈️",
        "TAKE-OFF": "🛫"
    }.get(phase, "")


@router.get("/states")
def get_state_progress(
    state: str | None = Query(None),
    phase: str | None = Query(None),

    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    # =========================
    # 🧮 TOTAL airports per state
    # =========================
    total_by_state = dict(
        db.query(
            Airport.state,
            func.count(Airport.airport_id)
        )
        .group_by(Airport.state)
        .all()
    )

    # =========================
    # 🧮 VISITED airports per state
    # =========================
    visited_by_state = dict(
        db.query(
            Airport.state,
            func.count(func.distinct(Visit.airport_id))
        )
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)
        .group_by(Airport.state)
        .all()
    )

    # =========================
    # 🎯 BUILD DATA
    # =========================
    result = []

    for st, total in total_by_state.items():
        visited = visited_by_state.get(st, 0)
        pct = (visited / total * 100) if total else 0
        ph = get_phase(pct)

        item = {
            "state": st,
            "visited": visited,
            "total": total,
            "percentage": round(pct, 1),
            "phase": ph,
            "icon": get_icon(ph)
        }

        result.append(item)

    # =========================
    # 🌎 CONUS
    # =========================
    total_all = sum(total_by_state.values())
    visited_all = sum(visited_by_state.values())
    pct_all = (visited_all / total_all * 100) if total_all else 0
    ph_all = get_phase(pct_all)

    result.append({
        "state": "CONUS",
        "visited": visited_all,
        "total": total_all,
        "percentage": round(pct_all, 1),
        "phase": ph_all,
        "icon": get_icon(ph_all)
    })

    # =========================
    # 🔎 FILTER
    # =========================
    if state:
        result = [r for r in result if r["state"].lower() == state.lower()]

    if phase:
        result = [r for r in result if r["phase"].lower() == phase.lower()]

    # =========================
    # 🔽 SORT
    # =========================
    result.sort(key=lambda x: x["percentage"], reverse=True)

    return success_response(result)