from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models import Visit, Airport
from typing import Optional

router = APIRouter(prefix="/visits", tags=["visits"])

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

    # 🔎 filter
    if airport_id:
        query = query.filter(Visit.airport_id == airport_id)

    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    # 🔽 sort
    if sort_by == "date_visited":
        sort_column = Visit.date_visited
    else:
        sort_column = Visit.id

    if order == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())

    # 📄 pagination
    results = query.offset(skip).limit(limit).all()

    return [
        {
            "id": visit.id,
            "user_id": visit.user_id,
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

@router.get("/me")
def get_my_visits(
    airport_id: Optional[str] = None,
    city: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    sort_by: str = "date_visited",
    order: str = "desc",
    db: Session = Depends(get_db),
    user = Depends(get_current_user)  # 👈 AUTH
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)  
    )

    # ========================
    # FILTER
    # ========================
    if airport_id:
        query = query.filter(Visit.airport_id == airport_id)

    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    # ========================
    # SORT
    # ========================
    if sort_by == "date_visited":
        sort_column = Visit.date_visited
    elif sort_by == "created_at":
        sort_column = Visit.created_at
    else:
        sort_column = Visit.id

    if order == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())

    # ========================
    # PAGINATION
    # ========================
    limit = min(limit, 100)

    results = query.offset(skip).limit(limit).all()

    # ========================
    # RESPONSE
    # ========================
    return [
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