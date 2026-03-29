from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models.airport import Airport
from app.models.user import User
from app.models.visit import Visit
from app.schemas.user import UserResponse

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/", response_model=list[UserResponse])
def get_users(
    skip: int = 0,
    limit: int = 10,

    # 🔎 filter
    email: str | None = None,
    handle: str | None = None,
    q: str | None = None,  # search tổng

    # 🔽 sort
    sort_by: str = "id",
    order: str = "desc",

    db: Session = Depends(get_db)
):
    query = db.query(User)

    # ========================
    # 🔍 FILTER
    # ========================
    if email:
        query = query.filter(User.email == email)

    if handle:
        query = query.filter(User.handle == handle)

    # search gần giống (LIKE)
    if q:
        query = query.filter(
            (User.email.ilike(f"%{q}%")) |
            (User.handle.ilike(f"%{q}%"))
        )

    # ========================
    # 🔽 SORT
    # ========================
    sort_column = getattr(User, sort_by, User.id)

    if order == "desc":
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(sort_column)

    # ========================
    # 📄 PAGINATION
    # ========================
    users = query.offset(skip).limit(limit).all()

    return users

