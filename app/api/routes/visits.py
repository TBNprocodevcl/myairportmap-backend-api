from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List

from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models import Visit, Airport

# ✅ IMPORT SCHEMA
from app.schemas.visit import VisitedAirportResponse
from helper.response import success_response

router = APIRouter(prefix="/visits", tags=["visits"])


# =========================================================
# 🧾 RAW VISITS (giữ lại nhưng clean hơn)
# =========================================================
@router.get("/")
def get_visits(
    user_id: str,
    airport_id: Optional[str] = None,
    city: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    sort_by: str = "date_visited",
    order: str = "desc",
    db: Session = Depends(get_db)
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user_id)
    )

    if airport_id:
        query = query.filter(Visit.airport_id == airport_id)

    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    # sort
    sort_column = Visit.date_visited if sort_by == "date_visited" else Visit.id
    query = query.order_by(sort_column.desc() if order == "desc" else sort_column.asc())

    results = query.offset(skip).limit(limit).all()

    data = [
        {
            "id": visit.id,
            "airport": {
                "airport_id": airport.airport_id,
                "name": airport.name,
                "city": airport.city,
                "state": airport.state,
            },
            "date_visited": visit.date_visited,
            "callsign": visit.callsign,
            "notes": visit.notes,
        }
        for visit, airport in results
    ]

    return success_response(data)


# =========================================================
# 🧾 MY RAW VISITS (AUTH)
# =========================================================
@router.get("/me")
def get_my_visits(
    airport_id: Optional[str] = None,
    city: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    sort_by: str = "date_visited",
    order: str = "desc",
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)
    )

    if airport_id:
        query = query.filter(Visit.airport_id == airport_id)

    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    # safe sort
    if sort_by == "date_visited":
        sort_column = Visit.date_visited
    elif sort_by == "created_at" and hasattr(Visit, "created_at"):
        sort_column = Visit.created_at
    else:
        sort_column = Visit.id

    query = query.order_by(sort_column.desc() if order == "desc" else sort_column.asc())

    limit = min(limit, 100)
    results = query.offset(skip).limit(limit).all()

    data = [
        {
            "id": visit.id,
            "airport": {
                "airport_id": airport.airport_id,
                "name": airport.name,
                "city": airport.city,
                "state": airport.state,
            },
            "date_visited": visit.date_visited,
            "callsign": visit.callsign,
            "notes": visit.notes,
        }
        for visit, airport in results
    ]

    return success_response(data)



# =========================================================
# 🗺️ VISITED AIRPORTS (🔥 MAIN API)
# =========================================================
@router.get("/me/airports")
def get_my_visited_airports(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    rows = (
        db.query(
            Visit.airport_id.label("id"),
            Airport.name,
            Airport.latitude,
            Airport.longitude,
            Airport.state,
            Airport.towered_status,
            func.count(Visit.id).label("visitCount"),
            func.max(Visit.date_visited).label("last_visited"),
            func.max(Visit.notes).label("notes"),
            func.max(Visit.callsign).label("airCraft"),
        )
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)
        .group_by(
            Visit.airport_id,
            Airport.name,
            Airport.latitude,
            Airport.longitude,
            Airport.state,
            Airport.towered_status,
        )
        .all()
    )

    data = [
        {
            "id": r.id,
            "name": r.name,
            "lat": r.latitude,
            "lng": r.longitude,
            "state": r.state,
            "status": r.towered_status,
            "visitCount": r.visitCount,
            "last_visited": r.last_visited,
            "notes": r.notes,
            "airCraft": r.airCraft,
        }
        for r in rows
    ]

    return success_response(data)