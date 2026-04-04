from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.certification import Certification
from app.models.user import User
from app.schemas.certification import (
    CertificationResponse,
    UpdateUserCertificationsRequest
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
@router.get("/me")
def get_my_certifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = [
        CertificationResponse.model_validate(c).model_dump()
        for c in current_user.certifications
    ]

    return success_response(data)


# ✅ 3. Update certifications (checkbox save)
@router.put("/me")
def update_my_certifications(
    payload: UpdateUserCertificationsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ❗ chỉ update nếu FE có gửi field này
    if payload.certification_ids is not None:

        if len(payload.certification_ids) == 0:
            # user bỏ hết checkbox
            current_user.certifications = []

        else:
            certs = db.query(Certification).filter(
                Certification.id.in_(payload.certification_ids)
            ).all()

            current_user.certifications = certs

    db.commit()

    return success_response(None)