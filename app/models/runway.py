from sqlalchemy import Column, Integer, String
from app.db.base import Base

class Runway(Base):
    __tablename__ = "runways"

    id = Column(Integer, primary_key=True, autoincrement=True)

    airport_ident = Column(String, index=True)

    le_ident = Column(String)
    he_ident = Column(String)