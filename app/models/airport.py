# app/models/airport.py

from sqlalchemy import Column, Integer, String, Float
from app.db.base import Base

class Airport(Base):
    __tablename__ = "airports"

    id = Column(Integer, primary_key=True, index=True)
    airport_id = Column(String, index=True)
    name = Column(String)
    city = Column(String, index=True)
    state = Column(String, index=True)
    latitude = Column(Float)
    longitude = Column(Float)
    elevation = Column(Float)
    towered_status = Column(String)