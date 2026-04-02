import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse
import os

from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.db.session import get_db
from app.models.visit import Visit

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/logbooktest.csv")
def download_logbook_test():
    file_path = "./logbooktest.csv"

    if not os.path.exists(file_path):
        return {
            "success": False,
            "message": "File not found"
        }

    return FileResponse(
        path=file_path,
        media_type="text/csv",
        filename="logbooktest.csv"
    )

@router.get("/airports.csv")
def download_airports():
    file_path = "./airports.csv"

    if not os.path.exists(file_path):
        return {
            "success": False,
            "message": "File not found"
        }

    return FileResponse(
        path=file_path,
        media_type="text/csv",
        filename="airports.csv"
    )
@router.get("/my-visits.csv")
def export_my_visits(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    visits = db.query(Visit).filter(
        Visit.user_id == user.id
    ).order_by(Visit.date_visited.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "Date",
        "Airport",
        "Callsign",
        "Notes"
    ])

    # Data
    for v in visits:
        writer.writerow([
            v.date_visited,
            v.airport_id,
            v.callsign or "",
            v.notes or ""
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=my_visits.csv"
        }
    )