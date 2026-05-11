from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.api.routes.auth import get_current_user
from app.api.routes.auth import _get_current_user_from_token
from app.db.session import get_db
from app.db.session import SessionLocal
from app.models.user import User
from app.models.visit import Visit
from app.schemas.user import UpdateProfileRequest, UpdateSharedRequest, UserResponse
from helper.response import success_response

router = APIRouter(prefix="/users", tags=["users"])
flask_bp = Blueprint("users_flask", __name__, url_prefix="/users/users")


def _flask_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise ValueError("Invalid token")
    return auth_header.split(" ", 1)[1].strip()


# ========================


@router.get("/")
def get_users(
    skip: int = 0,
    limit: int = 10,
    email: Optional[str] = None,
    handle: Optional[str] = None,
    q: Optional[str] = None,
    sort_by: str = "id",
    order: str = "desc",
    db: Session = Depends(get_db)
):
    # ========================
    # 🧠 BASE QUERY (JOIN VISIT)
    # ========================
    query = (
        db.query(
            User,
            func.count(func.distinct(Visit.airport_id)).label("total_airports")
        )
        .outerjoin(Visit, Visit.user_id == User.id)
        .group_by(User.id)
    )

    # ========================
    # 🔍 FILTER
    # ========================
    if email:
        query = query.filter(User.email == email)

    if handle:
        query = query.filter(User.handle == handle)

    if q:
        query = query.filter(
            (User.email.ilike(f"%{q}%")) |
            (User.handle.ilike(f"%{q}%"))
        )

    # ========================
    # 🔽 SORT
    # ========================
    if sort_by == "total_airports":
        sort_column = "total_airports"
    elif hasattr(User, sort_by):
        sort_column = getattr(User, sort_by)
    else:
        sort_column = User.id

    if sort_by == "total_airports":
        query = query.order_by(
            desc("total_airports") if order == "desc" else "total_airports"
        )
    else:
        query = query.order_by(
            desc(sort_column) if order == "desc" else sort_column
        )

    # ========================
    # 📄 PAGINATION
    # ========================
    limit = min(limit, 100)
    rows = query.offset(skip).limit(limit).all()

    # ========================
    # 🎯 MAP DATA
    # ========================
    data = []
    for user, total_airports in rows:
        u = UserResponse.model_validate(user).model_dump()
        u["total_airports"] = total_airports
        data.append(u)

    return success_response(data)

import time

@router.get("/featured")
def get_featured_users(
    db: Session = Depends(get_db)
):
    # ========================
    # ⏱️ TIME BLOCK (6h)
    # ========================
    current_time = int(time.time())
    block = current_time // (6 * 3600)

    # ========================
    # 🔢 TOTAL USERS
    # ========================
    total_users = db.query(func.count(User.id)).scalar() or 0

    if total_users == 0:
        return success_response([])

    # ========================
    # 🎯 OFFSET BASED ON TIME
    # ========================
    offset = block % max(total_users - 4, 1)

    # ========================
    # 🧠 QUERY
    # ========================
    rows = (
        db.query(
            User,
            func.count(func.distinct(Visit.airport_id)).label("total_airports")
        )
        .outerjoin(Visit, Visit.user_id == User.id)
        .group_by(User.id)
        .order_by(User.id)  # stable order
        .offset(offset)
        .limit(4)
        .all()
    )

    # ========================
    # 🎯 SERIALIZE
    # ========================
    data = []
    for user, total_airports in rows:
        u = UserResponse.model_validate(user).model_dump()
        u["total_airports"] = total_airports
        data.append(u)

    return success_response(data)

@router.put("/user/shared")
def update_shared_status(
    user_id: str,
    payload: UpdateSharedRequest,
    db: Session = Depends(get_db)
):
    # 🔍 find user
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # ✏️ update
    user.is_shared = payload.is_shared

    db.commit()
    db.refresh(user)

    return success_response({
        "id": str(user.id),
        "is_shared": user.is_shared
    })

@router.put("/profile")
def update_profile(
    payload: UpdateProfileRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # 🔥 FIX: lấy user từ session hiện tại
    db_user = db.query(User).filter(User.id == user.id).first()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    # update
    if payload.handle:
        db_user.handle = payload.handle

    if payload.avatar_url is not None:
        db_user.avatar_url = payload.avatar_url

    db.commit()
    db.refresh(db_user)  # ✅ giờ OK

    return success_response(
        UserResponse.model_validate(db_user).model_dump(),
        "Profile updated successfully"
    )


@flask_bp.get("/")
def flask_get_users():
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

        return jsonify(get_users(
            skip=_to_int("skip", 0),
            limit=_to_int("limit", 10),
            email=(request.args.get("email") or "").strip() or None,
            handle=(request.args.get("handle") or "").strip() or None,
            q=(request.args.get("q") or "").strip() or None,
            sort_by=(request.args.get("sort_by") or "id").strip() or "id",
            order=(request.args.get("order") or "desc").strip() or "desc",
            db=db,
        ))
    finally:
        db.close()


@flask_bp.get("/featured")
def flask_get_featured_users():
    db = SessionLocal()
    try:
        return jsonify(get_featured_users(db=db))
    finally:
        db.close()


@flask_bp.put("/user/shared")
def flask_update_shared_status():
    db = SessionLocal()
    try:
        user_id = (request.args.get("user_id") or "").strip()
        if not user_id:
            return jsonify({"detail": "Missing user_id"}), 400
        raw = request.get_json(silent=True) or {}
        try:
            payload = UpdateSharedRequest.model_validate(raw)
        except ValidationError as exc:
            return jsonify({"detail": exc.errors()}), 422
        return jsonify(update_shared_status(user_id=user_id, payload=payload, db=db))
    finally:
        db.close()


@flask_bp.put("/profile")
def flask_update_profile():
    db = SessionLocal()
    try:
        try:
            user = _get_current_user_from_token(_flask_token(), db)
        except Exception:
            return jsonify({"detail": "Invalid token"}), 401
        raw = request.get_json(silent=True) or {}
        try:
            payload = UpdateProfileRequest.model_validate(raw)
        except ValidationError as exc:
            return jsonify({"detail": exc.errors()}), 422
        return jsonify(update_profile(payload=payload, db=db, user=user))
    finally:
        db.close()
