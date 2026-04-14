from pydantic import BaseModel


class VerifySubscriptionRequest(BaseModel):
    transaction_id: str
    original_transaction_id: str
    product_id: str
    platform: str  # "ios" hoặc "android"