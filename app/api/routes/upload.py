from fastapi import APIRouter, Depends, File, UploadFile
from requests import Session
from starlette.exceptions import HTTPException
from starlette.routing import Router

from app.api.routes.airports import success_response
from app.api.routes.auth import get_current_user
from app.db.session import get_db

router = APIRouter(prefix="/upload", tags=["upload"])

@router.post("/profile/avatar")
def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # validate type
    if file.content_type not in ["image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    # TODO: upload to S3 / local
    file_url = f"/static/avatars/{file.filename}"

    user.avatar_url = file_url
    db.commit()

    return success_response({
        "avatar_url": file_url
    }, "Avatar uploaded")