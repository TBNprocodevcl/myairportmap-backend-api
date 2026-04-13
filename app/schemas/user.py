import re
from typing import Optional

from pydantic import BaseModel, EmailStr, Field
from uuid import UUID

class RegisterRequest(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: UUID
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    handle: str | None
    avatar_url: str | None
    total_airports: int = 0
    is_shared: Optional[bool] = False    
    class Config:
        from_attributes = True

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UpdateSharedRequest(BaseModel):
    is_shared: bool

class UpdateProfileRequest(BaseModel):
    handle: Optional[str] = Field(None, min_length=3, max_length=20)
    avatar_url: Optional[str] = None

    def validate_handle(self):
        if self.handle:
            if not re.match(r"^[a-zA-Z0-9_-]+$", self.handle):
                raise ValueError("Invalid handle format")
            
class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str
