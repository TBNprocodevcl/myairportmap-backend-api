# app/models/visit.py

from sqlalchemy import UUID, Column, Integer, String, Date, ForeignKey
from app.db.base import Base

class Visit(Base):
    __tablename__ = "visits"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(UUID, ForeignKey("users.id"))
    airport_id = Column(String, ForeignKey("airports.airport_id"))

    date_visited = Column(Date)
    callsign = Column(String)
    notes = Column(String)