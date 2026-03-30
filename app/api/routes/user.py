from typing import Optional, List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserResponse
from helper.response import success_response

router = APIRouter(prefix="/users", tags=["users"])


# ========================


@router.get("/")
def get_users(
    skip: int = 0,
    limit: int = 10,

    # 🔎 filter
    email: Optional[str] = None,
    handle: Optional[str] = None,
    q: Optional[str] = None,

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

    if q:
        query = query.filter(
            (User.email.ilike(f"%{q}%")) |
            (User.handle.ilike(f"%{q}%"))
        )

    # ========================
    # 🔽 SORT (safe)
    # ========================
    if hasattr(User, sort_by):
        sort_column = getattr(User, sort_by)
    else:
        sort_column = User.id

    if order == "desc":
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(sort_column)

    # ========================
    # 📄 PAGINATION
    # ========================
    limit = min(limit, 100)
    users = query.offset(skip).limit(limit).all()

    # ========================
    # 🎯 MAP DATA
    # ========================
    data = [
        UserResponse.model_validate(user).model_dump()
        for user in users
    ]

    return success_response(data)