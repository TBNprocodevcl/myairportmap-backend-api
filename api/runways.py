from flask import Blueprint, request, jsonify
from app import (
    login_required,
    current_user_handle,
    has_active_access,
    runway360_is_complete
)

runway_api = Blueprint("runway_api", __name__)

@runway_api.route("/api/runways/card", methods=["GET"])
@login_required
def api_runway_card():

    handle = (current_user_handle() or "").strip().lower()

    if not handle:
        return jsonify({"error": "Unauthorized"}), 401

    if not has_active_access(handle):
        return jsonify({"error": "Subscription required"}), 403

    if not runway360_is_complete(handle):
        return jsonify({"error": "Runway360 not complete"}), 403

    return jsonify({
        "download_url": "/runways/card.png"
    })