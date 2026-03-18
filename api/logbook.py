from flask import Blueprint

from app import _load_visits_csv, current_user_handle, login_required, resolve_visits_csv


logbook_api = Blueprint("logbook_api", __name__)

@logbook_api.route("/api/logbook", methods=["GET"])
@login_required
def api_logbook():
    handle = current_user_handle()

    visits_path = resolve_visits_csv(handle)
    df = _load_visits_csv(visits_path)

    visits = []

    for _, r in df.iterrows():
        visits.append({
            "date": r.get("date_visited"),
            "airport": r.get("airport_id"),
            "callsign": r.get("callsign"),
            "notes": r.get("notes")
        })

    return {
        "count": len(visits),
        "visits": visits
    }