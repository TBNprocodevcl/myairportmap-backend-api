from typing import Optional

from pydantic import BaseModel


class VerifySubscriptionRequest(BaseModel):
    transaction_id: str
    original_transaction_id: Optional[str]
    product_id: str
    platform: str  # "ios" hoặc "android"
    purchase_token: Optional[str] = None