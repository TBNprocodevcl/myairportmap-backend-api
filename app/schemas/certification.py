from pydantic import BaseModel
from uuid import UUID
from typing import List


class CertificationResponse(BaseModel):
    id: UUID
    code: str
    name: str
    group: str

    class Config:
        from_attributes = True


class UpdateUserCertificationsRequest(BaseModel):
    certification_ids: List[UUID]

class CertificationItem(BaseModel):
    id: UUID
    checked: bool

class UpdateUserCertificationsRequest2(BaseModel):
    items: List[CertificationItem]