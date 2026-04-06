from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.certification import Certification
from app.models.user import User
from app.schemas.certification import (
    CertificationResponse,
    UpdateUserCertificationsRequest,
    UpdateUserCertificationsRequest2
)
from app.api.routes.auth import get_current_user
from helper.response import success_response

router = APIRouter(prefix="/certifications", tags=["certifications"])


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