import time

from flask import Blueprint, jsonify, request
from jose import jwt

from app import APP_SESSION_COOKIE, _verify_clerk_token_string, login_required


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

@auth_api.post("/auth/exchange")
def auth_exchange():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "missing_token"}), 400

    # Reject expired tokens immediately (no signature verification needed for exp check)
    try:
        unv = jwt.get_unverified_claims(token) or {}
        exp = unv.get("exp")
        now = int(time.time())

        if isinstance(exp, int) and now >= exp:
            resp = jsonify({"ok": False, "error": "token_expired", "now": now, "exp": exp})
            # ✅ Clear CURRENT auth cookie (host-only) + legacy
            resp.delete_cookie(APP_SESSION_COOKIE, path="/")  # mam_auth
            resp.delete_cookie("session", path="/")
            return resp, 401

    except Exception:
        resp = jsonify({"ok": False, "error": "token_unparseable"})
        resp.delete_cookie(APP_SESSION_COOKIE, path="/")  # mam_auth
        resp.delete_cookie("session", path="/")
        return resp, 400

    # Signature + issuer verification
    claims = _verify_clerk_token_string(token)
    if not claims or not (claims.get("sub") or "").strip():
        resp = jsonify({"ok": False, "error": "invalid_token"})
        resp.delete_cookie(APP_SESSION_COOKIE, path="/")  # mam_auth
        resp.delete_cookie("session", path="/")
        return resp, 401

    resp = jsonify({"ok": True})

    # ✅ Overwrite auth cookie every time (7d) — host-only cookie
    resp.set_cookie(
        APP_SESSION_COOKIE,   # mam_auth
        token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=60 * 60 * 24 * 7,
    )

    # ✅ If we had a "just signed out" brake cookie, clear it on successful exchange
    resp.delete_cookie("mam_signed_out", path="/")

    return resp, 200