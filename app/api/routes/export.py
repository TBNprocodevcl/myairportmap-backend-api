import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse
import os
from flask import Blueprint, jsonify, request, send_file

from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.api.routes.auth import _get_current_user_from_token
from app.db.session import get_db
from app.db.session import SessionLocal
from app.models.visit import Visit

router = APIRouter(prefix="/export", tags=["export"])
flask_bp = Blueprint("export_flask", __name__, url_prefix="/export/export")


def _flask_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise ValueError("Invalid token")
    return auth_header.split(" ", 1)[1].strip()


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


@flask_bp.get("/logbooktest.csv")
def flask_download_logbook_test():
    file_path = "./logbooktest.csv"
    if not os.path.exists(file_path):
        return jsonify({"success": False, "message": "File not found"}), 404
    return send_file(file_path, mimetype="text/csv", as_attachment=True, download_name="logbooktest.csv")


@flask_bp.get("/airports.csv")
def flask_download_airports():
    file_path = "./airports.csv"
    if not os.path.exists(file_path):
        return jsonify({"success": False, "message": "File not found"}), 404
    return send_file(file_path, mimetype="text/csv", as_attachment=True, download_name="airports.csv")


@flask_bp.get("/my-visits.csv")
def flask_export_my_visits():
    db = SessionLocal()
    try:
        try:
            user = _get_current_user_from_token(_flask_token(), db)
        except Exception:
            return jsonify({"detail": "Invalid token"}), 401

        visits = db.query(Visit).filter(
            Visit.user_id == user.id
        ).order_by(Visit.date_visited.desc()).all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Date", "Airport", "Callsign", "Notes"])
        for v in visits:
            writer.writerow([v.date_visited, v.airport_id, v.callsign or "", v.notes or ""])

        bio = io.BytesIO(output.getvalue().encode("utf-8"))
        return send_file(
            bio,
            mimetype="text/csv",
            as_attachment=True,
            download_name="my_visits.csv",
        )
    finally:
        db.close()