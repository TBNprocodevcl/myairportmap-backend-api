from fastapi import APIRouter, Depends, File, UploadFile, Request
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from flask import Blueprint, jsonify, request
from types import SimpleNamespace
import os
from uuid import uuid4

from app.api.routes.airports import success_response
from app.api.routes.auth import get_current_user
from app.api.routes.auth import _get_current_user_from_token
from app.db.session import get_db
from app.db.session import SessionLocal

router = APIRouter(prefix="/upload", tags=["upload"])
flask_bp = Blueprint("upload_flask", __name__, url_prefix="/upload/upload")


@router.post("/profile/avatar")
def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # 1. validate
    if file.content_type not in ["image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    # 2. folder
    UPLOAD_DIR = "static/avatars"
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 3. filename unique
    ext = file.filename.split(".")[-1]
    filename = f"{uuid4()}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    # 4. save file
    with open(file_path, "wb") as buffer:
        buffer.write(file.file.read())

    # 5. build URL (QUAN TRỌNG)
    base_url = str(request.base_url).rstrip("/")
    file_url = f"{base_url}/static/avatars/{filename}"

    # 6. save DB
    user.avatar_url = file_url
    db.commit()

    return success_response({
        "avatar_url": file_url
    }, "Avatar uploaded")


def _flask_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise ValueError("Invalid token")
    return auth_header.split(" ", 1)[1].strip()


@flask_bp.post("/profile/avatar")
def flask_upload_avatar():
    db = SessionLocal()
    try:
        try:
            user = _get_current_user_from_token(_flask_token(), db)
        except Exception:
            return jsonify({"detail": "Invalid token"}), 401

        fs = request.files.get("file")
        if fs is None:
            return jsonify({"detail": "file is required"}), 400

        wrapped = SimpleNamespace(
            content_type=fs.content_type,
            filename=fs.filename,
            file=fs.stream,
        )
        return jsonify(upload_avatar(request=request, file=wrapped, db=db, user=user))
    except HTTPException as exc:
        return jsonify({"detail": str(exc.detail)}), exc.status_code
    finally:
        db.close()