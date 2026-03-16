from flask import Blueprint, jsonify
from app import _read_json_storage, avatar_url_for_handle, RUNWAY360_CLUB_KEY

runway_api = Blueprint("runway_api", __name__)


@runway_api.route("/api/runway360/club", methods=["GET"])
def api_runway360_club():
    """
    API for mobile to get Runway 360 Club members
    """

    club = _read_json_storage(RUNWAY360_CLUB_KEY) or {}
    rows = list(club.values())

    # sort newest first
    rows.sort(key=lambda r: (r.get("completed_at") or ""), reverse=True)

    members = []

    for r in rows[:500]:  # safety cap
        handle = (r.get("handle") or "").strip().lower()
        if not handle:
            continue

        members.append({
            "handle": handle,
            "avatar": avatar_url_for_handle(handle),
            "completed_at": r.get("completed_at")
        })

    return jsonify({
        "total_members": len(rows),
        "members": members
    })

@runway_api.route("/api/runway360/recent", methods=["GET"])
def api_runway360_recent():

    rows = runway360_join_log_last(20)

    result = []

    for r in rows:

        handle = (r.get("handle") or "").strip().lower()

        result.append({
            "handle": handle,
            "avatar": avatar_url_for_handle(handle),
            "joined_at": r.get("joined_at")
        })

    return jsonify({
        "recent": result
    })