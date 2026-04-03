import os

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import UUID, func
from starlette.responses import FileResponse

from app.constant.bussiness import CLASS_B_AIRPORTS
from app.constant.achievement_state import STATE_AIRPORTS

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

def normalize(code: str):
    if len(code) == 3:
        return f"K{code}"
    return code

def get_user_visited_airports(db: Session, user_id: str) -> set[str]:
    rows = (
        db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .distinct()
        .all()
    )
    return {normalize(r[0]) for r in rows}

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

def build_class_b_achievement(db: Session, user_id: str):
    visited_set = get_user_visited_airports(db, user_id)

    airports = db.query(Airport.airport_id, Airport.name).all()

    airport_map = {
        normalize(a.airport_id): a.name
        for a in airports
    }

    checklist = []
    visited_count = 0

    for airport_id in CLASS_B_AIRPORTS:
        is_visited = airport_id in visited_set

        if is_visited:
            visited_count += 1

        checklist.append({
            "id": airport_id,
            "name": airport_map.get(airport_id),
            "visited": is_visited
        })

    total = len(CLASS_B_AIRPORTS)
    percent = round((visited_count / total) * 100, 1)

    checklist.sort(key=lambda x: not x["visited"])

    phase = get_phase(percent)
    icon = get_icon(phase)

    return {
        "title": "BRAVO",
        "code": "CLASS_B",
        "total": total,
        "visited": visited_count,
        "percent": percent,
        "phase": phase,
        "icon": icon,
        "completed": visited_count == total,
        "checklist": checklist
    }
# def build_state_progress(db: Session, user_id: str):
#     total_by_state = dict(
#         db.query(
#             Airport.state,
#             func.count(Airport.airport_id)
#         )
#         .group_by(Airport.state)
#         .all()
#     )

#     visited_by_state = dict(
#         db.query(
#             Airport.state,
#             func.count(func.distinct(Visit.airport_id))
#         )
#         .join(Airport, Visit.airport_id == Airport.airport_id)
#         .filter(Visit.user_id == user_id)
#         .group_by(Airport.state)
#         .all()
#     )

#     result = []

#     for st, total in total_by_state.items():
#         visited = visited_by_state.get(st, 0)
#         pct = (visited / total * 100) if total else 0
#         ph = get_phase(pct)

#         result.append({
#             "state": st,
#             "visited": visited,
#             "total": total,
#             "percentage": round(pct, 1),
#             "phase": ph,
#             "icon": get_icon(ph)
#         })

#     # CONUS
#     total_all = sum(total_by_state.values())
#     visited_all = sum(visited_by_state.values())
#     pct_all = (visited_all / total_all * 100) if total_all else 0
#     ph_all = get_phase(pct_all)

#     result.append({
#         "state": "CONUS",
#         "visited": visited_all,
#         "total": total_all,
#         "percentage": round(pct_all, 1),
#         "phase": ph_all,
#         "icon": get_icon(ph_all)
#     })

#     return result
def build_state_progress(db: Session, user_id: str):

    # ✅ lấy toàn bộ airport user đã visit
    visited_airports = set(
        r[0] for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .all()
    )

    result = []

    total_all = 0
    visited_all = 0

    # ✅ loop qua constant (NOT DB)
    for state, airport_ids in STATE_AIRPORTS.items():
        total = len(airport_ids)

        # đếm visited trong state
        visited = sum(1 for aid in airport_ids if aid in visited_airports)

        pct = (visited / total * 100) if total else 0
        ph = get_phase(pct)

        result.append({
            "state": state,
            "visited": visited,
            "total": total,
            "percentage": round(pct, 1),
            "phase": ph,
            "icon": get_icon(ph)
        })

        total_all += total
        visited_all += visited

    # ✅ CONUS
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

    return result

@router.get("/class-b")
def get_class_b_achievement(
    user_id: str,
    db: Session = Depends(get_db),
):
    result = build_class_b_achievement(db, user_id)
    return success_response(result)


@router.get("/class-b/me")
def get_my_class_b_achievement(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    result = build_class_b_achievement(db, user.id)
    return success_response(result)

# =========================
# 🏆 ALL ACHIEVEMENTS (future-ready)
# =========================
@router.get("/")
def get_all_achievements(
    user_id: str,
    db: Session = Depends(get_db),
):

    visited_set = get_user_visited_airports(db, user_id)

    # 👉 Class B
    class_b_total = len(CLASS_B_AIRPORTS)
    class_b_visited = sum(1 for a in CLASS_B_AIRPORTS if a in visited_set)

    class_b_percent = round((class_b_visited / class_b_total) * 100, 1)

    achievements = [
        {
            "code": "CLASS_B",
            "title": "BRAVO",
            "total": class_b_total,
            "visited": class_b_visited,
            "percent": class_b_percent,
            "completed": class_b_visited == class_b_total
        }
    ]

    return success_response(achievements)


@router.get("/states")
def get_state_progress(
    user_id: str,
    state: str | None = Query(None),
    phase: str | None = Query(None),
    db: Session = Depends(get_db),
):
    result = build_state_progress(db, user_id)

    if state:
        result = [r for r in result if r["state"].lower() == state.lower()]

    if phase:
        result = [r for r in result if r["phase"].lower() == phase.lower()]

    result.sort(key=lambda x: x["percentage"], reverse=True)

    return success_response(result)


@router.get("/states/me")
def get_my_state_progress(
    state: str | None = Query(None),
    phase: str | None = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    result = build_state_progress(db, user.id)

    if state:
        result = [r for r in result if r["state"].lower() == state.lower()]

    if phase:
        result = [r for r in result if r["phase"].lower() == phase.lower()]

    result.sort(key=lambda x: x["percentage"], reverse=True)

    return success_response(result)


@router.get("/states/checklist")
def get_state_checklist(
    user_id: str,
    state: str,
    db: Session = Depends(get_db),
):
    state = state.upper()

    airport_ids = STATE_AIRPORTS.get(state)
    if not airport_ids:
        return success_response([], message="State not found")

    visited = set(
        r[0] for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .all()
    )

    # ✅ lấy airport info
    airports = db.query(Airport).filter(Airport.airport_id.in_(airport_ids)).all()
    airport_map = {a.airport_id: a for a in airports}

    result = []
    visited_count = 0

    for aid in airport_ids:
        is_visited = aid in visited

        if is_visited:
            visited_count += 1

        a = airport_map.get(aid)

        result.append({
            "airport_id": aid,
            "name": a.name if a else None,
            "visited": is_visited
        })

    total = len(result)
    percentage = (visited_count / total * 100) if total else 0
    completed = visited_count == total

    return success_response({
        "state": state,
        "visited": visited_count,
        "total": total,
        "percentage": round(percentage, 1),
        "completed": completed,
        "airports": result
    })

@router.get("/states/me/badge")
def download_state_badge(
    state: str,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):

    state = state.upper()

    airport_ids = STATE_AIRPORTS.get(state)
    if not airport_ids:
        return success_response(None, message="State not found")

    # ✅ lấy visited
    visited_airports = set(
        r[0] for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user.id)
        .all()
    )

    visited = sum(1 for aid in airport_ids if aid in visited_airports)
    total = len(airport_ids)

    # ❌ chưa complete → block
    if visited < total:
        return success_response(None, message="State not completed")

    # ✅ path image
    file_path = f"static/base/{state}.png"

    if not os.path.exists(file_path):
        return success_response(None, message="Badge not found")

    return FileResponse(
        file_path,
        media_type="image/png",
        filename=f"{state}.png"
    )