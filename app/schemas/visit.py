from pydantic import BaseModel
from datetime import date
from typing import Optional


class AirportBase(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    state: str
    status: str


class VisitedAirportResponse(AirportBase):
    visitCount: int
    last_visited: Optional[date] = None
    notes: Optional[str] = None
    airCraft: Optional[str] = None

    class Config:
        from_attributes = True  # dùng cho SQLAlchemy (pydantic v2)