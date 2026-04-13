from pydantic.v1 import BaseModel


class VerifyReceiptRequest(BaseModel):
    platform: str
    receipt_data: str
    product_id: str