
from sqlalchemy import UUID, Column, DateTime, String

from app.db.base import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID, primary_key=True)
    user_id = Column(UUID)

    product_id = Column(String)
    transaction_id = Column(String)
    original_transaction_id = Column(String)

    purchase_date = Column(DateTime)
    expiration_date = Column(DateTime)

    platform = Column(String)
    status = Column(String)  # active / expired

    created_at = Column(DateTime)