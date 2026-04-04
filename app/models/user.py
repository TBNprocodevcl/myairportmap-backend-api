from sqlalchemy import Column, String, Boolean
from sqlalchemy.dialects.postgresql import UUID
import uuid

from sqlalchemy.orm import relationship
from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True)
    handle = Column(String, unique=True)
    first_name = Column(String)
    last_name = Column(String)
    avatar_url = Column(String)
    is_paid = Column(Boolean, default=False)
    password = Column(String)
    google_id = Column(String, nullable=True)
    visits = relationship("Visit", backref="user")
    is_shared = Column(Boolean, default=False)

