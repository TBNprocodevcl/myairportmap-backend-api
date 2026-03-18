from flask import Blueprint, jsonify, request

from app import _validate_cert_keys, _write_json_to_storage, app, PROFILE_HANDLE_COOLDOWN_DAYS, _can_change_handle, _ensure_visits_csv_for_username, _fmt_utc_date, _load_json_from_storage, _migrate_profile_prefs, _profile_prefs_key, _safe_handle_for_avatar, _sanitize_username, _set_handle_change_ts, avatar_ct_key, avatar_key, avatar_url_for_handle, current_user_handle, emit_badge_event_once_if_sharing, get_handle_for_user, is_safe_image_content_type, login_required, set_handle_for_user
import storage_backend

profile_api = Blueprint("profile_api", __name__)

@profile_api.route("/api/profile", methods=["GET"])
@login_required
def api_profile():

    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    current = (current_user_handle() or "").strip().lower()

    if (not current) and user_id:
        mapped = (get_handle_for_user(user_id) or "").strip().lower()
        if mapped and (not mapped.startswith("user_")):
            current = mapped

    handle = current

    avatar = avatar_url_for_handle(handle) if handle else "/static/mam-logo.png"

    prefs = _load_json_from_storage(_profile_prefs_key(handle)) if handle else {}
    certs = prefs.get("achievements_certs") or []

    return jsonify({
        "handle": handle,
        "avatar": avatar,
        "certifications": certs
    })

@profile_api.route("/api/profile/username", methods=["POST"])
@login_required
def api_profile_username():

    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 90-day change rule
    eligible, next_ts = _can_change_handle(user_id)
    if not eligible:
        return jsonify({
            "error": f"User name can be changed every {PROFILE_HANDLE_COOLDOWN_DAYS} days.",
            "next_change": _fmt_utc_date(next_ts or 0)
        }), 429

    data = request.get_json(silent=True) or {}
    handle = _sanitize_username(data.get("handle", ""))

    if not 3 <= len(handle) <= 20:
        return jsonify({
            "error": "User name must be 3–20 characters."
        }), 400

    old = (get_handle_for_user(user_id) or "").strip().lower()
    if old.startswith("user_"):
        old = ""

    try:
        set_handle_for_user(user_id, handle)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    _set_handle_change_ts(user_id)

    if old:
        _migrate_profile_prefs(old, handle)

    _ensure_visits_csv_for_username(handle)

    return jsonify({
        "success": True,
        "handle": handle
    })


@profile_api.route("/api/profile/avatar", methods=["POST"])
@login_required
def api_profile_avatar_upload():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return {"error": "Unauthorized"}, 401

    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return {"error": "Set your user name first."}, 400

    safe = _safe_handle_for_avatar(handle)
    if not safe:
        return {"error": "Invalid user name."}, 400

    # Expect multipart/form-data with file field "avatar"
    f = request.files.get("avatar")
    if not f:
        return {"error": "Missing file."}, 400

    ct = (f.mimetype or "").lower()
    if not is_safe_image_content_type(ct):
        return {"error": "Avatar must be a JPG, PNG, or WebP image."}, 400

    # Size guard (3 MB)
    try:
        f.stream.seek(0, 2)
        size = int(f.stream.tell() or 0)
        f.stream.seek(0)
    except Exception:
        size = 0

    if size <= 0:
        return {"error": "Empty file."}, 400
    if size > 3 * 1024 * 1024:
        return {"error": "Avatar too large (max 3MB)."}, 400

    try:
        f.stream.seek(0)
    except Exception:
        pass
    data = f.stream.read()

    if not data:
        return {"error": "Empty file."}, 400

    # Store raw bytes + content-type sidecar
    key = avatar_key(safe)
    ct_key = avatar_ct_key(safe)

    try:
        try:
            storage_backend.write_bytes(key, data, content_type=ct, cache_control="no-store")
        except TypeError:
            storage_backend.write_bytes(key, data)

        try:
            storage_backend.write_bytes(
                ct_key,
                (ct + "\n").encode("utf-8"),
                content_type="text/plain",
                cache_control="no-store",
            )
        except TypeError:
            storage_backend.write_bytes(ct_key, (ct + "\n").encode("utf-8"))

    except Exception:
        return {"error": "Could not save avatar."}, 500

    # --- DEBUG: confirm we can read what we just wrote (same request) ---
    try:
        ok_exists = False
        try:
            ok_exists = bool(storage_backend.exists(key))
        except Exception:
            ok_exists = False

        try:
            rb = storage_backend.read_bytes(key)
            rb_len = len(rb or b"")
        except Exception:
            rb_len = -1

        app.logger.info(f"[avatar] saved handle={safe} key={key} exists={ok_exists} read_len={rb_len} ct={ct} size={len(data)}")
    except Exception:
        pass
        
    return {"ok": True, "avatar_url": f"/avatar/{safe}"}

@profile_api.route("/api/profile/achievements-certs", methods=["POST"])
@login_required
def api_profile_achievements_certs():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return {"error": "Unauthorized"}, 401

    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return {"error": "Missing user name."}, 400

    payload = request.get_json(silent=True) or {}
    certs = payload.get("certs") or []
    if not isinstance(certs, list):
        certs = []

    certs = _validate_cert_keys([str(x) for x in certs])

    key = _profile_prefs_key(handle)
    prefs = _load_json_from_storage(key)

    # Detect newly-added cert keys so we can publish to Lounge "Recent Achievements"
    prev = prefs.get("achievements_certs") or []
    if not isinstance(prev, list):
        prev = []
    prev = _validate_cert_keys([str(x) for x in prev])

    added = sorted(set(certs) - set(prev))

    prefs["achievements_certs"] = certs
    _write_json_to_storage(key, prefs)

    # Emit badge events (shared-only) for NEW ratings/certifications
    # Lounge filter already whitelists labels containing 'rating'/'certificate'/'certification'.
    if added:
        def _pretty_cert(k: str) -> str:
            k = (k or "").strip().lower()
            if k == "instrument":
                return "Instrument Rating"
            if k == "cfi_i":
                return "CFII"
            if k == "cfi":
                return "CFI"
            if k == "mei":
                return "MEI"
            if k == "uas":
                return "UAS"
            if k == "student_pilot":
                return "Student Pilot"
            if k == "flight_attendant":
                return "Flight Attendant"
            if k == "dispatcher":
                return "Aircraft Dispatcher"
            # patterns: atp_asel, cpl_amel, ppl_ases, etc.
            if "_" in k:
                a, b = k.split("_", 1)
                return f"{a.upper()} ({b.upper()})"
            return k.upper()

        for k in added:
            try:
                emit_badge_event_once_if_sharing(
                    handle=handle,
                    badge_key=f"cert_{k}",
                    badge_label=f"New rating: {_pretty_cert(k)}",
                    meta={"cert_key": k}, # type: ignore
                )
            except Exception:
                pass

    return {"ok": True, "certs": certs, "added": added}

@profile_api.route("/api/profile/logout", methods=["POST"])
@login_required
def api_logout():
    resp = jsonify({
        "ok": True,
        "message": "Logged out"
    })

    # ✅ Xoá cookie auth chính
    resp.delete_cookie("mam_auth", path="/")

    # (optional) Xoá thêm nếu từng dùng
    resp.delete_cookie("session", path="/")
    resp.delete_cookie("mam_web", path="/")

    # (optional) nếu có domain cookie
    resp.delete_cookie("mam_auth", path="/", domain=".myairportmap.com")

    # ✅ Brake cookie (tránh auto login lại)
    resp.set_cookie(
        "mam_signed_out",
        "1",
        max_age=300,   # 5 phút
        secure=True,
        samesite="Lax",
        path="/"
    )

    return resp