from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class UserCertification(Base):
    __tablename__ = "user_certifications"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    certification_id = Column(UUID(as_uuid=True), ForeignKey("certifications.id"), primary_key=True)