import uuid

from sqlalchemy import UUID, Column, String

from app.db.base import Base


class Certification(Base):
    __tablename__ = "certifications"

    id = Column(
            UUID(as_uuid=True),
            primary_key=True,
            default=uuid.uuid4,  # ✅ Python tự generate
        )    
    code = Column(String, unique=True)
    name = Column(String)
    group = Column(String)