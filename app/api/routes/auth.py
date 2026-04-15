from datetime import datetime, timedelta, timezone
import random
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from jose import jwt, JWTError
from urllib.parse import quote

from app.db.session import SessionLocal
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.google import GoogleLoginRequest
from app.schemas.user import ForgotPasswordRequest, RegisterRequest, LoginRequest, ResetPasswordOTPRequest, ResetPasswordRequest, TokenResponse, UserResponse
from app.core.security import create_reset_token, hash_password, verify_password, create_access_token
from app.core.config import settings
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.services.email import send_otp_email, send_reset_email
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from helper.response import success_response



security = HTTPBearer()

router = APIRouter(prefix="/auth", tags=["auth"])



SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

# dependency DB
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
def generate_unique_handle(db: Session, base_handle: str):
    handle = base_handle
    counter = 1

    while db.query(User).filter(User.handle == handle).first():
        handle = f"{base_handle}{counter}"
        counter += 1

    return handle

def normalize_handle(email: str):
    return email.split("@")[0].lower()

def avatar_url_for_handle(handle: str):
    return f"https://api.dicebear.com/7.x/initials/svg?seed={handle}"


# 🔥 REGISTER
@router.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    exists = db.query(User).filter(User.email == data.email).first()
    if exists:
        raise HTTPException(400, "Email already exists")

    base_handle = normalize_handle(data.email)
    handle = generate_unique_handle(db, base_handle)

    user = User(
        email=data.email,
        password=hash_password(data.password),
        first_name=data.first_name,
        last_name=data.last_name,
        handle=handle,
        avatar_url=avatar_url_for_handle(handle),
    )

    db.add(user)
    db.commit()
    db.refresh(user)
    
    return success_response(
        UserResponse.model_validate(user).model_dump(),
        "User created successfully"
    )

# 🔐 LOGIN
@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()

    if not user or not verify_password(data.password, user.password):
        raise HTTPException(401, "Invalid credentials")

    token = create_access_token({"sub": str(user.id)})

    return success_response(
        {
            "access_token": token,
            "token_type": "bearer",
            "user": UserResponse.model_validate(user).model_dump()
        },
        "Login successful"
    )


# 🔍 GET CURRENT USER
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    token = credentials.credentials

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid token")

    user = db.query(User).get(UUID(user_id))
    if not user:
        raise HTTPException(404, "User not found")

    return user


# 👤 /me
@router.get("/me")
def me(
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db_user = db.query(User).filter(User.id == user.id).first()

    now = datetime.now(timezone.utc)

    is_paid = db.query(Subscription).filter(
        Subscription.user_id == db_user.id,
        Subscription.expiration_date > now
    ).first() is not None

    db_user.is_paid = is_paid

    return success_response(
        UserResponse.model_validate(db_user).model_dump()
    )

@router.delete("/me")
def delete_user(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    # optional: cascade cleanup nếu cần (Visit, etc.)

    db.delete(user)
    db.commit()

    return success_response(
        {},
        "User deleted successfully"
    )

@router.post("/google", response_model=TokenResponse)
def login_google(payload: GoogleLoginRequest, db: Session = Depends(get_db)):
    token = payload.token

    if not token:
        raise HTTPException(status_code=400, detail="Token missing")

    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request()
        )

        # 🔥 check audience
        if idinfo["aud"] not in settings.GOOGLE_CLIENT_IDS:
            raise HTTPException(status_code=401, detail="Invalid audience")

        # 🔥 check issuer
        if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
            raise HTTPException(status_code=401, detail="Invalid issuer")

        email = idinfo.get("email")
        if not email:
            raise HTTPException(400, "Email not found")

        if not idinfo.get("email_verified"):
            raise HTTPException(400, "Email not verified")

    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    # ===== USER =====
    user = db.query(User).filter(User.email == email).first()

    if not user:
        handle = normalize_handle(email)

        user = User(
            email=email,
            password=None,
            handle=handle,
            avatar_url=idinfo.get("picture") or avatar_url_for_handle(handle),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # ===== JWT =====
    access_token = create_access_token({"sub": str(user.id)})

    return {"access_token": access_token}


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()

    # ⚠️ không nên trả lỗi để tránh lộ email tồn tại hay không
    if not user:
        return success_response({}, "If email exists, reset link sent")

    reset_token = create_reset_token({"sub": str(user.id)})

    reset_link = f"{settings.BASE_URL}{settings.RESET_PASSWORD_URL}?token={quote(reset_token)}"
    # TODO: gửi email thật
    send_reset_email(user.email, reset_link)

    return success_response({}, "If email exists, reset link sent")


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(data.token, SECRET_KEY, algorithms=[ALGORITHM])

        # 🔥 check token type
        if payload.get("type") != "reset":
            raise HTTPException(401, "Invalid token type")

        user_id = payload.get("sub")

    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

    user = db.query(User).get(UUID(user_id))
    if not user:
        raise HTTPException(404, "User not found")

    user.password = hash_password(data.new_password)
    db.commit()

    return success_response({}, "Password updated successfully")

@router.post("/forgot-password-otp")
def forgot_password_otp(data: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()

    if not user:
        return success_response({}, "If email exists, OTP sent")

    otp = str(random.randint(100000, 999999))

    user.reset_otp = otp
    user.reset_otp_expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    user.reset_otp_attempts = 0

    db.commit()

    send_otp_email(user.email, otp)

    return success_response({}, "If email exists, OTP sent")

@router.post("/reset-password-otp")
def reset_password_otp(data: ResetPasswordOTPRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()

    if not user:
        raise HTTPException(400, "Invalid request")

    if user.reset_otp_attempts >= 5:
        raise HTTPException(400, "Too many attempts")

    if user.reset_otp != data.otp:
        user.reset_otp_attempts += 1
        db.commit()
        raise HTTPException(400, "Invalid OTP")

    if user.reset_otp_expire < datetime.now(timezone.utc):
        raise HTTPException(400, "OTP expired")

    user.password = hash_password(data.new_password)

    # clear OTP
    user.reset_otp = None
    user.reset_otp_expire = None
    user.reset_otp_attempts = 0

    db.commit()

    return success_response({}, "Password updated successfully")