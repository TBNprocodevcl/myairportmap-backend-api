import os

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from flask import Blueprint, has_request_context, jsonify, request, send_file

from app.constant.bussiness import CLASS_B_AIRPORTS
from app.constant.achievement_state import STATE_AIRPORTS

from app.db.session import SessionLocal, get_db
from app.models.visit import Visit
from app.models.airport import Airport
from app.api.routes.auth import _get_current_user_from_token, get_current_user

router = APIRouter(prefix="/achievements", tags=["achievements"])
flask_bp = Blueprint("achievements_flask", __name__, url_prefix="/api/achievements")


def success_response(data, message="Success"):
    payload = {
        "success": True,
        "data": data,
        "message": message
    }
    # Flask mode: return a Flask JSON response.
    if has_request_context():
        return jsonify(payload)
    # FastAPI mode: return plain dict (auto-serialized by FastAPI).
    return payload


def file_response(file_path: str, state: str):
    if has_request_context():
        return send_file(
            file_path,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{state}.png",
        )

    return FileResponse(
        file_path,
        media_type="image/png",
        filename=f"{state}.png"
    )


def _flask_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(401, "Invalid token")
    return auth_header.split(" ", 1)[1].strip()


def _flask_error(exc: HTTPException):
    return jsonify({"detail": exc.detail}), exc.status_code

def normalize(code: str):
    if len(code) == 3:
        return f"K{code}"
    return code

def get_user_visited_airports(db: Session, user_id: str) -> set[str]:
    rows = (
        db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .distinct()
        .all()
    )
    return {normalize(r[0]) for r in rows}

def get_phase(pct: float):
    if pct >= 100:
        return "COMPLETED"
    elif pct >= 70:
        return "APPROACH"
    elif pct >= 20:
        return "CRUISING"
    else:
        return "TAKE-OFF"


def get_icon(phase):
    return {
        "COMPLETED": "🏆",
        "APPROACH": "↘️",
        "CRUISING": "✈️",
        "TAKE-OFF": "🛫"
    }.get(phase, "")

def build_class_b_achievement(db: Session, user_id: str):
    visited_set = get_user_visited_airports(db, user_id)

    airports = db.query(Airport.airport_id, Airport.name)\
        .filter(Airport.airport_id.in_(CLASS_B_AIRPORTS))\
        .all()
    airport_map = {
        normalize(a.airport_id): a.name
        for a in airports
    }

    checklist = []
    visited_count = 0

    for airport_id in CLASS_B_AIRPORTS:
        is_visited = airport_id in visited_set

        if is_visited:
            visited_count += 1

        checklist.append({
            "id": airport_id,
            "name": airport_map.get(airport_id),
            "visited": is_visited
        })

    total = len(CLASS_B_AIRPORTS)
    percent = round((visited_count / total) * 100, 1)

    checklist.sort(key=lambda x: not x["visited"])

    phase = get_phase(percent)
    icon = get_icon(phase)

    return {
        "title": "BRAVO",
        "code": "CLASS_B",
        "total": total,
        "visited": visited_count,
        "percent": percent,
        "phase": phase,
        "icon": icon,
        "completed": visited_count == total,
        "checklist": checklist
    }

def build_state_progress(db: Session, user_id: str):

    visited_airports = set(
        normalize(r[0]) for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .all()
    )

    result = []

    total_all = 0
    visited_all = 0

    # =========================
    # STATES (WITH CHECKLIST)
    # =========================
    for state, airport_ids in STATE_AIRPORTS.items():
        total = len(airport_ids)

        visited = sum(
            1 for aid in airport_ids
            if normalize(aid) in visited_airports
        )

        checklist = [
            {
                "airport_id": aid,
                "visited": normalize(aid) in visited_airports
            }
            for aid in airport_ids
        ]

        pct = (visited / total * 100) if total else 0
        ph = get_phase(pct)

        result.append({
            "state": state,
            "visited": visited,
            "total": total,
            "percentage": round(pct, 1),
            "phase": ph,
            "icon": get_icon(ph),
            "airports": checklist   # ✅ only states have checklist
        })

        total_all += total
        visited_all += visited

    # =========================
    # CONUS (NO CHECKLIST)
    # =========================
    all_airports = db.query(Airport.airport_id).all()

    conus_total = len(all_airports)

    conus_visited = sum(
        1 for a in all_airports
        if normalize(a[0]) in visited_airports
    )

    pct_all = (conus_visited / conus_total * 100) if conus_total else 0
    ph_all = get_phase(pct_all)

    result.append({
        "state": "CONUS",
        "visited": conus_visited,
        "total": conus_total,
        "percentage": round(pct_all, 1),
        "phase": ph_all,
        "icon": get_icon(ph_all),
        # ❌ no airports field
    })

    return result

@router.get("/class-b")
def get_class_b_achievement(
    user_id: str,
    db: Session = Depends(get_db),
):
    result = build_class_b_achievement(db, user_id)
    return success_response(result)


@router.get("/class-b/me")
def get_my_class_b_achievement(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    result = build_class_b_achievement(db, user.id)
    return success_response(result)

# =========================
# 🏆 ALL ACHIEVEMENTS (future-ready)
# =========================
@router.get("/")
def get_all_achievements(
    user_id: str,
    db: Session = Depends(get_db),
):

    visited_set = get_user_visited_airports(db, user_id)

    # 👉 Class B
    class_b_total = len(CLASS_B_AIRPORTS)
    class_b_visited = sum(1 for a in CLASS_B_AIRPORTS if a in visited_set)

    class_b_percent = round((class_b_visited / class_b_total) * 100, 1)

    achievements = [
        {
            "code": "CLASS_B",
            "title": "BRAVO",
            "total": class_b_total,
            "visited": class_b_visited,
            "percent": class_b_percent,
            "completed": class_b_visited == class_b_total
        }
    ]

    return success_response(achievements)

@router.get("/states")
def get_state_progress(
    user_id: str,
    state: str | None = Query(None),
    phase: str | None = Query(None),
    db: Session = Depends(get_db),
):
    result = build_state_progress(db, user_id)

    if state:
        result = [r for r in result if r["state"].lower() == state.lower()]

    if phase:
        result = [r for r in result if r["phase"].lower() == phase.lower()]

    result.sort(key=lambda x: (x["state"] == "CONUS", -x["percentage"]))


    return success_response(result)

@router.get("/states/me")
def get_my_state_progress(
    state: str | None = Query(None),
    phase: str | None = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    result = build_state_progress(db, user.id)

    if state:
        result = [r for r in result if r["state"].lower() == state.lower()]

    if phase:
        result = [r for r in result if r["phase"].lower() == phase.lower()]

    result.sort(key=lambda x: (x["state"] == "CONUS", -x["percentage"]))
    return success_response(result)


@router.get("/states/checklist")
def get_state_checklist(
    user_id: str,
    state: str,
    db: Session = Depends(get_db),
):
    state = state.upper()

    airport_ids = STATE_AIRPORTS.get(state)
    if not airport_ids:
        return success_response([], message="State not found")

    visited = set(
        normalize(r[0]) for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user_id)
        .all()
    )

    # ✅ lấy airport info
    airports = db.query(Airport).filter(Airport.airport_id.in_(airport_ids)).all()
    airport_map = {a.airport_id: a for a in airports}

    result = []
    visited_count = 0

    for aid in airport_ids:
        is_visited = aid in visited

        if is_visited:
            visited_count += 1

        a = airport_map.get(aid)

        result.append({
            "airport_id": aid,
            "name": a.name if a else None,
            "visited": is_visited
        })

    total = len(result)
    percentage = (visited_count / total * 100) if total else 0
    completed = visited_count == total

    return success_response({
        "state": state,
        "visited": visited_count,
        "total": total,
        "percentage": round(percentage, 1),
        "completed": completed,
        "airports": result
    })

@router.get("/states/me/badge")
def download_state_badge(
    state: str,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):

    state = state.upper()

    airport_ids = STATE_AIRPORTS.get(state)
    if not airport_ids:
        return success_response(None, message="State not found")

    # ✅ lấy visited
    visited_airports = set(
        r[0] for r in db.query(Visit.airport_id)
        .filter(Visit.user_id == user.id)
        .all()
    )

    visited = sum(1 for aid in airport_ids if aid in visited_airports)
    total = len(airport_ids)

    # ❌ chưa complete → block
    if visited < total:
        return success_response(None, message="State not completed")

    # ✅ path image
    file_path = f"static/base/{state}.png"

    if not os.path.exists(file_path):
        return success_response(None, message="Badge not found")

    return file_response(file_path, state)


@flask_bp.get("/class-b")
def flask_get_class_b_achievement():
    user_id = (request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"detail": "Missing user_id"}), 400

    db = SessionLocal()
    try:
        return get_class_b_achievement(user_id=user_id, db=db)
    finally:
        db.close()


@flask_bp.get("/class-b/me")
def flask_get_my_class_b_achievement():
    db = SessionLocal()
    try:
        user = _get_current_user_from_token(_flask_token(), db)
        return get_my_class_b_achievement(db=db, user=user)
    except HTTPException as exc:
        return _flask_error(exc)
    finally:
        db.close()


@flask_bp.get("")
@flask_bp.get("/")
def flask_get_all_achievements():
    user_id = (request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"detail": "Missing user_id"}), 400

    db = SessionLocal()
    try:
        return get_all_achievements(user_id=user_id, db=db)
    finally:
        db.close()


@flask_bp.get("/states")
def flask_get_state_progress():
    user_id = (request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"detail": "Missing user_id"}), 400

    state = request.args.get("state")
    phase = request.args.get("phase")

    db = SessionLocal()
    try:
        return get_state_progress(user_id=user_id, state=state, phase=phase, db=db)
    finally:
        db.close()


@flask_bp.get("/states/me")
def flask_get_my_state_progress():
    state = request.args.get("state")
    phase = request.args.get("phase")

    db = SessionLocal()
    try:
        user = _get_current_user_from_token(_flask_token(), db)
        return get_my_state_progress(state=state, phase=phase, db=db, user=user)
    except HTTPException as exc:
        return _flask_error(exc)
    finally:
        db.close()


@flask_bp.get("/states/checklist")
def flask_get_state_checklist():
    user_id = (request.args.get("user_id") or "").strip()
    state = (request.args.get("state") or "").strip()
    if not user_id or not state:
        return jsonify({"detail": "Missing user_id or state"}), 400

    db = SessionLocal()
    try:
        return get_state_checklist(user_id=user_id, state=state, db=db)
    finally:
        db.close()


@flask_bp.get("/states/me/badge")
def flask_download_state_badge():
    state = (request.args.get("state") or "").strip()
    if not state:
        return jsonify({"detail": "Missing state"}), 400

    db = SessionLocal()
    try:
        user = _get_current_user_from_token(_flask_token(), db)
        return download_state_badge(state=state, db=db, user=user)
    except HTTPException as exc:
        return _flask_error(exc)
    finally:
        db.close()