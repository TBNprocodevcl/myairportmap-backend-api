from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.db.session import get_db
from app.models.airport import Airport

router = APIRouter(prefix="/airports", tags=["airports"])


# ✅ reusable response
def success_response(data, message: str = "Success"):
    return {
        "success": True,
        "data": data,
        "message": message
    }


@router.get("/")
def get_airports(
    skip: int = 0,
    limit: int = 50,
    city: str | None = None,
    state: str | None = None,
    towered: str | None = None,
    q: str | None = None,
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lng: float | None = None,
    max_lng: float | None = None,
    db: Session = Depends(get_db)
):
    query = db.query(Airport)

    # =========================
    # 🔎 FILTER
    # =========================
    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    if state:
        query = query.filter(Airport.state.ilike(f"%{state}%"))

    if towered:
        query = query.filter(Airport.towered_status.ilike(f"%{towered}%"))

    # =========================
    # 🔍 SEARCH
    # =========================
    if q:
        query = query.filter(
            or_(
                Airport.name.ilike(f"%{q}%"),
                Airport.city.ilike(f"%{q}%"),
                Airport.airport_id.ilike(f"%{q}%"),
            )
        )

    # =========================
    # 🗺️ BOUNDING BOX
    # =========================
    if (
        min_lat is not None and
        max_lat is not None and
        min_lng is not None and
        max_lng is not None
    ):
        query = query.filter(
            Airport.latitude.between(min_lat, max_lat),
            Airport.longitude.between(min_lng, max_lng),
        )

    # =========================
    # 📄 PAGINATION
    # =========================
    limit = min(limit, 200)
    results = query.offset(skip).limit(limit).all()

    # =========================
    # 🎯 MAP DATA (quan trọng)
    # =========================
    data = [
        {
            "airport_id": a.airport_id,
            "name": a.name,
            "city": a.city,
            "state": a.state,
            "lat": a.latitude,
            "lng": a.longitude,
            "towered_status": a.towered_status,
        }
        for a in results
    ]

    return success_response(data)