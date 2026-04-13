import csv
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, UploadFile
import io
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func
from typing import Optional, List

from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models import Visit, Airport, airport

# ✅ IMPORT SCHEMA
from app.models.user import User
from app.schemas.visit import FlightCreateRequest, VisitedAirportResponse
from helper.response import success_response

from fastapi import HTTPException
from .achievements import normalize

def normalize_airport_id(code: str | None):
    if not code:
        return None

    code = code.strip().upper()

    # US ICAO → IATA (KJFK → JFK)
    if len(code) == 4 and code.startswith("K"):
        return code[1:]

    return code

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

@router.get("/me")
def get_my_visits(
    airport_id: Optional[str] = None,
    city: Optional[str] = None,
    bbox: Optional[str] = None,

    skip: int = 0,
    limit: int = 50,

    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    query = (
        db.query(Visit, Airport)
        .join(Airport, Visit.airport_id == Airport.airport_id)
        .filter(Visit.user_id == user.id)
    )

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
    if airport_id:
        airport_id = normalize_airport_id(airport_id)
        query = query.filter(Visit.airport_id == airport_id)

    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    # =====================
    # 🔽 SORT
    # =====================
    query = query.order_by(Visit.date_visited.desc(), Visit.id.desc())

    # =====================
    # 📄 PAGINATION
    # =====================
    limit = min(limit, 200)

    results = query.offset(skip).limit(limit).all()

    # =====================
    # 📦 SERIALIZE
    # =====================
    data = []
    for visit, airport in results:
        data.append({
            "id": visit.id,

            "airport": {
                "airport_id": airport.airport_id,
                "name": airport.name,
                "city": airport.city,
                "state": airport.state,
                "lat": airport.latitude,
                "lng": airport.longitude,
            },

            "date_visited": visit.date_visited,
            "callsign": visit.callsign,
            "notes": visit.notes,
        })

    return success_response({
        "total": len(data),
        "items": data
    })
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

@router.get("/airports/{handle}")
def get_airports_by_handle(
    handle: str,
    visited: Optional[bool] = None,
    city: Optional[str] = None,
    bbox: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # 🔍 FIND USER
    user = (
        db.query(User)
        .filter(func.lower(User.handle) == handle.lower())
        .first()
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 📦 BASE QUERY
    query = build_airport_query(db, user.id)

    # 📦 BBOX
    if bbox:
        parsed = parse_bbox(bbox)
        if parsed:
            min_lng, min_lat, max_lng, max_lat = parsed

            query = query.filter(
                Airport.latitude.between(min_lat, max_lat),
                Airport.longitude.between(min_lng, max_lng),
            )

    # 🔍 FILTER
    if city:
        query = query.filter(Airport.city.ilike(f"%{city}%"))

    if visited is True:
        query = query.having(func.count(Visit.id) > 0)
    elif visited is False:
        query = query.having(func.count(Visit.id) == 0)

    rows = query.limit(500).all()

    return success_response({
        "user_id": str(user.id),
        "handle": user.handle,
        "avatar_url": user.avatar_url,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "airports": [serialize_airport(r) for r in rows]
    })

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

@router.put("/visits/{visit_id}")
def update_visit(
    visit_id: int,
    payload: FlightCreateRequest,  # hoặc tạo schema riêng UpdateVisitRequest
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    visit = db.query(Visit).filter(
        Visit.id == visit_id,
        Visit.user_id == user.id
    ).first()

    if not visit:
        raise HTTPException(404, "Visit not found")

    # =====================
    # ✏️ UPDATE FIELD
    # =====================
    if payload.date_visited:
        visit.date_visited = payload.date_visited

    if payload.callsign is not None:
        visit.callsign = payload.callsign

    if payload.notes is not None:
        visit.notes = payload.notes

    db.commit()
    db.refresh(visit)

    return success_response({
        "id": visit.id,
        "airport_id": visit.airport_id,
        "date_visited": visit.date_visited,
        "callsign": visit.callsign,
        "notes": visit.notes,
    })

@router.delete("/visits/{visit_id}")
def delete_visit(
    visit_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    visit = db.query(Visit).filter(
        Visit.id == visit_id,
        Visit.user_id == user.id
    ).first()

    if not visit:
        raise HTTPException(404, "Visit not found")

    db.delete(visit)
    db.commit()

    return success_response({
        "deleted_id": visit_id
    })

@router.post("/import/foreflight")
def import_foreflight_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    # =====================
    # 📁 VALIDATE FILE
    # =====================
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV file allowed")

    try:
        content = file.file.read().decode("utf-8")
    except:
        raise HTTPException(400, "Cannot read file")

    # =====================
    # 🧹 CLEAN CSV (skip header rác)
    # =====================
    lines = content.splitlines()

    header_index = None
    for i, line in enumerate(lines):
        if line.startswith("Date"):
            header_index = i
            break

    if header_index is None:
        raise HTTPException(400, "Invalid ForeFlight CSV format")

    clean_content = "\n".join(lines[header_index:])
    reader = csv.DictReader(io.StringIO(clean_content))

    # =====================
    # ⚙️ PRELOAD AIRPORTS (optimize)
    # =====================
    airport_ids = set()
    rows = list(reader)

    for r in rows:
        f = normalize_airport_id(r.get("From"))
        t = normalize_airport_id(r.get("To"))

        if f:
            airport_ids.add(f)
        if t:
            airport_ids.add(t)

    airports = db.query(Airport).filter(
        Airport.airport_id.in_(airport_ids)
    ).all()

    airport_map = {a.airport_id: a for a in airports}

    # =====================
    # 🚀 PROCESS
    # =====================
    visits_to_create = []
    errors = []
    skipped = 0

    for i, row in enumerate(rows, start=1):
        try:
            # ---------------------
            # 📅 DATE
            # ---------------------
            date_str = row.get("Date")
            if not date_str:
                skipped += 1
                continue

            try:
                date_visited = datetime.strptime(date_str, "%Y-%m-%d").date()
            except:
                errors.append(f"Row {i}: invalid date '{date_str}'")
                continue

            # ---------------------
            # ✈️ BASIC FIELDS
            # ---------------------
            from_airport = normalize_airport_id(row.get("From"))
            to_airport = normalize_airport_id(row.get("To"))

            if not from_airport or not to_airport:
                skipped += 1
                continue

            if from_airport == to_airport:
                errors.append(f"Row {i}: same from/to airport")
                continue

            # ---------------------
            # 🛫 VALIDATE AIRPORT
            # ---------------------
            if from_airport not in airport_map:
                errors.append(f"Row {i}: unknown airport '{from_airport}'")
                continue

            if to_airport not in airport_map:
                errors.append(f"Row {i}: unknown airport '{to_airport}'")
                continue

            # ---------------------
            # ✈️ EXTRA DATA
            # ---------------------
            callsign = row.get("AircraftID") or None
            route = row.get("Route") or ""

            note_base = (row.get("PilotComments") or "").strip()            # ---------------------
            # 🚫 DUPLICATE CHECK
            # ---------------------
            exists = db.query(Visit).filter(
                Visit.user_id == user.id,
                Visit.airport_id == from_airport,
                Visit.date_visited == date_visited,
                Visit.callsign == callsign
            ).first()

            if exists:
                skipped += 1
                continue

            # ---------------------
            # 🧾 CREATE VISITS
            # ---------------------
            visits_to_create.append(
                Visit(
                    user_id=user.id,
                    airport_id=from_airport,
                    date_visited=date_visited,
                    callsign=callsign,
                    notes=f"{note_base}"
                )
            )

            visits_to_create.append(
                Visit(
                    user_id=user.id,
                    airport_id=to_airport,
                    date_visited=date_visited,
                    callsign=callsign,
                    notes=f"ARRIVAL | {note_base}"
                )
            )

        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    # =====================
    # 💾 BULK INSERT
    # =====================
    if visits_to_create:
        db.bulk_save_objects(visits_to_create)
        db.commit()

    # =====================
    # 📊 RESULT
    # =====================
    return success_response({
        "total_rows": len(rows),
        "created_visits": len(visits_to_create),
        "skipped": skipped,
        "errors": errors[:20]
    })
