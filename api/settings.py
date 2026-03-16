from flask import Blueprint, request

from app import _set_share_activity, current_user_handle, login_required


settings_api = Blueprint("settings_api", __name__)

@settings_api.route("/api/settings/privacy", methods=["POST"])
@login_required
def api_settings_privacy():
    
    handle = (current_user_handle() or "").strip().lower()

    payload = request.json or {}

    enabled = bool(payload.get("share_activity"))

    _set_share_activity(handle, enabled)

    return {
        "ok": True,
        "share_activity": enabled
    }