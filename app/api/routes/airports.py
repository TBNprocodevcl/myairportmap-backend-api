from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from flask import Blueprint, has_request_context, jsonify, request
from app.db.session import get_db
from app.db.session import SessionLocal
from app.models.airport import Airport

router = APIRouter(prefix="/airports", tags=["airports"])
flask_bp = Blueprint("airports_flask", __name__, url_prefix="/airports")


# ✅ reusable response
def success_response(data, message: str = "Success"):
    payload = {
        "success": True,
        "data": data,
        "message": message
    }
    if has_request_context():
        return jsonify(payload)
    return payload


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


@flask_bp.get("/")
def flask_get_airports():
    db = SessionLocal()
    try:
        def _to_int(name: str, default: int):
            raw = (request.args.get(name) or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except Exception:
                return default

        def _to_float(name: str):
            raw = (request.args.get(name) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except Exception:
                return None

        skip = _to_int("skip", 0)
        limit = _to_int("limit", 50)

        city = (request.args.get("city") or "").strip() or None
        state = (request.args.get("state") or "").strip() or None
        towered = (request.args.get("towered") or "").strip() or None
        q = (request.args.get("q") or "").strip() or None

        min_lat = _to_float("min_lat")
        max_lat = _to_float("max_lat")
        min_lng = _to_float("min_lng")
        max_lng = _to_float("max_lng")

        return get_airports(
            skip=skip,
            limit=limit,
            city=city,
            state=state,
            towered=towered,
            q=q,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lng=min_lng,
            max_lng=max_lng,
            db=db,
        )
    finally:
        db.close()