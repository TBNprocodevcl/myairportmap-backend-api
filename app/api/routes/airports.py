# app/api/routes/airports.py

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.db.session import get_db
from app.models.airport import Airport

router = APIRouter()

@router.get("/airports")
def get_airports(
    skip: int = 0,
    limit: int = 50,

    # 🔎 filter
    city: str | None = None,
    state: str | None = None,
    towered: str | None = None,

    # 🔍 search
    q: str | None = None,

    # 🗺 bounding box
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lng: float | None = None,
    max_lng: float | None = None,

    db: Session = Depends(get_db)
):
    query = db.query(Airport)

    # ========================
    # FILTER
    # ========================
    if city:
        query = query.filter(Airport.city == city)

    if state:
        query = query.filter(Airport.state == state)

    if towered:
        query = query.filter(Airport.towered_status == towered)

    # ========================
    # SEARCH
    # ========================
    if q:
        query = query.filter(
            or_(
                Airport.name.ilike(f"%{q}%"),
                Airport.city.ilike(f"%{q}%"),
                Airport.airport_id.ilike(f"%{q}%"),
            )
        )

    # ========================
    # MAP BOUNDING BOX
    # ========================
    if min_lat and max_lat and min_lng and max_lng:
        query = query.filter(
            Airport.latitude.between(min_lat, max_lat),
            Airport.longitude.between(min_lng, max_lng),
        )

    # ========================
    # PAGINATION
    # ========================
    airports = query.offset(skip).limit(limit).all()

    return airports