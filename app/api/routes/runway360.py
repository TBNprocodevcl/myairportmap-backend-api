from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.visit import Visit
from app.models.runway import Runway
from helper.response import success_response

router = APIRouter(prefix="/runway360", tags=["runway360"])


def normalize_runway(ident: str | None):
    if not ident or ident.startswith("H"):
        return None

    num = ""
    for c in ident:
        if c.isdigit():
            num += c
        else:
            break

    if not num:
        return None

    r = int(num)
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