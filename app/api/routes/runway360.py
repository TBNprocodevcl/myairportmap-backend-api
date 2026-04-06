from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models.runway_landings import RunwayLanding
from app.models.user import User
from app.models.visit import Visit
from app.models.runway import Runway
from app.schemas.runway import Runway360SaveRequest
from app.schemas.user import UserResponse
from helper.response import success_response

router = APIRouter(prefix="/runway360", tags=["runway360"])


import re

def normalize_runway(ident: str | None):
    if not ident:
        return None

    ident = ident.strip().upper()

    # ❌ heliport
    if ident.startswith("H"):
        return None

    # ❌ N/S
    if ident in {"N", "S"}:
        return None

    # ✅ lấy số đầu (handle: 18L, 06R, 16W, 5)
    match = re.match(r"(\d{1,2})", ident)
    if not match:
        return None

    r = int(match.group(1))

    return r if 1 <= r <= 36 else None


def get_phase(percent: float) -> str:
    if percent == 100:
        return "COMPLETED"
    elif percent >= 70:
        return "APPROACH"
    elif percent >= 30:
        return "CRUISING"
    return "TAKE-OFF"


# @router.get("/")
# def get_runway360(user_id: str, db: Session = Depends(get_db)):

#     rows = (
#         db.query(
#             Visit.airport_id,          # 🔥 KPVD ở đây
#             Visit.date_visited,
#             Visit.notes,
#             Runway.le_ident,
#             Runway.he_ident
#         )
#         .join(Runway, Visit.airport_id == Runway.airport_ident)
#         .filter(Visit.user_id == user_id)
#         .order_by(Visit.date_visited.asc())
#         .all()
#     )

#     runway_map = {}
    
#     for airport_id, date_visited, note, le, he in rows:
#         for ident in [le, he]:
#             r = normalize_runway(ident)
#             if not r:
#                 continue

#             if r not in runway_map:
#                 runway_map[r] = {
#                     "runway": r,
#                     "airport_ident": airport_id,   # ✅ thêm KPVD
#                     "first_visited": date_visited,
#                     "airport_note": note
#                 }

#     total = 36
#     completed = len(runway_map)
#     percent = round(completed / total * 100, 1)
#     phase = get_phase(percent)

#     return success_response({
#         "total": total,
#         "completed": completed,
#         "percent": percent,
#         "phase": phase,
#         "runways": sorted(runway_map.values(), key=lambda x: x["runway"])
#     })
@router.get("/")
def get_runway360(user_id: str, db: Session = Depends(get_db)):

    rows = (
        db.query(RunwayLanding)
        .filter(RunwayLanding.user_id == user_id)
        .order_by(RunwayLanding.date.asc())
        .all()
    )

    runway_map = {i: None for i in range(1, 37)}

    for r in rows:
        if not r.runway_heading or not (1 <= r.runway_heading <= 36):
            continue

        if runway_map[r.runway_heading] is None:
            runway_map[r.runway_heading] = {
                "runway": r.runway_heading,
                "airport_ident": r.airport_id,
                "first_visited": r.date.isoformat() if r.date else None,
                "aircraft": r.aircraft,
                "notes": r.notes
            }

    total = 36
    completed = sum(1 for v in runway_map.values() if v)
    percent = round(completed / total * 100, 1)
    phase = get_phase(percent)

    return success_response({
        "total": total,
        "completed": completed,
        "percent": percent,
        "phase": phase,
        "runways": [r for r in runway_map.values() if r]  # ✅ không crash
    })

# @router.get("/club")
# def get_runway360_club(db: Session = Depends(get_db)):

#     rows = (
#         db.query(
#             Visit.user_id,
#             Runway.le_ident,
#             Runway.he_ident
#         )
#         .join(Runway, Visit.airport_id == Runway.airport_ident)
#         .all()
#     )

#     user_runways = defaultdict(set)
#     unique_runways = set()   # ✅ log global luôn

#     for user_id, le, he in rows:
#         for ident in [le, he]:
#             r = normalize_runway(ident)
#             if r:
#                 user_runways[user_id].add(r)
#                 unique_runways.add(r)   # ✅ log ở đây

#     # 🔥 DEBUG LOG
#     print("UNIQUE RUNWAYS:", len(unique_runways))
#     print("RUNWAYS LIST:", sorted(unique_runways))
#     for user_id, le, he in rows:
#         for ident in [le, he]:
#             r = normalize_runway(ident)
#             if r:
#                 user_runways[user_id].add(r)

#     club_users = []

#     for user_id, runways in user_runways.items():
#         if len(runways) == 36:
#             club_users.append({
#                 "user_id": user_id,
#                 "total_runways": 36
#             })
    
#     return success_response({
#         "club_size": len(club_users),
#         "users": club_users
#     })

@router.get("/club")
def get_runway360_club(db: Session = Depends(get_db)):

    rows = (
        db.query(
            User,
            func.count(RunwayLanding.runway_heading.distinct()).label("total"),
            func.max(RunwayLanding.date).label("completed_at")
        )
        .join(RunwayLanding, RunwayLanding.user_id == User.id)
        .group_by(User.id)
        .having(func.count(RunwayLanding.runway_heading.distinct()) == 36)
        .all()
    )

    data = []

    for user, total, completed_at in rows:
        u = UserResponse.model_validate(user).model_dump()

        u["total_runways"] = total
        u["completed_at"] = completed_at

        data.append(u)

    return success_response({
        "club_size": len(data),
        "users": data
    })

@router.get("/manage")
def get_runway360_manage(
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    rows = db.query(RunwayLanding).filter(
        RunwayLanding.user_id == user.id
    ).all()

    # luôn có đủ 36 runway
    result = {i: None for i in range(1, 37)}

    for r in rows:
        # 🛡️ tránh dữ liệu bẩn
        if not r.runway_heading or not (1 <= r.runway_heading <= 36):
            continue

        result[r.runway_heading] = {
            "runway": r.runway_heading,   # ⭐ thêm cái này cho FE
            "airport_id": r.airport_id,
            "date": r.date.isoformat() if r.date else None,  # ⭐ tránh lỗi JSON
            "aircraft": r.aircraft,
            "notes": r.notes
        }

    completed = sum(1 for v in result.values() if v)

    return success_response({
        "total": 36,   # ⭐ thêm cho rõ
        "completed": completed,
        "progress": f"{completed}/36",
        "percent": round(completed / 36 * 100, 1),
        "data": result
    })

# @router.post("/manage")
# def save_runway360(
#     payload: Runway360SaveRequest,
#     db: Session = Depends(get_db),
#     user=Depends(get_current_user)
# ):
#     for heading, value in payload.data.items():

#         # nếu bạn dùng Dict[str,...] thì cần convert
#         heading = int(heading)

#         existing = db.query(RunwayLanding).filter(
#             RunwayLanding.user_id == user.id,
#             RunwayLanding.runway_heading == heading
#         ).first()

#         # ❌ user xóa ô
#         if value is None:
#             if existing:
#                 db.delete(existing)
#             continue

#         # insert nếu chưa có
#         if not existing:
#             existing = RunwayLanding(
#                 user_id=user.id,
#                 runway_heading=heading
#             )
#             db.add(existing)

#         # update
#         existing.airport_id = value.airport_id
#         existing.date = value.date
#         existing.aircraft = value.aircraft
#         existing.notes = value.notes

#     db.commit()

#     # 🔥 tính lại progress
#     completed = db.query(RunwayLanding).filter(
#         RunwayLanding.user_id == user.id
#     ).count()

#     total = 36
#     percent = round(completed / total * 100, 1)

#     return success_response(
#         data={
#             "completed": completed,
#             "total": total,
#             "percent": percent
#         },
#         message="Saved successfully"
#     )

@router.post("/manage")
def save_runway360(
    payload: Runway360SaveRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # =========================
    # 1. Load tất cả data hiện tại (1 query)
    # =========================
    existing_rows = db.query(RunwayLanding).filter(
        RunwayLanding.user_id == user.id
    ).all()

    existing_map = {
        r.runway_heading: r for r in existing_rows
    }

    # =========================
    # 2. Helper detect empty
    # =========================
    def is_empty(v):
        return (
            not v
            or (
                not v.airport_id
                and not v.date
                and not v.aircraft
                and not v.notes
            )
        )

    # =========================
    # 3. Loop payload (UPSERT + DELETE)
    # =========================
    payload_headings = set()

    for heading, value in payload.data.items():
        try:
            heading = int(heading)
        except:
            continue

        if not (1 <= heading <= 36):
            continue

        payload_headings.add(heading)

        existing = existing_map.get(heading)

        # ❌ DELETE
        if is_empty(value):
            if existing:
                db.delete(existing)
            continue

        # ➕ INSERT
        if not existing:
            existing = RunwayLanding(
                user_id=user.id,
                runway_heading=heading
            )
            db.add(existing)

        # ✏️ UPDATE
        existing.airport_id = value.airport_id
        existing.date = value.date
        existing.aircraft = value.aircraft
        existing.notes = value.notes

    # =========================
    # 4. DELETE những cái KHÔNG có trong payload
    # =========================
    for heading, row in existing_map.items():
        if heading not in payload_headings:
            db.delete(row)

    db.commit()

    # =========================
    # 5. Progress
    # =========================
    completed = db.query(RunwayLanding).filter(
        RunwayLanding.user_id == user.id
    ).count()

    total = 36

    return success_response(
        data={
            "completed": completed,
            "total": total,
            "percent": round(completed / total * 100, 1)
        },
        message="Saved successfully"
    )