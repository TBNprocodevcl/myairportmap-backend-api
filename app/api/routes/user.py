from typing import Optional, List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.db.session import get_db
from app.models.user import User
from app.models.visit import Visit
from app.schemas.user import UserResponse
from helper.response import success_response

router = APIRouter(prefix="/users", tags=["users"])


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