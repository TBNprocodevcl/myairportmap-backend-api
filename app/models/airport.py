# app/models/airport.py

from sqlalchemy import Column, Integer, String, Float
from sqlalchemy.orm import relationship
from app.db.base import Base

class Airport(Base):
    __tablename__ = "airports"

    airport_id = Column(String, primary_key=True, index=True)
    name = Column(String)
    city = Column(String, index=True)
    state = Column(String, index=True)
    latitude = Column(Float)
    longitude = Column(Float)
    elevation = Column(Float)
    towered_status = Column(String)
    visits = relationship("Visit", backref="airport")