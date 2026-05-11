from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.db.session import SessionLocal, get_db
from app.models.certification import Certification
from app.models.user import User
from app.schemas.certification import (
    CertificationResponse,
    UpdateUserCertificationsRequest,
    UpdateUserCertificationsRequest2
)
from app.api.routes.auth import get_current_user
from app.api.routes.auth import _get_current_user_from_token
from helper.response import success_response

router = APIRouter(prefix="/certifications", tags=["certifications"])
flask_bp = Blueprint("certifications_flask", __name__, url_prefix="/certifications/certifications")


def _flask_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise ValueError("Invalid token")
    return auth_header.split(" ", 1)[1].strip()


# ✅ 1. Get all certifications (group theo UI)
@router.get("/")
def get_certifications(db: Session = Depends(get_db)):
    rows = db.query(Certification).all()

    data = [
        CertificationResponse.model_validate(r).model_dump()
        for r in rows
    ]

    return success_response(data)


# ✅ 2. Get certifications của user
@router.get("/me/all")
def get_all_with_user_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    all_certs = db.query(Certification).all()

    user_cert_ids = {c.id for c in current_user.certifications}

    data = [
        {
            **CertificationResponse.model_validate(c).model_dump(),
            "checked": c.id in user_cert_ids
        }
        for c in all_certs
    ]

    return success_response(data)


# ✅ 3. Update certifications (checkbox save)
@router.put("/me")
def update_my_certifications(
    payload: UpdateUserCertificationsRequest2,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 🔥 Lấy user từ chính session db
    user = db.query(User).filter(User.id == current_user.id).first()

    checked_ids = list(set(
        item.id for item in payload.items if item.checked
    ))

    certs = db.query(Certification).filter(
        Certification.id.in_(checked_ids)
    ).all()

    if len(certs) != len(checked_ids):
        return success_response(None, message="Invalid certification IDs")

    user.certifications = certs

    db.commit()

    return success_response(None)


@flask_bp.get("/")
def flask_get_certifications():
    db = SessionLocal()
    try:
        return jsonify(get_certifications(db=db))
    finally:
        db.close()


@flask_bp.get("/me/all")
def flask_get_all_with_user_status():
    db = SessionLocal()
    try:
        try:
            current_user = _get_current_user_from_token(_flask_token(), db)
        except Exception:
            return jsonify({"detail": "Invalid token"}), 401
        return jsonify(get_all_with_user_status(db=db, current_user=current_user))
    finally:
        db.close()


@flask_bp.put("/me")
def flask_update_my_certifications():
    db = SessionLocal()
    try:
        try:
            current_user = _get_current_user_from_token(_flask_token(), db)
        except Exception:
            return jsonify({"detail": "Invalid token"}), 401

        raw = request.get_json(silent=True) or {}
        try:
            payload = UpdateUserCertificationsRequest2.model_validate(raw)
        except ValidationError as exc:
            return jsonify({"detail": exc.errors()}), 422

        return jsonify(update_my_certifications(payload=payload, db=db, current_user=current_user))
    finally:
        db.close()