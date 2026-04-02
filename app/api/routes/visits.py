from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func
from typing import Optional, List

from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models import Visit, Airport, airport

# ✅ IMPORT SCHEMA
from app.schemas.visit import FlightCreateRequest, VisitedAirportResponse
from helper.response import success_response

from fastapi import HTTPException


router = APIRouter(prefix="/visits", tags=["visits"])
def serialize_airport(r):
    return {
        "id": r.id,
        "name": r.name,
        "city": r.city,
        "state": r.state,

        "lat": r.latitude,
        "lng": r.longitude,
        "status": r.towered_status,

        "visitCount": r.visitCount or 0,
        "isVisited": (r.visitCount or 0) > 0,

        "first_visited": r.first_visited,
        "last_visited": r.last_visited,

        "callsign": r.callsign,
        "notes": r.notes,
    }
def parse_bbox(bbox: str):
    try:
        min_lng, min_lat, max_lng, max_lat = map(float, bbox.split(","))
        return min_lng, min_lat, max_lng, max_lat
    except:
        return None
    
def build_airport_query(db: Session, user_id: str):
    first_visit = (
        db.query(
            Visit.airport_id,
            func.min(Visit.date_visited).label("first_date")
        )
        .filter(Visit.user_id == user_id)
        .group_by(Visit.airport_id)
        .subquery()
    )
    
    first_visit_detail = aliased(Visit)

    return (
        db.query(
            Airport.airport_id.label("id"),
            Airport.name,
            Airport.city,
            Airport.state,
            Airport.latitude,
            Airport.longitude,
            Airport.towered_status,

            func.count(Visit.id).label("visitCount"),
            func.min(Visit.date_visited).label("first_visited"),
            func.max(Visit.date_visited).label("last_visited"),

            first_visit_detail.callsign,
            first_visit_detail.notes,
        )
        .outerjoin(
            Visit,
            (Visit.airport_id == Airport.airport_id) &
            (Visit.user_id == user_id)
        )
        .outerjoin(
            first_visit,
            first_visit.c.airport_id == Airport.airport_id
        )
        .outerjoin(
            first_visit_detail,
            (first_visit_detail.airport_id == Airport.airport_id) &
            (first_visit_detail.date_visited == first_visit.c.first_date) &
            (first_visit_detail.user_id == user_id)
        )
        .group_by(
            Airport.airport_id,
            Airport.name,
            Airport.city,
            Airport.state,
            Airport.latitude,
            Airport.longitude,
            Airport.towered_status,

            first_visit_detail.callsign,
            first_visit_detail.notes,
        )
    )

# =========================================================
# 🧾 RAW VISITS (giữ lại nhưng clean hơn)
# =========================================================
@router.get("/airports")
def get_airports_by_user(
    user_id: str,
    visited: Optional[bool] = None,
    city: Optional[str] = None,
    bbox: Optional[str] = None,

    db: Session = Depends(get_db),
):
    query = build_airport_query(db, user_id)

    # =====================
    # 📦 BBOX
    # =====================
    if bbox:
        parsed = parse_bbox(bbox)
        if parsed:
            min_lng, min_lat, max_lng, max_lat = parsed

            query = query.filter(
                Airport.latitude.between(min_lat, max_lat),
                Airport.longitude.between(min_lng, max_lng),
            )

    # =====================
    # 🔍 FILTER
    # =====================
    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    if visited is True:
        query = query.having(func.count(Visit.id) > 0)
    elif visited is False:
        query = query.having(func.count(Visit.id) == 0)

    rows = query.limit(500).all()

    return success_response([serialize_airport(r) for r in rows])


# # =========================================================
# # 🧾 MY RAW VISITS (AUTH)
# # =========================================================
# @router.get("/me")
# def get_my_visits(
#     airport_id: Optional[str] = None,
#     city: Optional[str] = None,
#     skip: int = 0,
#     limit: int = 20,
#     sort_by: str = "date_visited",
#     order: str = "desc",
#     db: Session = Depends(get_db),
#     user = Depends(get_current_user)
# ):
#     query = (
#         db.query(Visit, Airport)
#         .join(Airport, Visit.airport_id == Airport.airport_id)
#         .filter(Visit.user_id == user.id)
#     )

#     if airport_id:
#         query = query.filter(Visit.airport_id == airport_id)

#     if city:
#         query = query.filter(Airport.city.ilike(f"%{city}%"))

#     # safe sort
#     if sort_by == "date_visited":
#         sort_column = Visit.date_visited
#     elif sort_by == "created_at" and hasattr(Visit, "created_at"):
#         sort_column = Visit.created_at
#     else:
#         sort_column = Visit.id

#     query = query.order_by(sort_column.desc() if odef parse_bbox(bbox: str):
#     try:
#         min_lng, min_lat, max_lng, max_lat = map(float, bbox.split(","))
#         return min_lng, min_lat, max_lng, max_lat
#     except:
#         return Nonerder == "desc" else sort_column.asc())

#     limit = min(limit, 100)
#     results = query.offset(skip).limit(limit).all()

#     data = [
#         {
#             "id": visit.id,
#             "airport": {
#                 "airport_id": airport.airport_id,
#                 "name": airport.name,
#                 "city": airport.city,
#                 "state": airport.state,
#             },
#             "date_visited": visit.date_visited,
#             "callsign": visit.callsign,
#             "notes": visit.notes,
#         }
#         for visit, airport in results
#     ]

#     return success_response(data)



# =========================================================
# 🗺️ VISITED AIRPORTS (🔥 MAIN API)
# =========================================================
@router.get("/me/airports")
def get_my_airports(
    visited: Optional[bool] = None,
    city: Optional[str] = None,
    bbox: Optional[str] = None,

    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    query = build_airport_query(db, user.id)

    # =====================
    # 📦 BBOX FILTER
    # =====================
    if bbox:
        parsed = parse_bbox(bbox)
        if parsed:
            min_lng, min_lat, max_lng, max_lat = parsed

            query = query.filter(
                Airport.latitude.between(min_lat, max_lat),
                Airport.longitude.between(min_lng, max_lng),
            )

    # =====================
    # 🔍 FILTER
    # =====================
    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    if visited is True:
        query = query.having(func.count(Visit.id) > 0)
    elif visited is False:
        query = query.having(func.count(Visit.id) == 0)

    rows = query.limit(500).all()

    return success_response([serialize_airport(r) for r in rows])

@router.get("/airports/details")
def get_airport_visit_detail_by_user(
    airport_id: str,
    user_id: str,

    skip: int = 0,
    limit: int = 50,

    sort_by: str = "date_visited",
    order: str = "desc",

    db: Session = Depends(get_db),
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(
            Visit.user_id == user_id,
            Visit.airport_id == airport_id
        )
    )

    # =====================
    # 🔽 SORT
    # =====================
    if sort_by == "date_visited":
        sort_column = Visit.date_visited
    elif sort_by == "created_at" and hasattr(Visit, "created_at"):
        sort_column = Visit.created_at
    else:
        sort_column = Visit.id

    query = query.order_by(
        sort_column.desc() if order == "desc" else sort_column.asc()
    )

    # =====================
    # 📄 PAGINATION
    # =====================
    limit = min(limit, 100)
    results = query.offset(skip).limit(limit).all()

    # =====================
    # 🎯 SERIALIZE
    # =====================
    data = [
        {
            "id": visit.id,
            "date_visited": visit.date_visited,

            "callsign": visit.callsign,
            "notes": visit.notes,

            "airport": {
                "id": airport.airport_id,
                "name": airport.name,
                "city": airport.city,
                "state": airport.state,
                "lat": airport.latitude,
                "lng": airport.longitude,
            }
        }
        for visit, airport in results
    ]

    return success_response(data)

@router.get("/me/visits/search")
def search_my_visits(
    q: Optional[str] = None,          # search tổng (name, callsign, notes)
    airport_name: Optional[str] = None,
    callsign: Optional[str] = None,
    notes: Optional[str] = None,

    date_from: Optional[date] = None,
    date_to: Optional[date] = None,

    skip: int = 0,
    limit: int = 50,

    sort_by: str = "date_visited",
    order: str = "desc",

    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)
    )

    # =====================
    # 🔍 SEARCH
    # =====================
    if q:
        query = query.filter(
            Airport.name.ilike(f"%{q}%") |
            Visit.callsign.ilike(f"%{q}%") |
            Visit.notes.ilike(f"%{q}%")
        )

    if airport_name:
        query = query.filter(Airport.name.ilike(f"%{airport_name}%"))

    if callsign:
        query = query.filter(Visit.callsign.ilike(f"%{callsign}%"))

    if notes:
        query = query.filter(Visit.notes.ilike(f"%{notes}%"))

    # =====================
    # 📅 DATE FILTER
    # =====================
    if date_from:
        query = query.filter(Visit.date_visited >= date_from)

    if date_to:
        query = query.filter(Visit.date_visited <= date_to)

    # =====================
    # 🔽 SORT
    # =====================
    if sort_by == "date_visited":
        sort_column = Visit.date_visited
    elif sort_by == "created_at" and hasattr(Visit, "created_at"):
        sort_column = Visit.created_at
    else:
        sort_column = Visit.id

    query = query.order_by(
        sort_column.desc() if order == "desc" else sort_column.asc()
    )

    # =====================
    # 📄 PAGINATION
    # =====================
    limit = min(limit, 100)
    results = query.offset(skip).limit(limit).all()

    # =====================
    # 🎯 SERIALIZE
    # =====================
    data = [
        {
            "id": visit.id,
            "date_visited": visit.date_visited,
            "callsign": visit.callsign,
            "notes": visit.notes,
            "airport": {
                "id": airport.airport_id,
                "name": airport.name,
                "city": airport.city,
                "state": airport.state,
                "lat": airport.latitude,
                "lng": airport.longitude,
            }
        }
        for visit, airport in results
    ]

    return success_response(data)
@router.post("/create/visits")
def create_flight_as_visits(
    payload: FlightCreateRequest,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    # =====================
    # 🔍 VALIDATE
    # =====================
    if payload.from_airport_id == payload.to_airport_id:
        raise HTTPException(400, "from and to cannot be the same")

    from_airport = db.query(Airport).filter(
        Airport.airport_id == payload.from_airport_id
    ).first()

    to_airport = db.query(Airport).filter(
        Airport.airport_id == payload.to_airport_id
    ).first()

    if not from_airport or not to_airport:
        raise HTTPException(400, "Invalid airport")

    # =====================
    # ✈️ CREATE 2 VISITS
    # =====================
    visit_from = Visit(
        user_id=user.id,
        airport_id=payload.from_airport_id,
        date_visited=payload.date_visited,
        callsign=payload.callsign,
        notes=f"DEPARTURE | {payload.notes or ''}".strip(),
    )

    visit_to = Visit(
        user_id=user.id,
        airport_id=payload.to_airport_id,
        date_visited=payload.date_visited,
        callsign=payload.callsign,
        notes=f"ARRIVAL | {payload.notes or ''}".strip(),
    )

    db.add_all([visit_from, visit_to])
    db.commit()

    db.refresh(visit_from)
    db.refresh(visit_to)

    return success_response({
        "from": {
            "id": visit_from.id,
            "airport_id": visit_from.airport_id,
        },
        "to": {
            "id": visit_to.id,
            "airport_id": visit_to.airport_id,
        }
    })