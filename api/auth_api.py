from flask import Blueprint, jsonify, request

from app import login_required


auth_api = Blueprint("auth_api", __name__)

@auth_api.route("/api/me", methods=["GET"])
@login_required
def me():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    return jsonify({
        "user_id": user_id,
        "authenticated": True
    })