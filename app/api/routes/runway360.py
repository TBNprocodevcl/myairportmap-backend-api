from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.visit import Visit
from app.models.runway import Runway
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


@router.get("/")
def get_runway360(user_id: str, db: Session = Depends(get_db)):

    rows = (
        db.query(
            Visit.airport_id,          # 🔥 KPVD ở đây
            Visit.date_visited,
            Visit.notes,
            Runway.le_ident,
            Runway.he_ident
        )
        .join(Runway, Visit.airport_id == Runway.airport_ident)
        .filter(Visit.user_id == user_id)
        .order_by(Visit.date_visited.asc())
        .all()
    )

    runway_map = {}
    
    for airport_id, date_visited, note, le, he in rows:
        for ident in [le, he]:
            r = normalize_runway(ident)
            if not r:
                continue

            if r not in runway_map:
                runway_map[r] = {
                    "runway": r,
                    "airport_ident": airport_id,   # ✅ thêm KPVD
                    "first_visited": date_visited,
                    "airport_note": note
                }

    total = 36
    completed = len(runway_map)
    percent = round(completed / total * 100, 1)
    phase = get_phase(percent)

    return success_response({
        "total": total,
        "completed": completed,
        "percent": percent,
        "phase": phase,
        "runways": sorted(runway_map.values(), key=lambda x: x["runway"])
    })

@router.get("/club")
def get_runway360_club(db: Session = Depends(get_db)):

    rows = (
        db.query(
            Visit.user_id,
            Runway.le_ident,
            Runway.he_ident
        )
        .join(Runway, Visit.airport_id == Runway.airport_ident)
        .all()
    )

    user_runways = defaultdict(set)
    unique_runways = set()   # ✅ log global luôn

    for user_id, le, he in rows:
        for ident in [le, he]:
            r = normalize_runway(ident)
            if r:
                user_runways[user_id].add(r)
                unique_runways.add(r)   # ✅ log ở đây

    # 🔥 DEBUG LOG
    print("UNIQUE RUNWAYS:", len(unique_runways))
    print("RUNWAYS LIST:", sorted(unique_runways))
    for user_id, le, he in rows:
        for ident in [le, he]:
            r = normalize_runway(ident)
            if r:
                user_runways[user_id].add(r)

    club_users = []

    for user_id, runways in user_runways.items():
        if len(runways) == 36:
            club_users.append({
                "user_id": user_id,
                "total_runways": 36
            })
    
    return success_response({
        "club_size": len(club_users),
        "users": club_users
    })