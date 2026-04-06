from fastapi import APIRouter, Depends, File, UploadFile, Request
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
import os
from uuid import uuid4

from app.api.routes.airports import success_response
from app.api.routes.auth import get_current_user
from app.db.session import get_db

router = APIRouter(prefix="/upload", tags=["upload"])


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