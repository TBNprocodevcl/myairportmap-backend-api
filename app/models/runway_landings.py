from sqlalchemy import Column, Integer, String, Date, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class RunwayLanding(Base):
    __tablename__ = "runway_landings"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 👤 user
    user_id = Column(UUID, ForeignKey("users.id"), nullable=False, index=True)

    # 🛫 airport
    airport_id = Column(String, ForeignKey("airports.airport_id"), nullable=False, index=True)

    # 🧭 runway heading (1 → 36)
    runway_heading = Column(Integer, nullable=False)

    # 📅 info
    date = Column(Date, nullable=True)
    aircraft = Column(String, nullable=True)
    notes = Column(String, nullable=True)

    # 🚫 mỗi user chỉ có 1 record cho mỗi heading
    __table_args__ = (
        UniqueConstraint("user_id", "runway_heading", name="uq_user_runway_heading"),
        Index("idx_user_heading", "user_id", "runway_heading"),
    )