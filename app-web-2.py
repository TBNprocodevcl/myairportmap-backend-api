# MyAirportMap - Map42
#   - airports CSV (auto-detected)
#   - my_visits.csv
from __future__ import annotations
import random
import boto3
from botocore.config import Config
import io
import os
import re
import csv
import time
import html as _html
import json
import math
from jinja2.utils import F
import requests
from types import SimpleNamespace
import storage_backend
import functools
import tempfile
import pandas as pd
import folium
import secrets
import string
import stripe
import sqlite3
import datetime # module (do not use datetime.now; use _now_utc())
import urllib.parse
from datetime import date, datetime, timezone
from flask import Flask, jsonify, Response, request, send_file, redirect, send_from_directory, session, request, current_app
from functools import lru_cache
from typing import Optional, Dict, List, Any, Tuple
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from jose import jwt
from botocore.exceptions import ClientError
from urllib.parse import urlencode
from urllib.parse import quote
from app.db.session import SessionLocal
from app.api.routes.auth import login as auth_login, register as auth_register
from app.api.routes.auth import flask_bp as auth_flask_bp
from app.api.routes.airports import flask_bp as airports_flask_bp
from app.api.routes.achievements import flask_bp as achievements_flask_bp
from app.api.routes.certifications import flask_bp as certifications_flask_bp
from app.api.routes.export import flask_bp as export_flask_bp
from app.api.routes.runway360 import flask_bp as runway360_flask_bp
from app.api.routes.subcription import flask_bp as subcription_flask_bp
from app.api.routes.upload import flask_bp as upload_flask_bp
from app.api.routes.user import flask_bp as users_flask_bp
from app.api.routes.visits import flask_bp as visits_flask_bp
from app.api.routes import achievements as achievements_api
from app.api.routes import (
    auth as auth_api,
    user as user_api,
    airports as airports_api,
    visits as visits_api,
    achievements as achievements_router_api,
    runway360 as runway360_api,
    export as export_api,
    upload as upload_api,
    certifications as certifications_api,
    subcription as subcription_api,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from app.schemas.user import LoginRequest, RegisterRequest
from app.core.security import decode_access_token
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from folium.plugins import Fullscreen, MarkerCluster
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.env"))
print("CLERK_ISSUER =", os.getenv("CLERK_ISSUER"))

# -----------------------------
# Brand colors (single source of truth)
# -----------------------------
MAM_BLUE = "#1f77ff"
MAM_MAGENTA = "#cc00cc"
MAM_PURPLE = "#7a3db8"  # visit non-towered marker color (Map6 vibe)

# -----------------------------
# Clerk primary email + durable uniqueness (email -> user_id)
# -----------------------------
import hashlib
import requests

CLERK_SECRET_KEY = (os.getenv("CLERK_SECRET_KEY") or "").strip()
CLERK_API_BASE = "https://api.clerk.com/v1"

def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()

def _email_key(email: str) -> str:
    """
    Never use raw email in object keys.
    Use a stable SHA256-based key (privacy + safe path characters).
    """
    e = _normalize_email(email)
    if not e:
        return ""
    return hashlib.sha256(e.encode("utf-8")).hexdigest()

def _user_by_email_key(email: str) -> str:
    return f"users/by_email/{_email_key(email)}.json"

def clerk_fetch_user(user_id: str) -> dict | None:
    """
    Fetch Clerk user via Backend API to resolve primary email.
    Requires CLERK_SECRET_KEY (server-side only).
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return None
    if not CLERK_SECRET_KEY:
        # Local auth mode may not have Clerk configured.
        return None

    url = f"{CLERK_API_BASE}/users/{user_id}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def clerk_primary_email_for_user_id(user_id: str) -> str | None:
    """
    Resolve actual email using:
      primary_email_address_id -> email_addresses[].id match
    """
    u = clerk_fetch_user(user_id)
    if not u:
        return None

    primary_id = u.get("primary_email_address_id")
    emails = u.get("email_addresses") or []
    for e in emails:
        if e.get("id") == primary_id:
            return _normalize_email(e.get("email_address") or "")

    # fallback: first email if primary missing
    if emails:
        return _normalize_email(emails[0].get("email_address") or "")

    return None


def local_primary_email_for_user_id(user_id: str) -> str | None:
    """
    Resolve email from the local users table by UUID user_id.
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return None

    try:
        from uuid import UUID
        from app.models.user import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == UUID(user_id)).first()
            if not user:
                return None
            return _normalize_email(getattr(user, "email", "") or "") or None
        finally:
            db.close()
    except Exception:
        return None

def load_user_by_email(email: str) -> dict | None:
    key = _user_by_email_key(email)
    if not key:
        return None
    return _storage_get_json(key)

def write_user_by_email(email: str, obj: dict) -> None:
    key = _user_by_email_key(email)
    if not key:
        raise RuntimeError("Cannot write user_by_email: missing email")
    _storage_put_json(key, obj)

def ensure_email_uniqueness_index(*, user_id: str, email: str) -> None:
    """
    Enforce: one row per email.
    If email is already mapped to a different Clerk user_id, raise.
    Otherwise, write/confirm mapping email -> user_id.
    """
    user_id = (user_id or "").strip()
    email = _normalize_email(email)
    if not user_id:
        raise RuntimeError("ensure_email_uniqueness_index: missing user_id")
    if not email or "@" not in email:
        raise RuntimeError("ensure_email_uniqueness_index: invalid email")

    existing = load_user_by_email(email)
    if existing:
        existing_user_id = (existing.get("user_id") or "").strip()
        if existing_user_id and existing_user_id != user_id:
            # 🚨 HARD LOCK: email already belongs to someone else
            raise RuntimeError(
                f"Email collision: {email} is already linked to a different user_id."
            )
        return  # already mapped correctly

    # Create new mapping
    write_user_by_email(email, {
        "email": email,
        "user_id": user_id,
        "created_at": _now_utc().isoformat().replace("+00:00", "Z"),
        "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
        "v": 1,
    })

def bootstrap_user_identity_from_clerk(*, claims: dict) -> dict:
    """
    Resolve and return canonical identity:
      { user_id, email }
    and enforce email uniqueness mapping.
    """
    claims = claims or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        raise RuntimeError("bootstrap_user_identity_from_clerk: missing claims.sub")

    email = clerk_primary_email_for_user_id(user_id)
    if not email:
        raise RuntimeError("bootstrap_user_identity_from_clerk: could not resolve primary email")

    # 1) Persist email into user meta (by_id) for your own use
    patch_user_meta(user_id, {"email": email})

    # 2) Enforce global uniqueness: one account per email
    ensure_email_uniqueness_index(user_id=user_id, email=email)

    return {"user_id": user_id, "email": email}

# -----------------------------
# Canonical helper: get email for current user (fast-path + fallback)
# -----------------------------
def get_email_for_current_user(*, claims: dict | None = None) -> str | None:
    """
    Return the canonical email for the currently authenticated user.

    Order:
      1) Fast-path: durable user meta (users/by_id/<user_id>.json)
      2) Fallback: Clerk API (primary_email_address_id)
         - Persist email into meta
         - Enforce global uniqueness (email -> user_id)

    Returns:
      email (lowercase) or None

    Raises:
      RuntimeError on email collision (hard lock)
    """
    claims = claims or (getattr(request, "clerk_claims", {}) or {})
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return None

    # 1) Fast path: durable meta
    try:
        meta = load_user_meta(user_id) or {}
        email = _normalize_email(meta.get("email") or "")
        if email and "@" in email:
            return email
    except Exception as e:
        print("[AUTH] get_email_for_current_user meta read failed:", repr(e))

    # 2) Fallback: resolve from local DB, persist, and lock
    email = local_primary_email_for_user_id(user_id)
    if not email:
        # 3) Optional Clerk fallback for environments that still provide Clerk claims
        email = clerk_primary_email_for_user_id(user_id)
        if not email:
            return None

    # Persist + enforce uniqueness (collision should HARD FAIL)
    patch_user_meta(user_id, {"email": email})
    ensure_email_uniqueness_index(user_id=user_id, email=email)

    return email


def current_user_email() -> str | None:
    return get_email_for_current_user()

# -----------------------------
# Clerk / App base URLs (normalized)
# -----------------------------
CLERK_ISSUER = (os.getenv("CLERK_ISSUER") or "").strip().rstrip("/")
APP_BASE_URL = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")

# Defensive default: if APP_BASE_URL isn't set, fall back to Render/host at runtime when needed.
# (Prefer setting APP_BASE_URL in Render env vars.)


APP_VERSION = os.getenv("APP_VERSION") or "Map42"
GIT_SHA = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "").strip()
if GIT_SHA:
    GIT_SHA = GIT_SHA[:7]
print(f"APP VERSION: {APP_VERSION} / {GIT_SHA or 'unknown'}")

# --- Canonical copy (do not drift) ---
BRAND_HEADLINE = "MyAirportMap.com"
BRAND_STATEMENT = (
    "MyAirportMap.com is a logbook companion that celebrates safe flying, airport exploration, "
    "and the pilot community.<br>"
    "It answers the simple question every pilot is asked: “Where have you flown?”"
)
BADGE_TIE_IN = "State achievements appear when flights are logged."
# Payment portal (Stripe later). Keep safe stub for Map31.
PAY_PORTAL_URL = (os.environ.get("PAY_PORTAL_URL") or "").strip()

_DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([A-Z0-9]+)\s+(\([A-Z0-9]+\))\s*(.*)$")
_FLOAT_RE = re.compile(r"^\d+(?:\.\d+)?$")

from flask import Flask
app = Flask(__name__)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

@app.route("/.well-known/<path:filename>")
def well_known(filename):
    return send_from_directory("well-known", filename)

app.register_blueprint(auth_flask_bp)
app.register_blueprint(airports_flask_bp)
app.register_blueprint(achievements_flask_bp)
app.register_blueprint(certifications_flask_bp)
app.register_blueprint(export_flask_bp)
app.register_blueprint(runway360_flask_bp)
app.register_blueprint(subcription_flask_bp)
app.register_blueprint(upload_flask_bp)
app.register_blueprint(users_flask_bp)
app.register_blueprint(visits_flask_bp)

from functools import wraps

def _has_clerk_session_cookie() -> bool:
    ck = request.cookies or {}
    if "__session" in ck:
        return True
    # Clerk often uses suffixed cookies like __session_PlylUUXM
    return any(k.startswith("__session_") for k in ck.keys())
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # DEV BYPASS (LOCAL ONLY)
        if app.debug and os.getenv("MAP20_DEV_BYPASS_AUTH") == "1":
            request.clerk_claims = {"sub": os.getenv("MAP20_DEV_USER_ID", "demo")}
            return fn(*args, **kwargs)

        claims = verify_clerk_session(request)
        if not claims:
            next_path = quote(request.full_path or "/app", safe="/=?&")
            return redirect(f"/sign-in?next={next_path}", code=302)

        request.clerk_claims = claims

        # ------------------------------------------------------------
        # ✅ Resolve canonical email (fast meta -> Clerk fallback)
        # ✅ Enforce one-account-per-email (HARD LOCK on collision)
        # ------------------------------------------------------------
        try:
            user_id = (claims.get("sub") or "").strip()
            if not user_id:
                return Response("Unauthorized", status=401)

            email = get_email_for_current_user(claims=claims)  # may raise on collision
            if email:
                request.user_id = user_id
                request.user_email = email
                # convenient for downstream billing code that reads claims
                request.clerk_claims["email"] = email
            else:
                # Email couldn't be resolved (usually missing CLERK_SECRET_KEY or Clerk API issue)
                request.user_id = user_id
                request.user_email = None

        except Exception as e:
            # Fail closed (better than silent duplicate accounts)
            print("[AUTH] email bootstrap failed:", repr(e))
            return Response("This email is already linked to another account.", status=403)

        # ------------------------------------------------------------
        # ✅ MyAirportMap user name onboarding gate
        #
        # If the user name is missing (or still the default user_*),
        # force them through /onboard/handle before any private page.
        # ------------------------------------------------------------
        try:
            user_id = (claims.get("sub") or "").strip()
            path = request.path or "/"

            # Allow these paths to function without onboarding loops
            allow_prefixes = (
                "/static/",
                "/favicon",
                "/logo.png",
            )
            allow_exact = {
                "/sign-in",
                "/sign-out",
                "/signed-out",
                "/onboard/handle",
                "/api/onboard/handle",
                "/profile",
                "/api/profile/username",
            }

            if user_id and (path not in allow_exact) and (not path.startswith(allow_prefixes)):
                current = (get_handle_for_user(user_id) or "").strip()
                if (not current) or current.startswith("user_"):
                    return redirect("/onboard/handle", code=302)
        except Exception:
            # Never block auth if onboarding check fails unexpectedly
            pass

        return fn(*args, **kwargs)

    return wrapper

# --- R2 client helpers (for global event feed) ---
_R2_CLIENT = None

def _r2_client():
    """
    Create (and cache) an S3-compatible client for Cloudflare R2 using env vars.
    Returns None if not configured, so callers can safely fall back.
    """
    global _R2_CLIENT
    if _R2_CLIENT is not None:
        return _R2_CLIENT

    endpoint = (os.environ.get("R2_ENDPOINT_URL") or "").strip()
    key_id = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()

    if not (endpoint and key_id and secret):
        return None

    _R2_CLIENT = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return _R2_CLIENT


def _env(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""

def _r2_enabled() -> bool:
    return bool(
        _env("R2_ENDPOINT_URL", "CLOUDFLARE_R2_ENDPOINT_URL", "AWS_ENDPOINT_URL")
        and _env("R2_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        and _env("R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
        and _env("R2_BUCKET_NAME", "R2_BUCKET")
    )

def _r2_diag() -> dict:
    return {
        "endpoint": bool(_env("R2_ENDPOINT_URL", "CLOUDFLARE_R2_ENDPOINT_URL", "AWS_ENDPOINT_URL")),
        "access_key": bool(_env("R2_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")),
        "secret": bool(_env("R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")),
        "bucket": bool(_env("R2_BUCKET_NAME", "R2_BUCKET")),
    }


def _r2_bucket() -> str | None:
    b = (os.environ.get("R2_BUCKET_NAME") or "").strip()
    return b or None

def emit_badge_event(*, handle: str, badge_label: str, ts_iso: str | None = None) -> None:
    """
    Emit a global badge event to R2 under events/badges/ so Pilot's Lounge can list it.
    Best-effort: never raises.
    """
    try:
        if not _r2_enabled():
            return

        s3 = _r2_client()
        bucket = _r2_bucket()
        if not s3 or not bucket:
            return

        h = (handle or "").strip().lower()
        if not h:
            return

        # Use UTC timestamp for sortability + uniqueness
        ts = (ts_iso or _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")).strip()
        safe_ts = ts.replace(":", "").replace("-", "")  # yyyymmddThhmmssZ-ish
        # Key sorts newest-first when listed + reversed
        key = f"events/badges/{safe_ts}_{h}_bravo.json"

        payload = {
            "ts": ts,
            "handle": h,
            "badge_label": badge_label,
            "type": "badge",
            "badge_type": "bravo",
        }

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    except Exception:
        return

def emit_badge_event_once_if_sharing(*, handle: str, badge_key: str, badge_label: str) -> None:
    """
    Emit a global badge event to events/badges/ (Recent Achievements feed),
    but ONLY if the user has share_activity enabled.

    Payload/key format matches _record_new_badge_events().
    Best-effort: never raises.
    """
    try:
        handle = (handle or "").strip().lower()
        badge_key = (badge_key or "").strip().lower()
        badge_label = (badge_label or "").strip()

        if not handle or not badge_key or not badge_label:
            return

        if not _get_share_activity(handle):
            return

        if not _r2_enabled():
            return

        s3 = _r2_client()
        bucket = _r2_bucket()
        if not s3 or not bucket:
            return

        payload = {
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            "handle": handle,
            "badge_key": badge_key,
            "badge_label": badge_label,
        }

        key = _event_key("badges", handle, badge_key)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-store",
        )
    except Exception:
        return


def build_bravo_airport_html(*, df_badges: pd.DataFrame, bravo_targets: set[str], visited_ids: set[str]) -> str:
    try:
        name_by_norm = {}
        if df_badges is not None and not df_badges.empty:
            # assumes df_badges has norm_id + ARPT_NAME already normalized
            for _, r in df_badges.iterrows():
                nid = (r.get("norm_id") or "").strip().upper()
                if nid and nid not in name_by_norm:
                    name_by_norm[nid] = (
                        r.get("ARPT_NAME")
                        or r.get("name")
                        or r.get("airport_name")
                        or r.get("facility_name")
                        or "Unknown"
                    )

        rows = []
        for nid in sorted(bravo_targets):
            is_visited = nid in (visited_ids or set())
            icon = "✅" if is_visited else "⭕"
            style = "color:#4caf50;" if is_visited else "color:#666;"
            nm = name_by_norm.get(nid, "Unknown")
            rows.append(
                f'<div class="airport-row" style="padding:4px 0; border-bottom:1px solid #333; {style}">'
                f'{icon} <b>{nid}</b> - {nm}'
                f"</div>"
            )
        return "".join(rows)
    except Exception as e:
        print("[bravo][list_err]", repr(e))
        return ""

# -----------------------------
# Durable user meta (user_id -> handle)
# -----------------------------
import json
def _user_meta_key(user_id: str) -> str:
    return f"users/by_id/{user_id}.json"

# -----------------------------
# Compatibility aliases (old names)
# -----------------------------

def _read_object_bytes(key: str) -> bytes | None:
    try:
        return storage_backend.read_bytes(key)
    except FileNotFoundError:
        return None
    except AttributeError:
        try:
            return storage_backend.get_bytes(key)
        except FileNotFoundError:
            return None

def _write_object_bytes(key: str, data: bytes, *,
                        content_type: str | None = None,
                        cache_control: str | None = None) -> None:
    """Back-compat: older code expected this name."""
    try:
        storage_backend.write_bytes(
            key,
            data,
            content_type=content_type,
            cache_control=cache_control,
        )
    except TypeError:
        # some backends don't accept kwargs
        storage_backend.write_bytes(key, data)

def get_handle_for_user_legacy(user_id: str) -> str:
    """Back-compat shim."""
    return get_handle_for_user(user_id)

def storage_get_bytes(key: str) -> bytes | None:
    if READ_FN is None:
        raise RuntimeError("READ_FN not bound to a storage read function")
    return READ_FN(key)

def storage_put_bytes(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
    if WRITE_FN is None:
        raise RuntimeError("WRITE_FN not bound to a storage write function")
    # If your write fn doesn't accept content_type, wrap it here.
    try:
        return WRITE_FN(key, data, content_type=content_type)
    except TypeError:
        return WRITE_FN(key, data)

def _storage_get_json(key: str) -> dict | None:
    raw = storage_get_bytes(key)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _storage_put_json(key: str, obj: dict) -> None:
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    storage_put_bytes(key, b, content_type="application/json")

def load_user_meta(user_id: str) -> dict:
    if not user_id:
        return {}
    meta = _storage_get_json(_user_meta_key(user_id))
    return meta or {"user_id": user_id}

def patch_user_meta(user_id: str, patch: dict) -> dict:
    meta = load_user_meta(user_id)
    meta.update(patch or {})
    meta["user_id"] = user_id
    meta["updated_at"] = _now_utc().isoformat().replace("+00:00", "Z")
    _storage_put_json(_user_meta_key(user_id), meta)
    return meta

def get_handle_for_user(user_id: str) -> str | None:
    meta = load_user_meta(user_id)
    h = (meta.get("handle") or "").strip().lower()
    return h or None

def set_handle_for_user_durable(user_id: str, handle: str) -> dict:
    return patch_user_meta(user_id, {"handle": handle.strip().lower()})


_URL_RE = re.compile(r"(https?://[^\s<]+)", re.IGNORECASE)

def linkify_text(s: str) -> str:
    """
    Escape text for HTML, then convert http(s)://... to clickable links.
    Safe because we escape first, then only inject controlled <a> tags.
    """
    s = _html.escape(s or "")
    if not s:
        return ""
    s = s.replace("\n", "<br>")
    return _URL_RE.sub(
        lambda m: (
            f'<a href="{m.group(1)}" target="_blank" '
            f'rel="noopener noreferrer">{m.group(1)}</a>'
        ),
        s
    )

# -----------------------------
# Storage shims (bind to your real durable storage funcs)
# -----------------------------

READ_FN = _read_object_bytes
WRITE_FN = _write_object_bytes

# ─────────────────────────────────────────────
# Time helpers (module-safe)
# NOTE: this file uses `import datetime` (module)
# ─────────────────────────────────────────────
def _now_utc():
    """Return current UTC datetime (module-safe)."""
    return datetime.datetime.now(datetime.timezone.utc)

def _today_utc_date() -> date:
    """Return today's UTC date."""
    return _now_utc().date()

def _iso_utc(dt_obj) -> str:
    """Return an ISO string with Z suffix (best-effort)."""
    try:
        return dt_obj.isoformat().replace("+00:00", "Z")
    except Exception:
        return str(dt_obj)


def _verify_clerk_token_string(token: str):
    """
    Verify a raw token string (not pulled from request),
    using your existing JWKS + issuer logic.
    """
    class _FakeReq:
        headers = {}
        cookies = {"__session": token}

    return verify_clerk_session(_FakeReq())

def _settings_key(handle: str) -> str:
    h = (handle or "").strip().lower()
    return f"users/{h}/settings.json"

def _id_map_key(user_id: str) -> str:
    u = (user_id or "").strip()
    return f"users_by_id/{u}.json"

def _slugify_handle(raw: str) -> str:
    raw = (raw or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        return ""
    # keep it sane
    return raw[:24]


def resolve_handle_for_user_id(user_id: str, claims: dict | None = None) -> str:
    user_id = (user_id or "").strip()
    if not user_id:
        return "demo"

    chosen = (get_handle_for_user(user_id) or "").strip()
    if chosen:
        return chosen

    # 1) Cached mapping
    try:
        b = storage_backend.get_bytes(_id_map_key(user_id))
        if b:
            obj = json.loads(b.decode("utf-8"))
            h = (obj.get("handle") or "").strip()
            if h:
                return h
    except Exception:
        pass

    claims = claims or {}
    # 2) Derive from Clerk-provided fields if present
    # (Depending on token template, these may or may not exist.)
    cand = ""
    for k in ("username", "preferred_username", "handle"):
        cand = (claims.get(k) or "").strip()
        if cand:
            break

    if not cand:
        email = (claims.get("email") or claims.get("email_address") or "").strip().lower()
        if "@" in email:
            cand = email.split("@", 1)[0]

    cand = _slugify_handle(cand)
    if not cand:
        cand = _slugify_handle("user_" + user_id[-8:]) or "user"

    # 3) Persist mapping
    try:
        storage_backend.put_bytes(
            _id_map_key(user_id),
            json.dumps({"user_id": user_id, "handle": cand}).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        pass

    return cand



def _badges_cache_key(handle: str) -> str:
    return f"users/{handle}/badges.json"

def _event_key(prefix: str, handle: str, badge_key: str) -> str:
    # lexicographically sortable by timestamp for easy 'latest N' retrieval
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    rand = ''.join(random.choice('0123456789abcdef') for _ in range(8))
    safe_handle = ''.join(ch for ch in handle.lower() if ch.isalnum() or ch in ('-','_'))
    safe_badge = ''.join(ch for ch in badge_key.lower() if ch.isalnum() or ch in ('-','_'))
    return f"events/{prefix}/{ts}_{safe_handle}_{safe_badge}_{rand}.json"

def _read_json_r2(key: str) -> dict | None:
    if not _r2_enabled():
        return None

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return None

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        raw = obj["Body"].read()
        return json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return None


def _write_json_r2(key: str, data: dict) -> None:
    if not _r2_enabled():
        return

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return

    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(data).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-store",
        )
    except Exception:
        # Best-effort: never break a request because R2 is flaky.
        return

def canonical_handle_for_request() -> str | None:
    """
    Returns the best handle to use for UI + redirects.
    Priority:
      1) If logged in and user has a chosen handle: return it
      2) Else if trial cookie exists: return trial handle
      3) Else: None
    IMPORTANT: never returns raw 'user_...' clerk sub.
    """
    # 1) Logged-in: try to resolve chosen handle from Clerk sub
    claims = getattr(request, "clerk_claims", {}) or {}
    sub = (claims.get("sub") or "").strip()

    if sub:
        # Replace with your existing mapping getter:
        # e.g., handle = get_handle_for_user((sub)
        handle = get_handle_for_user(sub)  # <-- you already have some mapping function
        if handle and is_valid_handle(handle) and not handle.startswith("user_"):
            return handle

    # 2) Trial cookie / anon identity (replace with your current trial handle getter)
    trial = (request.cookies or {}).get("trial_handle") or (request.cookies or {}).get("handle")
    if trial and is_valid_handle(trial):
        return trial

    return None

def _get_share_activity(handle: str) -> bool:
    """
    Community sharing master switch (Map41 Option A).

    - New users default OFF (opt-in public) via share_activity.
    - Back-compat: if share_activity is unset, honor legacy public_share_enabled if present.
    """
    h = (handle or "").strip().lower()
    if not h:
        return False

    data = _read_json_r2(_settings_key(h)) or {}

    # Canonical Map41 flag
    if "share_activity" in data:
        return bool(data.get("share_activity"))

    # Back-compat for legacy users
    if "public_share_enabled" in data:
        legacy = bool(data.get("public_share_enabled"))
        # Optional write-through so UI stops depending on legacy key
        try:
            data["share_activity"] = legacy
            _write_json_r2(_settings_key(h), data)
        except Exception:
            pass
        return legacy

    return False

def _set_share_activity(handle: str, enabled: bool) -> None:
    h = (handle or "").strip().lower()
    if not h:
        return

    cur = _read_json_r2(_settings_key(h)) or {}
    cur["share_activity"] = bool(enabled)
    _write_json_r2(_settings_key(h), cur)

    # -----------------------------
    # Map41: sync Pilot's Lounge directory
    # -----------------------------
    try:
        directory_upsert_public(
            h,
            share_on=bool(enabled),
            avatar_url=f"/avatar/{h}",
            airports=_get_unique_airport_count(h),
        )
    except Exception:
        pass

def is_public_share_enabled(handle: str) -> bool:
    """
    Public visibility gate (Map41).

    Single source of truth:
    - Backed by share_activity setting.
    - Default: OFF (opt-in public).
    - Paid status does NOT imply public visibility.
    """
    try:
        return _get_share_activity(handle)
    except Exception:
        return False

TOS_EFFECTIVE_DATE = "January 2026"

def render_terms_html() -> str:
    """
    Returns the Terms of Use HTML fragment inserted into /terms.
    Keep this as a pure HTML string (no Response).
    """
    eff = _html.escape((globals().get("TOS_EFFECTIVE_DATE") or "January 2026"))
    ver = _html.escape((globals().get("TOS_VERSION") or ""))
    brand = "MyAirportMap"

    return f"""
<div style="line-height:1.55;color:#e6e9ef;font-size:14px;">
  <div style="font-weight:950;font-size:16px;margin-bottom:6px;">Terms of Use</div>
  <div style="color:#b9c0cc;margin-bottom:14px;">
    Effective: <b>{eff}</b>{(" • Version: <b>"+ver+"</b>") if ver else ""}
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">1) What this is</div>
    <div style="color:#d7dbe3;">
      {brand} is a flying experience companion that turns your logbook into a personal map and achievements.
      It is intended for post-flight reflection, sharing, and community building.
      It has no navigational value and must not be used while operating an aircraft.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">2) Safety disclaimer</div>
    <div style="color:#d7dbe3;">
      Do not use {brand} during flight operations, taxi, takeoff, approach, or landing.
      The pilot in command is solely responsible for the safe operation of the aircraft and compliance with all applicable laws, rules, and procedures.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">3) The airports</div>
    <div style="color:#d7dbe3;">
      After much consideration, we include only airports that are publicly accessible and have hard-surfaced runways.
      This is not a complete list of all airports.
      We currently do not include military or private airports.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">4) Data & accuracy</div>
    <div style="color:#d7dbe3;">
      Imported files, airport data, and derived achievements may contain errors or omissions.
      Verify anything important independently.
      We make no guarantees regarding completeness or accuracy.
      Information you upload is not independently verified.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">5) Public sharing</div>
    <div style="color:#d7dbe3;">
      Public pages are visible only when explicitly enabled by the user and require an active paid plan.
      If you share a profile link, you control what is visible by toggling public sharing on the Manage Visits page.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">6) No affiliation</div>
    <div style="color:#d7dbe3;">
      {brand} is an independent product and is not affiliated with any EFB provider, aviation authority, or aircraft manufacturer.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">7) Intellectual property</div>
    <div style="color:#d7dbe3;">
      {brand} and all associated software, source code, design elements, features, and content are owned by Blue Nexus, LLC and are protected by United States and international copyright laws.
      No part of the service may be copied, modified, distributed, or reverse engineered without prior written permission.
      <br><br>
      © 2026 Blue Nexus, LLC. All rights reserved.
    </div>
  </div>

  <div style="margin-bottom:14px;">
    <div style="font-weight:900;margin-bottom:6px;">8) Fees</div>
    <div style="color:#d7dbe3;">
      After the initial 30-day trial period, continued access to {brand} requires a yearly membership fee of $22.00 USD.
      Promotional codes may occasionally be offered.
      The yearly membership provides full access and is billed annually.
      Users may cancel at any time to prevent renewal.
      Payments are processed by Stripe, and charges will appear as Blue Nexus, LLC.
    </div>
  </div>

  <div style="margin-bottom:0;">
    <div style="font-weight:900;margin-bottom:6px;">9) Changes</div>
    <div style="color:#d7dbe3;">
      We may update these Terms from time to time.
      Continued use of the service after changes constitutes acceptance of the updated Terms.
    </div>
  </div>
</div>
"""

TOS_KEY = "users/_tos.json"   # object key in R2, local path under BASE_DIR when not on R2

def _tos_path_local() -> str:
    return os.path.join(BASE_DIR, TOS_KEY)

def _tos_storage_key() -> str:
    try:
        if getattr(storage_backend, "_r2_enabled", lambda: False)():
            return TOS_KEY
    except Exception:
        pass
    return _tos_path_local()

def _read_tos_map() -> dict:
    key = _tos_storage_key()
    try:
        if not storage_backend.exists(key):
            return {}
        raw = storage_backend.read_bytes(key) or b"{}"
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print("_read_tos_map failed:", "key=", key, "err=", repr(e))
        return {}

def _write_tos_map(m: dict) -> None:
    key = _tos_storage_key()
    if not isinstance(m, dict):
        m = {}
    payload = json.dumps(m, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        if key.startswith(BASE_DIR):
            os.makedirs(os.path.dirname(key), exist_ok=True)

        try:
            storage_backend.write_bytes(
                key,
                payload,
                content_type="application/json",
                cache_control="no-store",
            )
        except TypeError:
            storage_backend.write_bytes(key, payload)

    except Exception as e:
        print("_write_tos_map failed:", "key=", key, "err=", repr(e))

def _tos_accepted_for_user_id(user_id: str) -> bool:
    user_id = (user_id or "").strip()
    if not user_id:
        return False
    m = _read_tos_map()
    entry = m.get(user_id)
    if not isinstance(entry, dict):
        return False
    return (entry.get("tos_version") or "") == TOS_VERSION


def _compute_badge_catalog(df_visits: pd.DataFrame) -> list[dict]:
    """Compute earned badges from current totals only (no timeline)."""
    if df_visits is None or df_visits.empty or "airport_id" not in df_visits.columns:
        return []
    airports = (
        df_visits["airport_id"].astype(str).str.strip().str.upper()
        .replace({"": pd.NA}).dropna().unique()
    )
    n_airports = int(len(airports))

    # State + towered require airports table mapping (best-effort)
    n_states = 0
    towered_count = 0
    try:
        df_airports = load_airports_cached()
        st_map = dict(zip(df_airports["airport_id"].astype(str).str.upper(), df_airports["state"].astype(str)))
        tw_map = dict(zip(df_airports["airport_id"].astype(str).str.upper(), df_airports["towered_status"].astype(str)))
        states = set()
        for a in airports:
            st = (st_map.get(a) or "").strip().upper()
            if st and st not in ("PR","VI","GU","AS","MP"):
                states.add(st)
            if (tw_map.get(a) or "").lower().startswith("towered"):
                towered_count += 1
        n_states = len(states)
    except Exception:
        n_states = 0
        towered_count = 0

    badges: list[dict] = []
    for t in [1,5,10,25,50,100,200,300,500]:
        if n_airports >= t:
            badges.append({"key": f"airports_{t}", "label": f"{t} Airports"})
    if n_states > 0:
        for t in [1,5,10,20,30,40,48]:
            if n_states >= t:
                badges.append({"key": f"states_{t}", "label": f"{t} States"})
    if towered_count > 0:
        for t in [1,5,10,25,50,100]:
            if towered_count >= t:
                badges.append({"key": f"towered_{t}", "label": f"{t} Towered"})
    return badges

def _read_badges_cache(handle: str) -> set[str]:
    data = _read_json_r2(_badges_cache_key(handle)) or {}
    earned = data.get("earned") or []
    return set(str(x) for x in earned)

def _write_badges_cache(handle: str, earned_keys: set[str]) -> None:
    _write_json_r2(_badges_cache_key(handle), {"earned": sorted(earned_keys)})

def _record_new_badge_events(handle: str, df_visits: pd.DataFrame) -> None:
    # Always update badge cache; only publish events if user allows sharing.
    catalog = _compute_badge_catalog(df_visits)
    cur = set(b["key"] for b in catalog)
    prev = _read_badges_cache(handle)
    new = [b for b in catalog if b["key"] not in prev]
    _write_badges_cache(handle, cur)

    if not new or not _get_share_activity(handle):
        return

    if not _r2_enabled():
        return

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return

    for b in new:
        payload = {
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            "handle": handle,
            "badge_key": b["key"],
            "badge_label": b["label"],
        }
        key = _event_key("badges", handle, b["key"])
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload).encode("utf-8"),
                ContentType="application/json",
                CacheControl="no-store",
            )
        except Exception:
            # Best-effort: keep going; don't break the page/import flow.
            continue


def get_global_badge_events(limit: int = 10) -> list[dict]:
    """Fetch latest badge events across the platform (best-effort)."""
    if not _r2_enabled():
        return []

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return []

    prefix = "events/badges/"
    keys: list[str] = []
    want = max(limit * 50, 200)  # buffer to avoid over-listing

    try:
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 200}
            if token:
                kwargs["ContinuationToken"] = token

            resp = s3.list_objects_v2(**kwargs)

            for item in resp.get("Contents", []) or []:
                k = item.get("Key")
                if k:
                    keys.append(k)
                if len(keys) >= want:
                    break

            # Stop paginating once we have enough keys
            if len(keys) >= want:
                break

            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                if not token:
                    break
            else:
                break

    except Exception:
        return []

    # De-dupe defensively, then newest-first
    keys = list(dict.fromkeys(keys))
    keys.sort(reverse=True)

    out: list[dict] = []
    for k in keys[:limit]:
        try:
            obj = s3.get_object(Bucket=bucket, Key=k)
            raw = obj["Body"].read()
            if raw:
                out.append(json.loads(raw.decode("utf-8")))
        except Exception:
            continue

    return out

# =========================
# Milestones (Pilot's Lounge)
# =========================

def _milestones_cache_key(handle: str) -> str:
    return f"users/{handle}/milestones.json"

def _read_milestones_cache(handle: str) -> set[str]:
    data = _read_json_r2(_milestones_cache_key(handle)) or {}
    emitted = data.get("emitted") or []
    return set(str(x) for x in emitted)

def _write_milestones_cache(handle: str, emitted_keys: set[str]) -> None:
    _write_json_r2(_milestones_cache_key(handle), {"emitted": sorted(emitted_keys)})

def emit_milestone_once(handle: str, milestone_key: str, label: str, meta: dict | None = None) -> bool:
    """
    Emits a milestone one time per user.
    - Always writes a per-user marker so we never repeat.
    - Writes to global feed ONLY if sharing is enabled.
    Returns True if newly emitted, False if it already existed.
    """
    handle = (handle or "").strip().lower()
    milestone_key = (milestone_key or "").strip().lower()
    if not handle or not milestone_key:
        return False

    if not _r2_enabled():
        # If R2 isn't enabled, we can't persist the dedupe marker or global event reliably.
        return False

    marker_key = f"milestones/{handle}/{milestone_key}.json"
    try:
        if _r2_exists(marker_key):
            return False
    except Exception:
        # If exists-check fails, fail safe: don't emit duplicates.
        return False

    payload = {
        "v": 1,
        "created_at": _utc_now_iso(),
        "handle": handle,
        "type": "milestone",
        "milestone_key": milestone_key,
        "label": label,
        "meta": meta or {},
    }

    # 1) marker (dedupe)
    try:
        _r2_put_json(marker_key, payload)
    except Exception:
        return False

    # 2) global feed event (only if sharing enabled)
    try:
        if _is_public_share_enabled(handle):
            event_key = f"events/milestones/{_ts_key()}_{handle}_{milestone_key}_{_rand6()}.json"
            _r2_put_json(event_key, payload)
    except Exception:
        pass

    return True


def _derive_first_airport_from_visits(df_visits: pd.DataFrame) -> str:
    if df_visits is None or df_visits.empty or "airport_id" not in df_visits.columns:
        return ""
    df = df_visits.copy()
    df["airport_id"] = df["airport_id"].astype(str).str.strip().str.upper()
    df = df[df["airport_id"].ne("")].copy()
    if df.empty:
        return ""

    if "date_visited" in df.columns:
        dt = pd.to_datetime(df["date_visited"], errors="coerce", infer_datetime_format=True, utc=False)
        df["_dt"] = dt
        # earliest meaningful
        df = df.sort_values(by="_dt", ascending=True, na_position="last")
        df = df.drop(columns=["_dt"], errors="ignore")

    return str(df.iloc[0].get("airport_id") or "").strip().upper()

def _derive_first_state_from_visits(df_visits: pd.DataFrame) -> str:
    a = _derive_first_airport_from_visits(df_visits)
    if not a:
        return ""
    try:
        df_airports = load_airports_cached()
        st_map = dict(
            zip(
                df_airports["airport_id"].astype(str).str.upper(),
                df_airports["state"].astype(str).str.upper(),
            )
        )
        st = (st_map.get(a) or "").strip().upper()
        if st and st not in ("PR", "VI", "GU", "AS", "MP"):
            return st
    except Exception:
        pass
    return ""

def _record_new_milestone_events(handle: str, df_visits: pd.DataFrame) -> None:
    """
    Derived-only milestones from visits file:
      - first_airport
      - first_state

    (Joined + First upload are emitted elsewhere.)
    """
    try:
        if not handle or handle == "demo":
            return

        # First airport
        first_airport = _derive_first_airport_from_visits(df_visits)
        if first_airport:
            emit_milestone_once(
                handle,
                "first_airport",
                "First airport",
                meta={"airport_id": first_airport},
            )

        # First state
        first_state = _derive_first_state_from_visits(df_visits)
        if first_state:
            emit_milestone_once(
                handle,
                "first_state",
                "First state",
                meta={"state": first_state},
            )

    except Exception:
        return

def get_global_milestone_events(limit: int = 20) -> list[dict]:
    """Fetch latest milestone events across the platform (best-effort)."""
    try:
        want = max(limit * 50, 200)
    except Exception:
        want = 200
    
    if not _r2_enabled():
        return []

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return []

    prefix = "events/milestones/"
    keys: list[str] = []
    want = max(limit * 50, 200)

    try:
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 200}
            if token:
                kwargs["ContinuationToken"] = token

            resp = s3.list_objects_v2(**kwargs)

            for item in resp.get("Contents", []) or []:
                k = item.get("Key")
                if k:
                    keys.append(k)
                if len(keys) >= want:
                    break

            if len(keys) >= want:
                break

            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                if not token:
                    break
            else:
                break

    except Exception:
        return []

    keys = list(dict.fromkeys(keys))
    keys.sort(reverse=True)

    out: list[dict] = []
    for k in keys[:limit]:
        try:
            obj = s3.get_object(Bucket=bucket, Key=k)
            raw = obj["Body"].read()
            if raw:
                out.append(json.loads(raw.decode("utf-8")))
        except Exception:
            continue

    return out

def _derive_first_airport(df_visits: pd.DataFrame) -> str:
    if df_visits is None or df_visits.empty or "airport_id" not in df_visits.columns:
        return ""
    df = df_visits.copy()

    df["airport_id"] = df["airport_id"].astype(str).str.strip().str.upper()
    df = df[df["airport_id"].ne("")].copy()
    if df.empty:
        return ""

    if "date_visited" in df.columns:
        dt = pd.to_datetime(df["date_visited"], errors="coerce", infer_datetime_format=True, utc=False)
        df["_dt"] = dt
        df = df.sort_values(by="_dt", ascending=True, na_position="last").drop(columns=["_dt"], errors="ignore")

    return str(df.iloc[0].get("airport_id") or "").strip().upper()

def _derive_first_state(df_visits: pd.DataFrame) -> str:
    if df_visits is None or df_visits.empty:
        return ""

    try:
        df_airports = load_airports_cached()
        st_map = dict(
            zip(
                df_airports["airport_id"].astype(str).str.upper(),
                df_airports["state"].astype(str).str.upper(),
            )
        )
    except Exception:
        st_map = {}

    if not st_map:
        return ""

    # choose earliest visit that maps to a real state
    df = df_visits.copy()
    if "airport_id" not in df.columns:
        return ""

    df["airport_id"] = df["airport_id"].astype(str).str.strip().str.upper()
    df = df[df["airport_id"].ne("")].copy()
    if df.empty:
        return ""

    if "date_visited" in df.columns:
        dt = pd.to_datetime(df["date_visited"], errors="coerce", infer_datetime_format=True, utc=False)
        df["_dt"] = dt
        df = df.sort_values(by="_dt", ascending=True, na_position="last").drop(columns=["_dt"], errors="ignore")

    for _, r in df.iterrows():
        a = str(r.get("airport_id") or "").strip().upper()
        st = (st_map.get(a) or "").strip().upper()
        if st and st not in ("PR", "VI", "GU", "AS", "MP"):
            return st

    return ""

def _record_new_milestone_events(handle: str, df_visits: pd.DataFrame) -> None:
    try:
        if not handle or handle == "demo":
            return

        a = _derive_first_airport(df_visits)
        if a:
            emit_milestone_once(handle, "first_airport", "First airport", {"airport_id": a})

        st = _derive_first_state(df_visits)
        if st:
            emit_milestone_once(handle, "first_state", "First state", {"state": st})

    except Exception:
        return


# =========================
# Clerk auth helpers (SINGLE canonical block)
# =========================

from functools import wraps

import os
import requests

CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY")  # your Clerk Secret Key (server-side only)
CLERK_API_BASE = "https://api.clerk.com/v1"

def clerk_get_primary_email(user_id: str) -> str | None:
    """
    Fetch the Clerk user and return the primary email address.
    Uses primary_email_address_id -> email_addresses[*].id match.
    """
    if not CLERK_SECRET_KEY:
        raise RuntimeError("CLERK_SECRET_KEY is not set")

    url = f"{CLERK_API_BASE}/users/{user_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"}, timeout=10)
    r.raise_for_status()
    u = r.json()

    primary_id = u.get("primary_email_address_id")
    emails = u.get("email_addresses") or []
    for e in emails:
        if e.get("id") == primary_id:
            return e.get("email_address")

    # fallback: first email if primary missing (should be rare)
    if emails:
        return emails[0].get("email_address")

    return None


def _safe_unverified(token: str) -> dict:
    """
    Debug helper: NEVER prints the token. Only unverified header + a few unverified claims.
    Safe to enable under AUTH_DEBUG=1.
    """
    out: dict = {}

    # header (unverified)
    try:
        hdr = jwt.get_unverified_header(token)
        out["kid"] = hdr.get("kid")
        out["alg"] = hdr.get("alg")
        out["typ"] = hdr.get("typ")
    except Exception as e:
        out["header_err"] = repr(e)

    # payload (unverified)
    try:
        payload = jwt.get_unverified_claims(token)
        out["iss"] = payload.get("iss")
        out["aud"] = payload.get("aud")
        out["azp"] = payload.get("azp")
        out["sub"] = payload.get("sub")
        out["exp"] = payload.get("exp")
        out["iat"] = payload.get("iat")
    except Exception as e:
        out["payload_err"] = repr(e)

    return out

def _normalize_internal_path(p: str | None, *, default: str = "/logbook") -> str:
    """
    Normalize any incoming path/URL into a safe internal path + optional query.
    Guarantees:
      - Starts with "/"
      - Never protocol-relative ("//")
      - Never ends with "?" or "&"
      - If absolute URL: keeps only path + query
    """
    p = (p or default).strip() or default

    # Normalize absolute URLs to internal path+query
    if p.startswith("http://") or p.startswith("https://"):
        try:
            u = urllib.parse.urlparse(p)
            p = (u.path or default) + (("?" + u.query) if u.query else "")
        except Exception:
            p = default

    # Must be internal and not protocol-relative
    if (not p.startswith("/")) or p.startswith("//"):
        p = default

    # Clean up common junk (prevents "/app?" and "/app?&")
    while p.endswith("?") or p.endswith("&"):
        p = p[:-1]

    # Collapse any accidental "?&" sequences
    p = p.replace("?&", "?")

    # Prevent loops back to auth routes
    if p in ("/login", "/sign-in", "/sign-up") or p.startswith("/login?") or p.startswith("/sign-in?") or p.startswith("/sign-up?"):
        p = default

    return p


def sign_in_redirect(path: str = "/logbook"):
    p = _normalize_internal_path(path, default="/logbook")
    fresh = "1" if request.cookies.get("mam_signed_out") == "1" else "0"

    qs = urlencode({"next": p, "fresh": fresh}, safe="/?=&")
    return redirect(f"/sign-in?{qs}", code=302)


def sign_in_href(path: str = "/logbook") -> str:
    """
    Return a sign-in URL string for use in HTML hrefs.
    """
    p = _normalize_internal_path(path, default="/logbook")
    fresh = "1" if request.cookies.get("mam_signed_out") == "1" else "0"

    qs = urlencode({"next": p, "fresh": fresh}, safe="/?=&")
    return f"/sign-in?{qs}"

# Backwards compatibility alias
clerk_sign_in_redirect = sign_in_redirect
clerk_sign_in_href = sign_in_href


APP_TITLE = "MyAirportMap (Map41): Stable Map42 - Post Beta"
DEFAULT_CENTER = [39.5, -98.35]  # CONUS-ish
DEFAULT_ZOOM = 4

TRIAL_COOKIE = "trial_handle"
TRIAL_MAX_AGE = 60 * 60 * 24 * 365  # keep identity; trial expiry enforced separately (30d)


def _mask(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "…" + s[-2:]
    return s[:4] + "…" + s[-4:]


def render_upgrade_page(next_path: str = "/logbook") -> str:
    # Sanitize next_path again (defense-in-depth)
    next_path = (next_path or "/logbook").strip() or "/logbook"
    if (not next_path.startswith("/")) or next_path.startswith("//"):
        next_path = "/logbook"

    # ✅ On /upgrade (public route), current_user_handle() is often empty because clerk_claims
    # are only attached on login_required routes. Use current_handle() which best-effort verifies.
    handle = (current_handle() or "").strip().lower()

    # -----------------------------
    # Billing readiness (same logic as /upgrade)
    # -----------------------------
    sk = (STRIPE_SECRET_KEY or "").strip()
    price_id = (STRIPE_PRICE_ID_ANNUAL or STRIPE_PRICE_ID or "").strip()
    base_url = (APP_BASE_URL or (getattr(request, "url_root", "") or "")).strip().rstrip("/")

    sk_ok = (sk.startswith("sk_test_") or sk.startswith("sk_live_"))
    sk_is_webhook = sk.startswith("whsec_")

    billing_ready = bool(sk_ok and (not sk_is_webhook) and price_id and base_url)

    if not billing_ready:
        try:
            print("[BILLING_NOT_READY]",
                  "sk=", _mask(sk),
                  "price=", _mask(price_id),
                  "app_base=", base_url,
                  "whsec=", _mask(STRIPE_WEBHOOK_SECRET))
        except Exception:
            pass
        return Response(
            "Billing is not configured (missing/invalid STRIPE_SECRET_KEY, Price ID, or APP_BASE_URL).",
            status=500,
        )

    # ✅ Use THIS (never STRIPE_PRICE_ID_ANNUAL directly)
    line_items = [{"price": price_id, "quantity": 1}]

    # ✅ URL string for links (NOT a redirect Response)
    sign_in_href = f"/sign-in?next={quote('/upgrade?next=' + next_path, safe='/=?&')}&fresh=1"


    # Stripe session creation endpoint
    checkout_action = f"/billing/create-checkout-session?next={quote(next_path, safe='/')}"

    # -----------------------------
    # Primary CTA (sign-in vs join)
    # -----------------------------
    status_html = ""
    if not handle:
        primary_html = f"""
          <a href="{_html.escape(sign_in_href)}"
             style="display:inline-flex;align-items:center;justify-content:center;
                    padding:12px 14px;border-radius:12px;background:#fff;color:#111;
                    font-weight:950;border:1px solid rgba(0,0,0,0.12);text-decoration:none;">
            Sign in to continue
          </a>
        """
        status_html = """
          <div style="margin-top:12px;color:rgba(255,255,255,0.72);font-size:13px;line-height:1.45;">
            Sign in to start membership checkout. Promo code <b>NEWUSER12</b> is entered on Stripe.
          </div>
        """
    else:
        if billing_ready:
            primary_html = f"""
              <form method="POST" action="{_html.escape(checkout_action)}" style="margin:0;">
                <button type="submit"
                        style="display:inline-flex;align-items:center;justify-content:center;
                               padding:12px 14px;border-radius:12px;background:#fff;color:#111;
                               font-weight:950;border:1px solid rgba(0,0,0,0.12);cursor:pointer;">
                  Join Now
                </button>
              </form>
            """
            status_html = """
              <div style="margin-top:12px;color:rgba(255,255,255,0.72);font-size:13px;line-height:1.45;">
                Promo code <b>NEWUSER12</b> is entered during Stripe checkout.
              </div>
            """
        else:
            primary_html = """
              <button type="button" disabled
                      style="display:inline-flex;align-items:center;justify-content:center;
                             padding:12px 14px;border-radius:12px;background:#fff;color:#111;
                             font-weight:950;border:1px solid rgba(0,0,0,0.12);
                             opacity:0.55;cursor:not-allowed;">
                Join Now (Unavailable)
              </button>
            """
            status_html = """
              <div style="margin-top:12px;color:rgba(255,255,255,0.72);font-size:13px;line-height:1.45;">
                <b>Membership checkout isn’t available yet.</b><br>
                Billing isn’t configured (missing Stripe keys / price id / APP_BASE_URL).
              </div>
            """

    # -----------------------------
    # Swipe panels: Pricing + Promo Codes (mobile-friendly)
    # -----------------------------
    pricing_panel = """
      <div class="panel">
        <div class="ptitle">💳 Membership</div>
        <div class="pbig">$22</div>
        <div class="psub">per year</div>
        <div class="pline">Use code <b>NEWUSER12</b> for $10 off your 1st year.</div>
        <div class="pline">First year becomes <b>$12</b>.</div>
        <div class="pline muted">Renews at $22/year unless canceled.</div>
        <div class="pline muted">Check SPAM email folders for verification codes.</div>
      </div>
    """

    promo_panel = """
      <div class="panel">
        <div class="ptitle">🏷️ Promo code</div>
        <div class="pline"><b>NEWUSER12</b> — $10 off your first year.</div>
        <div class="pline muted">Tap <b>Join Now</b> and enter the code during Stripe checkout.</div>
      </div>
    """

    # ✅ Navbar display handle (public route safe):
    # - Never show trial_* (looks broken)
    # - Best-effort: if user is actually logged in on this public route, show their real handle
    navbar_html = ""  # ✅ ensure defined on all paths

    # ✅ Navbar display handle (public route safe)
    navbar_handle = None
    if handle and (not handle.startswith("trial_")):
        navbar_handle = handle
    if not navbar_handle:
        try:
            c = verify_clerk_session(request)  # best-effort; do NOT attach to request
            user_id = (c.get("sub") or "").strip() if c else ""
            if user_id:
                if user_id == "demo":
                    navbar_handle = "demo"
                else:
                    chosen = (get_handle_for_user(user_id) or "").strip()
                    if chosen:
                        navbar_handle = chosen
                    else:
                        try:
                            navbar_handle = (resolve_handle_for_user_id(user_id, claims=c) or "").strip()
                        except Exception:
                            navbar_handle = None
        except Exception:
            pass

    try:
        navbar_html = get_navbar("home", handle=navbar_handle)
    except Exception:
        navbar_html = ""

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Upgrade</title>
  <style>
    body {{
      background:#0f1115; color:#fff;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      margin:0; padding-top:70px;
    }}
    .wrap {{ max-width:720px; margin:30px auto; padding:0 16px; }}
    .card {{
      background:#171a21;
      border:1px solid #2a2f3a;
      border-radius:16px;
      padding:18px;
    }}
    .muted {{ color:#aab2c0; }}
    a.link {{
      color:#dbe9ff;
      text-decoration:underline;
      text-underline-offset:3px;
    }}

    /* ---- swipe row ---- */
    .swipe {{
      margin-top:14px;
      display:flex;
      gap:12px;
      overflow-x:auto;
      -webkit-overflow-scrolling: touch;
      scroll-snap-type:x mandatory;
      padding-bottom:6px;
    }}
    .swipe::-webkit-scrollbar {{ display:none; }}
    .panel {{
      scroll-snap-align:start;
      min-width: min(520px, calc(100vw - 64px));
      background:#12151b;
      border:1px solid #2a2f3a;
      border-radius:16px;
      padding:14px;
    }}
    .ptitle {{ font-weight:950; margin:0 0 8px; }}
    .pbig {{ font-size:34px; font-weight:1000; letter-spacing:-0.02em; line-height:1; }}
    .psub {{ margin-top:2px; color:#aab2c0; font-size:13px; }}
    .pline {{ margin-top:8px; line-height:1.45; }}
    @media (min-width: 740px) {{
      .panel {{ min-width: 320px; }}
    }}

    /* -----------------------------
       MyAirportMap loading overlay (Upgrade page)
       NOTE: f-string safe braces {{ }}
       ----------------------------- */
    #mam-loading {{
      position: fixed;
      inset: 0;
      z-index: 100000;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(10, 12, 16, 0.55);
      backdrop-filter: blur(4px);
      opacity: 0;
      transition: opacity 0.15s ease-out;
    }}
    #mam-loading.mam-visible {{ opacity: 1; }}
    #mam-loading .mam-loader {{ position: relative; width: 92px; height: 92px; }}
    #mam-loading .mam-ring {{
      position: absolute; inset: 0;
      border-radius: 999px;
      border: 6px solid rgba(255, 255, 255, 0.18);
      border-top-color: rgba(255, 255, 255, 0.92);
      animation: mamSpin 0.85s linear infinite;
    }}
    #mam-loading .mam-logo {{
      position: absolute; left: 50%; top: 50%;
      width: 34px; height: 34px;
      transform: translate(-50%, -50%);
      border-radius: 10px;
      box-shadow: 0 10px 26px rgba(0,0,0,0.35);
    }}
    #mam-loading .mam-text {{
      margin-top: 14px;
      text-align: center;
      font-weight: 950;
      font-size: 14px;
      color: rgba(255, 255, 255, 0.92);
      letter-spacing: -0.2px;
    }}
    @keyframes mamSpin {{
      from {{ transform: rotate(0deg); }}
      to   {{ transform: rotate(360deg); }}
    }}
  </style>
</head>
<body>
  {navbar_html}

  <div class="wrap">
    <div class="card">
      <h1 style="margin:0 0 8px;font-weight:950;">Upgrade</h1>
      <div class="muted" style="margin:0 0 14px;">
        Unlock achievements and public sharing features.
      </div>

      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
        {primary_html}
        <a class="link" href="/terms">Terms</a>
      </div>

      {status_html}

      <div class="swipe" aria-label="Membership details">
        {pricing_panel}
        {promo_panel}
      </div>

      <div class="muted" style="margin-top:8px;font-size:12px;line-height:1.4;">
        Tip: swipe left/right for pricing and promo info.
      </div>
    </div>
  </div>

  <!-- ✅ Loading overlay -->
  <div id="mam-loading" aria-label="Loading">
    <div style="display:flex; flex-direction:column; align-items:center;">
      <div class="mam-loader">
        <div class="mam-ring"></div>
        <img class="mam-logo" src="/static/favicon.png" alt="MyAirportMap">
      </div>
      <div class="mam-text">Loading…</div>
    </div>
  </div>

  <script>
  (function () {{
    const el = document.getElementById("mam-loading");
    if (!el) return;

    function show(msg) {{
      try {{
        const t = el.querySelector(".mam-text");
        if (t && msg) t.textContent = msg;
      }} catch (_) {{}}
      el.style.display = "flex";
      requestAnimationFrame(() => {{ el.classList.add("mam-visible"); }});
    }}

    function hide() {{
      el.classList.remove("mam-visible");
      setTimeout(() => {{ el.style.display = "none"; }}, 180);
    }}

    // Show loader when user triggers checkout (Join Now POST)
    document.addEventListener("submit", function (e) {{
      try {{
        const form = e.target;
        const action = (form && form.getAttribute) ? (form.getAttribute("action") || "") : "";
        if (action.indexOf("/billing/create-checkout-session") >= 0) {{
          show("Opening checkout…");
        }}
      }} catch (_) {{}}
    }}, true);

    // Optional: show on internal navigation clicks
    document.addEventListener("click", function (e) {{
      try {{
        const a = e.target && e.target.closest ? e.target.closest("a") : null;
        if (!a) return;
        const href = a.getAttribute("href") || "";
        if (href.startsWith("/") && !href.startsWith("//") && !href.startsWith("/static/") && !href.startsWith("#")) {{
          show("Loading…");
        }}
      }} catch (_) {{}}
    }}, true);

    window.addEventListener("load", function () {{
      setTimeout(() => {{ try {{ hide(); }} catch(_) {{}} }}, 50);
    }});
  }})();
  </script>

</body>
</html>"""

@app.route("/upgrade", methods=["GET"])
def route_upgrade():
    next_path = (request.args.get("next") or "/logbook").strip() or "/logbook"
    if (not next_path.startswith("/")) or next_path.startswith("//"):
        next_path = "/logbook"

    # If logged in + already has access, bounce back
    try:
        h = (current_handle() or "").strip().lower()
        if h and has_active_access(h):
            return redirect(next_path)
    except Exception:
        pass

    resp = Response(render_upgrade_page(next_path), mimetype="text/html")
    return attach_trial_cookie(resp, handle=None)

def current_handle() -> str:
    """
    Canonical handle resolver.

    - If request.clerk_claims is attached (login_required routes), use it.
    - Otherwise, best-effort verify Clerk session to detect logged-in users on public routes
      (DO NOT attach request.clerk_claims here).
    - If logged in: return the user's CHOSEN handle if present; otherwise fallback to a stable derived handle.
    - If not logged in: return persistent per-browser trial handle via cookie (or generate).
    - Dev-bypass: allow local public routes to behave as logged-in when enabled.
    """
    # 1) Prefer attached claims (private/login_required routes)
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    # 2) Dev-bypass convenience (LOCAL ONLY)
    if (not user_id) and app.debug and os.getenv("MAP20_DEV_BYPASS_AUTH") == "1":
        user_id = (os.getenv("MAP20_DEV_USER_ID") or "demo").strip()

    # 3) Public-route best-effort: verify token to detect logged-in user
    if not user_id:
        try:
            c = verify_clerk_session(request)  # best-effort; DO NOT attach to request
            user_id = (c.get("sub") or "").strip() if c else ""
            # use verified claims for fallback handle derivation if needed
            claims = c or claims
        except Exception:
            user_id = ""

    if user_id:
        if user_id == "demo":
            return "demo"

        # ✅ user-owned handle first
        chosen = (get_handle_for_user(user_id) or "").strip()
        if chosen:
            return chosen

        # fallback for brand new users (stable derived handle; persists mapping)
        return resolve_handle_for_user_id(user_id, claims=claims)

    # 4) Trial cookie identity
    h = (request.cookies.get(TRIAL_COOKIE) or "").strip()
    if h:
        return h

    return _new_trial_handle()


def current_user_handle() -> str | None:
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return None

    # Prefer durable mapping
    h = get_handle_for_user(user_id)
    if h:
        return h

    # Fallback: if you have an older in-memory or file-based mapping:
    try:
        h2 = get_handle_for_user_legacy(user_id)  # optional: if exists
        if h2:
            # backfill durable for next time
            set_handle_for_user_durable(user_id, h2)
            return h2
    except Exception:
        pass

    return None

import secrets

def _new_trial_handle() -> str:
    # short, cookie-safe, no PII; 16 chars is plenty
    return "trial_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].lower()

def attach_trial_cookie(resp, handle: str | None):
    """
    Persist a per-browser trial handle for logged-out users only.

    ✅ If cookie already exists AND is non-empty -> keep it.
    ✅ If cookie exists but is empty -> replace it (fixes legacy bug).
    ✅ If not logged in -> ensure cookie is a real trial identity.
    """
    existing = (request.cookies.get(TRIAL_COOKIE) or "").strip()
    if existing:
        return resp

    # If logged in (best-effort), do not set a trial cookie.
    claims = None
    try:
        claims = getattr(request, "clerk_claims", None) or verify_clerk_session(request)
    except Exception:
        claims = getattr(request, "clerk_claims", None)

    user_id = ((claims or {}).get("sub") or "").strip()
    if user_id:
        return resp

    # Determine trial identity
    v = (handle or "").strip().lower()
    if (not v) or v == "demo" or (not v.startswith("trial_")):
        v = _new_trial_handle()

    resp.set_cookie(
        TRIAL_COOKIE,
        v,
        max_age=TRIAL_MAX_AGE,
        samesite="Lax",
        secure=True,
        httponly=True,
        path="/",
    )
    return resp


_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")  # 2–32 chars

def is_valid_handle(handle: str) -> bool:
    if not handle:
        return False
    # Reject Windows-illegal filename chars and path separators
    if any(ch in handle for ch in '<>:"/\\|?*'):
        return False
    if handle in (".", ".."):
        return False
    return bool(_HANDLE_RE.match(handle))

def ensure_user_initialized(handle: str) -> None:
    """
    Ensure both per-user CSVs exist:
      - users/<handle>/my_visits.csv
      - users/<handle>/foreflight_logbook.csv (with ForeFlight sentinel row)
    Only creates if missing. Never resets.

    NOTE: In local dev, older builds stored demo data in BASE_DIR/my_visits.csv.
    We migrate that file into users/<handle>/my_visits.csv once if needed.
    """
    if not handle:
        return
    # Defensive: never touch filesystem for an invalid/unsafe handle.
    if not is_valid_handle(handle):
        return

    # --- 1) visits file ---
    visits_path = user_visits_path(handle)  # IMPORTANT: avoid resolve_visits_csv fallback
    raw_visits = _read_visits_bytes(visits_path, handle=handle)
    if raw_visits is None:
        # Migration: if this is the demo handle and a legacy my_visits.csv exists, copy it over.
        if (not _r2_enabled()) and handle == "demo":
            legacy_path = os.path.join(BASE_DIR, "my_visits.csv")
            try:
                if os.path.exists(legacy_path):
                    with open(legacy_path, "rb") as f:
                        legacy_bytes = f.read()
                    if legacy_bytes:
                        _write_visits_bytes(visits_path, legacy_bytes, handle=handle)
                        raw_visits = legacy_bytes
            except Exception:
                # Fall back to creating an empty file below
                pass

    if raw_visits is None:
        df_empty = pd.DataFrame(columns=["airport_id", "date_visited", "callsign", "notes"])
        buf = io.BytesIO()
        df_empty.to_csv(buf, index=False)
        _write_visits_bytes(visits_path, buf.getvalue(), handle=handle)

    # --- 2) foreflight import file ---
    ff_path = resolve_foreflight_csv(handle)
    raw_ff = _read_foreflight_bytes(ff_path, handle=handle)
    if not raw_ff:
        ff_bytes = write_foreflight_import_csv_bytes([])
        _write_foreflight_bytes(ff_path, ff_bytes, handle=handle)


def current_user_display_handle() -> str:
    """
    UI-friendly identifier for navbar/titles.

    - MUST NOT recurse.
    - MUST NOT change storage keys / CSV paths.
    - Uses canonical handle (current_user_handle) as source of truth,
      and shortens it for display when needed.
    """
    try:
        h = (current_user_handle() or "").strip()
    except Exception:
        h = ""

    if not h:
        return ""

    if h == "demo":
        return "@demo"

    if h.startswith("user_"):
        suffix = h[-4:] if len(h) >= 4 else h
        return f"@user…{suffix}"

    if h.startswith("trial_"):
        return "@trial"

    return f"@{h}"

def current_user_display_label() -> str:
    return (current_user_display_handle() or "").lstrip("@")

def _sanitize_username(raw: str) -> str:
    raw = (raw or "").strip().lower()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    return safe

def _validate_username(safe: str) -> None:
    if not safe:
        raise ValueError("User name is required.")
    if len(safe) < 3:
        raise ValueError("User name is too short (min 3).")
    if len(safe) > 20:
        raise ValueError("User name is too long (max 20).")
    if safe.startswith("user_"):
        raise ValueError("That user name is reserved. Pick something custom.")
    # light reserved list (avoid obvious collisions)
    reserved = {"app", "map", "logbook", "welcome", "admin", "api", "static", "profile", "upgrade"}
    if safe in reserved:
        raise ValueError("That user name is reserved. Choose another.")

import html as _html

def get_navbar(active: str, handle: str | None = None, **_ignored) -> str:
    """
    Private app navbar.
    """
    active = (active or "home").strip() or "home"

    # Choose display name
    display = (handle or "").strip()
    if not display:
        try:
            display = current_user_handle()
        except Exception:
            display = ""

    display = (display or "").strip().lower()

    # UI title: "(username)'s MyAirportMap"
    if display:
        brand_label = f"{_html.escape(display)}&#39;s MyAirportMap"
    else:
        brand_label = "MyAirportMap"

    # ✅ Map40: a brand-new user should never see "trial ended" due to missing/late account records.
    raw_status = ""
    try:
        raw_status = (get_account_status(display) or "").strip().lower()
    except Exception:
        raw_status = ""

    # Normalize + default: treat unknown/missing as trial (prevents false "ended" on day 1)
    if raw_status in ("member", "trial"):
        status = raw_status
    elif raw_status in ("none", "free", "expired", "ended", "inactive", ""):
        status = "trial"
    else:
        status = "trial"

    # Label should align with normalized status (avoid scary messaging)
    try:
        status_label = "✅ Member" if status == "member" else "⏳ Trial"
    except Exception:
        status_label = "⏳ Trial"

    # ✅ Full nav for trial + members
    show_full_nav = (status in ("trial", "member"))

    def tab(label: str, href: str, key: str) -> str:
        is_on = (active == key)
        cls = "tab tab-active" if is_on else "tab tab-inactive"
        return f'<a class="{cls}" href="{href}">{label}</a>'

    tabs_html = ""
    if show_full_nav:
        tabs_html = f"""
        <div class="tabs mam-tabs">
          {tab("Pilot's Lounge", "/logbook", "logbook")}
          {tab("Map", "/map", "map")}
          {tab("Achievements", "/achievements", "achievements")}
        </div>
        """

    # Menu contents vary by status
    if show_full_nav:
        upgrade_link_html = "" if status == "member" else '<a href="/upgrade">Upgrade</a>'
        menu_body = f"""
        <div class="muted"><u>Navigate</u></div>
        <a href="/logbook/manage">Manage Visits</a>
        <a href="/map">Map</a>
        <a href="/achievements">Achievements</a>

        <div class="menusep"></div>

        <div class="muted"><u>Account</u></div>
        <a href="/profile">Profile</a>
        {upgrade_link_html}
        <a href="/terms">Terms of Service</a>
        <a href="/sign-out" class="danger">Sign out</a>
        """
    else:
        menu_body = f"""
        <div class="muted"><u>Account</u></div>
        <a href="/upgrade">Upgrade</a>
        <a href="/terms">Terms of Service</a>
        <a href="/sign-out" class="danger">Sign out</a>
        """

    # Map41: avatar in navbar (always something)
    safe = _safe_handle_for_avatar(display or "")
    avatar_src = f"/avatar/{safe}" if safe else "/static/mam-logo.png"

    try:
        import time as _time
        avatar_v = str(int(_time.time())) if display else "0"
    except Exception:
        avatar_v = "0"

    return f"""
<style>
    :root {{ --mam-nav-h: 88px; }}
    @media (max-width: 900px) {{ :root {{ --mam-nav-h: 112px; }} }}
    @media (max-width: 640px) {{ :root {{ --mam-nav-h: 150px; }} }}

    body {{ padding-top: var(--mam-nav-h, 96px) !important; }}

    html.mam-loading .brandmark-wrap::before {{ opacity: 1; }}
    .brandmark-wrap {{
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .brandmark-wrap::before {{
      content: "";
      position: absolute;
      width: 38px;
      height: 38px;
      border-radius: 999px;
      border: 3px solid rgba(255,255,255,0.20);
      border-top-color: rgba(226,0,116,0.95);
      animation: mamspin 1.0s linear infinite;
      opacity: 0;
      pointer-events: none;
    }}
    @keyframes mamspin {{ to {{ transform: rotate(360deg); }} }}

    @media (max-width: 640px) {{
      .brandmark-wrap::before {{
        width: 34px;
        height: 34px;
        border-width: 3px;
      }}
    }}

    .navwrap {{
      position:fixed; top:0; left:0; right:0; z-index:9999;
      background:linear-gradient(180deg, rgba(15,17,21,0.98), rgba(15,17,21,0.86));
      border-bottom:1px solid rgba(255,255,255,0.10);
      backdrop-filter: blur(10px);
    }}
    .navinner {{
      max-width:980px; margin:0 auto; padding:12px 16px;
      display:flex; align-items:center; justify-content:space-between; gap:12px;
    }}
    .brand {{
      display:flex; align-items:center; gap:10px; text-decoration:none; color:#fff;
      font-weight:950; letter-spacing:-0.2px;
    }}
    .brand img {{ height:28px; width:auto; border-radius:8px; }}
    .tabs {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .tab {{
      display:inline-block; padding:8px 12px; border-radius:999px;
      text-decoration:none; font-weight:900; font-size:12px;
    }}
    .tab-active {{ background:#fff; color:#0f1115; }}
    .tab-inactive {{ background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.14); color:#fff; }}

    .acct {{ position:relative; }}

    .pill {{
      display:inline-flex; align-items:center; gap:8px;
      padding:8px 12px; border-radius:999px;
      background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.14);
      color:#fff; font-weight:950; font-size:12px; cursor:pointer;
      user-select:none;
    }}

    .pill .navavatar {{
      width: 24px;
      height: 24px;
      border-radius: 999px;
      overflow: hidden;
      background: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
    }}
    .pill .navavatar img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}

    .menu {{
      position:absolute; right:0; top:44px; min-width:220px;
      background:#12141a; border:1px solid rgba(255,255,255,0.14);
      border-radius:14px; padding:8px; display:none;
      box-shadow: 0 12px 40px rgba(0,0,0,0.45);
    }}
    .menu a {{
      display:block; padding:10px 10px; border-radius:10px;
      text-decoration:none; color:#fff; font-weight:850; font-size:13px;
    }}
    .menu a:hover {{ background:rgba(255,255,255,0.08); }}
    .muted {{ color:#9aa3b2; font-weight:800; font-size:12px; padding:8px 10px; }}
    .danger {{ color:#ff5a5f !important; }}

    .statusrow {{
      padding:10px 10px;
      border-radius:10px;
      background:rgba(255,255,255,0.06);
      border:1px solid rgba(255,255,255,0.10);
      font-weight:950;
      font-size:13px;
      color:#fff;
      margin-bottom:8px;
    }}
    .menusep {{
      height:1px;
      background:rgba(255,255,255,0.10);
      margin:8px 0;
      border-radius:999px;
    }}

    @media (max-width: 640px) {{
      .navinner {{
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 10px;
      }}
      .brand {{
        width: 100%;
        justify-content: center;
        text-align: center;
      }}
      .brand img {{ height: 24px; }}
      .brand span {{
        font-size: 16px;
        line-height: 1.1;
        white-space: nowrap;
        text-align: center;
      }}
      .tabs {{
        width: 100%;
        justify-content: center;
        gap: 8px;
      }}
      .tab {{
        font-size: 15px;
        padding: 10px 12px;
      }}
      .acct {{
        width: 100%;
        display: flex;
        justify-content: center;
      }}
      .pill {{
        font-size: 15px;
        padding: 10px 14px;
      }}
      .menu {{
        right: 50%;
        transform: translateX(50%);
        top: 48px;
      }}
    }}
</style>

<div class="navwrap">
  <div class="navinner">
    <a class="brand" href="/logbook">
      <span class="brandmark-wrap">
        <img src="/logo.png" alt="MyAirportMap">
      </span>
      <span>{brand_label}</span>
    </a>

    {tabs_html}

    <div class="acct">
      <div class="pill"
           onclick="var m=document.getElementById('acctmenu'); if(!m) return false; m.style.display=(m.style.display==='block'?'none':'block'); return false;">
        <span class="navavatar" aria-hidden="true">
          <img src="{avatar_src}?v={avatar_v}"
               onerror="this.onerror=null;this.src='/static/mam-logo.png';"
               alt="Avatar">
        </span>
        <span>Menu ▾</span>
      </div>

      <div class="menu" id="acctmenu">
        <div class="statusrow">{_html.escape(status_label)}</div>
        {menu_body}
      </div>
    </div>
  </div>
</div>

<script>
  window.addEventListener("click", (e) => {{
    const m = document.getElementById("acctmenu");
    if (!m) return;
    const pill = m.parentElement && m.parentElement.querySelector(".pill");
    if (pill && (pill === e.target || pill.contains(e.target))) return;
    if (m.contains(e.target)) return;
    m.style.display = "none";
  }});

  (function () {{
    function setNavH() {{
      try {{
        var nav = document.querySelector(".navwrap");
        if (!nav) return;
        var h = Math.ceil(nav.getBoundingClientRect().height || nav.offsetHeight || 0);
        if (h > 0) {{
          document.documentElement.style.setProperty("--mam-nav-h", h + "px");
        }}
      }} catch (_) {{}}
    }}
    setNavH();
    setTimeout(setNavH, 50);
    setTimeout(setNavH, 200);
    window.addEventListener("resize", setNavH);
  }})();

  (function () {{
    function arm(a) {{
      if (!a) return;
      a.addEventListener("click", function () {{
        try {{
          var href = a.getAttribute("href") || "";
          if (!href || href === "#" || href.startsWith("javascript:")) return;
          document.documentElement.classList.add("mam-loading");
        }} catch (_) {{}}
      }});
    }}
    try {{
      document.querySelectorAll(".navwrap a").forEach(arm);
    }} catch (_) {{}}
  }})();
</script>
"""

def avatar_url_for_handle(handle: str | None) -> str:
    h = (handle or "").strip().lower()
    if h:
        return f"/avatar/{h}"
    return "/static/mam-logo.png"

def render_avatar_chip(handle: str | None, size: int = 34) -> str:
    url = avatar_url_for_handle(handle)
    s = int(size) if size else 34
    return f"""
<div style="width:{s}px;height:{s}px;border-radius:999px;overflow:hidden;background:#fff;display:flex;align-items:center;justify-content:center;">
  <img src="{url}"
       onerror="this.onerror=null;this.src='/static/mam-logo.png';"
       alt="Avatar"
       style="width:100%;height:100%;object-fit:cover;display:block;">
</div>
""".strip()

def render_account_status_menu_item(handle: str) -> str:
    status = get_account_status(handle)  # "member" | "trial" | "none"

    label = {
        "member": "✅ Member",
        "trial": "⏳ Trial",
        "none": "🔒 Free",
    }.get(status, "🔒 Free")

    # If you want a trial countdown, you can swap label here later.
    return f"""
      <div class="menu-item" style="font-weight:900; opacity:0.95;">
        {label}
      </div>
      <div class="menu-sep" style="height:1px; background:rgba(255,255,255,0.10); margin:8px 0;"></div>
    """


@app.route("/billing/create-checkout-session", methods=["GET", "POST"])
@login_required
def billing_create_checkout_session():
    """
    Stripe checkout session creation.

    - Clerk auth bounce may land here via GET (browser redirect). If POST-only, it 405s.
      So GET returns a tiny auto-POST bridge page.
    - POST performs the real Stripe session creation.
    """
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return "Unauthorized", 401

    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return "Missing user name", 400

    # ✅ Always return users to the stable private entry point
    next_path = (request.args.get("next") or "/logbook").strip() or "/logbook"
    if (not next_path.startswith("/")) or next_path.startswith("//"):
        next_path = "/logbook"

    # ✅ Billing config (same logic as /upgrade)
    sk = (STRIPE_SECRET_KEY or "").strip()
    price_id = (STRIPE_PRICE_ID_ANNUAL or STRIPE_PRICE_ID or "").strip()
    base_url = (APP_BASE_URL or (getattr(request, "url_root", "") or "")).strip().rstrip("/")

    sk_ok = (sk.startswith("sk_test_") or sk.startswith("sk_live_"))
    sk_is_webhook = sk.startswith("whsec_")
    billing_ready = bool(sk_ok and (not sk_is_webhook) and price_id and base_url)

    if not billing_ready:
        try:
            print("[BILLING_NOT_READY]",
                  "sk=", _mask(sk),
                  "price=", _mask(price_id),
                  "app_base=", base_url,
                  "whsec=", _mask(STRIPE_WEBHOOK_SECRET))
        except Exception:
            pass
        return Response(
            "Billing is not configured (missing/invalid STRIPE_SECRET_KEY, Price ID, or APP_BASE_URL).",
            status=500,
        )

    # -----------------------------
    # GET bridge: auto-submit POST (fixes Clerk redirect -> GET)
    # -----------------------------
    if request.method == "GET":
        action = f"/billing/create-checkout-session?next={quote(next_path, safe='/')}"
        html_out = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Opening checkout…</title>
  <style>
    body {{
      margin:0;
      background:#0f1115;
      color:#fff;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    }}
    .wrap {{
      min-height:100vh;
      display:flex;
      align-items:center;
      justify-content:center;
      padding:20px;
    }}
    .card {{
      width:min(520px, calc(100vw - 40px));
      background:#171a21;
      border:1px solid #2a2f3a;
      border-radius:16px;
      padding:18px;
      text-align:center;
      box-shadow: 0 18px 50px rgba(0,0,0,0.35);
    }}
    .mam-loader {{ position: relative; width: 92px; height: 92px; margin: 6px auto 12px; }}
    .mam-ring {{
      position: absolute; inset: 0;
      border-radius: 999px;
      border: 6px solid rgba(255, 255, 255, 0.18);
      border-top-color: rgba(255, 255, 255, 0.92);
      animation: mamSpin 0.85s linear infinite;
    }}
    .mam-logo {{
      position: absolute; left: 50%; top: 50%;
      width: 34px; height: 34px;
      transform: translate(-50%, -50%);
      border-radius: 10px;
      box-shadow: 0 10px 26px rgba(0,0,0,0.35);
    }}
    .title {{
      margin: 0 0 6px;
      font-weight: 950;
      letter-spacing: -0.2px;
      font-size: 18px;
    }}
    .sub {{
      margin: 0 0 14px;
      color: rgba(255,255,255,0.72);
      font-size: 13px;
      line-height: 1.45;
    }}
    .btn {{
      display:inline-block;
      padding:12px 14px;
      border-radius:12px;
      background:#fff;
      color:#111;
      text-decoration:none;
      font-weight:950;
      border: 1px solid rgba(0,0,0,0.12);
      cursor:pointer;
    }}
    @keyframes mamSpin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="mam-loader" aria-label="Loading">
        <div class="mam-ring"></div>
        <img class="mam-logo" src="/static/favicon.png" alt="MyAirportMap">
      </div>
      <h2 class="title">Opening checkout…</h2>
      <p class="sub">One moment — redirecting to Stripe.</p>

      <form id="mam-post" method="POST" action="{_html.escape(action)}" style="margin:0;">
        <button class="btn" type="submit">Continue</button>
      </form>
    </div>
  </div>

  <script>
    (function () {{
      try {{
        var f = document.getElementById("mam-post");
        if (f) f.submit();
      }} catch (_) {{}}
    }})();
  </script>
</body>
</html>"""
        return Response(html_out, mimetype="text/html", status=200)

    # -----------------------------
    # POST: real session creation logic
    # -----------------------------
    try:
        stripe.api_key = sk


        success_url = (
            f"{base_url}/billing/success"
            f"?next={quote(next_path, safe='/')}"
            f"&session_id={{CHECKOUT_SESSION_ID}}"
        )
        cancel_url = f"{base_url}/upgrade?next={quote(next_path, safe='/')}"

       # ✅ Durable Stripe customer (email resolved via get_email_for_current_user())
        customer_id = ensure_stripe_customer_for_current_user(handle=handle)

        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            client_reference_id=user_id,  # ✅ stable, non-mutable identity
            metadata={"handle": handle, "user_id": user_id},
        )

        return redirect(session.url, code=303)


    except Exception as e:
        print("Failed to create checkout session:", repr(e))
        return Response(f"Failed to create checkout session: {repr(e)}", status=500)

import datetime
import html
@app.route("/billing/success", methods=["GET"])
@login_required
def billing_success():
    """
    Stripe success landing.

    Goals:
    - Redirect immediately if entitlements already flipped (webhook already processed).
    - If session_id is present, confirm with Stripe to eliminate webhook timing race.
    - SECURITY: prevent session_id swapping by validating the session belongs to this user/handle.
    - Fall back to a short "Activating..." page that redirects.
    """
    # Current signed-in identity
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return redirect("/logbook")

    # Safe next path
    next_path = (request.args.get("next") or "/logbook").strip() or "/logbook"
    if (not next_path.startswith("/")) or next_path.startswith("//"):
        next_path = "/logbook"

    # If already marked paid, continue immediately
    if is_paid_user_handle(handle):
        return redirect(next_path)

    session_id = (request.args.get("session_id") or "").strip()
    price_id = (STRIPE_PRICE_ID_ANNUAL or STRIPE_PRICE_ID or "").strip() or None

    # If we can confirm with Stripe, activate immediately (removes webhook timing race)
    if session_id:
        try:
            _stripe_init()

            sess = stripe.checkout.Session.retrieve(
                session_id,
                expand=["subscription", "customer"],
            )

            meta = sess.get("metadata") or {}
            meta_handle = (meta.get("handle") or "").strip().lower()
            meta_user_id = (meta.get("user_id") or "").strip()

            # client_reference_id may be either handle OR user_id depending on your config.
            # Accept either, but require at least one strong match (prevents swapping session_id).
            client_ref = (sess.get("client_reference_id") or "").strip()
            client_ref_lc = client_ref.lower()

            ok_owner = False
            if handle and (meta_handle == handle or client_ref_lc == handle):
                ok_owner = True
            if user_id and (meta_user_id == user_id or client_ref == user_id):
                ok_owner = True

            if not ok_owner:
                print(
                    "billing_success: session owner mismatch",
                    "handle=", handle,
                    "user_id=", user_id,
                    "meta_handle=", meta_handle,
                    "meta_user_id=", meta_user_id,
                    "client_ref=", client_ref,
                )
                return Response("Session verification failed.", status=403)

            sub = sess.get("subscription")
            sub_id = sub.get("id") if isinstance(sub, dict) else (sub if isinstance(sub, str) else None)

            cust = sess.get("customer")
            cust_id = cust.get("id") if isinstance(cust, dict) else (cust if isinstance(cust, str) else None)

            status = (sess.get("status") or "").lower()
            pay_status = (sess.get("payment_status") or "").lower()

            # ✅ Compute paid_through from subscription current period end
            paid_through_iso = None
            if isinstance(sub, dict):
                cpe = sub.get("current_period_end")
                if isinstance(cpe, (int, float)) and cpe > 0:
                    paid_through_iso = _iso_from_unix_ts(int(cpe))

            # ✅ $0 promo checkouts may be "no_payment_required"
            ok_paid = (status in ("complete",) or pay_status in ("paid", "no_payment_required"))

            # Helpful trace (safe)
            try:
                print(
                    "billing_success:",
                    "handle=", handle,
                    "user_id=", user_id,
                    "status=", status,
                    "payment_status=", pay_status,
                    "sub_id=", sub_id,
                    "cust_id=", cust_id,
                )
            except Exception:
                pass

            if ok_paid:
                _mark_paid_stripe(
                    handle,
                    stripe_customer_id=cust_id,
                    stripe_subscription_id=sub_id,
                    stripe_price_id=price_id,
                    paid_through_iso=paid_through_iso,
                )

                # ✅ Bust paywall/map caches after entitlement flips
                try:
                    invalidate_user_caches(handle)
                except Exception as e:
                    print("billing_success: cache invalidate failed:", repr(e))

                return redirect(next_path)

            # Not OK yet (rare): fall through to activation page
            print(
                "billing_success: session not complete/paid yet",
                "status=", status,
                "payment_status=", pay_status,
            )

        except Exception as e:
            print("billing_success: confirm failed:", repr(e))
            # fall through to activation page

    # Fallback: show activation page + redirect (works even if webhook is slightly delayed)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Activating membership…</title>
  <style>
    body {{
      background:#0f1115; color:#fff;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      margin:0; padding-top:70px;
    }}
    .wrap {{ max-width:720px; margin:30px auto; padding:0 16px; }}
    .card {{ background:#171a21; border:1px solid #2a2f3a; border-radius:16px; padding:18px; }}
    .muted {{ color:#aab2c0; }}
  </style>
</head>
<body>
  {get_navbar("home", handle=handle)}

  <div class="wrap">
    <div class="card">
      <h1 style="margin:0 0 8px;">Activating your membership…</h1>
      <p class="muted" style="margin:0 0 12px;">
        We’re unlocking your account now. This usually takes a few seconds.
      </p>
      <p class="muted" style="margin:0;">
        If you’re not redirected automatically,
        <a href="{_html.escape(next_path)}" style="color:#dbe9ff;">click here</a>.
      </p>
    </div>
  </div>

  <script>
    setTimeout(function () {{
      window.location.href = "{_html.escape(next_path)}";
    }}, 2200);
  </script>
</body>
</html>
"""


@app.route("/runways/manage", methods=["GET", "POST"])
@login_required
def manage_runway360():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return redirect("/onboard/handle?next=/runways/manage", code=302)

    ensure_user_initialized(handle)

    # Gate: only trial or member
    if not has_active_access(handle):
        cur = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
        if not cur.startswith("/"):
            cur = "/runways/manage"
        return redirect("/trial/ended?next=" + quote(cur, safe="/=?&"), code=302)

    if request.method == "POST":
        data = parse_runway360_form(request.form)
        save_runway360(handle, data)

        # ------------------------------------------------------------
        # ✅ Runway 360: set-once completion → durable stamp + roster + milestone + recent achievements badge
        # Fires immediately on Save (authoritative moment).
        # ------------------------------------------------------------
        try:
            completed = runway360_completed_set(data)
            done = len(completed)

            if done >= 36:
                just_completed = record_runway360_completion_once(
                    user_id=getattr(request, "user_id", None),
                    handle=handle,
                    completed_count=done,
                    total=36,
                )

                # Only emit/roster on the FIRST time
                if just_completed:
                    # Pilot’s Lounge milestone (set-once; global publish gated internally)
                    maybe_emit_runway360_milestone(handle=handle, just_completed=True)

                    # ✅ Recent Achievements (badge feed) — global publish gated by share_activity
                    try:
                        emit_badge_event_once_if_sharing(
                            handle=handle,
                            badge_key="runway360_complete",
                            badge_label="Runway 360 Complete (36/36)",
                        )
                    except Exception:
                        pass

                    # ✅ Populate /runway360/club roster immediately (not only when card is generated)
                    try:
                        runway360_club_upsert(handle)
                    except Exception:
                        pass

        except Exception:
            # Never block save/redirect on achievement plumbing
            pass

        return redirect("/achievements?msg=runways_saved", code=302)

    # GET: render page
    data = load_runway360(handle)
    items = (data.get("items", {}) if isinstance(data, dict) else {}) or {}
    completed = runway360_completed_set(data)
    pct = int(round((len(completed) / 36) * 100))

    def v(num: str, key: str) -> str:
        obj = items.get(num) or {}
        val = obj.get(key) or ""
        return _html.escape(str(val))

    rows = []
    for num in RUNWAY360_NUMBERS:
        n = str(num).zfill(2)
        is_done = n in completed
        badge = "✅" if is_done else "⭕"
        row_bg = "rgba(0,136,255,0.10)" if is_done else "transparent"
        rows.append(f"""
<tr style="background:{row_bg};">
  <td style="padding:10px 8px; border-bottom:1px solid #2a2a2a; width:90px;">
    <div style="font-weight:950;">{badge} RWY {n}</div>
  </td>
  <td style="padding:10px 8px; border-bottom:1px solid #2a2a2a;">
    <input name="rwy_{n}_date" value="{v(n,'date')}" placeholder="MM/DD/YYYY"
           style="width:100%; box-sizing:border-box; padding:10px 12px; border-radius:12px;
                  background:#0a0a0a; border:1px solid #333; color:#fff; font-size:14px;">
  </td>
  <td style="padding:10px 8px; border-bottom:1px solid #2a2a2a;">
    <input name="rwy_{n}_airport" value="{v(n,'airport')}" placeholder="Airport (e.g., KCDW)"
           style="width:100%; box-sizing:border-box; padding:10px 12px; border-radius:12px;
                  background:#0a0a0a; border:1px solid #333; color:#fff; font-size:14px;">
  </td>
  <td style="padding:10px 8px; border-bottom:1px solid #2a2a2a;">
    <input name="rwy_{n}_aircraft" value="{v(n,'aircraft')}" placeholder="Notes"
           style="width:100%; box-sizing:border-box; padding:10px 12px; border-radius:12px;
                  background:#0a0a0a; border:1px solid #333; color:#fff; font-size:14px;">
  </td>
</tr>
""")

    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runway 360 · Manage</title>
  <style>
    body {{
      background:#0f0f0f; color:#fff;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      margin:0; padding-top:70px;
    }}
    .container {{ max-width:1000px; margin:0 auto; padding:18px; }}
    .card {{ background:#151515; border:1px solid #2a2a2a; border-radius:18px; padding:14px; margin:12px 0; }}
    .muted {{ color:#a0a0a0; font-size:14px; }}
    .btn {{
      display:inline-block; padding:10px 12px; border-radius:12px;
      background:#1f1f1f; border:1px solid #3a3a3a; color:#fff; text-decoration:none; font-weight:900;
    }}
    .btn-primary {{ background:#ffffff; color:#0f1115; border:1px solid rgba(255,255,255,0.20); }}
    .btn:hover {{ border-color:#666; }}
    .table {{ width:100%; border-collapse:collapse; }}
    .table th {{
      text-align:left; font-size:12px; color:#cfcfcf; letter-spacing:0.04em; text-transform:uppercase;
      padding:10px 8px; border-bottom:1px solid #2a2a2a;
    }}

    /* ✅ Global loading overlay (brace-safe for f-string) */
    #mam-loading {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(10, 12, 16, 0.72);
      z-index: 999999;
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
      opacity: 0;
      transition: opacity 160ms ease;
    }}
    #mam-loading.mam-visible {{ opacity: 1; }}
    #mam-loading .mam-loader {{ position: relative; width: 66px; height: 66px; }}
    #mam-loading .mam-ring {{
      position: absolute; inset: 0;
      border-radius: 999px;
      border: 6px solid rgba(255, 255, 255, 0.18);
      border-top-color: rgba(255, 255, 255, 0.92);
      animation: mamSpin 0.85s linear infinite;
    }}
    #mam-loading .mam-logo {{
      position: absolute; left: 50%; top: 50%;
      width: 34px; height: 34px;
      transform: translate(-50%, -50%);
      border-radius: 10px;
      box-shadow: 0 10px 26px rgba(0,0,0,0.35);
    }}
    #mam-loading .mam-text {{
      margin-top: 14px;
      text-align: center;
      font-weight: 950;
      font-size: 14px;
      color: rgba(255, 255, 255, 0.92);
      letter-spacing: -0.2px;
    }}
    @keyframes mamSpin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  {get_navbar("achievements", handle=handle)}

  <div class="container">
    <div class="card">
      <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:center;">
        <div>
          <div style="font-weight:950; font-size:20px;">Runway 360 Club · Manage</div>
          <div class="muted" style="margin-top:6px;">
            Fill the cells for the runway number on which you have landed (take-offs don't count) with a date, airport, and notes. Save to update your Runway 360 Achievements ring.
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <a class="btn" href="/achievements">Back</a>
          <button class="btn btn-primary" type="submit" form="rwyForm">Save</button>
        </div>
      </div>

      <div style="margin-top:12px;">
        <div class="muted" style="margin-bottom:6px;">Progress: <b>{len(completed)}/36</b> · {pct}%</div>
        <div style="height:12px; background:#111; border-radius:6px; overflow:hidden; border:1px solid #2a2a2a;">
          <div style="width:{pct}%; height:100%; background:#0088FF;"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <form id="rwyForm" method="post" action="/runways/manage">
        <table class="table">
          <tr>
            <th>Runway</th>
            <th>Date</th>
            <th>Airport</th>
            <th>Aircraft</th>
          </tr>
          {''.join(rows)}
        </table>

        <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;">
          <button class="btn btn-primary" type="submit">Save Runways</button>
          <a class="btn" href="/achievements">Cancel</a>
        </div>
      </form>
    </div>
  </div>

  <!-- ✅ Loading overlay -->
  <div id="mam-loading" aria-label="Loading">
    <div style="display:flex; flex-direction:column; align-items:center;">
      <div class="mam-loader">
        <div class="mam-ring"></div>
        <img class="mam-logo" src="/static/favicon.png" alt="MyAirportMap">
      </div>
      <div class="mam-text">Loading…</div>
    </div>
  </div>

  <script>
  (function () {{
    var el = document.getElementById("mam-loading");
    if (!el) return;

    function show(msg) {{
      try {{
        var t = el.querySelector(".mam-text");
        if (t && msg) t.textContent = msg;
      }} catch (_) {{}}
      el.style.display = "flex";
      requestAnimationFrame(function () {{ el.classList.add("mam-visible"); }});
    }}

    // ✅ Show on ANY navigation away from Manage (navbar, Back, Cancel, etc.)
    document.addEventListener("click", function (e) {{
      try {{
        var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
        if (!a) return;

        var href = a.getAttribute("href") || "";
        if (!href || href === "#" || href.startsWith("javascript:")) return;
        if (a.getAttribute("target") === "_blank") return;
        if (href.startsWith("#")) return;

        show("Loading…");
      }} catch (_) {{}}
    }}, true);

    // ✅ Show on Save/POST
    document.addEventListener("submit", function (e) {{
      try {{
        var form = e.target;
        var action = (form && form.getAttribute) ? (form.getAttribute("action") || "") : "";
        if (action.indexOf("/runways/manage") >= 0) show("Saving…");
        else show("Loading…");
      }} catch (_) {{
        show("Loading…");
      }}
    }}, true);

    // Safety net: browser-driven navigations
    window.addEventListener("beforeunload", function () {{
      try {{ show("Loading…"); }} catch (_) {{}}
    }});
  }})();
  </script>

</body>
</html>""",
        mimetype="text/html",
    )

# -----------------------------
# Paths
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VISITS_CSV = os.path.join(BASE_DIR, "my_visits.csv")

STATUS_PATH = os.path.join(BASE_DIR, "_last_upload.json")

AIRPORTS_CANDIDATES = [
    os.path.join(BASE_DIR, "conus_airports.csv"),
    os.path.join(BASE_DIR, "airports_conus.csv"),
    os.path.join(BASE_DIR, "airports.csv"),
    os.path.join(BASE_DIR, "conus.csv"),
    os.path.join(BASE_DIR, "map_airports.csv"),
]

# -----------------------------
# Helpers
# -----------------------------
from threading import Lock

INDEX_PATH = os.path.join("users", "_index.json")
INDEX_LOCK = Lock()

def entitlements_path(handle: str) -> str:
    # Defensive: never create folders/files for unsafe handles (Windows path safety)
    if not is_valid_handle(handle):
        return ""
    return os.path.join("users", handle, "entitlements.json")

def _read_entitlements(handle: str) -> dict:
    path = entitlements_path(handle)
    if not path:
        return {}
    try:
        raw = storage_backend.read_bytes(path)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def _write_entitlements(handle: str, data: dict) -> None:
    path = entitlements_path(handle)
    if not path:
        return
    storage_backend.write_bytes(path, json.dumps(data, indent=2).encode("utf-8"))

import math

TRIAL_DAYS = 30

def _now_utc():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)

import time
import hashlib

# Simple in-process cache (per gunicorn worker)
_MAP_HTML_CACHE: dict[str, tuple[float, str]] = {}
_MAP_HTML_TTL_SECONDS = 180  # 3 minutes; safe for alpha
_MAP_HTML_CACHE_MAX = 32     # keep memory bounded

def _map_cache_key(handle: str | None, filter_state: str | None, navbar_mode: str) -> str:
    h = (handle or "").strip()
    fs = (filter_state or "").strip().upper()
    nm = (navbar_mode or "").strip()
    raw = f"h={h}|fs={fs}|nm={nm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _map_cache_get(key: str) -> str | None:
    item = _MAP_HTML_CACHE.get(key)
    if not item:
        return None
    ts, html_out = item
    if (time.time() - ts) > _MAP_HTML_TTL_SECONDS:
        _MAP_HTML_CACHE.pop(key, None)
        return None
    return html_out

def _map_cache_put(key: str, html_out: str) -> None:
    now = time.time()

    # prune expired entries opportunistically
    try:
        expired = [k for k, (ts, _) in _MAP_HTML_CACHE.items() if (now - ts) > _MAP_HTML_TTL_SECONDS]
        for k in expired:
            _MAP_HTML_CACHE.pop(k, None)
    except Exception:
        pass

    # prune if too large (drop oldest until we're under max)
    try:
        while len(_MAP_HTML_CACHE) >= _MAP_HTML_CACHE_MAX:
            oldest_key = None
            oldest_ts = None
            for k, (ts, _) in _MAP_HTML_CACHE.items():
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                    oldest_key = k
            if not oldest_key:
                break
            _MAP_HTML_CACHE.pop(oldest_key, None)
    except Exception:
        pass
    # -----------------------------
    # Cache guard: avoid poisoning cache with tiny/partial pages
    # -----------------------------
    try:
        # If not a string, or suspiciously small, don't cache it.
        if not isinstance(html_out, str):
            return
        if len(html_out) < 20000:
            return

        lo = html_out.lower()
        if "leaflet" not in lo or "leaflet-control" not in lo:
            return
    except Exception:
        return

    _MAP_HTML_CACHE[key] = (now, html_out)

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _ts_key() -> str:
    # sortable-ish timestamp key
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

def _rand6() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))

def _r2_get_json(key: str) -> dict | None:
    """
    Map41-safe: calls your existing JSON getter if present; else returns None.
    Expected signature: fn(key: str) -> dict|None
    """
    try:
        fn = globals().get("r2_get_json") or globals().get("get_r2_json") or globals().get("_r2_get_json_impl")
        if callable(fn):
            obj = fn(key)
            return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    return None

def _r2_put_json(key: str, obj: dict) -> None:
    """
    Map41-safe: calls your existing JSON writer if present; else no-op.
    Expected signature: fn(key: str, obj: dict) -> None
    """
    try:
        fn = globals().get("r2_put_json") or globals().get("put_r2_json") or globals().get("_r2_put_json_impl")
        if callable(fn):
            fn(key, obj)
    except Exception:
        pass

def _r2_exists(key: str) -> bool:
    try:
        return _r2_get_json(key) is not None
    except Exception:
        return False

def _is_public_share_enabled(handle: str) -> bool:
    # Reuse your existing share gate if you already have it.
    try:
        return bool(is_public_share_enabled(handle))  # <- you likely have this
    except Exception:
        return False

def emit_milestone_once(handle: str, milestone_key: str, label: str, meta: dict | None = None) -> bool:
    """
    Emits a milestone one time per user.
    - Always writes a per-user marker so we never repeat.
    - Writes to global feed ONLY if sharing is enabled.
    Returns True if newly emitted, False if it already existed.
    """
    handle = (handle or "").strip().lower()
    milestone_key = (milestone_key or "").strip().lower()
    if not handle or not milestone_key:
        return False

    marker_key = f"milestones/{handle}/{milestone_key}.json"
    if _r2_exists(marker_key):
        return False

    payload = {
        "v": 1,
        "created_at": _utc_now_iso(),
        "handle": handle,
        "type": "milestone",
        "milestone_key": milestone_key,
        "label": label,
        "meta": meta or {},
    }

    # 1) marker (dedupe)
    _r2_put_json(marker_key, payload)

    # 2) global feed event (only if sharing enabled)
    if _is_public_share_enabled(handle):
        key = f"events/milestones/{_ts_key()}_{handle}_{milestone_key}_{_rand6()}.json"
        _r2_put_json(_event_key, payload)

    return True

def get_global_milestone_events(limit: int = 20) -> list[dict]:
    """Fetch latest milestone events across the platform (best-effort)."""
    try:
        limit = max(1, min(int(limit or 20), 50))
    except Exception:
        limit = 20

    if not _r2_enabled():
        return []

    s3 = _r2_client()
    bucket = _r2_bucket()
    if not s3 or not bucket:
        return []

    prefix = "events/milestones/"
    keys: list[str] = []
    want = max(limit * 50, 200)

    try:
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 200}
            if token:
                kwargs["ContinuationToken"] = token

            resp = s3.list_objects_v2(**kwargs)

            for item in resp.get("Contents", []) or []:
                k = item.get("Key")
                if k:
                    keys.append(k)
                if len(keys) >= want:
                    break

            if len(keys) >= want:
                break

            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                if not token:
                    break
            else:
                break

    except Exception:
        return []

    # Deduplicate + newest-first
    keys = list(dict.fromkeys(keys))
    keys.sort(reverse=True)

    out: list[dict] = []
    for k in keys[:limit]:
        try:
            obj = s3.get_object(Bucket=bucket, Key=k)
            raw = obj["Body"].read()
            if not raw:
                continue
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                out.append(data)
        except Exception:
            continue

    return out

def _map_cache_clear() -> None:
    try:
        _MAP_HTML_CACHE.clear()
    except Exception:
        pass

import datetime
import math
from datetime import timedelta, timezone

TRIAL_DAYS = 30

def _parse_iso_utc(s: str) -> datetime.datetime | None:
    """
    Parse ISO8601 timestamps with optional 'Z' and optional timezone.
    Returns an aware UTC datetime or None.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def ensure_trial_initialized(handle: str) -> None:
    """
    Ensure entitlements.json exists for this handle with a trial start + expiration.

    Safety rules:
      - Never writes for invalid handles.
      - Never shortens an existing trial.
      - If we detect a suspiciously short trial (<20 days after start), repair to full 30 days.
      - If only expires exists (legacy), backfill started_at conservatively.
    """
    if handle == "demo":
        return
    if not is_valid_handle(handle):
        return

    h = handle.strip().lower()
    e = _read_entitlements(h) or {}

    now = _now_utc()  # should already be timezone-aware UTC in your app

    started_raw = (e.get("trial_started_at") or "").strip()
    exp_raw = (e.get("trial_expires_at") or "").strip()

    started_dt = _parse_iso_utc(started_raw)
    exp_dt = _parse_iso_utc(exp_raw)

    # If both exist: never shorten; repair if it's obviously wrong
    if started_dt and exp_dt:
        # suspiciously early expiry (e.g. 2-day trial) -> repair once
        min_ok = started_dt + timedelta(days=20)
        if exp_dt < min_ok:
            repaired = started_dt + timedelta(days=TRIAL_DAYS)
            e["trial_expires_at"] = repaired.isoformat().replace("+00:00", "Z")
            e["updated_at"] = now.isoformat().replace("+00:00", "Z")
            e.setdefault("is_paid", False)
            e.setdefault("created_at", now.isoformat().replace("+00:00", "Z"))
            _write_entitlements(h, e)
        return

    # Legacy case: we have expires but no started_at -> backfill started_at
    if exp_dt and not started_dt:
        # Backfill start as "exp - TRIAL_DAYS" (conservative and stable)
        backfill_start = exp_dt - timedelta(days=TRIAL_DAYS)
        e["trial_started_at"] = backfill_start.isoformat().replace("+00:00", "Z")
        e["trial_expires_at"] = exp_dt.isoformat().replace("+00:00", "Z")  # normalize
        e["updated_at"] = now.isoformat().replace("+00:00", "Z")
        e.setdefault("is_paid", False)
        e.setdefault("created_at", now.isoformat().replace("+00:00", "Z"))
        _write_entitlements(h, e)
        return

    # No expires at all -> first-time initialization
    if not exp_dt:
        started = now
        expires = now + timedelta(days=TRIAL_DAYS)

        e["trial_started_at"] = started.isoformat().replace("+00:00", "Z")
        e["trial_expires_at"] = expires.isoformat().replace("+00:00", "Z")
        e.setdefault("is_paid", False)
        e.setdefault("created_at", now.isoformat().replace("+00:00", "Z"))
        e["updated_at"] = now.isoformat().replace("+00:00", "Z")
        _write_entitlements(h, e)
        return

def trial_is_active_for_handle(handle: str) -> bool:
    if handle == "demo":
        return False
    if not is_valid_handle(handle):
        return False

    h = handle.strip().lower()
    ensure_trial_initialized(h)  # safe + idempotent

    e = _read_entitlements(h) or {}
    exp_dt = _parse_iso_utc((e.get("trial_expires_at") or "").strip())
    if not exp_dt:
        return False

    now = _now_utc()
    return now <= exp_dt

def trial_days_left(handle: str) -> int | None:
    """
    Returns:
      None -> no trial exists (should be rare because ensure_trial_initialized backfills)
      0+  -> days remaining (ceil)
    """
    if handle == "demo":
        return 0
    if not is_valid_handle(handle):
        return None

    h = handle.strip().lower()
    ensure_trial_initialized(h)  # safe + idempotent

    e = _read_entitlements(h) or {}
    exp_dt = _parse_iso_utc((e.get("trial_expires_at") or "").strip())
    if not exp_dt:
        return None

    seconds = (exp_dt - _now_utc()).total_seconds()
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 86400.0))

import re
import html

def _linkify(text: str | None) -> str:
    """
    Convert URLs in plain text to safe clickable links.
    - Escapes HTML first
    - Only linkifies http/https URLs
    """
    if not text:
        return ""

    # Escape first to prevent injection
    safe = _html.escape(str(text))

    url_re = re.compile(r"(https?://[^\s<]+)")
    return url_re.sub(
        r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>',
        safe,
    )

def _invoice_paid_through_iso(invoice: dict | None) -> str | None:
    """
    Extract the paid-through date from a Stripe invoice-like object
    and return it as an ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ).

    Tries, in order:
      - invoice['lines']['data'][0]['period']['end']
      - invoice['period_end']
      - invoice['current_period_end']

    Returns None if unavailable.
    """
    if not invoice:
        return None

    ts = None

    try:
        # Common Stripe invoice structure
        lines = invoice.get("lines", {}).get("data", [])
        if lines:
            ts = lines[0].get("period", {}).get("end")
    except Exception:
        ts = None

    if not ts:
        ts = invoice.get("period_end") or invoice.get("current_period_end")

    if not ts:
        return None

    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None

# -----------------------------
# Terms of Use acceptance (first login gate)
# Stored per Clerk user_id.
# MUST be stable across deploys + storage modes.
# -----------------------------
TOS_VERSION = "2025-01"
TOS_KEY = "users/_tos.json"

def _tos_path_local() -> str:
    return os.path.join(BASE_DIR, TOS_KEY)

def _tos_storage_key() -> str:
    """
    R2 enabled -> object key "users/_tos.json"
    Local dev  -> filesystem path "<BASE_DIR>/users/_tos.json"
    """
    try:
        if getattr(storage_backend, "_r2_enabled", lambda: False)():
            return TOS_KEY
    except Exception:
        pass
    return _tos_path_local()

def _read_tos_map() -> dict:
    key = _tos_storage_key()
    try:
        if not storage_backend.exists(key):
            return {}
        raw = storage_backend.read_bytes(key) or b"{}"
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print("_read_tos_map failed:", "key=", key, "err=", repr(e))
        return {}

def _write_tos_map(obj: dict) -> None:
    key = _tos_storage_key()
    if not isinstance(obj, dict):
        obj = {}

    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        # Ensure local parent directory exists
        if key.startswith(BASE_DIR):
            os.makedirs(os.path.dirname(key), exist_ok=True)

        try:
            storage_backend.write_bytes(
                key,
                payload,
                content_type="application/json",
                cache_control="no-store",
            )
        except TypeError:
            storage_backend.write_bytes(key, payload)
    except Exception as e:
        print("_write_tos_map failed:", "key=", key, "err=", repr(e))

def tos_accepted_for_user(user_id: str) -> bool:
    user_id = (user_id or "").strip()
    if not user_id:
        return False
    m = _read_tos_map()
    entry = m.get(user_id)
    if not isinstance(entry, dict):
        return False
    return (entry.get("version") or "").strip() == TOS_VERSION

def set_tos_accepted_for_user(user_id: str) -> None:
    user_id = (user_id or "").strip()
    if not user_id:
        return
    m = _read_tos_map()
    m[user_id] = {
        "version": TOS_VERSION,
        "accepted_at": _now_utc().isoformat().replace("+00:00", "Z"),
    }
    _write_tos_map(m)

def is_paid_user_handle(handle: str) -> bool:
    """
    True iff the handle currently has an active paid entitlement.

    Rules:
      - Requires entitlements.json { "is_paid": true }
      - If "paid_through" exists, it must be >= now (UTC)
      - If "paid_through" is missing, treat as paid (backward compatible)
    """
    e = _read_entitlements(handle) or {}
    if not bool(e.get("is_paid", False)):
        return False

    paid_through = _parse_iso_utc((e.get("paid_through") or "").strip())
    if paid_through is None:
        return True
    return _now_utc() <= paid_through

# =============================
# Stripe billing (Map33)
# =============================
import stripe
import hmac
import hashlib

STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
STRIPE_PRICE_ID_ANNUAL = (os.getenv("STRIPE_PRICE_ID_ANNUAL") or "").strip()
STRIPE_PRICE_ID = (os.getenv("STRIPE_PRICE_ID") or "").strip()

# Prefer explicit URLs for prod stability (Render/proxies).
STRIPE_SUCCESS_URL = (os.getenv("STRIPE_SUCCESS_URL") or "").strip()
STRIPE_CANCEL_URL = (os.getenv("STRIPE_CANCEL_URL") or "").strip()

def _stripe_webhook_ready() -> bool:
    sk = (STRIPE_SECRET_KEY or "").strip()
    wh = (STRIPE_WEBHOOK_SECRET or "").strip()
    if not sk or not wh:
        return False
    if sk.startswith("whsec_"):
        return False
    return True

def _stripe_checkout_ready() -> bool:
    sk = (STRIPE_SECRET_KEY or "").strip()
    price = (STRIPE_PRICE_ID_ANNUAL or STRIPE_PRICE_ID or "").strip()
    base = (APP_BASE_URL or "").strip()  # if you treat it as required
    if not sk or sk.startswith("whsec_"):
        return False
    if not price:
        return False
    if not base:
        return False
    return True


 # -----------------------------
# Stripe customer bootstrap (email-based, durable)
# -----------------------------
def _stripe_init() -> None:
    sk = (STRIPE_SECRET_KEY or "").strip()
    if not sk or sk.startswith("whsec_"):
        raise RuntimeError("Invalid STRIPE_SECRET_KEY")
    stripe.api_key = sk

def _get_stripe_customer_id_from_user_meta(user_id: str) -> str | None:
    meta = load_user_meta(user_id)
    cid = (meta.get("stripe_customer_id") or "").strip()
    return cid or None

def _set_stripe_customer_id_in_user_meta(user_id: str, customer_id: str) -> None:
    if not user_id or not customer_id:
        return
    patch_user_meta(user_id, {"stripe_customer_id": customer_id})

def ensure_stripe_customer_for_current_user(*, handle: str | None = None) -> str:
    """
    Create (once) and persist a Stripe Customer for the logged-in user.
    - Uses canonical email: get_email_for_current_user()
    - Stores stripe_customer_id in durable user meta (users/by_id/<user_id>.json)
    - Updates customer->handle index for webhook reconciliation
    """
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")

    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        raise RuntimeError("Not signed in")

    # 1) Fast-path: already have customer id saved
    existing = _get_stripe_customer_id_from_user_meta(user_id)
    if existing:
        # keep your webhook index warm
        if handle and is_valid_handle(handle):
            _index_customer_to_handle(existing, handle)
        return existing

    # 2) Resolve canonical email (your helper: fast meta, fallback Clerk API)
    email = get_email_for_current_user(claims=claims)
    if not email:
        raise RuntimeError("Could not resolve user email for billing")

    # Validate + set stripe.api_key (raises if invalid)
    _stripe_init()


    customer = stripe.Customer.create(
        email=email,
        metadata={
            "map_user_id": user_id,
            "handle": (handle or "").strip(),
        },
        description=f"MyAirportMap user {handle or user_id}",
    )

    customer_id = (customer.get("id") if isinstance(customer, dict) else getattr(customer, "id", "")) or ""
    customer_id = customer_id.strip()
    if not customer_id:
        raise RuntimeError("Stripe customer creation returned no id")

    # 4) Persist + index
    _set_stripe_customer_id_in_user_meta(user_id, customer_id)
    if handle and is_valid_handle(handle):
        _index_customer_to_handle(customer_id, handle)

    return customer_id
   
def render_loading_bridge(message: str, target_url: str) -> str:
    # target_url must already be safe/validated by caller
    msg = _html.escape(message or "Loading…")
    href = _html.escape(target_url or "/")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{msg}</title>
  <style>
    body {{
      margin:0; background:#0f1115; color:#fff;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    }}
    #mam-loading {{
      position: fixed;
      inset: 0;
      z-index: 100000;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(10, 12, 16, 0.55);
      backdrop-filter: blur(4px);
      opacity: 1;
    }}
    #mam-loading .mam-loader {{ position: relative; width: 92px; height: 92px; }}
    #mam-loading .mam-ring {{
      position: absolute; inset: 0;
      border-radius: 999px;
      border: 6px solid rgba(255, 255, 255, 0.18);
      border-top-color: rgba(255, 255, 255, 0.92);
      animation: mamSpin 0.85s linear infinite;
    }}
    #mam-loading .mam-logo {{
      position: absolute; left: 50%; top: 50%;
      width: 34px; height: 34px;
      transform: translate(-50%, -50%);
      border-radius: 10px;
      box-shadow: 0 10px 26px rgba(0,0,0,0.35);
    }}
    #mam-loading .mam-text {{
      margin-top: 14px;
      text-align: center;
      font-weight: 950;
      font-size: 14px;
      color: rgba(255, 255, 255, 0.92);
      letter-spacing: -0.2px;
    }}
    @keyframes mamSpin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div id="mam-loading" aria-label="Loading">
    <div style="display:flex; flex-direction:column; align-items:center;">
      <div class="mam-loader">
        <div class="mam-ring"></div>
        <img class="mam-logo" src="/static/favicon.png" alt="MyAirportMap">
      </div>
      <div class="mam-text">{msg}</div>
    </div>
  </div>

  <script>
    // redirect immediately (JS) and provide a fallback (meta)
    window.location.replace("{href}");
  </script>
  <noscript>
    <meta http-equiv="refresh" content="0;url={href}">
  </noscript>
</body>
</html>"""
  

def _entitlements_update(handle: str, patch: dict) -> dict:
    """
    Merge-update entitlements.json for a handle.
    Safe: never writes for invalid handles.
    """
    if not is_valid_handle(handle):
        return {}
    cur = _read_entitlements(handle) or {}
    cur.update(patch or {})
    _write_entitlements(handle, cur)
    return cur

def _stripe_customer_index_path() -> str:
    return os.path.join("users", "_stripe_customers.json")

def _stripe_subscription_index_path() -> str:
    return os.path.join("users", "_stripe_subscriptions.json")

def _read_json_map(path: str) -> dict:
    try:
        raw = storage_backend.read_bytes(path) or b"{}"
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _write_json_map(path: str, obj: dict) -> None:
    storage_backend.write_bytes(path, json.dumps(obj, indent=2).encode("utf-8"))
def _mark_paid_stripe(handle: str, *,
                      stripe_customer_id: str | None = None,
                      stripe_subscription_id: str | None = None,
                      stripe_price_id: str | None = None,
                      paid_through_iso: str | None = None) -> dict:
    patch = {
        "is_paid": True,
        "plan": "member",
        "billing_source": "stripe",
        "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
    }
    if stripe_customer_id:
        patch["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id:
        patch["stripe_subscription_id"] = stripe_subscription_id
    if stripe_price_id:
        patch["stripe_price_id"] = stripe_price_id
    if paid_through_iso:
        patch["paid_through"] = paid_through_iso

    if stripe_customer_id:
        _index_customer_to_handle(stripe_customer_id, handle)
    if stripe_subscription_id:
        _index_subscription_to_handle(stripe_subscription_id, handle)

    # Optional: if request has a logged-in user_id, backfill durable mapping too
    try:
        claims = getattr(request, "clerk_claims", {}) or {}
        user_id = (claims.get("sub") or "").strip()
        if user_id and handle:
            set_handle_for_user_durable(user_id, handle)
    except Exception:
        pass

    # Map41 policy: payment does NOT imply public visibility.
    # Community sharing is opt-in (default OFF) via share_activity; do not flip any sharing flags here.
    # (Legacy key public_share_enabled may exist in old settings; it is no longer authoritative.)
    return _entitlements_update(handle, patch)

def _mark_unpaid(handle: str, reason: str = "") -> dict:
    patch = {
        "is_paid": False,
        "plan": "trial",
        "billing_source": ( _read_entitlements(handle) or {}).get("billing_source", "stripe"),
        "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
        "unpaid_reason": reason[:120],
    }
    return _entitlements_update(handle, patch)

def _handle_from_customer_or_sub(customer_id: str | None, sub_id: str | None) -> str:
    if sub_id:
        h = _handle_lookup_by_subscription(sub_id)
        if isinstance(h, str) and h.strip():
            return h.strip()

    if customer_id:
        h = _handle_lookup_by_customer(customer_id)
        if isinstance(h, str) and h.strip():
            return h.strip()

    return ""

# Simple webhook idempotency (store processed Stripe event IDs)
def _processed_events_path() -> str:
    # global file; ok in R2/local via storage_backend
    return os.path.join("users", "_stripe_events.json")

def _event_seen(event_id: str) -> bool:
    if not event_id:
        return False
    key = _processed_events_path()
    try:
        raw = storage_backend.get_bytes(key) or b"{}"
        obj = json.loads(raw.decode("utf-8"))
        return bool(obj.get(event_id))
    except Exception:
        return False

def _handle_lookup_by_customer(customer_id: str) -> str:
    customer_id = (customer_id or "").strip()
    if not customer_id:
        return ""
    idx = _read_json_map(_stripe_customer_index_path())
    return (idx.get(customer_id) or "").strip()

def _handle_lookup_by_subscription(sub_id: str) -> str:
    sub_id = (sub_id or "").strip()
    if not sub_id:
        return ""
    idx = _read_json_map(_stripe_subscription_index_path())
    return (idx.get(sub_id) or "").strip()

def _event_mark(event_id: str) -> None:
    if not event_id:
        return
    key = _processed_events_path()
    try:
        raw = storage_backend.get_bytes(key) or b"{}"
        obj = json.loads(raw.decode("utf-8"))
        obj[event_id] = _now_utc().isoformat().replace("+00:00", "Z")
        storage_backend.put_bytes(
            key,
            json.dumps(obj, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        pass

def _index_customer_to_handle(customer_id: str, handle: str) -> None:
    customer_id = (customer_id or "").strip()
    handle = (handle or "").strip()
    if not customer_id or not handle or not is_valid_handle(handle):
        return
    path = _stripe_customer_index_path()
    idx = _read_json_map(path)
    idx[customer_id] = handle
    _write_json_map(path, idx)

def _index_subscription_to_handle(sub_id: str, handle: str) -> None:
    sub_id = (sub_id or "").strip()
    handle = (handle or "").strip()
    if not sub_id or not handle or not is_valid_handle(handle):
        return
    path = _stripe_subscription_index_path()
    idx = _read_json_map(path)
    idx[sub_id] = handle
    _write_json_map(path, idx)

def _iso_from_unix_ts(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None

def has_active_access(handle: str | None) -> bool:
    """
    Only two states grant access:
      - member (paid)
      - trial (not expired)
    Everything else is trial-ended.
    """
    if not handle:
        return False
    h = handle.strip().lower()
    if not h or h == "demo":
        return False

    # Paid wins
    if is_paid_user_handle(h):
        return True

    # Trial access
    return bool(trial_is_active_for_handle(h))

def public_display_name(handle: str) -> str:
    """Display name for public profile headers."""
    return f"{handle}'s MyAirportMap"

def get_user_visits_count() -> int:
    """Return visits count for the currently authenticated user (per-handle)."""
    try:
        claims = getattr(request, "clerk_claims", {}) or {}
        user_id = claims.get("sub", "demo")

        if user_id == "demo":
            handle = "demo"
        else:
            handle = get_or_create_handle_for_user(user_id)

        path = resolve_visits_csv(handle)
        df = _load_visits_csv(path, handle=handle)
        return 0 if df is None else int(len(df))
    except Exception:
        # Fail open: don't break the app due to a counting issue
        return 0

def should_show_welcome() -> bool:
    dismissed = request.cookies.get("welcome_dismissed") == "1"
    if dismissed:
        return False

    # Only show welcome for authenticated users
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return False

    # ✅ Require a real MyAirportMap user name first
    current = (get_handle_for_user(user_id) or "").strip()
    if (not current) or current.startswith("user_"):
        return False

    # Only show welcome if they haven't imported any visits yet
    try:
        return get_user_visits_count() == 0
    except Exception:
        return True

def _read_visits_bytes(path: str, handle: str | None = None) -> bytes | None:
    """
    Returns bytes or None if not found.
    - If R2 is enabled: reads key 'path' (e.g., users/<handle>/my_visits.csv) from R2.
    - Else: reads local filesystem at 'path'.
    """
    if storage_backend._r2_enabled():
        return storage_backend.read_bytes(path)

    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None

def _write_visits_bytes(path: str, data: bytes, handle: str | None = None) -> None:
    """
    Write my_visits.csv bytes for a user.
    - If R2 is enabled: writes key 'path' to R2.
    - Else: writes to local filesystem path.
    """
    if storage_backend._r2_enabled():
        storage_backend.write_bytes(path, data)
        return

    # local filesystem
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def invalidate_user_caches(handle: str) -> None:
    """
    Called after membership / entitlements change.

    Keep this surgical:
    - Invalidate anything that could "stick" as paywalled after upgrade
    - Only call invalidators that actually exist
    """
    h = (handle or "").strip().lower()
    if not h:
        return

    # Map cache invalidation (if present)
    fn = globals().get("_map_cache_invalidate_prefix")
    if callable(fn):
        try:
            fn(h)
        except Exception as e:
            print("invalidate_user_caches: _map_cache_invalidate_prefix failed:", repr(e))

    # HTML cache invalidation (if present) — NOTE: only call if defined
    fn = globals().get("_html_cache_invalidate_prefix")
    if callable(fn):
        try:
            fn(h)
        except Exception as e:
            print("invalidate_user_caches: _html_cache_invalidate_prefix failed:", repr(e))

# -----------------------------
# Visited set cache (per gunicorn worker)
# -----------------------------
_VISITED_SET_CACHE: dict[str, tuple[float, set[str]]] = {}
_VISITED_SET_TTL_SECONDS = 300   # 5 minutes
_VISITED_SET_CACHE_MAX = 64

def _visited_set_cache_get(handle: str) -> set[str] | None:
    item = _VISITED_SET_CACHE.get(handle)
    if not item:
        return None
    ts, s = item
    if (time.time() - ts) > _VISITED_SET_TTL_SECONDS:
        _VISITED_SET_CACHE.pop(handle, None)
        return None
    return s

def _visited_set_cache_put(handle: str, s: set[str]) -> None:
    if len(_VISITED_SET_CACHE) >= _VISITED_SET_CACHE_MAX:
        # drop oldest
        oldest_key = None
        oldest_ts = None
        for k, (ts, _) in _VISITED_SET_CACHE.items():
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
                oldest_key = k
        if oldest_key:
            _VISITED_SET_CACHE.pop(oldest_key, None)
    _VISITED_SET_CACHE[handle] = (time.time(), s)

def get_visited_norm_ids(handle: str) -> set[str]:
    cached = _visited_set_cache_get(handle)
    if cached is not None:
        return cached

    visits_csv = resolve_visits_csv(handle)
    df_visits = _load_visits_csv(visits_csv, handle=handle)

    visited: set[str] = set()
    if df_visits is not None and (not df_visits.empty):
        if "norm_id" in df_visits.columns:
            visited = set(df_visits["norm_id"].astype(str))
        elif "airport_id" in df_visits.columns:
            visited = set(df_visits["airport_id"].astype(str).apply(normalize_id))

    _visited_set_cache_put(handle, visited)
    return visited

def _first_visits_per_airport(df_visits: pd.DataFrame) -> pd.DataFrame:
    if df_visits is None or df_visits.empty or "airport_id" not in df_visits.columns:
        return df_visits.iloc[0:0]

    df = df_visits.copy()
    df["airport_id"] = (
        df["airport_id"].astype(str).str.strip().str.upper().replace({"": pd.NA}).dropna()
    )

    # Choose a timestamp column (adjust candidates to your schema)
    candidates = [
        "date", "DATE",
        "landing_date", "LANDING_DATE",
        "visit_date", "VISIT_DATE",
        "timestamp", "TIMESTAMP",
        "time", "TIME",
    ]
    dt_col = next((c for c in candidates if c in df.columns), None)

    if dt_col:
        df["_dt"] = pd.to_datetime(df[dt_col], errors="coerce")
    else:
        # Worst-case: keep first row per airport without date ordering
        df["_dt"] = pd.NaT

    df = df.sort_values(["airport_id", "_dt"], ascending=[True, True], na_position="last")
    df = df.drop_duplicates(subset=["airport_id"], keep="first").drop(columns=["_dt"])
    return df

def _map_cache_invalidate_prefix(handle: str) -> None:
    """
    Remove any cached map renders for this handle.
    Assumes your cache key includes the handle somewhere (recommended).
    """
    h = (handle or "").strip().lower()
    if not h:
        return

    # Example: if you store cache in a dict called MAP_HTML_CACHE
    cache = globals().get("MAP_HTML_CACHE")
    if not isinstance(cache, dict):
        return

    kill = [k for k in cache.keys() if h in str(k)]
    for k in kill:
        cache.pop(k, None)

    if kill:
        print(f"_map_cache_invalidate_prefix: cleared {len(kill)} keys for @{h}")

# -----------------------------
# Undo (Option A): backup whole my_visits.csv before destructive ops (delete)
# -----------------------------
UNDO_VISITS_KEY_FMT = "users/{handle}/my_visits_undo.csv"

def _undo_visits_local_path(visits_path: str) -> str:
    # keep next to my_visits.csv
    base_dir = os.path.dirname(visits_path)
    return os.path.join(base_dir, "my_visits_undo.csv")

def _read_undo_visits_bytes(visits_path: str, handle: str | None = None) -> bytes | None:
    """Read the undo backup bytes for my_visits.csv, if present."""
    if _r2_enabled() and handle:
        key = UNDO_VISITS_KEY_FMT.format(handle=handle)
        s3 = _r2_client()
        try:
            obj = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
            return obj["Body"].read()
        except ClientError as e:
            code = (e.response.get("Error", {}) or {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    upath = _undo_visits_local_path(visits_path)
    if os.path.exists(upath):
        with open(upath, "rb") as f:
            return f.read()
    return None

def _write_undo_visits_bytes(visits_path: str, data: bytes, handle: str | None = None) -> None:
    """Write the undo backup bytes for my_visits.csv."""
    if _r2_enabled() and handle:
        key = UNDO_VISITS_KEY_FMT.format(handle=handle)
        s3 = _r2_client()
        s3.put_object(
            Bucket=os.environ["R2_BUCKET_NAME"],
            Key=key,
            Body=data,
            ContentType="text/csv",
        )
        return

    upath = _undo_visits_local_path(visits_path)
    os.makedirs(os.path.dirname(upath), exist_ok=True)
    with open(upath, "wb") as f:
        f.write(data)

def _delete_undo_visits(visits_path: str, handle: str | None = None) -> None:
    """Best-effort delete of undo backup."""
    if _r2_enabled() and handle:
        key = UNDO_VISITS_KEY_FMT.format(handle=handle)
        s3 = _r2_client()
        try:
            s3.delete_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
        except Exception:
            pass
        return

    upath = _undo_visits_local_path(visits_path)
    try:
        if os.path.exists(upath):
            os.remove(upath)
    except Exception:
        pass

FORE_FLIGHT_KEY_FMT = "users/{handle}/foreflight_logbook.csv"

def _read_foreflight_bytes(path: str, handle: str | None = None) -> bytes | None:
    """
    Read the user's ForeFlight import CSV bytes.
    - In R2 mode: reads users/<handle>/foreflight_logbook.csv
    - Local mode: reads the provided filesystem path
    """
    if _r2_enabled() and handle:
        key = FORE_FLIGHT_KEY_FMT.format(handle=handle)
        s3 = _r2_client()
        try:
            obj = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
            return obj["Body"].read()
        except ClientError as e:
            code = (e.response.get("Error", {}) or {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None

def _write_foreflight_bytes(path: str, data: bytes, handle: str | None = None) -> None:
    """
    Write the user's ForeFlight import CSV bytes.
    - In R2 mode: writes users/<handle>/foreflight_logbook.csv
    - Local mode: writes the provided filesystem path
    """
    if _r2_enabled() and handle:
        key = FORE_FLIGHT_KEY_FMT.format(handle=handle)
        s3 = _r2_client()
        s3.put_object(
            Bucket=os.environ["R2_BUCKET_NAME"],
            Key=key,
            Body=data,
            ContentType="text/csv; charset=utf-8",
            CacheControl="no-store",
        )
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def foreflight_rows_to_visits_df(rows: list[dict]) -> pd.DataFrame:
    collected = []

    for r in rows:
        date_raw = (r.get("Date") or "").strip()
        date_str = _coerce_date(date_raw) if date_raw else ""
        callsign = (r.get("AircraftID") or "").strip().upper()
        notes = (r.get("PilotComments") or r.get("Comments") or r.get("Remarks") or "").strip()

        # Pull all three sources
        frm = r.get("From") or ""
        to  = r.get("To") or ""
        route = r.get("Route") or ""

        airports = set()

        for val in (frm, to):
            apt = _token_to_valid_airport(str(val))
            if apt:
                airports.add(apt)

        # Route: regex scrub, not whitespace split
        for tok in clean_route_points(route):
            apt = _token_to_valid_airport(tok)
            if apt:
                airports.add(apt)

        for apt in sorted(airports):
            collected.append({
                "airport_id": apt,
                "date_visited": date_str,
                "callsign": callsign,
                "notes": notes
            })

    df_out = pd.DataFrame(collected, columns=["airport_id","date_visited","callsign","notes"])
    if not df_out.empty:
        df_out["airport_id"] = df_out["airport_id"].astype(str).str.strip().str.upper()
        df_out["date_visited"] = df_out["date_visited"].astype(str).str.strip()
        df_out = df_out.drop_duplicates(subset=["airport_id","date_visited"], keep="first").reset_index(drop=True)

    return df_out

def rebuild_stripe_indexes() -> dict:
    """
    Rebuild customer/subscription -> handle indexes from entitlements across all known handles.
    Safe for local + R2. Returns stats dict.
    """
    customers: dict[str, str] = {}
    subs: dict[str, str] = {}
    seen_handles: set[str] = set()

    idx = _index_read() or {}
    handles = [h for h in idx.values() if isinstance(h, str)]

    for handle in handles:
        handle = (handle or "").strip()

        # Skip invalid + demo
        if handle == "demo":
            continue
        if not is_valid_handle(handle):
            continue
        if handle in seen_handles:
            continue
        seen_handles.add(handle)

        try:
            ent = _read_entitlements(handle) or {}
        except Exception:
            continue

        cus = (ent.get("stripe_customer_id") or "").strip()
        sub = (ent.get("stripe_subscription_id") or "").strip()

        if cus:
            customers[cus] = handle
        if sub:
            subs[sub] = handle

    _write_json_map(_stripe_customer_index_path(), customers)
    _write_json_map(_stripe_subscription_index_path(), subs)

    return {
        "handles_scanned": len(seen_handles),
        "customers_indexed": len(customers),
        "subs_indexed": len(subs),
    }

def render_public_page(*, title: str, body_html: str, extra_head_html: str = "") -> str:
    """
    Canonical wrapper for ALL public pages.

    Map41:
    - Minimal public navbar
    - Avatar + handle only (no menus, no tabs)
    - Quiet, map-first presentation
    """
    safe_title = _html.escape(title)

    # Extract handle from title if present (titles are "@handle · Map", "@handle · Achievements")
    handle = ""
    try:
        if title.startswith("@"):
            handle = title.split("·", 1)[0].lstrip("@").strip().lower()
    except Exception:
        handle = ""

    safe_handle = _safe_handle_for_avatar(handle)
    avatar_src = f"/avatar/{safe_handle}" if safe_handle else "/static/mam-logo.png"

    try:
        import time as _time
        avatar_v = str(int(_time.time())) if safe_handle else "0"
    except Exception:
        avatar_v = "0"

    public_nav = f"""
<style>
  .mam-public-nav {{
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 9000;
    background: linear-gradient(180deg, rgba(15,17,21,0.98), rgba(15,17,21,0.86));
    border-bottom: 1px solid rgba(255,255,255,0.10);
    backdrop-filter: blur(10px);
  }}

  .mam-public-inner {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }}

  .mam-public-brand {{
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: #fff;
    font-weight: 950;
    letter-spacing: -0.2px;
  }}

  .mam-public-brand img {{
    height: 26px;
    width: auto;
    border-radius: 8px;
  }}

  .mam-public-avatar {{
    width: 30px;
    height: 30px;
    border-radius: 999px;
    overflow: hidden;
    background: #ffffff;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
  }}

  .mam-public-avatar img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }}

  /* ✅ Reliable offset: spacer restores flow under fixed nav (mobile Safari-safe) */
  .mam-public-spacer {{ height: 72px; }}
  @media (max-width: 640px) {{
    .mam-public-spacer {{ height: 86px; }}
  }}

  /* Keep existing body padding (harmless redundancy / back-compat) */
  body.mam-public-padtop {{
    padding-top: 64px;
  }}
  @media (max-width: 640px) {{
    body.mam-public-padtop {{
      padding-top: 72px;
    }}
  }}
</style>

<div class="mam-public-nav">
  <div class="mam-public-inner">
    <a class="mam-public-brand" href="/">
      <img src="/logo.png" alt="MyAirportMap">
      <span>@{_html.escape(handle)}</span>
    </a>

    <span class="mam-public-avatar" aria-hidden="true">
      <img src="{avatar_src}?v={avatar_v}"
           onerror="this.onerror=null;this.src='/static/mam-logo.png';"
           alt="Avatar">
    </span>
  </div>
</div>

<!-- ✅ Spacer that pushes public content below fixed nav -->
<div class="mam-public-spacer" aria-hidden="true"></div>
"""


    # Ensure body has the padtop class AND ensure the fixed public nav
    # renders INSIDE <body> so padding applies.
    if "<body" in (body_html or "").lower():
        import re

        # 1) Add mam-public-padtop to <body ...>
        m = re.search(r"<body([^>]*)>", body_html, flags=re.IGNORECASE)
        if m:
            tag = m.group(0)
            attrs = m.group(1) or ""
            if "mam-public-padtop" not in attrs:
                if re.search(r'\bclass\s*=\s*"', attrs, flags=re.IGNORECASE):
                    body_html = re.sub(
                        r'(<body[^>]*\bclass\s*=\s*")',
                        r'\1mam-public-padtop ',
                        body_html,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                else:
                    body_html = body_html.replace(tag, tag[:-1] + ' class="mam-public-padtop">', 1)

        # 2) Inject public_nav right after the opening <body ...> tag (so it’s not above body)
        body_html = re.sub(
            r"(<body[^>]*>)",
            r"\1\n" + public_nav + "\n",
            body_html,
            count=1,
            flags=re.IGNORECASE,
        )

        body_block = body_html

    else:
        # body_html is a fragment; wrap it in a real body with the padtop class
        body_block = f"""<body class="mam-public-padtop">
{public_nav}
{body_html}
</body>"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  {extra_head_html}
</head>
{body_block}
</html>"""


def runway360_phase_icon(pct: float) -> tuple[str, str]:
    """
    Returns (icon, color) for Runway 360 based on percent complete.
    Mirrors State Badge progression.
    """
    if pct >= 100:
        return "🏆", "#FFD700"
    if pct > 80:
        return "🛬", "#00FF00"
    if pct > 50:
        return "↘️", "#FF00FF"
    if pct > 20:
        return "✈️", "#0088FF"
    return "🛫", "#00FFFF"

# -----------------------------
# Storage helpers (R2-ready) — CANONICAL USER INDEX (single source of truth)
# -----------------------------
USER_INDEX_KEY = "users/_index.json"  # object key in R2, relative path under BASE_DIR for local

def _index_path_local() -> str:
    return os.path.join(BASE_DIR, USER_INDEX_KEY)

def _index_storage_key() -> str:
    r2 = _r2_enabled()
    k = USER_INDEX_KEY if r2 else os.path.join(BASE_DIR, USER_INDEX_KEY)
    print("[INDEX] r2_enabled=", r2, "key=", k)
    return k

def _index_read() -> dict:
    key = _index_storage_key()
    try:
        raw = storage_backend.read_bytes(key)
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print("_index_read failed:", "key=", key, "err=", repr(e))
        return {}

def _index_write(idx: dict) -> None:
    key = _index_storage_key()
    if not isinstance(idx, dict):
        idx = {}
    payload = json.dumps(idx, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        # Only create dirs in local mode (absolute path)
        if key.startswith(BASE_DIR):
            os.makedirs(os.path.dirname(key), exist_ok=True)

        try:
            storage_backend.write_bytes(
                key,
                payload,
                content_type="application/json",
                cache_control="no-store",
            )
        except TypeError:
            storage_backend.write_bytes(key, payload)

    except Exception as e:
        print("_index_write failed:", "key=", key, "err=", repr(e))

def get_handle_for_user(user_id: str) -> str:
    """
    Return the chosen handle for a Clerk user_id, or "" if none.
    MUST be stable across deploys.
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return ""
    idx = _index_read()
    h = idx.get(user_id)
    return h.strip() if isinstance(h, str) else ""


def set_handle_for_user(user_id: str, handle: str) -> str:
    """
    Persist the chosen handle for a Clerk user_id.
    Returns the stored handle.
    """
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("Missing user_id.")

    safe = "".join(ch for ch in (handle or "").lower().strip() if ch.isalnum() or ch in ("-", "_"))
    if len(safe) < 3 or len(safe) > 20:
        raise ValueError("Handle must be 3–20 characters.")
    if not is_valid_handle(safe):
        raise ValueError("Invalid handle format.")

    idx = _index_read()

    for uid, h in idx.items():
        if isinstance(h, str) and h == safe and uid != user_id:
            raise ValueError("Handle already taken.")

    idx[user_id] = safe
    _index_write(idx)
    return safe

def user_visits_key(handle: str) -> str:
    """Canonical object key for a user's visits CSV (R2) or relative path segment."""
    safe = "".join(ch for ch in handle.lower() if ch.isalnum() or ch in ("-", "_"))
    return f"users/{safe}/my_visits.csv"

def resolve_visits_csv(handle: Optional[str]) -> str:
    if not handle:
        return VISITS_CSV

    safe = handle.strip()
    if not safe:
        return VISITS_CSV

    return user_visits_path(safe)

def resolve_visits_key(handle: Optional[str]) -> str:
    # Backwards-compatible alias (older code used resolve_visits_key)
    return resolve_visits_csv(handle)

def resolve_foreflight_csv(handle: str) -> str:
    """Canonical per-user ForeFlight import CSV location (R2 key or local path)."""
    safe = "".join(ch for ch in (handle or "").lower() if ch.isalnum() or ch in ("-", "_"))
    key = f"users/{safe}/foreflight_logbook.csv"
    if _r2_enabled():
        return key
    return os.path.join(BASE_DIR, key)

def user_visits_path(handle: str) -> str:
    safe = "".join(ch for ch in handle.lower() if ch.isalnum() or ch in ("-", "_"))
    key = f"users/{safe}/my_visits.csv"
    if _r2_enabled():
        return key
    return os.path.join(BASE_DIR, key)

RUNWAY360_NUMBERS = [f"{i:02d}" for i in range(1, 37)]

def user_runway360_path(handle: str) -> str:
    safe = "".join(ch for ch in handle.lower() if ch.isalnum() or ch in ("-", "_"))
    key = f"users/{safe}/runway360.json"
    if _r2_enabled():
        return key
    return os.path.join(BASE_DIR, key)

def _read_json_bytes(path: str) -> bytes | None:
    try:
        if _r2_enabled():
            return storage_backend.read_bytes(path)  # type: ignore[attr-defined]
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None

def _write_json_bytes(path: str, data: bytes) -> None:
    if _r2_enabled():
        storage_backend.write_bytes(path, data)  # type: ignore[attr-defined]
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def load_runway360(handle: str) -> dict:
    path = user_runway360_path(handle)
    raw = _read_json_bytes(path)
    if not raw:
        return {"items": {}}
    try:
        obj = json.loads(raw.decode("utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("items"), dict):
            return obj
    except Exception:
        pass
    return {"items": {}}

def save_runway360(handle: str, data: dict) -> None:
    path = user_runway360_path(handle)
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    _write_json_bytes(path, payload)

def runway360_completed_set(data: dict) -> set[str]:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return set()
    done = set()
    for rwy, rec in items.items():
        if not isinstance(rwy, str):
            continue
        if not isinstance(rec, dict):
            continue
        # consider “complete” if any meaningful field exists
        if (rec.get("date") or "").strip() or (rec.get("airport") or "").strip() or (rec.get("aircraft") or "").strip():
            done.add(rwy)
    return done

def _index_path_local() -> str:
    return os.path.join(BASE_DIR, USER_INDEX_KEY)

def _read_user_index() -> dict:
    try:
        key = USER_INDEX_KEY
        if not storage_backend._r2_enabled():  # type: ignore[attr-defined]
            key = _index_path_local()
        if not storage_backend.exists(key):
            return {}
        raw = storage_backend.read_bytes(key).decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}

def _write_user_index(idx: dict) -> None:
    data = json.dumps(idx, indent=2, sort_keys=True).encode("utf-8")
    key = USER_INDEX_KEY
    if not storage_backend._r2_enabled():  # type: ignore[attr-defined]
        key = _index_path_local()
    storage_backend.write_bytes(key, data, content_type="application/json", cache_control="no-store")

def _user_recent_achievements_key(handle: str) -> str:
    handle = (handle or "").strip().lower()
    return f"users/{handle}/recent_achievements.json"

def _public_recent_achievements_key() -> str:
    return "public/recent_achievements.json"

def _append_recent_achievement_once(*, handle: str, event: dict, also_public: bool) -> None:
    """
    Append a recent-achievement event once to:
      - users/<handle>/recent_achievements.json
      - optionally public/recent_achievements.json (Pilot's Lounge right column)
    """
    handle = (handle or "").strip().lower()
    if not handle:
        return

    ev_id = str(event.get("id") or "").strip()
    if not ev_id:
        return

    def _load_list(key: str) -> list:
        try:
            obj = _load_json_from_storage(key)
            return obj if isinstance(obj, list) else []
        except Exception:
            return []

    def _save_list(key: str, items: list) -> None:
        _put_json_to_storage(key, items)

    # user list
    key_user = _user_recent_achievements_key(handle)
    items = _load_list(key_user)
    if not any(isinstance(x, dict) and str(x.get("id") or "") == ev_id for x in items):
        items.insert(0, event)
        items = items[:100]   # cap
        _save_list(key_user, items)

    # public list (optional)
    if also_public:
        key_pub = _public_recent_achievements_key()
        pub = _load_list(key_pub)
        if not any(isinstance(x, dict) and str(x.get("id") or "") == ev_id for x in pub):
            pub.insert(0, event)
            pub = pub[:200]   # cap
            _save_list(key_pub, pub)



def get_or_create_handle_for_user(user_id: str) -> str:
    """
    MVP: default handle is the Clerk user_id (sanitized). This guarantees uniqueness and prevents collisions.
    Later: replace with a proper handle-claiming flow.
    """
    idx = _read_user_index()
    if user_id in idx and idx[user_id]:
        return idx[user_id]
    # use user_id as default handle
    handle = "".join(ch for ch in user_id.lower() if ch.isalnum() or ch in ("-", "_"))[:40] or "user"
    idx[user_id] = handle
    _write_user_index(idx)
    return handle

def render_public_gate_page(next_path: str = "/", *, mode: str = "trial_ended") -> str:
    """
    Public-facing gate page for /u/<handle> routes.

    mode:
      - "trial_ended": trial expired (upgrade)
      - "members_only": public share is a paid feature (upgrade)
      - "private": owner hasn't enabled public share yet (sign in)
    """
    next_path = (next_path or "/").strip() or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"

    # Where Upgrade should return afterward
    up = "/upgrade?next=" + quote(next_path, safe="/=?&")

    # Safe sign-in return (for people who are actually the owner)
    sign_in = "/sign-in?next=" + quote(next_path, safe="/=?&")

    mode = (mode or "trial_ended").strip().lower()

    if mode == "private":
        title = "This profile is private"
        subtitle = "The owner hasn’t enabled public sharing yet. If this is your profile, sign in to manage sharing."
        primary_href = sign_in
        primary_label = "Sign in"
    elif mode == "members_only":
        title = "Members only"
        subtitle = "Public maps are available after upgrading. Upgrade to unlock sharing instantly."
        primary_href = up
        primary_label = "Upgrade"
    else:
        # "trial_ended" default
        title = "Your 30-day trial has ended"
        subtitle = "Upgrade to keep your map and achievements available to others."
        primary_href = up
        primary_label = "Upgrade"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{_html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background:#0f1115; color:#fff; margin:0; }}
    .wrap {{ max-width:820px; margin:50px auto; padding:0 16px; }}
    .card {{ background:#171a21; border:1px solid #2a2f3a; border-radius:16px; padding:18px; }}
    .btn {{ display:inline-block; padding:12px 14px; border-radius:12px; text-decoration:none; font-weight:800; }}
    .primary {{ background:#2b7cff; color:#fff; }}
    .muted {{ color:#aab2c0; font-size:14px; line-height:1.5; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0 0 10px; font-size:28px;">{_html.escape(title)}</h1>
      <p class="muted" style="margin:0 0 10px;">{_html.escape(subtitle)}</p>
      <div class="row">
        <a class="btn primary" href="{primary_href}">{_html.escape(primary_label)}</a>
        <a class="muted" href="{sign_in}" style="text-decoration:none;">Sign in</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

def generate_home_content(handle: str | None = None) -> str:
    # TEMP: until multi-user is wired to a DB, keep a simple “directory”
    profiles = [
        {"handle": "demo", "display": "Demo Pilot", "blurb": "Sample public profile (map + badges)."},
        # {"handle": "billy", "display": "Billy", "blurb": "MyAirportMap profile"},
    ]

    cards = []
    for p in profiles:
        h = _html.escape(p["handle"])          # ✅ don't overwrite function arg
        display = _html.escape(p["display"])
        blurb = _html.escape(p["blurb"])
        cards.append(f"""
          <div class="card profile" data-handle="{h}" data-display="{display}">
            <div class="row">
              <div>
                <div class="title">{display}</div>
                <div class="muted">@{h} · {blurb}</div>
              </div>
              <div class="actions">
                <a class="btn" href="/u/{h}">View profile</a>
              </div>
            </div>
          </div>
        """)

    cards_html = "\n".join(cards) if cards else '<div class="muted">No profiles yet.</div>'

    # ✅ correct indentation + uses the real logged-in handle
    continue_html = ""
    if handle and handle != "demo":
        safe_handle = _html.escape(str(handle))
        continue_html = f"""
          <div class="card" style="border:1px solid #2a2a2a;">
            <div class="row" style="align-items:center; justify-content:space-between;">
              <div>
                <div class="title">Welcome back, @{safe_handle}</div>
                <div class="muted">Continue to your dashboard.</div>
              </div>
              <div class="actions">
                <a class="btn" href="/app">Continue</a>
              </div>
            </div>
          </div>
        """
            # Pills: always safe links (no paywall route)
    demo_map_url = "/u/demo/map"
    demo_badges_url = "/u/demo/achievements"

    user_map_url = None
    user_badges_url = None
    if handle and handle != "demo":
        safe_handle = _html.escape(str(handle))
        user_map_url = f"/u/{safe_handle}/map"
        user_badges_url = f"/u/{safe_handle}/achievements"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MyAirportMap</title>
 <style>
  body {{ background:#0f0f0f; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding-top:70px; }}
  .container {{ max-width:900px; margin:0 auto; padding:22px; }}
  .hero {{ background:#151515; border:1px solid #2a2a2a; border-radius:18px; padding:18px; margin-bottom:16px; }}
  .h1 {{ font-size:26px; font-weight:900; margin:0 0 6px; }}
  .muted {{ color:#a0a0a0; font-size:14px; }}
  .row {{ display:flex; justify-content:space-between; gap:14px; align-items:center; }}
  .search {{ width:100%; box-sizing:border-box; padding:12px 14px; border-radius:14px; background:#0a0a0a; border:1px solid #333; color:#fff; font-size:15px; }}
  .card {{ background:#141414; border:1px solid #2a2a2a; border-radius:18px; padding:14px; margin:10px 0; }}
  .title {{ font-size:17px; font-weight:850; }}
  .btn {{ display:inline-block; padding:10px 12px; border-radius:12px; background:#1f1f1f; border:1px solid #3a3a3a; color:#fff; text-decoration:none; font-weight:700; }}
  .btn:hover {{ border-color:#666; }}
  .pillbar {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
  .pill {{ padding:8px 10px; border-radius:999px; background:#111; border:1px solid #2d2d2d; color:#cfcfcf; text-decoration:none; font-size:13px; }}
</style>
</head>
<body>
  {get_navbar("home")}
  <div class="container">

    {continue_html}

    <div class="hero">
      <div class="h1">MyAirportMap</div>
      <div class="muted">View public profiles (maps + badges). Logbooks stay private.</div>

      <div style="height:12px;"></div>
      <input id="q" class="search" placeholder="Search users (handle or name)…" />

      <div class="pillbar">
        <a class="pill" href="{demo_map_url}">Open demo map</a>
        <a class="pill" href="{demo_badges_url}">Open demo achievements</a>
        <a class="pill" href="/logbook">Logbook (private)</a>
        {"<a class='pill' href='" + user_map_url + "'>Your public map</a>" if user_map_url else ""}
        {"<a class='pill' href='" + user_badges_url + "'>Your public achievements</a>" if user_badges_url else ""}
      </div>
    </div>
    <div id="results">
      {cards_html}
    </div>
  </div>

  <script>
    const q = document.getElementById('q');
    const cards = Array.from(document.querySelectorAll('.profile'));
    q.addEventListener('input', () => {{
      const term = (q.value || '').toLowerCase().trim();
      cards.forEach(c => {{
        const hay = (c.dataset.handle + ' ' + c.dataset.display).toLowerCase();
        c.style.display = (!term || hay.includes(term)) ? '' : 'none';
      }});
    }});
  </script>
</body>
</html>
"""

def normalize_id(val: Any) -> str:
    """Normalize airport identifiers to a stable join key (similar intent to Map6 normalize_id)."""
    s = "" if val is None else str(val)
    s = s.strip().upper()
    s = s.replace(",", "").replace(" ", "")
    s = s.strip(";:|")
    # Strip leading K for ICAO-style codes when it looks like K + 3 letters (KCDW -> CDW)
    if len(s) == 4 and s.startswith("K") and s[1:].isalnum():
        return s[1:]
    return s

def normalize_towered_status(val: str) -> str:
    """Normalize towered status to 'Towered' or 'Non-Towered' (Map6 semantics)."""
    s = (val or "").strip()
    if not s:
        return "Non-Towered"
    sl = s.lower()

    # explicit negatives
    if "non" in sl and "tower" in sl:
        return "Non-Towered"
    if sl in {"no", "n", "false", "0", "uncontrolled", "ctaf", "none"}:
        return "Non-Towered"

    # explicit positives
    if sl in {"yes", "y", "true", "1", "towered", "controlled", "twr"}:
        return "Towered"
    if "tower" in sl and "non" not in sl:
        return "Towered"
    if "twr" in sl:
        return "Towered"
    if "atc" in sl and "none" not in sl:
        return "Towered"

    # default (conservative): treat unknown as Non-Towered
    return "Non-Towered"

def _find_airports_file() -> str:
    for p in AIRPORTS_CANDIDATES:
        if storage_backend.exists(p):
            return p
    # fallback: first CSV in folder that looks like airports data
    for fn in os.listdir(BASE_DIR):
        if fn.lower().endswith(".csv") and "visit" not in fn.lower() and "logbook" not in fn.lower():
            return os.path.join(BASE_DIR, fn)
    raise FileNotFoundError("Could not find an airports CSV in BASE_DIR.")

def _pick_col(cols: List[str], candidates: List[str]) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None

def _normalize_state_value(val: Any) -> str:
    """Return a 2-letter US state code when possible (supports iso_region like US-NJ)."""
    s = "" if val is None else str(val).strip()
    if not s:
        return ""
    # iso_region format (e.g., US-NJ)
    m = re.match(r"^[A-Za-z]{2}-([A-Za-z]{2})$", s)
    if m:
        return m.group(1).upper()
    # already a 2-letter code
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return s

@functools.lru_cache(maxsize=1)
def load_airports_cached() -> pd.DataFrame:
    """
    Map38 canonical airports loader (LOCKED).

    ✅ Loads ONLY: BASE_DIR/airports.csv (no fallback finder, no merges)
    ✅ Requires EXACT canonical columns:
       airport_id, state, lat, long, towered_status, name
    ✅ Returns dataframe with canonical columns PLUS norm_id (compat)
       and MAY include optional columns (e.g., airspace_b) if present.
    """
    airports_path = os.path.join(BASE_DIR, "airports.csv")

    if not os.path.exists(airports_path):
        raise FileNotFoundError(
            f"[Map38] Canonical airports.csv not found: {airports_path}. "
            f"Fix: ensure airports.csv is committed at repo root alongside app.py."
        )

    df = pd.read_csv(airports_path)

    required = ["airport_id", "state", "lat", "long", "towered_status", "name"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"[Map38] airports.csv missing required columns {missing}. "
            f"Found: {list(df.columns)}"
        )

    # ✅ Optional passthrough columns (do NOT require)
    optional = []
    for col in ("airspace_b", "airspace_c"):
        if col in df.columns:
            optional.append(col)

    out = df[required + optional].copy()

    # ✅ Compat: norm_id (3-letter canonical), relies on existing K-stripper behavior
    # We only use 3-letter IDs in airports.csv; this protects joins when visits/logs contain KXXX.
    if "norm_id" not in out.columns:
        out["norm_id"] = out["airport_id"].astype(str).map(normalize_airport)

    cols = ["airport_id", "norm_id", "name", "state", "lat", "long", "towered_status"]
    for col in ("airspace_b", "airspace_c"):
        if col in out.columns:
            cols.append(col)

    return out[cols]


def load_visits(visits_csv: Optional[str] = None, handle: Optional[str] = None) -> pd.DataFrame:
    """Load visits CSV from R2 (when enabled) or local filesystem.

    - If handle is provided and R2 is enabled, reads users/<handle>/my_visits.csv via storage_backend.
    - Otherwise reads from the provided visits_csv path (or VISITS_CSV) on local disk.
    """
    path = visits_csv or resolve_visits_csv(handle)
    raw = _read_visits_bytes(path, handle=handle)

    if not raw:
        return pd.DataFrame(columns=["airport_id", "norm_id", "date_visited", "callsign", "notes"])

    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception:
        return pd.DataFrame(columns=["airport_id", "norm_id", "date_visited", "callsign", "notes"])

    # Standardize columns
    if "airport_id" not in df.columns:
        cand = _pick_col(df.columns.tolist(), ["airport_id_visit", "airport", "ident"])
        df["airport_id"] = df[cand] if cand else ""

    if "norm_id" not in df.columns:
        df["norm_id"] = df["airport_id"].apply(normalize_id)
    else:
        df["norm_id"] = df["norm_id"].apply(normalize_id)

    if "date_visited" not in df.columns:
        cand = _pick_col(df.columns.tolist(), ["date", "visited_date", "visit_date"])
        df["date_visited"] = df[cand] if cand else ""

    if "callsign" not in df.columns:
        cand = _pick_col(df.columns.tolist(), ["tail", "aircraft", "registration", "callsign_used"])
        df["callsign"] = df[cand] if cand else ""

    if "notes" not in df.columns:
        cand = _pick_col(df.columns.tolist(), ["note", "remarks", "comment"])
        df["notes"] = df[cand] if cand else ""

    # Keep only canonical columns (in stable order)
    df = df[["airport_id", "norm_id", "date_visited", "callsign", "notes"]].copy()

    # Clean up
    df["airport_id"] = df["airport_id"].astype(str).str.strip().str.upper()
    df["norm_id"] = df["airport_id"].apply(normalize_id)
    df["date_visited"] = df["date_visited"].astype(str).str.strip()
    df["callsign"] = df["callsign"].astype(str).str.strip()
    df["notes"] = df["notes"].astype(str)

    return df.reset_index(drop=True)

def load_data(visits_csv: Optional[str] = None, handle: Optional[str] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df_airports = load_airports_cached()
    df_visits = load_visits(visits_csv=visits_csv, handle=handle)
    print(f"load_data: {len(df_airports)} map airports, {len(df_visits)} visits")
    return df_airports, df_visits



PROFILE_HANDLE_COOLDOWN_DAYS = 90

# Adjust this to your actual static logo path if different
MAM_LOGO_URL = "/static/mam-logo.png"

def _now_utc_ts() -> int:
    return int(time.time())

def _fmt_utc_date(ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%b %d, %Y")
    except Exception:
        return "a later date"

def _profile_prefs_key(handle: str) -> str:
    handle = (handle or "").strip().lower()
    return f"profiles/{handle}/prefs.json"

def _profile_handle_meta_key(user_id: str) -> str:
    user_id = (user_id or "").strip()
    return f"profile_meta/{user_id}.json"

def _load_json_from_storage(key: str) -> dict:
    try:
        if not key or not storage_backend.exists(key):
            return {}
        b = storage_backend.read_bytes(key)
        if not b:
            return {}
        return json.loads(b.decode("utf-8") or "{}") or {}
    except Exception:
        return {}

def _write_json_to_storage(key: str, obj: dict) -> None:
    try:
        raw = json.dumps(obj or {}, ensure_ascii=False).encode("utf-8")
        storage_backend.write_bytes(key, raw, content_type="application/json", cache_control="no-store")
    except Exception:
        pass

def _can_change_handle(user_id: str) -> tuple[bool, int | None]:
    """
    Returns (eligible, next_eligible_ts)
    If we have no record, eligible=True.
    """
    meta = _load_json_from_storage(_profile_handle_meta_key(user_id))
    last_ts = int(meta.get("last_handle_change_ts") or 0)
    if last_ts <= 0:
        return True, None

    cooldown = PROFILE_HANDLE_COOLDOWN_DAYS * 24 * 60 * 60
    next_ts = last_ts + cooldown
    now = _now_utc_ts()
    if now >= next_ts:
        return True, None
    return False, next_ts

def _set_handle_change_ts(user_id: str) -> None:
    meta_key = _profile_handle_meta_key(user_id)
    meta = _load_json_from_storage(meta_key)
    meta["last_handle_change_ts"] = _now_utc_ts()
    _write_json_to_storage(meta_key, meta)

def _migrate_profile_prefs(old_handle: str, new_handle: str) -> None:
    old_handle = (old_handle or "").strip().lower()
    new_handle = (new_handle or "").strip().lower()
    if not old_handle or not new_handle or old_handle == new_handle:
        return
    old_key = _profile_prefs_key(old_handle)
    new_key = _profile_prefs_key(new_handle)

    try:
        if storage_backend.exists(old_key) and not storage_backend.exists(new_key):
            data = _load_json_from_storage(old_key)
            _write_json_to_storage(new_key, data)
            # Best-effort cleanup (if your backend supports delete; if not, ignore)
            try:
                storage_backend.delete(old_key)  # type: ignore[attr-defined]
            except Exception:
                pass
    except Exception:
        pass

def _extract_avatar_url_from_claims(claims: dict) -> str:
    """
    Clerk claim keys vary; we try a few common ones.
    Only returns a URL-ish string or "".
    """
    if not isinstance(claims, dict):
        return ""
    for k in ("image_url", "imageUrl", "picture", "avatar", "photo", "profile_image_url"):
        v = (claims.get(k) or "").strip()
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""

def _validate_cert_keys(keys: list[str]) -> list[str]:
    allowed = {
        "atp_asel","atp_amel","atp_ases","atp_ames","atp_heli",
        "cpl_asel","cpl_amel","cpl_ases","cpl_ames","cpl_heli",
        "ppl_asel","ppl_amel","ppl_ases","ppl_ames","ppl_heli",
        "cfi","cfi_i","mei","cfi_heli",
        "flight_attendant","dispatcher","student_pilot","uas",
        # NEW – Other
        "dpe",
        "ap",
        "atc",
        "flight_engineer",
        "instrument",
    }
    out: list[str] = []
    for k in keys or []:
        k = (k or "").strip().lower()
        if k in allowed and k not in out:
            out.append(k)
    return out

def format_certifications_line(selected: list[str], username: str = "") -> str:
    s = set((k or "").strip().lower() for k in (selected or []))
    if not s:
        return ""

    def group(prefix: str, mapping: list[tuple[str, str]]) -> str:
        items = [label for key, label in mapping if key in s]
        if not items:
            return ""
        return f"{prefix}: " + ", ".join(items)

    parts: list[str] = []

    g_atp = group("ATP", [
        ("atp_asel","ASEL"), ("atp_amel","AMEL"), ("atp_ases","ASES"), ("atp_ames","AMES"), ("atp_heli","Helicopter")
    ])
    if g_atp: parts.append(g_atp)

    g_cpl = group("CPL", [
        ("cpl_asel","ASEL"), ("cpl_amel","AMEL"), ("cpl_ases","ASES"), ("cpl_ames","AMES"), ("cpl_heli","Helicopter")
    ])
    if g_cpl: parts.append(g_cpl)

    g_ppl = group("PPL", [
        ("ppl_asel","ASEL"), ("ppl_amel","AMEL"), ("ppl_ases","ASES"), ("ppl_ames","AMES"), ("ppl_heli","Helicopter")
    ])
    if g_ppl: parts.append(g_ppl)

    g_cfi = group("CFI", [
        ("cfi","CFI"), ("cfi_i","CFI-I"), ("mei","MEI"), ("cfi_heli","Helicopter")
    ])
    if g_cfi: parts.append(g_cfi)

    # Instrument Rating (standalone line, not under CFI:)
    if "instrument" in s:
        parts.append("Instrument Rated")

    others_map = [
        ("flight_attendant","Flight Attendant"),
        ("dispatcher","Aircraft Dispatcher"),
        ("student_pilot","Student Pilot"),
        ("uas","UAS"),
        ("dpe","Designated Pilot Examiner (DPE)"),
        ("ap","A&P Mechanic"),
        ("atc","Air Traffic Controller"),
        ("flight_engineer","Flight Engineer"),
    ]
    others = [label for key, label in others_map if key in s]
    if others:
        parts.append(", ".join(others))

    u = (username or "").strip()
    header = "FAA Certificates:"
    if u:
        header = f"{u}’s FAA Certificates:"

    lines = [header]          # ← header is ALWAYS its own line
    lines.extend(parts)       # ← each group already formatted as "ATP: ...", etc.

    return "<br>".join(lines)

# -----------------------------
# UI helpers
# -----------------------------
def get_account_status(handle: str | None) -> str:
    """
    Returns: "member" | "trial" | "trial_ended"
    """
    if not handle:
        return "trial_ended"
    h = handle.strip().lower()
    if not h:
        return "trial_ended"
    if is_paid_user_handle(h):
        return "member"
    if trial_is_active_for_handle(h):
        return "trial"
    return "trial_ended"

PUBLIC_NAVBAR_CSS = """
<style>
  .public-title { color:#fff; font-weight:950; font-size:14px; letter-spacing:-0.2px; }
  .ptab { display:inline-block; padding:9px 12px; border-radius:999px; text-decoration:none; font-weight:900; font-size:12px; }
  .ptab-active { background:#fff; color:#111; }
  .ptab-inactive { background:rgba(255,255,255,0.12); color:#fff; border:1px solid rgba(255,255,255,0.18); }
</style>
"""

def get_public_navbar(public_handle: str, active: str) -> str:
    safe_handle = (public_handle or "").strip().lower()
    h_esc = _html.escape(safe_handle)
    title = f"{h_esc}&#39;s MyAirportMap - Shared"

    # Map41: public avatar (always something)
    avatar_src = f"/avatar/{safe_handle}" if safe_handle else "/static/mam-logo.png"

    def tab(href: str, label: str, key: str) -> str:
        is_active = (active == key)
        cls = "ptab ptab-active" if is_active else "ptab ptab-inactive"
        return f'<a class="{cls}" href="{href}">{label}</a>'

    map_href = f"/u/{safe_handle}/map"
    ach_href = f"/u/{safe_handle}/achievements"

    return f"""
<style>
  /* iOS/Safari sizing stability */
  html, body {{
    -webkit-text-size-adjust: 100%;
    text-size-adjust: 100%;
  }}

  /* Public navbar base (hardened) */
  .mam-public-bar {{
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 9999;
    background: #111;
    border-bottom: 1px solid #333;
    padding: 12px 16px;
  }}

  .mam-public-inner {{
    max-width: 1100px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
  }}

  .mam-public-brand {{
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
  }}

  .mam-public-brand img {{
    height: 28px !important;
    width: auto !important;
  }}

  /* Map41: public avatar chip (always something) */
  .mam-public-avatar {{
    width: 34px;
    height: 34px;
    border-radius: 999px;
    overflow: hidden;
    background: #fff;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
  }}
  .mam-public-avatar img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }}

  .public-title {{
    color: #fff !important;
    font-weight: 950 !important;
    font-size: 14px !important;
    letter-spacing: -0.2px !important;
    line-height: 1.15 !important;
  }}

  .mam-public-actions {{
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }}

  /* Tabs: FIX "circle" look by controlling vertical padding, no min-width */
  .ptab {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 7px 14px;
    min-height: 34px;
    border-radius: 999px;
    text-decoration: none;
    font-weight: 900;
    font-size: 12px !important;
    line-height: 1 !important;
    touch-action: manipulation;
    -webkit-tap-highlight-color: rgba(0,0,0,0);
  }}

  .ptab-active {{
    background: #fff;
    color: #111 !important;
  }}

  .ptab-inactive {{
    background: rgba(255,255,255,0.12);
    color: #fff !important;
    border: 1px solid rgba(255,255,255,0.18);
  }}

  /* Helper: add this to <body> on public pages to clear the fixed bar */
  .mam-public-padtop {{ padding-top: 76px; }}

  @media (max-width: 640px) {{
    .mam-public-padtop {{ padding-top: 92px; }}

    .mam-public-inner {{
      flex-direction: column !important;
      align-items: center !important;
      justify-content: center !important;
      gap: 10px !important;
    }}

    .mam-public-brand {{
      width: 100% !important;
      justify-content: center !important;
      text-align: center !important;
    }}

    .mam-public-brand img {{
      height: 24px !important;
    }}

    .public-title {{
      font-size: 16px !important;
      text-align: center !important;
      white-space: normal !important;
      max-width: 100% !important;
    }}

    .mam-public-actions {{
      width: 100% !important;
      justify-content: center !important;
      gap: 8px !important;
    }}

    .ptab {{
      font-size: 15px !important;
      padding: 8px 14px !important;
      min-height: 38px !important;
    }}
  }}
</style>

<div class="mam-public-bar">
  <div class="mam-public-inner">
    <a href="/sign-in?next=/u/{safe_handle}/map" class="mam-public-brand">
      <img src="/logo.png" alt="MyAirportMap">
      <div class="public-title">{title}</div>
    </a>

    <div class="mam-public-actions">
      <div class="mam-public-avatar" aria-hidden="true">
        <img src="{avatar_src}"
             onerror="this.onerror=null;this.src='/static/mam-logo.png';"
             alt="Avatar">
      </div>
      {tab(map_href, "Map", "map")}
      {tab(ach_href, "Achievements", "achievements")}
    </div>
  </div>
</div>
"""


def tos_accepted() -> bool:
    try:
        return (request.cookies.get("tos_accepted") or "") == "1"
    except Exception:
        return False

from datetime import datetime, timezone, timedelta

def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept "Z"
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def redirect_to_upgrade_from_public_gate():
    """
    Redirect to /upgrade, preserving the exact page the user tried to view.
    """
    next_path = _current_path_with_qs()
    return redirect(f"/upgrade?next={quote(next_path)}", code=302)
    
def _current_path_with_qs() -> str:
    """
    Return the current request path + query string as a relative URL.

    Safe for use in next=...
    Never includes scheme or host.
    """
    path = request.path or "/"
    qs = request.query_string.decode("utf-8", errors="ignore") if request.query_string else ""
    return f"{path}?{qs}" if qs else path

def avatar_key(handle: str) -> str:
    h = (handle or "").strip().lower()
    return f"avatars/{h}.bin"

def avatar_ct_key(handle: str) -> str:
    h = _safe_handle_for_avatar(handle)
    return f"avatars/{h}.content_type"

def is_safe_image_content_type(ct: str) -> bool:
    ct = (ct or "").lower().split(";")[0].strip()
    return ct in ("image/jpeg", "image/jpg", "image/png", "image/webp")

def _safe_handle_for_avatar(handle: str) -> str:
    # mirror your username sanitizer expectations
    h = (handle or "").strip().lower()
    h = re.sub(r"[^a-z0-9_-]+", "", h)
    return h

# -----------------------------
# Pilot Lounge directory (opt-in only)
# -----------------------------
DIRECTORY_KEY = "users/_directory.json"
DIRECTORY_LOCK = Lock()

_DIR_CACHE = {"ts": 0.0, "data": {}}
_DIR_TTL = 30.0  # seconds (per worker)

def _directory_read_cached() -> dict:
    now = time.time()
    if _DIR_CACHE["data"] and (now - _DIR_CACHE["ts"]) < _DIR_TTL:
        return _DIR_CACHE["data"]

    try:
        # ✅ If R2 is enabled, directory lives in R2 (same as share_activity)
        if _r2_enabled():
            obj = _read_json_r2(DIRECTORY_KEY) or {}
            d = obj if isinstance(obj, dict) else {}
        else:
            raw = storage_backend.read_bytes(DIRECTORY_KEY)
            if not raw:
                d = {}
            else:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
                d = obj if isinstance(obj, dict) else {}
    except Exception:
        d = {}

    _DIR_CACHE["ts"] = now
    _DIR_CACHE["data"] = d
    return d

def _directory_write(d: dict) -> None:
    try:
        # ✅ If R2 is enabled, write directory into R2
        if _r2_enabled():
            _write_json_r2(DIRECTORY_KEY, d if isinstance(d, dict) else {})
            return

        payload = json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            storage_backend.write_bytes(
                DIRECTORY_KEY,
                payload,
                content_type="application/json",
                cache_control="no-store",
            )
        except TypeError:
            storage_backend.write_bytes(DIRECTORY_KEY, payload)
    except Exception as e:
        print("[directory_write][err]", repr(e))


# Back-compat aliases (Map41: keep names stable for call sites)
def _directory_read() -> dict:
    d = _directory_read_cached()
    return dict(d) if isinstance(d, dict) else {}

def _directory_upsert(handle: str, *, share_on: bool, avatar_url: str | None, airports: int | None) -> None:
    directory_upsert_public(handle, share_on=share_on, avatar_url=avatar_url, airports=airports)

def _get_unique_airport_count(handle: str) -> int:
    """
    Unique airports visited for a handle (best-effort).
    Used for Lounge directory summaries only.
    """
    try:
        h = (handle or "").strip().lower()
        if not h:
            return 0
        path = resolve_visits_csv(h)
        df = _load_visits_csv(path, handle=h)
        if df is None or df.empty or ("airport_id" not in df.columns):
            return 0
        return int(df["airport_id"].astype(str).str.strip().str.upper().nunique())
    except Exception:
        return 0


def directory_upsert_public(handle: str, *, share_on: bool, avatar_url: str | None, airports: int | None) -> None:
    """
    Single source of truth for Lounge/search membership.
    - If share_on=False => remove from directory.
    - If share_on=True  => upsert minimal public-safe fields.
    """
    h = (handle or "").strip().lower()
    if not h:
        return

    with DIRECTORY_LOCK:
        d = _directory_read_cached().copy()

        if not share_on:
            d.pop(h, None)
        else:
            d[h] = {
                "handle": h,
                "avatar_url": (avatar_url or f"/avatar/{h}"),
                "airports": int(airports or 0),
                "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
            }

        _directory_write(d)
        # bust local cache
        _DIR_CACHE["ts"] = 0.0
        _DIR_CACHE["data"] = {}

def _directory_refresh_for_handle(handle: str) -> None:
    """
    Map41: refresh lounge directory entry for a handle (opt-in only).
    Safe to call after any visits write.
    """
    h = (handle or "").strip().lower()
    if not h:
        return
    try:
        directory_upsert_public(
            h,
            share_on=_get_share_activity(h),
            avatar_url=f"/avatar/{h}",
            airports=_get_unique_airport_count(h),
        )
    except Exception:
        pass

# -----------------------------
# Pilot Lounge spotlight: 4 seats, rotate 1 every 6 hours
# -----------------------------
LOUNGE_SNAPSHOT_KEY = "users/_lounge_snapshot.json"
LOUNGE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
LOUNGE_SEATS = 4

def _lounge_load_snapshot() -> dict:
    try:
        raw = storage_backend.read_bytes(LOUNGE_SNAPSHOT_KEY)
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _lounge_save_snapshot(obj: dict) -> None:
    try:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            storage_backend.write_bytes(LOUNGE_SNAPSHOT_KEY, payload, content_type="application/json", cache_control="no-store")
        except TypeError:
            storage_backend.write_bytes(LOUNGE_SNAPSHOT_KEY, payload)
    except Exception as e:
        print("[lounge_snapshot_write][err]", repr(e))

def _choose_next_lounge_seats(directory_items: list[dict], prev_seats: list[dict]) -> list[dict]:
    """
    4-seat lounge:
      - keep 3 stable
      - rotate 1 every 6 hours
      - random replacement, avoid repeats when possible
      - never duplicate handles in the final seat list
    """
    # Normalize directory to unique-by-handle dict (latest wins)
    dmap: dict[str, dict] = {}
    for u in (directory_items or []):
        if not isinstance(u, dict):
            continue
        h = (u.get("handle") or "").strip().lower()
        if not h:
            continue
        dmap[h] = u
    if not dmap:
        return []

    all_handles = list(dmap.keys())

    # Normalize previous handles (order-preserving)
    prev_handles: list[str] = []
    seen = set()
    for s in (prev_seats or []):
        if not isinstance(s, dict):
            continue
        h = (s.get("handle") or "").strip().lower()
        if not h or h in seen:
            continue
        seen.add(h)
        # keep only if still eligible
        if h in dmap:
            prev_handles.append(h)

    # Keep first 3 stable, rotate out the last seat if present
    stable_handles = prev_handles[: max(0, LOUNGE_SEATS - 1)]
    rotated_out = prev_handles[LOUNGE_SEATS - 1] if len(prev_handles) >= LOUNGE_SEATS else ""

    current_set = set(stable_handles)

    # Candidate pool: prefer not currently shown
    pool = [h for h in all_handles if h not in current_set]

    # Repeat resistance: if possible, avoid immediately bringing back the rotated-out handle
    if rotated_out and rotated_out in pool and len(pool) > 1:
        pool = [h for h in pool if h != rotated_out]

    # Tiny-N fallback: if pool is empty (eligible <= current seats), allow any eligible
    if not pool:
        pool = all_handles[:]

    pick_handle = random.choice(pool) if pool else ""
    out_handles = stable_handles + ([pick_handle] if pick_handle else [])

    # Final de-dupe (preserve order) + pad if needed
    final_handles: list[str] = []
    used = set()
    for h in out_handles:
        if not h or h in used:
            continue
        if h in dmap:
            used.add(h)
            final_handles.append(h)
        if len(final_handles) >= LOUNGE_SEATS:
            break

    # Pad up to LOUNGE_SEATS if still short (very small eligible pool)
    if len(final_handles) < LOUNGE_SEATS:
        for h in all_handles:
            if h in used:
                continue
            used.add(h)
            final_handles.append(h)
            if len(final_handles) >= LOUNGE_SEATS:
                break

    return [dmap[h] for h in final_handles if h in dmap][:LOUNGE_SEATS]

# -----------------------------
# Map Page (Map36 baseline + Map37 cache + JS lazy airport dots)
#   ✅ Pins behave like Map36 (towered blue, non-towered purple)
#   ✅ Navbar injected at TOP of <body> so dropdown works
#   ✅ Cache key includes filter_state + navbar_mode (NO dots flag anymore)
#   ✅ Unvisited airport dots are now client-side (lazy, viewport-based)
# -----------------------------
def generate_map_content(filter_state=None, visits_csv=None, handle=None, navbar_mode: str = "owner"):
    visits_csv = visits_csv or resolve_visits_csv(handle)

    # -----------------------------
    # Progressive visits flag (safe rollout)
    #   pv=1 => do NOT embed visit markers in Folium HTML
    #          (JS will hydrate First Visits + All Visits by viewport)
    # -----------------------------
    DEFAULT_NEARBY_MILES = 500

    try:
        pv_raw = (request.args.get("pv") or "").strip().lower()
        progressive_visits = pv_raw not in {"0", "false", "no", "off"}
    except Exception:
        progressive_visits = True

    # nearby_miles MUST always be defined (even when pv=0)
    nearby_miles = DEFAULT_NEARBY_MILES
    if progressive_visits:
        try:
            miles_raw = (request.args.get("miles") or "").strip()
            if miles_raw:
                miles_q = int(float(miles_raw))
            else:
                miles_q = DEFAULT_NEARBY_MILES
            if miles_q < 50:
                miles_q = 50
            if miles_q > 2000:
                miles_q = 2000
            nearby_miles = miles_q
        except Exception:
            nearby_miles = DEFAULT_NEARBY_MILES

    # -----------------------------
    # Cache fast-path (must include pv+miles to avoid collisions)
    # -----------------------------
    try:
        pv_tag = "pv1" if progressive_visits else "pv0"
    except Exception:
        pv_tag = "pv0"

    cache_key = _map_cache_key(handle, filter_state, navbar_mode) + f":map36_reset:jsdots:{pv_tag}:m{int(nearby_miles)}"
    cached = _map_cache_get(cache_key)
    if cached:
        return cached

    # -----------------------------
    # Timing stamps
    # -----------------------------
    t0 = time.perf_counter()
    def _t(label: str):
        try:
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"[MAP_TIMING] {label} ms={ms} handle={handle or ''} public={navbar_mode=='public'} state={filter_state or ''}")
        except Exception:
            pass

    _t("start")

    df_airports, df_visits = load_data(visits_csv=visits_csv, handle=handle)
    _t(f"after load_data airports={len(df_airports)} visits={len(df_visits)}")


    # Optional state filter (keeps any downstream work smaller)
    if filter_state:
        fs = str(filter_state).strip().upper()
        if "state" in df_airports.columns:
            df_airports = df_airports[df_airports["state"].astype(str).str.upper() == fs].copy()

    # --- Normalize towered_status column name (Map36 compatibility) ---
    if "towered_status" not in df_airports.columns:
        for cand in ("Towered_Status", "tower_status", "tower_status_text", "_tower_status", "towered"):
            if cand in df_airports.columns:
                df_airports["towered_status"] = df_airports[cand]
                break

    # --- Canonicalize tower status into exactly: "Towered" or "Non-Towered" ---
    def _canon_towered_status(v) -> str:
        s = str(v or "").strip().lower()
        if not s or s in {"nan", "none", "null"}:
            return "Non-Towered"

        # explicit non-towered first (prevents "no tower" being misread)
        if ("non" in s and "tower" in s) or ("no tower" in s):
            return "Non-Towered"
        if s in {"no", "n", "false", "0", "uncontrolled", "ctaf", "untowered", "none"}:
            return "Non-Towered"

        # explicit towered
        if s in {"towered", "twr", "yes", "y", "true", "1", "controlled", "ct", "c"}:
            return "Towered"
        if ("tower" in s and "non" not in s) or ("twr" in s):
            return "Towered"

        return "Non-Towered"

    if "towered_status" not in df_airports.columns:
        df_airports["towered_status"] = "Non-Towered"
    df_airports["towered_status"] = df_airports["towered_status"].map(_canon_towered_status)

    # -----------------------------
    # Pseudo-geolocate: center map on user's most recent visit (safe)
    # -----------------------------
    map_center = DEFAULT_CENTER
    try:
        if handle and isinstance(df_visits, pd.DataFrame) and not df_visits.empty:
            if "norm_id" in df_visits.columns and "norm_id" in df_airports.columns:
                cols_v = ["norm_id"]
                if "date_visited" in df_visits.columns:
                    cols_v.append("date_visited")

                cols_a = ["norm_id", "lat", "long"]
                tmp = pd.merge(
                    df_visits[cols_v].copy(),
                    df_airports[cols_a].copy(),
                    on="norm_id",
                    how="left",
                )

                tmp["lat"] = pd.to_numeric(tmp["lat"], errors="coerce")
                tmp["long"] = pd.to_numeric(tmp["long"], errors="coerce")
                tmp = tmp.dropna(subset=["lat", "long"]).copy()

                if not tmp.empty:
                    if "date_visited" in tmp.columns:
                        tmp["_dt"] = pd.to_datetime(tmp["date_visited"], errors="coerce", utc=False)
                        tmp = tmp.sort_values(by="_dt", ascending=False, na_position="last")
                    row0 = tmp.iloc[0]
                    lat0 = float(row0["lat"])
                    lon0 = float(row0["long"])
                    if abs(lat0) <= 90 and abs(lon0) <= 180:
                        map_center = [lat0, lon0]
    except Exception:
        pass

    # Base map
    m = folium.Map(
        location=map_center,
        zoom_start=DEFAULT_ZOOM + 1,
        tiles=None,
        prefer_canvas=True,
        control_scale=True,
    )

    # ✅ Map40: Map page opts out of global body padding (navbar already fixed-position)
    # This prevents the "white strip" / double spacing under the navbar.
    m.get_root().html.add_child(folium.Element("""
    <script>
    try { document.body.classList.add("mam-map"); } catch (e) {}
    </script>
    """))

    # ✅ MarkerCluster + CSS (must load BEFORE pv/unvisited JS is injected)
    m.get_root().header.add_child(folium.Element(r"""
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.awesome-markers/2.0.4/leaflet.awesome-markers.css">
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
    <link rel="icon" href="/static/favicon.ico" sizes="any">
    
    <style>
    /* -----------------------------
    MarkerCluster visibility (keep)
    ----------------------------- */
    .marker-cluster, .marker-cluster div{ box-sizing:content-box !important; }
    .marker-cluster{ z-index:650 !important; }

    /* -----------------------------
    MyAirportMap pin styling (keep)
    ----------------------------- */
    .mam-pin-hit{
        width:44px; height:44px;
        display:flex; align-items:center; justify-content:center;
    }
    .mam-pin-icon{
        background: transparent !important;
        border: none !important;
    }
    .mam-pin-icon svg{
        width: 30px;
        height: 30px;
        display:block;
        pointer-events:none;
    }

    /* -----------------------------
    Popups ABOVE Leaflet controls
    ----------------------------- */
    .leaflet-control-container { z-index: 1000 !important; }
    .leaflet-popup-pane,
    .leaflet-tooltip-pane { z-index: 7000 !important; }



    /* -----------------------------
    LayerControl: compact (and overrides old “giant” rules)
    ----------------------------- */
    .leaflet-control-layers{
        font-size: 12px !important;
        line-height: 1.15 !important;
        padding: 4px 6px !important;
        border-radius: 10px !important;
        max-height: 60vh !important;
        overflow-y: auto !important;   /* keep vertical scroll */
        overflow-x: hidden !important; /* permanently kill horizontal */
    }

    /* rows (base + overlays) */
    .leaflet-control-layers-overlays label,
    .leaflet-control-layers-base label{
        display:flex !important;
        align-items:center !important;
        gap:6px !important;

        padding: 2px 4px !important;   /* <- THIS defeats the old 8px 10px */
        margin: 0 !important;
        line-height: 1.15 !important;

        user-select:none !important;
        -webkit-user-select:none !important;
    }

    /* checkboxes (defeats old 20x20 + any transform scale) */
    .leaflet-control-layers input[type="checkbox"]{
        width: 14px !important;
        height: 14px !important;
        margin: 0 6px 0 0 !important;
        transform: none !important;
        flex: 0 0 auto !important;
    }

    /* ✅ Ensure Leaflet links remain clickable (popups + attribution) */
    .leaflet-container a,
    .leaflet-control-attribution a,
    .leaflet-popup-content a {
      pointer-events: auto !important;
    }

    /* ✅ Make sure the popup pane itself can receive clicks */
    .leaflet-popup-pane,
    .leaflet-popup,
    .leaflet-popup-content-wrapper,
    .leaflet-popup-content {
      pointer-events: auto !important;
    }

    /* mobile tighten */
    @media (max-width: 640px){
        .leaflet-control-layers{ font-size: 11px !important; padding: 4px !important; }
        .leaflet-control-layers-overlays label,
        .leaflet-control-layers-base label{ padding: 2px 3px !important; }
    }

    </style>
    """))


    # Keep Leaflet controls below fixed header/navbar
    # + Mobile typography boost
    # + Loading overlay (logo + spinning ring) — delayed show (prevents flash on fast loads)
    m.get_root().header.add_child(folium.Element(r"""
    <style>
    /* -----------------------------
    Mobile typography boost
    (Public site + Achievements / Manage / Lounge)
    ----------------------------- */
    @media (max-width: 640px) {
    body { font-size: 16px; line-height: 1.5; }
    p, li, label, input, select, textarea { font-size: 16px; line-height: 1.5; }

    table { font-size: 15px; }
    th, td { padding: 8px 10px; }

    h1 { font-size: 20px; }
    h2 { font-size: 18px; }
    h3 { font-size: 16px; }

    nav, .navbar { font-size: 15px; }
    nav a, .navbar a, .navbar button { font-size: 15px; padding: 10px 12px; }

    .pill, .pill-menu a, .btn, button { font-size: 15px; }
    }

    /* -----------------------------
    MyAirportMap loading overlay
    ----------------------------- */
    #mam-loading {
    position: fixed;
    inset: 0;
    z-index: 100000;
    display: none;            /* start hidden */
    align-items: center;
    justify-content: center;
    background: rgba(10, 12, 16, 0.55);
    backdrop-filter: blur(4px);
    opacity: 0;
    transition: opacity 0.15s ease-out;
    }
    #mam-loading.mam-visible { opacity: 1; }

    #mam-loading .mam-loader { position: relative; width: 92px; height: 92px; }
    #mam-loading .mam-ring {
    position: absolute;
    inset: 0;
    border-radius: 999px;
    border: 6px solid rgba(255, 255, 255, 0.18);
    border-top-color: rgba(255, 255, 255, 0.92);
    animation: mamSpin 0.85s linear infinite;
    }
    #mam-loading .mam-logo {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 34px;
    height: 34px;
    transform: translate(-50%, -50%);
    border-radius: 10px;
    box-shadow: 0 10px 26px rgba(0,0,0,0.35);
    }
    #mam-loading .mam-text {
    margin-top: 14px;
    text-align: center;
    font-weight: 950;
    font-size: 14px;
    color: rgba(255, 255, 255, 0.92);
    letter-spacing: -0.2px;
    }

    @keyframes mamSpin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
    }

    @media (max-width: 640px) {
    #mam-loading .mam-loader { width: 104px; height: 104px; }
    #mam-loading .mam-logo { width: 38px; height: 38px; }
    #mam-loading .mam-text { font-size: 15px; }
    }

    /* -----------------------------
    Map page: eliminate any global padding/margins + prevent "white strip"
    ----------------------------- */
    html, body { margin: 0 !important; padding: 0 !important; }
    body.mam-map, body.mam-public-padtop {
    margin: 0 !important;
    padding-top: 0 !important;   /* critical: no global header padding on map pages */
    background: #0b0f14 !important;  /* prevents white gap if anything ever shows */
    }

    /* -----------------------------
    Leaflet control positioning (FINAL OVERRIDE)
    Put last so it always wins.
    ----------------------------- */
    /* Owner map (signed-in navbar) — tuned UP */
    body.mam-map .leaflet-top { top: 125px !important; }
    @media (max-width: 640px) { body.mam-map .leaflet-top { top: 150px !important; } }


    /* Public/shared map (fixed public header) — tuned UP */
    body.mam-public-padtop .leaflet-top { top: 115px !important; }
    @media (max-width: 640px) { body.mam-public-padtop .leaflet-top { top: 130px !important; } }
    </style>
    """))


    m.get_root().html.add_child(folium.Element(r"""
    <div id="mam-loading" aria-label="Loading">
    <div style="display:flex; flex-direction:column; align-items:center;">
        <div class="mam-loader">
        <div class="mam-ring"></div>
        <img class="mam-logo" src="/static/favicon.png" alt="MyAirportMap">
        </div>
        <div class="mam-text">Loading…</div>
    </div>
    </div>

    <script>
    (function () {
    const el = document.getElementById("mam-loading");
    if (!el) return;

    function show(msg) {
        try {
        const t = el.querySelector(".mam-text");
        if (t && msg) t.textContent = msg;
        } catch (_) {}
        el.style.display = "flex";
        requestAnimationFrame(function () { el.classList.add("mam-visible"); });
    }

    function hide() {
        el.classList.remove("mam-visible");
        setTimeout(function () { el.style.display = "none"; }, 160);
    }

    window.mamShowLoading = show;
    window.mamHideLoading = hide;

    let pageTimer = setTimeout(function () {
        try { show("Loading map…"); } catch (_) {}
    }, 150);

    window.addEventListener("load", function () {
        try { if (pageTimer) { clearTimeout(pageTimer); pageTimer = null; } } catch (_) {}
        setTimeout(function () { try { hide(); } catch (_) {} }, 80);
    });
    })();
    </script>
    """))

    folium.TileLayer("OpenStreetMap", name="Street Map").add_to(m)
    folium.TileLayer("CartoDB positron", name="Plain Map").add_to(m)
    Fullscreen(position="topleft").add_to(m)

    # --- Popup builder (Map36/Map6 style) ---
    def create_popup_html(
        airport_id: str,
        name: str,
        state: str,
        status: str,
        visit_date: str,
        callsign: str,
        notes: str,
        is_first_visit: bool = False,
    ) -> str:
        status = (status or "").strip() or "Non-Towered"
        header_color = "#0044cc" if status == "Towered" else "#cc00cc"
        title = "First Visit" if is_first_visit else "Visit Details"

        safe_airport_id = _html.escape(airport_id or "")
        safe_name = _html.escape(name or "")
        safe_state = _html.escape(state or "")
        safe_status = _html.escape(status or "")
        safe_visit_date = _html.escape(visit_date or "")
        safe_callsign = _html.escape(callsign or "")
        safe_notes = linkify_text(notes or "").replace("\n", "<br>")

        out = f"""
        <div style="font-family:sans-serif; min-width:180px;">
          <div style="background:{header_color}; color:white; padding:8px; font-weight:bold;">
            {safe_airport_id} <span style="font-weight:normal;">({safe_state})</span>
          </div>
          <div style="padding:10px; color:#333;">
            <div style="font-weight:bold;">{safe_name}</div>
            <div style="font-size:11px; color:#666;">{safe_status}</div>
            <hr style="margin:8px 0; border-top:1px solid #eee;">
            <div style="font-size:10px; color:#888;">{title}</div>
            <div style="display:flex; justify-content:space-between;"><span>Date:</span><b>{safe_visit_date}</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Aircraft:</span><b>{safe_callsign}</b></div>
        """
        if safe_notes and safe_notes.lower() not in ["nan", "none"]:
            out += f"""
            <div style="margin-top:8px; background:#f9f9f9; padding:5px; font-style:italic;">
              {safe_notes}
            </div>
            """
        out += "</div></div>"
        return out
    # -----------------------------
    # Visits (clusters + flags) — Map37-stable baseline for Map38
    #   - All Visits is a Folium MarkerCluster (reliable plugin injection)
    #   - First Visits is a FeatureGroup
    #   - pv=1: JS hydrates both layers; pv=0: Python can still populate if desired
    # -----------------------------
    fg_first  = folium.FeatureGroup(name="First Visit per Airport", show=True)
    fg_visits = MarkerCluster(name="All Visits", show=False)

    fg_first.add_to(m)
    fg_visits.add_to(m)


    if not df_visits.empty:
        # Join visits -> airport metadata/coords
        merge_cols = ["norm_id", "airport_id", "name", "state", "lat", "long", "towered_status"]
        if "towered_status" not in df_airports.columns:
            df_airports["towered_status"] = "Non-Towered"

        df_vis_plot = pd.merge(
            df_visits,
            df_airports[merge_cols],
            on="norm_id",
            how="left",
            suffixes=("", "_apt"),
        )
        df_vis_plot = df_vis_plot.dropna(subset=["lat", "long"]).copy()

        # ✅ Repair towered_status if merge didn't carry it (or carried mostly blanks)
        if "towered_status" not in df_vis_plot.columns:
            df_vis_plot["towered_status"] = None

        try:
            missing_ratio = float(df_vis_plot["towered_status"].isna().mean())
        except Exception:
            missing_ratio = 1.0

        if missing_ratio > 0.10:
            tower_map = dict(
                zip(
                    df_airports["norm_id"].astype(str),
                    df_airports["towered_status"].astype(str),
                )
            )
            df_vis_plot["towered_status"] = df_vis_plot["norm_id"].astype(str).map(tower_map)

        # ✅ Canonicalize so colors never drift
        df_vis_plot["towered_status"] = (
            df_vis_plot["towered_status"]
            .fillna("Non-Towered")
            .map(_canon_towered_status)
        )

        # ✅ Gate heavy All Visits population (but keep checkbox visible)
        try:
            all_flag = (request.args.get("all") or "").strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            all_flag = False

        # ✅ Your chosen threshold
        ALL_VISITS_THRESHOLD = 5000
        
        if not progressive_visits:
            if all_flag or len(df_vis_plot) <= ALL_VISITS_THRESHOLD:
                # All visits markers
                for _, vr in df_vis_plot.iterrows():
                    status = str(vr.get("towered_status") or "Non-Towered")
                    col = "blue" if status == "Towered" else "purple"

                    disp_id = str(vr.get("airport_id", "") or "")
                    pop = create_popup_html(
                        airport_id=disp_id,
                        name=str(vr.get("name", "") or ""),
                        state=str(vr.get("state", "") or ""),
                        status=status,
                        visit_date=str(vr.get("date_visited", "") or ""),
                        callsign=str(vr.get("callsign", "") or ""),
                        notes=str(vr.get("notes", "") or ""),
                        is_first_visit=False,
                    )
                    folium.Marker(
                        location=[float(vr["lat"]), float(vr["long"])],
                        popup=folium.Popup(pop, max_width=250),
                        icon=folium.Icon(color=col, icon="flag"),
                        tooltip=f"Visit: {disp_id}",
                    ).add_to(fg_visits)

            # First visit per airport (always)
            df_first_df = df_vis_plot.sort_values("date_visited").drop_duplicates("norm_id", keep="first")
            for _, vr in df_first_df.iterrows():
                status = str(vr.get("towered_status") or "Non-Towered")
                col = "blue" if status == "Towered" else "purple"

                disp_id = str(vr.get("airport_id", "") or "")
                pop = create_popup_html(
                    airport_id=disp_id,
                    name=str(vr.get("name", "") or ""),
                    state=str(vr.get("state", "") or ""),
                    status=status,
                    visit_date=str(vr.get("date_visited", "") or ""),
                    callsign=str(vr.get("callsign", "") or ""),
                    notes=str(vr.get("notes", "") or ""),
                    is_first_visit=True,
                )
                folium.Marker(
                    location=[float(vr["lat"]), float(vr["long"])],
                    popup=folium.Popup(pop, max_width=250),
                    icon=folium.Icon(color=col, icon="flag"),
                    tooltip=f"First Visit: {disp_id}",
                ).add_to(fg_first)

    # -----------------------------
    # Airports not visited (overlay placeholder; JS fills it lazily)
    # -----------------------------
    fg_unvisited = folium.FeatureGroup(name="Airports not visited", show=False)
    fg_unvisited.add_to(m)

    # ✅ ONE LayerControl ONLY (top-right)
    folium.LayerControl(position="topright", collapsed=False).add_to(m)

    _t("after markers+layers")

    # --- Legend (Map40: mobile-friendly auto-hide) ---
    safe_handle = _html.escape(handle or "")
    who = safe_handle if safe_handle else "This pilot"

    legend = f"""
    <div id="mam-legend"
        style="position:fixed; bottom:8px; right:8px; width:200px;
                background:rgba(255,255,255,0.96);
                z-index:9999; padding:8px 10px;
                border-radius:10px;
                font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;
                box-shadow:0 0 15px rgba(0,0,0,0.2);
                transition: opacity 180ms ease-out;">

    <!-- Close button -->
    <button type="button"
            aria-label="Hide legend"
            data-legend-close
            style="position:absolute; top:6px; right:6px;
                    width:22px; height:22px;
                    border:none; border-radius:8px;
                    background:rgba(0,0,0,0.06);
                    font-size:16px; line-height:22px;
                    cursor:pointer;">
        ×
    </button>

    <div style="font-size:12px; color:#111; line-height:1.2; margin-bottom:6px; padding-right:22px;">
        <b>{who}</b> has logged flights to the pinned airports.
    </div>

    <div style="font-size:12px; margin-bottom:3px;">
        <span style="color:#1f77ff; font-weight:900;">⚑</span> Towered visit
    </div>
    <div style="font-size:12px;">
        <span style="color:#7a3db8; font-weight:900;">⚑</span> Non-towered visit
    </div>

    <div style="font-size:11px; color:#333; margin-top:4px;">
        Tip: Use the layer selector to toggle <b>First</b> vs <b>All</b>.
    </div>
    </div>

    <script>
    (function () {{
    var el = document.getElementById("mam-legend");
    if (!el) return;

    function hideLegend(persist) {{
        el.style.opacity = "0";
        el.style.pointerEvents = "none";
        setTimeout(function () {{ el.style.display = "none"; }}, 190);
        if (persist) {{
        try {{ localStorage.setItem("mamLegendHidden", "1"); }} catch (_) {{}}
        }}
    }}

    // Manual close (persist)
    try {{
        var btn = el.querySelector("[data-legend-close]");
        if (btn) btn.addEventListener("click", function (e) {{
        e.preventDefault(); e.stopPropagation();
        hideLegend(true);
        }});
    }} catch (_) {{}}

    // Respect prior dismissal
    try {{
        if (localStorage.getItem("mamLegendHidden") === "1") {{
        el.style.display = "none";
        return;
        }}
    }} catch (_) {{}}

    // Auto-hide on small phones only (once per session)
    var isSmall = false;
    try {{
        isSmall = window.matchMedia("(max-width: 420px)").matches;
    }} catch (_) {{}}

    if (!isSmall) return;

    try {{
        if (sessionStorage.getItem("mamLegendAutoHide") === "1") return;
        sessionStorage.setItem("mamLegendAutoHide", "1");
    }} catch (_) {{}}

    setTimeout(function () {{ hideLegend(false); }}, 3500);
    }})();
    </script>
    """

    map_html = m.get_root().render()

    _t(f"after folium render bytes={len(map_html)}")

    navbar = (
        get_public_navbar(handle or "", "map")
        if (navbar_mode == "public" and handle)
        else get_navbar("map", handle=handle)
    )

    # -----------------------------
    # JS:
    #  (A) All Visits gating: if marker cluster is empty (due to gating), prompt + reload ?all=1
    #  (B) Unvisited lazy overlay: fetch/clear driven by LayerControl checkbox
    # -----------------------------
    ALL_VISITS_THRESHOLD = 5000
    try:
        all_flag = (request.args.get("all") or "").strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        all_flag = False
    all_visits_loaded = "1" if (all_flag or (not df_visits.empty and len(df_visits) <= ALL_VISITS_THRESHOLD)) else "0"

    # -----------------------------
    # JS: Progressive visits loader (pv=1 only)
    #  - shows spinner until first-visits pins appear
    #  - tries geolocation; if denied/timeout => uses current viewport
    #  - loads First Visits by viewport (auto)
    #  - loads All Visits only when checkbox is selected
    # -----------------------------
    visits_js = r"""
    <script>
    (function () {

    var MAP_NAME   = "__MAP__";
    var FIRST_NAME = "__FIRST_GRP__";
    var ALL_NAME   = "__ALL_GRP__";
    var HANDLE     = "__HANDLE__";

    var map = null;
    var firstGroup = null;
    var allGroup = null;

    // Persistent state to track last loaded view and prevent double-fetching
    var state = {
        first: { lastKey: "", loading: false },
        all:   { lastKey: "", loading: false }
    };

    function log() {
        try { console.log.apply(console, arguments); } catch (_) {}
    }

    function resolveMap() {
        try {
            var m = window[MAP_NAME];
            if (m && typeof m.getBounds === "function" && typeof m.on === "function") return m;
        } catch (_) {}
        return null;
    }

    function resolveGroup(name) {
        try {
            var g = window[name];
            if (g && (typeof g.addLayer === "function" || typeof g.addLayers === "function")) return g;
        } catch (_) {}
        return null;
    }

    function isLayerOn(layer) {
        try { return map && map.hasLayer && map.hasLayer(layer); } catch (_) {}
        return false;
    }

    function quant(n) { return (Math.round(n * 20) / 20).toFixed(2); } // ~0.05° buckets
    
    function bboxFromBounds(b) {
        return [
            quant(b.getWest()),
            quant(b.getSouth()),
            quant(b.getEast()),
            quant(b.getNorth())
        ].join(",");
    }

    function viewportKey(mode) {
        if (!map) return "";
        return mode + "|" + bboxFromBounds(map.getBounds());
    }

    function urlFor(mode, bbox, limit) {
        var u = "/api/visits?mode=" + mode +
                "&bbox=" + encodeURIComponent(bbox) +
                "&limit=" + (limit || 400);
        if (HANDLE) u += "&handle=" + encodeURIComponent(HANDLE);
        return u;
    }

    function makeVisitIcon(towered) {
    var colorClass = towered
        ? "awesome-marker-icon-blue"
        : "awesome-marker-icon-purple";

    // ✅ NO manual margins. Let Leaflet handle anchoring via iconAnchor.
    var html =
        "<div class='awesome-marker " + colorClass + "' style='width:35px; height:45px;'>" +
        "<i class='fa-rotate-0 glyphicon glyphicon-flag icon-white'></i>" +
        "</div>";

    return L.divIcon({
        className: "",          // keep: prevents Leaflet default wrapper styles
        html: html,
        iconSize: [35, 45],
        iconAnchor: [17, 45],   // bottom-center of pin
        popupAnchor: [0, -38]
    });
    }

    function clearLayer(layer) {
    try {
        if (!layer) return;

        // MarkerClusterGroup + FeatureGroup both usually have this
        if (typeof layer.clearLayers === "function") { layer.clearLayers(); return; }

        // Fallback: brute remove
        if (typeof layer.getLayers === "function" && typeof layer.removeLayer === "function") {
        var ls = layer.getLayers() || [];
        for (var i = 0; i < ls.length; i++) layer.removeLayer(ls[i]);
        return;
        }

        // Fallback: eachLayer path
        if (typeof layer.eachLayer === "function" && typeof layer.removeLayer === "function") {
        var kill = [];
        layer.eachLayer(function (l) { kill.push(l); });
        for (var j = 0; j < kill.length; j++) layer.removeLayer(kill[j]);
        }
    } catch (_) {}
    }


    function addMany(layer, markers) {
    try {
        if (!layer || !markers) return;
        if (!Array.isArray(markers) || markers.length === 0) return;

        // MarkerClusterGroup path
        if (typeof layer.addLayers === "function") { layer.addLayers(markers); return; }

        // FeatureGroup path
        if (typeof layer.addLayer === "function") {
        for (var k = 0; k < markers.length; k++) layer.addLayer(markers[k]);
        }
    } catch (_) {}
    }

    function computePopupPads() {
    // Treat UI overlays as "reserved space" so popups never open under them.
    var out = { top: 12, right: 12, bottom: 12 };

    try {
        if (!map || !map.getContainer) return out;

        var mapRect = map.getContainer().getBoundingClientRect();
        var margin = 10;

        // --- LayerControl (top-right) ---
        var ctrl = document.querySelector(".leaflet-control-layers");
        if (ctrl) {
            var c = ctrl.getBoundingClientRect();
            var top = (c.bottom - mapRect.top) + margin;
            var right = (mapRect.right - c.left) + margin;

            if (isFinite(top) && top > out.top) out.top = Math.min(top, 280);
            if (isFinite(right) && right > out.right) out.right = Math.min(right, 340);
        }

        // --- Fixed legend (bottom-right) ---
        // Your legend is a fixed <div style="position:fixed; bottom:8px; right:8px; width:200px; ...">
        // We find it by looking for a fixed-position div near bottom-right with that width.
        var candidates = document.querySelectorAll("div[style*='position:fixed'][style*='bottom'][style*='right']");
        for (var i = 0; i < candidates.length; i++) {
            var el = candidates[i];
            var r = el.getBoundingClientRect();

            // Heuristic: legend sits in bottom-right and is ~200px wide
            var nearRight = (mapRect.right - r.right) <= 40;
            var nearBottom = (mapRect.bottom - r.bottom) <= 40;
            var wideEnough = r.width >= 160 && r.width <= 260;

            if (nearRight && nearBottom && wideEnough) {
                var bottom = (mapRect.bottom - r.top) + margin;
                var right2 = (mapRect.right - r.left) + margin;

                if (isFinite(bottom) && bottom > out.bottom) out.bottom = Math.min(bottom, 240);
                if (isFinite(right2) && right2 > out.right) out.right = Math.min(right2, 340);
                break;
            }
        }

    } catch (_) {}

    return out;
    }



    function loadVisits(mode) {
    var config = state[mode];
    if (!config) return;

    // Don’t fire queued refreshes after layer toggles
    if (mode === "all" && !isLayerOn(allGroup)) return;
    if (mode === "first" && !isLayerOn(firstGroup)) return;

    var key = viewportKey(mode);
    if (config.lastKey === key || config.loading) return;

    config.lastKey = key;
    config.loading = true;

    var bbox = bboxFromBounds(map.getBounds());
    var url = urlFor(mode, bbox, mode === "all" ? 650 : 450);
    log("[pv] fetch", url);

    fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
        var layer = (mode === "first") ? firstGroup : allGroup;

        clearLayer(layer);

        // -----------------------------
        // ✅ Popup close "X" reliability
        // Some mobile/zoom/pan states can swallow the close click.
        // Add a ONE-TIME capture handler to force close.
        // -----------------------------
        try {
          if (!window.__mam_popup_close_fix) {
            window.__mam_popup_close_fix = true;

            document.addEventListener("click", function (e) {
              try {
                var btn = e && e.target && e.target.closest
                  ? e.target.closest(".leaflet-popup-close-button")
                  : null;
                if (!btn) return;

                e.preventDefault();
                e.stopPropagation();

                // Close any open popup on this map
                try { if (typeof map !== "undefined" && map) map.closePopup(); } catch (_) {}
              } catch (_) {}
            }, true); // capture phase
          }
        } catch (_) {}

        var items = data.items || [];

        // ✅ one-time sample log per fetch (NOT per marker)
        if (items.length) console.log("[pv] sample", mode, items[0]);

        var pads = items.length
            ? computePopupPads()
            : { top: 12, right: 12, bottom: 12 };

        var markers = [];
        for (var i = 0; i < items.length; i++) {
            var it = items[i];

            var lat = parseFloat(it.lat);
            var lon = parseFloat(it.lon);
            if (!isFinite(lat) || !isFinite(lon)) continue;
            if (Math.abs(lat) > 90 || Math.abs(lon) > 180) continue;

            var mk = L.marker([lat, lon], { icon: makeVisitIcon(!!it.towered) });

            if (it.popup_html) {
            mk.bindPopup(it.popup_html, {
                autoPan: true,
                autoPanPaddingTopLeft: L.point(12, pads.top),
                autoPanPaddingBottomRight: L.point(pads.right, pads.bottom),
                keepInView: true
            });

            // ✅ prevent popup auto-pan from triggering a refresh fetch
            // ✅ allow real hyperlinks inside popup content
            mk.on("popupopen", function (e) {
                suppressVisitsFor(400);

                try {
                if (!e || !e.popup || !e.popup.getElement) return;
                var node = e.popup.getElement();
                if (!node) return;

                // Prevent Leaflet/map from swallowing popup clicks
                if (window.L && L.DomEvent) {
                    L.DomEvent.disableClickPropagation(node);
                    L.DomEvent.disableScrollPropagation(node);
                }

                // Allow anchor clicks to navigate normally
                var links = node.querySelectorAll("a");
                for (var k = 0; k < links.length; k++) {
                    links[k].addEventListener(
                    "click",
                    function (ev) {
                        try { ev.stopPropagation(); } catch (_) {}
                        // intentionally NOT preventDefault()
                    },
                    true
                    );
                }
                } catch (_) {}
            });
            }

            markers.push(mk);
        }

        // ✅ Add once per fetch (not once per marker)
        addMany(layer, markers);
      })
      .catch(function (err) { log("[pv] error", err); })
      .finally(function () { config.loading = false; });
    } // ✅ closes function loadVisits(mode)




    // -----------------------------
    // Viewport refresh debounce (pv)
    // -----------------------------

    // ✅ Suppress refreshes briefly after popup auto-pan
    var suppressVisitsUntil = 0;
    function suppressVisitsFor(ms) {
        suppressVisitsUntil = Date.now() + (ms || 350);
    }

    var moveTimer = null;

    function scheduleVisitsRefresh() {
        try {
            // ✅ ignore moveend/zoomend triggered by popup auto-pan
            if (Date.now() < suppressVisitsUntil) return;

            if (moveTimer) clearTimeout(moveTimer);
        } catch (_) {}

        moveTimer = setTimeout(function () {
            moveTimer = null;
            if (isLayerOn(firstGroup)) loadVisits("first");
            if (isLayerOn(allGroup))   loadVisits("all");
        }, 180);
    }



    /* -----------------------------
        Init
    ----------------------------- */
    var tries = 0;
    var timer = setInterval(function () {
    tries += 1;

    if (!map) map = resolveMap();
    if (!firstGroup) firstGroup = resolveGroup(FIRST_NAME);
    if (!allGroup) allGroup = resolveGroup(ALL_NAME);

    if (map && typeof map.on !== "function") map = null;

    if (map && firstGroup && allGroup) {
        clearInterval(timer);

    // ✅ Ensure First Visits is ON at boot (layer + checkbox + Leaflet wiring)
    try { map.addLayer(firstGroup); } catch (_) {}

    try {
    var root = document.querySelector(".leaflet-control-layers-overlays");
    if (root) {
        var labels = root.querySelectorAll("label");
        for (var i = 0; i < labels.length; i++) {
        var txt = (labels[i].textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (txt.indexOf("first visit") !== -1) {
            var cb = labels[i].querySelector("input[type=checkbox]");
            if (cb && !cb.checked) {
            cb.checked = true;
            cb.dispatchEvent(new Event("change", { bubbles: true }));  // ✅ this is the key
            }
            break;
        }
        }
    }
    } catch (_) {}

    // ✅ Let Leaflet settle one tick before first fetch
    setTimeout(function () { loadVisits("first"); }, 0);


        map.on("overlayadd", function (e) {
        if (e.layer === firstGroup) loadVisits("first");
        if (e.layer === allGroup)   loadVisits("all");
        });

        map.on("overlayremove", function (e) {
        if (e.layer === firstGroup) clearLayer(firstGroup);
        if (e.layer === allGroup)   clearLayer(allGroup);
        state.first.lastKey = "";
        state.all.lastKey = "";

        // ✅ cancel any queued refresh
        if (moveTimer) { try { clearTimeout(moveTimer); } catch (_) {} moveTimer = null; }
        });

        map.on("moveend", scheduleVisitsRefresh);
        map.on("zoomend", scheduleVisitsRefresh);

        return;
    }

    if (tries > 140) {
        clearInterval(timer);
        log("[pv] bind timeout", "map?", !!map, "first?", !!firstGroup, "all?", !!allGroup);
    }
    }, 100);


    })();
    </script>

    """

    # --- JS: Lazy airport dots layer (tied to Leaflet LayerControl checkbox) ---
    airport_js = r"""
    <script>
    (function () {
    var MAP_NAME = "__MAP__";                  // string name, NOT a direct object reference
    var UNVISITED_NAME = "__UNVISITED_GRP__";  // string name, NOT a direct object reference

    try { console.log("[unvisited] JS loaded"); } catch (_) {}

    var map = null;
    var unvisitedGroup = null;
    var inFlight = false;

    // ✅ trailing-fetch state (final viewport always wins)
    var desiredKey = "";
    var desiredBbox = "";

    // Debounce + viewport suppression
    var lastKey = "";
    var unvisitedTimer = null;
    try { console.log("[unvisited] module init ok"); } catch (_) {}


    function resolveLeafletMap() {
        try {
        var m = window[MAP_NAME];
        if (m && typeof m.getBounds === "function" && typeof m.on === "function") {
            return m;
        }
        } catch (e) {}
        return null;
    }

    function resolveUnvisitedGroup() {
        try {
            var g = window[UNVISITED_NAME];
            // Must be a layer group we can add/remove child layers from
            if (g && (typeof g.addLayer === "function" || typeof g.addLayers === "function") && typeof g.addTo === "function") {
            return g;
            }
        } catch (_) {}
        return null;
    }

    function clearGroup() {
        try {
            if (!unvisitedGroup) return;

            // FeatureGroup / LayerGroup / MarkerClusterGroup
            if (typeof unvisitedGroup.clearLayers === "function") { unvisitedGroup.clearLayers(); return; }

            // Fallback: remove children manually
            if (typeof unvisitedGroup.getLayers === "function" && typeof unvisitedGroup.removeLayer === "function") {
            var ls = unvisitedGroup.getLayers() || [];
            for (var i = 0; i < ls.length; i++) {
                try { unvisitedGroup.removeLayer(ls[i]); } catch (_) {}
            }
            }
        } catch (_) {}
    }


    function quant(n) {
        return (Math.round(n * 100) / 100).toFixed(2);
    }

    function safeMeta(meta, key, fallback) {
        try {
            if (!meta) return fallback;
            var v = meta[key];
            return (v === undefined || v === null) ? fallback : v;
        } catch (_) { return fallback; }
    }


    function getBbox() {
        // Quantize so bbox matches viewportKey suppression
        var b = map.getBounds();
        return [
        quant(b.getWest()),
        quant(b.getSouth()),
        quant(b.getEast()),
        quant(b.getNorth())
        ].join(",");
    }

    function viewportKey() {
        if (!map) return "";
        var z = (typeof map.getZoom === "function") ? map.getZoom() : 0;
        return String(z) + "|" + getBbox();
    }

    function urlFor(bbox) {
        var h = "__HANDLE__";
        var z = (typeof map.getZoom === "function") ? map.getZoom() : 0;
        var base =
            "/api/airports?bbox=" + encodeURIComponent(bbox) +
            "&unvisited=1" +
            "&zoom=" + encodeURIComponent(String(z)) +
            "&limit=2500";

        // ✅ only append handle if it was actually injected
        if (h && h.indexOf("__HANDLE__") === -1) base += "&handle=" + encodeURIComponent(h);
        return base;
    }

    function computePopupPadsUnvisited() {
    // Mirror the visited-layer logic: reserve space for LayerControl + fixed legend.
    var out = { top: 12, right: 12, bottom: 12 };

    try {
        if (!map || !map.getContainer) return out;

        var mapRect = map.getContainer().getBoundingClientRect();
        var margin = 10;

        // --- LayerControl (top-right) ---
        var ctrl = document.querySelector(".leaflet-control-layers");
        if (ctrl) {
        var c = ctrl.getBoundingClientRect();
        var top = (c.bottom - mapRect.top) + margin;
        var right = (mapRect.right - c.left) + margin;

        if (isFinite(top) && top > out.top) out.top = Math.min(top, 280);
        if (isFinite(right) && right > out.right) out.right = Math.min(right, 340);
        }

        // --- Fixed legend (bottom-right) ---
        var candidates = document.querySelectorAll("div[style*='position:fixed'][style*='bottom'][style*='right']");
        for (var i = 0; i < candidates.length; i++) {
        var el = candidates[i];
        var r = el.getBoundingClientRect();

        var nearRight = (mapRect.right - r.right) <= 40;
        var nearBottom = (mapRect.bottom - r.bottom) <= 40;
        var wideEnough = r.width >= 160 && r.width <= 260;

        if (nearRight && nearBottom && wideEnough) {
            var bottom = (mapRect.bottom - r.top) + margin;
            var right2 = (mapRect.right - r.left) + margin;

            if (isFinite(bottom) && bottom > out.bottom) out.bottom = Math.min(bottom, 240);
            if (isFinite(right2) && right2 > out.right) out.right = Math.min(right2, 340);
            break;
        }
        }
    } catch (_) {}

    return out;
    }

    function fetchAndRender() {
        if (!map || !unvisitedGroup) return;
        if (inFlight) return;
        inFlight = true;

        var bbox = getBbox();
        var url = urlFor(bbox);

        try { console.log("[unvisited] url:", url); } catch (_) {}


        try { console.log("[unvisited] fetch:", url); } catch (_) {}
        try { console.log("[unvisited] fetchAndRender ENTER", "map?", !!map, "group?", !!unvisitedGroup); } catch (_) {}


        // ✅ Delay loader slightly to avoid blink on fast responses
        var loaderTimer = null;
        if (window.mamShowLoading) {
        loaderTimer = setTimeout(function () {
            try { window.mamShowLoading("Loading airports…"); } catch (_) {}
        }, 150);
        }

        fetch(url)
        .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        })
        .then(function (data) {
            clearGroup();

            var n = (data && data.features && data.features.length)
            ? data.features.length
            : 0;

            try { console.log("[unvisited] features:", n, "meta:", data && data.meta); } catch (_) {}

        // ✅ One-line LOD telemetry (only when zoomed out)
        try {
            var meta = data && data.meta;
            var zlod = (map && typeof map.getZoom === "function") ? map.getZoom() : null;

            if (meta && zlod !== null && zlod <= 6) {
                console.log(
                "[unvisited][LOD]",
                "zoom=" + zlod,
                "total=" + safeMeta(meta, "total_in_bbox", "?"),
                "returned=" + safeMeta(meta, "returned", n),
                "after=" + safeMeta(meta, "total_after_sampling", "?"),
                "sampled=" + safeMeta(meta, "sampled", false),
                "capped=" + safeMeta(meta, "capped", false),
                "full=" + safeMeta(meta, "full_mode", false)
                );
            }
        } catch (_) {}


            // ✅ Popup close "X" reliability (one-time, capture phase)
            // Some devices/zoom states swallow the close click; force-close if the X is tapped.
            try {
              if (!window.__mam_popup_close_fix) {
                window.__mam_popup_close_fix = true;
                document.addEventListener("click", function (e) {
                  try {
                    var btn = e && e.target && e.target.closest
                      ? e.target.closest(".leaflet-popup-close-button")
                      : null;
                    if (!btn) return;
                    e.preventDefault();
                    e.stopPropagation();
                    try { if (map && typeof map.closePopup === "function") map.closePopup(); } catch (_) {}
                  } catch (_) {}
                }, true);
              }
            } catch (_) {}

            // GeoJSON layer: pointToLayer + onEachFeature are siblings (correct)
            var gj = L.geoJSON(data, {
              renderer: L.canvas({ padding: 0.5 }),

              pointToLayer: function (feature, latlng) {
                var p = feature && feature.properties ? feature.properties : {};
                var isTowered = !!p.towered;

                // visible dot
                var dot = L.circleMarker(latlng, {
                  radius: 3,
                  weight: 0,
                  opacity: 1.0,
                  fillOpacity: isTowered ? 0.50 : 0.35,
                  fillColor: isTowered ? "#005589" : "#E20074"
                });

                // ✅ invisible hit-halo (bigger tap target, no visual change)
                // Make it larger when zoomed out (dense dots) to improve click precision.
                var z = (map && typeof map.getZoom === "function") ? map.getZoom() : 0;
                var hitR = (z <= 6) ? 18 : (z <= 8) ? 14 : 10;

                var halo = L.circleMarker(latlng, {
                  radius: hitR,
                  weight: 0,
                  opacity: 0.0,
                  fillOpacity: 0.0,
                  interactive: true
                });

                // return one layer; bind on the group for consistency
                return L.featureGroup([halo, dot], { interactive: true });
              },

            onEachFeature: function (feature, layer) {
            var p = feature && feature.properties ? feature.properties : {};
            var ident = p.airport_id || "";
            var name = p.name || "Airport";
            var label = ident ? (ident + " — " + name) : name;

            // ✅ match visited popup behavior: reserve space for LayerControl + legend
            var pads = { top: 12, right: 12, bottom: 12 };
            try { pads = computePopupPadsUnvisited(); } catch (_) {}

            layer.bindPopup(
                "<div style='font-weight:800;'>" + label + "</div>",
                {
                maxWidth: 220,
                closeButton: true,
                autoPan: true,
                keepInView: true,
                autoPanPaddingTopLeft: L.point(12, pads.top),
                autoPanPaddingBottomRight: L.point(pads.right, pads.bottom)
                }
            );

            // Tooltip only when zoomed in (optional)
            var z = (map && typeof map.getZoom === "function") ? map.getZoom() : 0;
            if (z >= 9) {
                layer.bindTooltip(
                "<strong>" + label + "</strong>",
                { direction: "top", sticky: true, opacity: 0.95 }
                );
            }
            }
            }); // ✅ closes L.geoJSON(data, {...})

            // ✅ Robust add: avoid adding a nested GeoJSON group as a single child.
            // Add each child layer instead (works for FeatureGroup and cluster-ish groups).
            try {
            if (unvisitedGroup && typeof unvisitedGroup.addLayer === "function" && typeof gj.eachLayer === "function") {
                gj.eachLayer(function (l) {
                try { unvisitedGroup.addLayer(l); } catch (_) {}
                });
            } else {
                gj.addTo(unvisitedGroup);
            }
            } catch (_) {
            try { gj.addTo(unvisitedGroup); } catch (_) {}
            }

        })
        .catch(function (e) {
            try { console.warn("[unvisited] fetch failed:", e); } catch (_) {}
        })
        .finally(function () {
            inFlight = false;

            // ✅ Prevent delayed loader from firing after we're done
            if (loaderTimer) {
            try { clearTimeout(loaderTimer); } catch (_) {}
            loaderTimer = null;
            }

            // ✅ Hide loader when done (success or failure)
            if (window.mamHideLoading) {
            try { window.mamHideLoading(); } catch (_) {}
            }
        });
    }

    function scheduleFetch() {
        if (!map) return;

        if (unvisitedTimer) { try { clearTimeout(unvisitedTimer); } catch (_) {} }
        unvisitedTimer = setTimeout(function () {
            unvisitedTimer = null;

            if (!unvisitedGroup) return;

            fetchAndRender();
        }, 200);
    }

    function findUnvisitedCheckbox() {
    try {
        var root = document.querySelector(".leaflet-control-layers-overlays");
        if (!root) return null;

        var labels = root.querySelectorAll("label");
        for (var i = 0; i < labels.length; i++) {
        var txt = (labels[i].textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (txt.indexOf("airports not visited") !== -1 || txt.indexOf("unvisited") !== -1) {
            var cb = labels[i].querySelector("input[type=checkbox]");
            if (cb) return cb;
        }
        }
    } catch (_) {}
    return null;
    }


    function setOn(on) {
        if (!map || !unvisitedGroup) return;

        on = !!on;
        try { console.log("[unvisited] setOn:", on); } catch (_) {}

        if (!on) {
        clearGroup();
        lastKey = ""; // reset so next ON forces a fetch
        try { map.removeLayer(unvisitedGroup); } catch (e) {}
        return;
        }

        try { map.addLayer(unvisitedGroup); } catch (e) {}

        // Immediate fetch on enable (no waiting)
        lastKey = "";
        fetchAndRender();
    }

    var tries = 0;
    var t = setInterval(function () {
        tries += 1;

        if (!map) {
            map = resolveLeafletMap();
            if (map) {
            try { console.log("[unvisited] map resolved:", MAP_NAME); } catch (_) {}
            }
        }

        if (!unvisitedGroup) {
            unvisitedGroup = resolveUnvisitedGroup();
            if (unvisitedGroup) {
            try { console.log("[unvisited] group resolved:", UNVISITED_NAME); } catch (_) {}
            }
        }

        var cb = findUnvisitedCheckbox();

        if (map && unvisitedGroup && cb) {
            clearInterval(t);
            try { console.log("[unvisited] checkbox bound"); } catch (_) {}
            try { console.log("[unvisited] initial cb.checked =", cb.checked); } catch (_) {}

            // Ensure timer exists
            if (typeof unvisitedTimer === "undefined") { unvisitedTimer = null; }

            // ✅ Boot state (setOn(true) will add layer + immediate fetch)
            setOn(!!cb.checked);

            // ✅ Toggle
            cb.addEventListener("change", function () {
            setOn(!!cb.checked);
            });

            // ✅ Debounced refresh on navigation
            map.on("moveend", function () { if (cb.checked) scheduleFetch(); });
            map.on("zoomend", function () { if (cb.checked) scheduleFetch(); });

            return;
        }

        if (tries >= 120) {
            clearInterval(t);
            try {
            console.warn(
                "[unvisited] bind timeout",
                "map?", !!map,
                "group?", !!unvisitedGroup,
                "checkbox?", !!cb
            );
            } catch (_) {}
        }
    }, 100);
    })();
    </script>
    """

    airport_js = airport_js.replace("__MAP__", m.get_name())
    airport_js = airport_js.replace("__UNVISITED_GRP__", fg_unvisited.get_name())
    airport_js = airport_js.replace("__HANDLE__", handle or "")


    # ✅ Inject navbar + legend immediately after <body>
    # ✅ ALSO: stamp body classes reliably (works even if <body ...> has attributes)
    try:
        import re

        # Map pages always opt out of global body padding
        # Public maps ALSO need mam-public-padtop for Leaflet control offset
        required = ["mam-map"]
        if navbar_mode == "public":
            required.append("mam-public-padtop")

        def _inject_body(html: str) -> str:
            def repl(m):
                attrs = m.group(1) or ""
                # merge classes (preserve existing)
                cm = re.search(r'\bclass\s*=\s*"([^"]*)"', attrs, flags=re.I)
                if cm:
                    existing = [c for c in (cm.group(1) or "").split() if c]
                    existing = [c for c in existing if c not in ("mam-map", "mam-public-padtop")]
                    new_classes = " ".join(required + existing)
                    new_attrs = re.sub(r'\bclass\s*=\s*"[^"]*"', f'class="{new_classes}"', attrs, count=1, flags=re.I)
                else:
                    new_attrs = attrs + f' class="{" ".join(required)}"'
                return f"<body{new_attrs}>" + navbar + legend
            return re.sub(r"<body([^>]*)>", repl, html, count=1, flags=re.I)

        out_html = _inject_body(map_html)

    except Exception:
        # fallback (only works if map_html literally contains "<body>")
        body_tag = '<body class="mam-map mam-public-padtop">' if navbar_mode == "public" else '<body class="mam-map">'
        out_html = map_html.replace("<body>", body_tag + navbar + legend, 1)

    # Replace placeholders
    if progressive_visits and visits_js:
        visits_js = visits_js.replace("__MAP__", m.get_name())
        visits_js = visits_js.replace("__FIRST_GRP__", fg_first.get_name())
        visits_js = visits_js.replace("__ALL_GRP__", fg_visits.get_name())
        visits_js = visits_js.replace("__HANDLE__", handle or "")
        visits_js = visits_js.replace("__NEARBY_MILES__", str(int(nearby_miles)))

    # ✅ Append scripts at end (before </body>)
    # IMPORTANT: visits_js must come before airport_js (either is fine, but keep consistent)
    out_html = out_html.replace("</body>", (visits_js + airport_js) + "</body>", 1)



    _t(f"after inject bytes={len(out_html)}")

    try:
        _map_cache_put(cache_key, out_html)
    except Exception:
        pass
    _t("end")

    return out_html

# ============================================================
# BADGES PAGE (Map6 UI restored + Dropdown filter)
# ============================================================
import math

def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg)
    return (cx + r * math.cos(rad), cy + r * math.sin(rad))

def runway360_phase_info(pct: float) -> tuple[str, str, str]:
    # returns (phase_label, icon, icon_color)
    if pct >= 100:
        return "COMPLETED", "🏆", "#FFD700"
    if pct > 80:
        return "SHORT FINAL", "🛬", "#00FF00"
    if pct > 50:
        return "APPROACH", "↘️", "#FF00FF"
    if pct > 20:
        return "CRUISING", "✈️", "#0088FF"
    return "TAKE-OFF", "🛫", "#00FFFF"

def build_runway360_svg(
    completed: set[str],
    items: dict | None = None,
    size: int = 220,
    interactive: bool = False,
) -> str:
    """
    Runway 360 ring SVG.

    ✅ Informational by default: wedges never navigate.
    ✅ Big invisible hit targets for calm mobile taps.
    ✅ Saved details embedded in data-info/title for consistent display.
    """
    items = items if isinstance(items, dict) else {}

    cx = cy = size / 2
    r = (size / 2) - 18
    stroke_w = 14

    # --- tick marks (10° graduations) ---
    ticks: list[str] = []
    r_outer_ticks = r + (stroke_w / 2) + 2
    short_len = 7
    long_len = 12

    for n in range(0, 36):
        ang = -90 + n * 10
        x0, y0 = _polar(cx, cy, r_outer_ticks, ang)

        is_long = (n % 3 == 0)  # every 30°
        ln = long_len if is_long else short_len
        x1, y1 = _polar(cx, cy, r_outer_ticks + ln, ang)

        ticks.append(
            f'<line x1="{x0:.2f}" y1="{y0:.2f}" '
            f'x2="{x1:.2f}" y2="{y1:.2f}" '
            f'stroke="#666" stroke-width="2" stroke-linecap="round" />'
        )

    # --- runway cardinal labels (AVIATION CORRECT) ---
    # 36 = North, 09 = East, 18 = South, 27 = West
    cardinals = [
        ("36", -90),
        ("09",   0),
        ("18",  90),
        ("27", 180),
    ]

    labels: list[str] = []
    label_r = r - 26
    for txt, ang in cardinals:
        x, y = _polar(cx, cy, label_r, ang)
        labels.append(
            f'<text x="{x:.2f}" y="{y:.2f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-size="12" fill="#d0d6e2" font-weight="900">{txt}</text>'
        )

    segs: list[str] = []  # visible wedges
    hits: list[str] = []  # invisible fat hit arcs

    # Big invisible hit target for mobile taps
    hit_w = max(stroke_w * 2.8, 40)

    # Wedge geometry (outer/inner radii)
    r_outer = r + (stroke_w / 2)
    r_inner = r - (stroke_w / 2)

    for n in range(1, 37):
        label = f"{n:02d}"
        # ✅ Center each wedge on the runway bearing (5° either side)
        # Runway 01 centered at 10°, Runway 36 centered at 0°
        a0 = -90 + (n - 1) * 10 + 5
        a1 = -90 + n * 10 + 5


        is_done = label in completed

        # saved info (calm + readable)
        rec = items.get(label) if isinstance(items.get(label), dict) else {}
        date_iso = (rec.get("date") or "").strip()
        date = _fmt_mmddyyyy(date_iso) or date_iso
        airport = (rec.get("airport") or "").strip()
        aircraft = (rec.get("aircraft") or "").strip()

        parts: list[str] = []
        if date:
            parts.append(f"Date: {date}")
        if airport:
            parts.append(f"Airport: {airport}")
        if aircraft:
            parts.append(f"Notes: {aircraft}")

        info = "\n".join(parts).strip() or "No details saved yet."
        info_esc = _html.escape(info, quote=True)

        # --- visible wedge (sector) ---
        xo0, yo0 = _polar(cx, cy, r_outer, a0)
        xo1, yo1 = _polar(cx, cy, r_outer, a1)
        xi1, yi1 = _polar(cx, cy, r_inner, a1)
        xi0, yi0 = _polar(cx, cy, r_inner, a0)

        wedge_d = (
            f"M {xo0:.2f} {yo0:.2f} "
            f"A {r_outer:.2f} {r_outer:.2f} 0 0 1 {xo1:.2f} {yo1:.2f} "
            f"L {xi1:.2f} {yi1:.2f} "
            f"A {r_inner:.2f} {r_inner:.2f} 0 0 0 {xi0:.2f} {yi0:.2f} "
            f"Z"
        )

        fill = "#0088FF" if is_done else "#2a2a2a"
        outline = "rgba(255,255,255,0.10)" if is_done else "rgba(255,255,255,0.07)"
        glow = (
            ' style="filter: drop-shadow(0 0 4px rgba(0,136,255,0.55));"'
            if is_done else ""
        )

        segs.append(
            f'<path class="r360-wedge" d="{wedge_d}" fill="{fill}" '
            f'stroke="{outline}" stroke-width="1"{glow}>'
            f'<title>{info_esc}</title>'
            f'</path>'
        )

        # --- invisible fat hit arc (captures taps) ---
        # ✅ Put hit-testing at the OUTER end of the hashmarks (feels "dead-on")
        # ticks start at r_outer_ticks and extend by (short_len/long_len)
        # ✅ between short + long tick tips (consistent feel everywhere)
        r_hit = r_outer_ticks + (short_len + long_len) / 2


        # ✅ small angle pad so taps right on the boundary still register
        # ✅ scale pad slightly with target width
        pad = 0.7 if hit_w >= 44 else 0.5
        aa0 = a0 + pad
        aa1 = a1 - pad

        x0, y0 = _polar(cx, cy, r_hit, aa0)
        x1, y1 = _polar(cx, cy, r_hit, aa1)
        d = f"M {x0:.2f} {y0:.2f} A {r_hit:.2f} {r_hit:.2f} 0 0 1 {x1:.2f} {y1:.2f}"


        # ✅ NEVER wrap in <a> — only Manage button navigates
        hits.append(
            f'<path class="r360-hit" d="{d}" '
            f'data-rwy="{label}" data-info="{info_esc}">'
            f'<title>{info_esc}</title>'
            f'</path>'
        )

    # --- center readout ---
    pct = int(round((len(completed) / 36) * 100))
    phase, icon, icon_color = runway360_phase_info(float(pct))
    icon_opacity = "1.0" if pct >= 100 else "0.92"

    completed_stamp = ""
    if pct >= 100:
        completed_stamp = (
            f'<text x="{cx:.0f}" y="{cy+56:.0f}" text-anchor="middle" '
            f'font-size="12" fill="#ffd27a" font-weight="900">RUNWAY 360 CLUB</text>'
        )

    center = (
        f'<g class="r360-center">'
        f'<text x="{cx:.0f}" y="{cy-10:.0f}" text-anchor="middle" '
        f'font-size="28" fill="#fff" font-weight="800">{pct}%</text>'
        f'<text x="{cx:.0f}" y="{cy+16:.0f}" text-anchor="middle" '
        f'font-size="22" fill="{icon_color}" opacity="{icon_opacity}">{icon}</text>'
        f'<text x="{cx:.0f}" y="{cy+36:.0f}" text-anchor="middle" '
        f'font-size="12" fill="#bbb">{phase}</text>'
        f'{completed_stamp}'
        f'</g>'
    )

    svg_style = f"""
<style>
  .r360-wedge {{ pointer-events: none; }}

  .r360-hit {{
    fill: none;
    stroke: rgba(0,0,0,0);
    stroke-width: {hit_w};
    stroke-linecap: round;

    /* ✅ Make taps predictable across mobile browsers */
    pointer-events: stroke;
    cursor: pointer;
    touch-action: manipulation;

    /* iOS/Chrome mobile: reduce "tap highlight" + accidental text behaviors */
    -webkit-tap-highlight-color: transparent;
    user-select: none;
    -webkit-user-select: none;
  }}

  .r360-center {{
    pointer-events: none; /* ✅ don't steal taps */
    user-select: none;
    -webkit-user-select: none;
  }}
</style>
"""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="Runway 360 Club progress">'
        f'{svg_style}'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" '
        f'stroke="#2a2a2a" stroke-width="{stroke_w}" fill="none" />'
        f'{"".join(ticks)}'
        f'{"".join(labels)}'
        f'{"".join(segs)}'
        f'{"".join(hits)}'
        f'{center}'
        f'</svg>'
    )

def parse_runway360_form(form) -> dict:
    """
    Reads /runways/manage POSTed fields and returns:
      {"items": {"01": {"date":"YYYY-MM-DD","airport":"KXYZ","aircraft":"..."}, ...}}

    - Accepts date input as YYYY-MM-DD OR MM/DD/YYYY (or M/D/YYYY)
    - Stores date as YYYY-MM-DD when parseable; otherwise stores raw text (doesn't block save)
    """
    items: dict[str, dict] = {}

    for num in RUNWAY360_NUMBERS:
        n = str(num).zfill(2)

        date_raw = (form.get(f"rwy_{n}_date", "") or "").strip()
        airport  = (form.get(f"rwy_{n}_airport", "") or "").strip()
        aircraft = (form.get(f"rwy_{n}_aircraft", "") or "").strip()

        # Normalize date if possible; keep raw if not
        date_norm = ""
        if date_raw:
            try:
                date_norm = _normalize_date(date_raw) or ""
            except Exception:
                date_norm = ""

        date_out = date_norm if date_norm else date_raw

        if date_out or airport or aircraft:
            items[n] = {
                "date": date_out,
                "airport": airport,
                "aircraft": aircraft,
            }

    return {"items": items}

def _runway360_complete_meta_key() -> str:
    """
    Stored in users/by_id/<user_id>.json (durable user meta).
    Keeping this as a function lets us rename safely later.
    """
    return "runway360_completed_at"

def record_runway360_completion_once(
    *,
    user_id: str,
    handle: str,
    completed_count: int,
    total: int = 36,
) -> bool:
    """
    Returns True only the first time we flip to completed.

    Side-effects on first completion:
      - Stores completion timestamp in durable user meta
      - Adds handle to the Runway360 join log (powers /runway360 members page)
    """
    user_id = (user_id or "").strip()
    handle = (handle or "").strip().lower()

    if not user_id or not handle:
        return False

    try:
        total_i = int(total)
        done_i = int(completed_count)
    except Exception:
        return False

    if total_i <= 0 or done_i < total_i:
        return False

    try:
        meta = load_user_meta(user_id) or {}
        k = _runway360_complete_meta_key()

        # Already completed -> do nothing
        if meta.get(k):
            return False

        # Set-once completion stamp (UTC Z)
        patch_user_meta(
            user_id,
            {k: _now_utc().isoformat().replace("+00:00", "Z")},
        )

        # ✅ Log membership for /runway360 directory
        # (Uses durable storage key RUNWAY360_JOIN_LOG_KEY)
        try:
            runway360_join_log_add(handle)
        except Exception:
            pass

        return True

    except Exception:
        return False

def maybe_emit_runway360_milestone(*, handle: str, just_completed: bool) -> None:
    """
    Emits the Pilot's Lounge milestone exactly once per user.

    Note: emit_milestone_once() already:
      - records a per-user emitted marker even if share is OFF
      - only publishes to global feed when share_activity is ON
    """
    if not just_completed:
        return

    handle = (handle or "").strip().lower()
    if not handle:
        return

    emit_milestone_once(
        handle,
        "runway360_complete",
        "Runway 360 complete",
        meta={"total": 36},
    )

def _normalize_date(val: str) -> str:
    if not val:
        return ""

    v = val.strip()

    # Try ISO first (YYYY-MM-DD)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # If it doesn't parse cleanly, store raw (don't block save)
    return v

def generate_runway360_section(handle: str | None, navbar_mode: str) -> str:
    if not handle:
        return ""

    data = load_runway360(handle)
    items = data.get("items", {}) if isinstance(data, dict) else {}
    completed = runway360_completed_set(data)

    # ✅ Runway 360 ring is informational (never navigates). Only the Manage button navigates.
    svg = build_runway360_svg(completed, items=items, size=220, interactive=False)

    done = len(completed)
    pct = int(round((done / 36) * 100)) if 36 else 0
    is_complete = done >= 36

    # ------------------------------------------------------------
    # Runway 360: Layout helpers (header first, compass below)
    # + Gold OUTER RING only when complete (no wedge changes)
    # ------------------------------------------------------------

    gold_css = ""
    gold_class = ""
    if is_complete:
        gold_class = " r360-complete"
        gold_css = """
<style>
  /* Gold finish for 100% — OUTER RING ONLY */
  .r360-complete svg > circle {
    stroke: #ffd27a !important;
    filter: drop-shadow(0 0 6px rgba(255,210,122,0.45));
  }
</style>
"""

    header_html = f"""
      <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:flex-start;">
        <div style="min-width:220px;">
          <div style="font-size:16px; font-weight:900;">
            <a href="/runway360/club"
               style="font-weight:900; font-size:16px; color:#fff; text-decoration:none;"
               onmouseover="this.style.textDecoration='underline'"
               onmouseout="this.style.textDecoration='none'">
              Runway 360 Club →
            </a>
          </div>
          <div style="font-size:12px; color:#bbb; margin-top:4px;">
            Complete all runway numbers 01–36 (each lights a 10° arc)
          </div>

          <div style="margin-top:10px; font-size:12px; color:#bbb;">
            <span style="font-weight:900; color:#fff;">Progress</span><br>
            {done} / 36 runway numbers logged • {pct}% complete
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <a href="/runways/manage"
             style="display:inline-block; text-decoration:none; background:#1f1f1f; color:#fff;
                    padding:6px 12px; border-radius:6px; font-weight:900; border:1px solid rgba(255,255,255,0.14);">
            Manage Runways
          </a>
        </div>
      </div>
    """

    compass_html = f"""
      {gold_css}
      <div class="r360-wrap{gold_class}" style="margin-top:14px; display:flex; justify-content:center;">
        {svg}
      </div>
    """


    # ------------------------------------------------------------
    # ✅ Runway 360: set-once completion → durable stamp + roster + milestone
    # Owner-only, because public viewers shouldn’t trigger writes.
    # ------------------------------------------------------------
    if navbar_mode == "owner" and is_complete:
        try:
            just_completed = record_runway360_completion_once(
                user_id=getattr(request, "user_id", None),
                handle=handle,
                completed_count=done,
                total=36,
            )
            maybe_emit_runway360_milestone(handle=handle, just_completed=just_completed)
        except Exception:
            pass

    # Owner-only manage button
    manage_btn = ""
    if navbar_mode == "owner":
        manage_btn = """
          <div style="margin-top:12px;">
            <a href="/runways/manage"
               style="display:inline-block; text-decoration:none; background:#0088FF; color:#fff; padding:6px 12px; border-radius:6px; font-weight:800;">
              Manage Runways
            </a>
          </div>
        """

    # ------------------------------------------------------------
    # Runway 360: Layout helpers (header first, compass below)
    # ------------------------------------------------------------
    header_html = f"""
      <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:flex-start;">
        <div style="min-width:220px;">
          <div style="font-size:16px; font-weight:900;">
            <a href="/runway360/club"
               style="font-weight:900; font-size:16px; color:#fff; text-decoration:none;"
               onmouseover="this.style.textDecoration='underline'"
               onmouseout="this.style.textDecoration='none'">
              Runway 360 Club →
            </a>
          </div>
          <div style="font-size:12px; color:#bbb; margin-top:4px;">
            Complete all runway numbers 01–36 (each lights a 10° arc)
          </div>

          <div style="margin-top:10px; font-size:12px; color:#bbb;">
            <span style="font-weight:900; color:#fff;">Progress</span><br>
            {done} / 36 runway numbers logged • {pct}% complete
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <a href="/runways/manage"
             style="display:inline-block; text-decoration:none; background:#1f1f1f; color:#fff;
                    padding:6px 12px; border-radius:6px; font-weight:900; border:1px solid rgba(255,255,255,0.14);">
            Manage Runways
          </a>
        </div>
      </div>
    """

    compass_html = f"""
      <div style="margin-top:14px; display:flex; justify-content:center;">
        {svg}
      </div>
    """

    # ------------------------------------------------------------
    # Completion actions: share card download + club roster
    # (shown ONLY when complete; below compass)
    # ------------------------------------------------------------
    completion_cta = ""
    if navbar_mode == "owner" and is_complete:
        completion_cta = """
          <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
            <a href="/runways/card.png"
               style="display:inline-block; text-decoration:none; background:#ffffff; color:#0f1115; padding:6px 12px; border-radius:6px; font-weight:900; border:1px solid rgba(255,255,255,0.18);">
              Download Share Card
            </a>
            <a href="/runway360/club"
               style="display:inline-block; text-decoration:none; background:#1f1f1f; color:#fff; padding:6px 12px; border-radius:6px; font-weight:900; border:1px solid rgba(255,255,255,0.14);">
              View Club Members
            </a>
          </div>
          <div style="margin-top:8px; font-size:12px; color:#9aa3b2;">
            Tip: Share your card on social media and tag a pilot friend.
          </div>
        """

    # ------------------------------------------------------------
    # If not complete, show a subtle hint (below compass)
    # ------------------------------------------------------------
    not_done_hint = ""
    if navbar_mode == "owner" and (not is_complete):
        not_done_hint = """
          <div style="margin-top:12px; font-size:12px; color:#9aa3b2;">
            Complete all 36 runway numbers to unlock a downloadable share card.
          </div>
        """

    return f"""
<div class="badge-card" style="border:1px solid #333;">
  <!-- HEADER (NOT FLEX WITH COMPASS) -->
  <div class="badge-header" style="cursor:default;">
    <div style="display:flex; flex-direction:column; gap:2px;">
      <div>
        <a href="/runway360/club"
           style="
             font-weight:900;
             font-size:16px;
             color:#fff;
             text-decoration:none;
           "
           onmouseover="this.style.textDecoration='underline'"
           onmouseout="this.style.textDecoration='none'">
          Runway 360 Club →
        </a>
      </div>

      <div style="font-size:12px; color:#bbb;">
        Complete all runway numbers 01–36 (each lights a 10° arc)
      </div>
    </div>
  </div>

  <!-- CONTENT (SIDE-BY-SIDE LIVES HERE) -->
  <div style="padding:16px 18px; background:#222; border-top:1px solid #444;">
    <div style="display:flex; flex-wrap:wrap; gap:18px; align-items:center;">

      <!-- LEFT: COMPASS -->
      <div style="flex:0 0 auto;">
        {svg}
      </div>

      <!-- RIGHT: PROGRESS + ACTIONS -->
      <div style="flex:1 1 220px;">
        <div style="font-size:14px; font-weight:800;">Progress</div>

        <div style="margin-top:6px; color:#bbb; font-size:13px;">
          {done} / 36 runway numbers logged • {pct}% complete
        </div>

        <div style="margin-top:10px;">
          <div class="progress-bar-bg" style="max-width:340px;">
            <div class="progress-bar-fill" style="width:{pct}%; background:#0088FF;"></div>
          </div>
        </div>

        {manage_btn}
        {completion_cta}
        {not_done_hint}
      </div>

    </div>
  </div>
</div>



<style>
  /* Make SVG runway segments reliably clickable (stroke-based) */
  svg [data-rwy], svg [data-rwy-seg] {{
    cursor: pointer;
    pointer-events: stroke;
    touch-action: manipulation;
  }}

  /* Bottom-sheet info panel */
  #rwy360-sheet {{
    position: fixed;
    left: 12px;
    right: 12px;
    bottom: 12px;
    z-index: 99999;
    display: none;
    background: rgba(18, 20, 26, 0.98);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 16px;
    box-shadow: 0 18px 60px rgba(0,0,0,0.55);
    padding: 12px 12px 10px;
    backdrop-filter: blur(8px);
  }}

  #rwy360-sheet .row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
  }}

  #rwy360-sheet .title {{
    font-weight: 950;
    font-size: 15px;
    color: #fff;
    letter-spacing: -0.2px;
  }}

  #rwy360-sheet .close {{
    background: transparent;
    border: none;
    color: rgba(255,255,255,0.75);
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
  }}

  #rwy360-sheet .body {{
    color: rgba(255,255,255,0.90);
    font-size: 14px;
    line-height: 1.45;
    white-space: pre-wrap;
  }}

  @media (max-width: 640px) {{
    #rwy360-sheet {{ left: 10px; right: 10px; bottom: 10px; }}
    #rwy360-sheet .title {{ font-size: 16px; }}
    #rwy360-sheet .body  {{ font-size: 15px; }}
  }}
</style>

<div id="rwy360-sheet" role="dialog" aria-label="Runway details">
  <div class="row">
    <div class="title" id="rwy360-title">Runway</div>
    <button class="close" type="button" aria-label="Close" onclick="window.rwy360Hide()">×</button>
  </div>
  <div class="body" id="rwy360-body"></div>
</div>

<script>
(function () {{
  // Hover-capable devices (desktop/trackpad) should NOT use the bottom sheet on hover,
  // but desktop CLICK should still show details.
  var hoverCapable = false;
  try {{
    hoverCapable = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  }} catch (_) {{}}

  function findSeg(target) {{
    if (!target) return null;
    try {{
      return target.closest("[data-rwy]") || target.closest("[data-rwy-seg]");
    }} catch (e) {{
      return null;
    }}
  }}

  function segRunway(el) {{
    var r = el.getAttribute("data-rwy") || el.getAttribute("data-rwy-seg") || "";
    return String(r || "").trim();
  }}

  function segInfo(el) {{
    var msg = el.getAttribute("data-info") || el.getAttribute("data-note") || "";
    msg = String(msg || "").trim();
    if (msg) return msg;

    try {{
      var t = el.querySelector("title");
      if (t && t.textContent) return String(t.textContent).trim();
    }} catch (e) {{}}

    return "";
  }}

  function show(rwy, text) {{
    var sheet = document.getElementById("rwy360-sheet");
    var title = document.getElementById("rwy360-title");
    var body  = document.getElementById("rwy360-body");
    if (!sheet || !title || !body) return;

    title.textContent = rwy ? ("Runway " + rwy) : "Runway";
    body.textContent = text || "";
    sheet.style.display = "block";
  }}

  function hide() {{
    var sheet = document.getElementById("rwy360-sheet");
    if (sheet) sheet.style.display = "none";
  }}

  window.rwy360Hide = hide;

  // Suppress double-fire (pointerup + click)
  var lastKey = "";
  var lastTs = 0;

  function activateFromEvent(e) {{
    var el = findSeg(e.target);
    if (!el) return false;

    // Desktop hover should not activate (but click/pointerup should)
    if (hoverCapable && e && e.type === "mouseover") return false;

    // We own this interaction: prevent scroll/tap weirdness and any accidental navigation.
    try {{ e.preventDefault(); }} catch (_) {{}}
    try {{ e.stopPropagation(); }} catch (_) {{}}

    var rwy = segRunway(el);
    var info = segInfo(el) || "No details saved yet for this runway.";

    // Debounce duplicates
    var now = Date.now();
    var key = rwy + "|" + info;
    if (key === lastKey && (now - lastTs) < 450) return true;
    lastKey = key;
    lastTs = now;

    show(rwy, info);
    return true;
  }}

  // ✅ Mobile + Desktop: pointerup is calmer than pointerdown (gesture already resolved)
  document.addEventListener("pointerup", function (e) {{
    activateFromEvent(e);
  }}, {{ passive: false, capture: true }});

  // ✅ Click fallback (older browsers / edge cases)
  document.addEventListener("click", function (e) {{
    // If we already handled this via pointerup, ignore.
    var now = Date.now();
    if ((now - lastTs) < 350) return;

    if (activateFromEvent(e)) return;
  }}, {{ passive: false, capture: true }});

  // Tap/click outside to close (keep calm)
  document.addEventListener("click", function(e) {{
    var sheet = document.getElementById("rwy360-sheet");
    if (!sheet || sheet.style.display !== "block") return;
    if (sheet.contains(e.target)) return;
    if (findSeg(e.target)) return;
    hide();
  }});
}})();
</script>
""".strip()

def should_animate_runways() -> bool:
    return request.args.get("msg") == "runways_saved"

# -----------------------------
# Progress / status helpers
# -----------------------------
def progress_status(pct: int) -> tuple[str, str, str]:
    if pct >= 100:
        return "COMPLETED", "#FFD700", "🏆"
    if pct >= 80:
        return "SHORT FINAL", "#00FF00", "🛬"
    if pct >= 50:
        return "APPROACH", "#FF00FF", "↘️"
    if pct >= 20:
        return "CRUISING", "#0088FF", "✈️"
    return "TAKE-OFF", "#00FFFF", "🛫"


def _eligible_badge_airports(df_conus: pd.DataFrame) -> pd.DataFrame:
    """
    Achievements-only filter to align 'total airports' with what pilots expect:
    48-state airports, public-use, open, airport-only, de-duped.

    Defensive: only applies a filter if the column exists.
    """
    df = df_conus.copy()

    # --- de-dupe (norm_id is your canonical ID) ---
    if "norm_id" in df.columns:
        df = df.drop_duplicates(subset=["norm_id"])
    elif "airport_id" in df.columns:
        df = df.drop_duplicates(subset=["airport_id"])

    # --- state filter (keep 50 states; add DC if you want) ---
    if "state" in df.columns:
        df["state"] = df["state"].astype(str).str.upper().str.strip()
        states_48 = {
            "AL","AZ","AR","CA","CO","CT","DE","FL","GA","ID","IL","IN","IA","KS","KY","LA","ME","MD",
            "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
            "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
            # "DC",
        }
        df = df[df["state"].isin(states_48)]

    # --- open/not closed ---
    status_col = next((c for c in ["status", "facility_status", "FACILITY_STATUS"] if c in df.columns), None)
    if status_col:
        s = df[status_col].astype(str).str.upper()
        df = df[~s.str.contains("CLOSED", na=False)]

    # --- public-use only ---
    use_col = next((c for c in ["use", "facility_use", "FACILITY_USE"] if c in df.columns), None)
    if use_col:
        u = df[use_col].astype(str).str.upper()
        df = df[u.str.contains("PUBLIC", na=False)]

    # --- airport-only (exclude heliports / seaplane bases etc.) ---
    type_col = next((c for c in ["type", "facility_type", "FACILITY_TYPE"] if c in df.columns), None)
    if type_col:
        t = df[type_col].astype(str).str.upper()
        # Drop obvious non-airport facilities
        df = df[~t.str.contains("HELIPORT", na=False)]
        df = df[~t.str.contains("SEAPLANE", na=False)]
        df = df[~t.str.contains("BALLOON", na=False)]
        df = df[~t.str.contains("GLIDER", na=False)]
        df = df[~t.str.contains("ULTRALIGHT", na=False)]

    return df
 
def generate_badges_content(
    visits_csv: Optional[str] = None,
    handle: Optional[str] = None,
    navbar_mode: str = "owner",
    certifications_line: str = "",   # ✅ Map41 additive
):

    # ✅ Safety: if handle is provided but visits_csv isn't, force the canonical per-user path
    h = (handle or "").strip().lower() or None
    if h and not visits_csv:
        visits_csv = resolve_visits_csv(h)

    df_conus, df_visits = load_data(visits_csv=visits_csv, handle=h)

    # Normalize airport name column (Map6 compatibility)
    if "ARPT_NAME" not in df_conus.columns:
        if "name" in df_conus.columns:
            df_conus["ARPT_NAME"] = df_conus["name"]
        elif "airport_name" in df_conus.columns:
            df_conus["ARPT_NAME"] = df_conus["airport_name"]
        elif "facility_name" in df_conus.columns:
            df_conus["ARPT_NAME"] = df_conus["facility_name"]
        else:
            df_conus["ARPT_NAME"] = "Unknown"

    # ✅ Achievements-only: filter to "public-use airports" expectation
    df_badges = _eligible_badge_airports(df_conus)

    # -----------------------------
    # Ensure norm_id exists everywhere (airports.csv may only have airport_id)
    # -----------------------------
    def _norm(x):
        if x is None:
            return None
        s = str(x).strip().upper()
        if not s or s in ("NAN", "NONE"):
            return None
        return s

    if "norm_id" not in df_conus.columns:
        if "airport_id" in df_conus.columns:
            df_conus["norm_id"] = df_conus["airport_id"].map(_norm)
        elif "ident" in df_conus.columns:
            df_conus["norm_id"] = df_conus["ident"].map(_norm)

    if "norm_id" not in df_badges.columns:
        if "airport_id" in df_badges.columns:
            df_badges["norm_id"] = df_badges["airport_id"].map(_norm)
        elif "ident" in df_badges.columns:
            df_badges["norm_id"] = df_badges["ident"].map(_norm)

    # Now safe: state totals from achievement-eligible universe
    state_totals = df_badges["state"].value_counts()

    # -----------------------------
    # Visits: norm_id-aligned + gated to achievement-eligible airports
    # -----------------------------
    visited_ids = set()
    if not df_visits.empty and "norm_id" in df_visits.columns:
        visited_ids = set(
            df_visits["norm_id"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .unique()
        )

    eligible_norm = set(
        df_badges["norm_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    visited_ids = visited_ids & eligible_norm

    # -----------------------------
    # Map41: Bravo, Bravo! (Class B completion)
    # Source of truth: df_conus airspace_b
    # Eligibility alignment: df_badges norm_id
    # -----------------------------
    bravo_airport_html = ""              # ✅ always defined
    b_status, b_color, b_icon = progress_status(0)   # ✅ always defined

    bravo_progress = {"total": 0, "visited": 0, "pct": 0, "complete": False}
    bravo_completed_date = None

    try:
        if "airspace_b" not in df_conus.columns:
            raise ValueError(f"[bravo] df_conus missing airspace_b. cols={list(df_conus.columns)}")

        def _truthy(v):
            if v is None:
                return False
            try:
                if isinstance(v, float) and pd.isna(v):
                    return False
            except Exception:
                pass
            if isinstance(v, (int, float)):
                return int(v) == 1
            return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "x", "b")

        # Class B targets from airports.csv (df_conus)
        bravo_all = set(
            df_conus.loc[df_conus["airspace_b"].apply(_truthy), "norm_id"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .unique()
        )

        # Align with achievements eligibility
        bravo_targets = bravo_all & eligible_norm

        total_b = len(bravo_targets)  # should be 35
        hits_b = len(bravo_targets & visited_ids)

        complete_b = (total_b > 0 and hits_b >= total_b)
        pct_b = 100 if complete_b else (int(round((hits_b / total_b) * 100)) if total_b else 0)

        bravo_progress = {"total": total_b, "visited": hits_b, "pct": pct_b, "complete": complete_b}
        b_status, b_color, b_icon = progress_status(pct_b)

        bravo_airport_html = build_bravo_airport_html(
            df_badges=df_badges,
            bravo_targets=bravo_targets,
            visited_ids=visited_ids,
        )


        if complete_b and h:
            bravo_completed_date = set_bravo_completed_date_once(h)

        print(
            "[bravo][dbg]",
            "B_all=", len(bravo_all),
            "eligible=", len(eligible_norm),
            "targets=", total_b,
            "visited_ids=", len(visited_ids),
            "hits=", hits_b,
            "sample_targets=", list(sorted(list(bravo_targets)))[:6],
        )

    except Exception as e:
        print("[bravo][err]", repr(e))
        bravo_progress = {"total": 0, "visited": 0, "pct": 0, "complete": False}
        bravo_completed_date = None
        # bravo_airport_html stays ""
        # b_status/b_color/b_icon stay at progress_status(0)


    den = len(df_badges)
    total_us_vis = len(visited_ids)
    total_us_pct = (total_us_vis / den) * 100 if den > 0 else 0

    badge_rows = []
    for state in state_totals.index:
        state_airports = df_badges[df_badges["state"] == state].sort_values("airport_id")
        tot = len(state_airports)

        airport_list = []
        vis_count = 0

        for _, row in state_airports.iterrows():
            is_visited = row["norm_id"] in visited_ids
            if is_visited:
                vis_count += 1

            icon = "✅" if is_visited else "⭕"
            style = "color:#4caf50;" if is_visited else "color:#666;"

            airport_name = (
                row.get("ARPT_NAME")
                or row.get("name")
                or row.get("airport_name")
                or row.get("facility_name")
                or "Unknown"
            )

            # ✅ Use a class instead of hardcoded font-size so mobile CSS can scale it
            airport_list.append(
                f'<div class="airport-row" style="padding:4px 0; border-bottom:1px solid #333; {style}">'
                f'{icon} <b>{row["airport_id"]}</b> - {airport_name}'
                f"</div>"
            )

        pct = (vis_count / tot) * 100 if tot > 0 else 0

        status, color, icon_state = progress_status(pct)

        # Keep Map6 behavior: only show states with progress
        if vis_count > 0:
            badge_rows.append(
                {
                    "state": state,
                    "visited": vis_count,
                    "total": tot,
                    "percent": pct,
                    "status": status,
                    "color": color,
                    "icon": icon_state,
                    "airport_html": "".join(airport_list),
                }
            )

    # -----------------------------
    # CONUS summary row (for final row in list)
    # - Non-interactive (airport_html empty)
    # - Not included in dropdown (we append AFTER dropdown is built)
    # -----------------------------
    conus_tot = len(df_badges)
    conus_vis = len(visited_ids)
    conus_pct = (conus_vis / conus_tot) * 100 if conus_tot > 0 else 0

    conus_status, conus_color, conus_icon = progress_status(conus_pct)

    conus_row = {
        "state": "CONUS",
        "visited": conus_vis,
        "total": conus_tot,
        "percent": conus_pct,
        "status": conus_status,
        "color": conus_color,
        "icon": conus_icon,
        "airport_html": "",   # empty => non-expandable
        "is_conus": True,
    }

    badge_rows.sort(key=lambda x: x["percent"], reverse=True)

    # Build state dropdown options
    state_options = ['<option value="ALL">All states</option>']
    for r in badge_rows:
        state_options.append(f'<option value="{r["state"]}">{r["state"]}</option>')
    state_options_html = "\n".join(state_options)

    # -----------------------------
    # Append CONUS as the final row in the scroll list (not in dropdown)
    # Assumes conus_row has already been computed above.
    # -----------------------------
    try:
        badge_rows.append(conus_row)
    except Exception:
        pass

    # ✅ Correct navbar selection + pass handle for owner navbar branding/status
    navbar_html = (
        get_public_navbar(h, "achievements")
        if (navbar_mode == "public" and h)
        else get_navbar("achievements", handle=h)
    )

    animate_class = "rwy-animate" if should_animate_runways() else ""

    # --- Map41: one-line certifications header (Achievements-only) ---
    cert_html = ""
    try:
        s = (certifications_line or "").strip()
        if s:
            cert_html = f"""
    <div style="margin:10px 0 14px;color:#b9c0cc;font-weight:900;line-height:1.4;">
    {_html.escape(s).replace("&lt;br&gt;", "<br>")}
    </div>
    """
    except Exception:
        cert_html = ""

    # ✅ Mobile boost FIRST (scoped to Achievements content ONLY)
    # Do NOT style .mam-public-bar here — it must match the Map page navbar.
    mobile_page_boost = """
<style>
  @media (max-width: 640px) {
    html, body { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }

    /* -----------------------------
       Achievements CONTENT sizing only
       ----------------------------- */
    .ach-body { font-size: 16px; line-height: 1.55; }
    .ach-body .container { padding: 14px; }

    .ach-body h1 { font-size: 22px; line-height: 1.2; margin: 0 0 10px 0; }
    .ach-body .hint { font-size: 14px; line-height: 1.4; margin: 10px 0 14px 0; }

    /* Filter controls */
    .ach-body .filterbar { padding: 10px; gap: 10px; }
    .ach-body .filterbar label { font-size: 12px; }
    .ach-body .filterbar select { font-size: 14px; min-width: 180px; padding: 8px 10px; }

    /* -----------------------------
       State cards (state-pill, badge-mid, badge-sub, badge-icon)
       ----------------------------- */
    .ach-body .badge-header { padding: 9px; gap: 8px; align-items: center; }

    /* Tighten space below the header inside each card */
    .ach-body .badge-mid { gap: 4px; }

    /* Reduce whitespace around the progress bar line */
    .ach-body .progress-bar-bg { height: 9px; margin: 0; }

    .ach-body .state-pill {
      font-size: 18px !important;
      font-weight: 900;
      width: 50px !important;
      text-align: center;
      flex-shrink: 0;
    }

    .ach-body .badge-mid {
      flex: 1 1 auto;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
    }

    .ach-body .progress-bar-bg { height: 10px; margin: 0; }

    .ach-body .badge-sub {
      font-size: 11px;
      line-height: 1.3;
      color: #bbb;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .ach-body .badge-phase { font-weight: 900; color: #ddd; }
    .ach-body .badge-dot { opacity: 0.65; }

    .ach-body .badge-icon {
      font-size: 18px;
      padding: 6px 8px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.04);
      flex-shrink: 0;
      line-height: 1;
      text-decoration: none;
    }

    /* Airport checklist rows */
    .ach-body .airport-row {
      font-size: 14px !important;
      line-height: 1.45 !important;
      padding: 5px 0 !important;
    }

    /* -----------------------------
       Footer: compact on mobile
       ----------------------------- */
    .ach-body .footer-bar {
      padding: 6px 12px !important;
      max-height: 22vh !important;
      overflow-y: auto !important;
    }

    .ach-body .footer-bar .legend { font-size: 11px !important; }
    .ach-body .footer-bar span,
    .ach-body .footer-bar div { font-size: 11px !important; line-height: 1.25 !important; }
  }
</style>
"""
    # -----------------------------
    # Navbar selection (Achievements page)
    #  - public (/u/<handle>/achievements): use public navbar pills (matches public map)
    #  - owner/internal: keep private navbar
    # -----------------------------
    h = (handle or "").strip() or current_user_handle()

    if navbar_mode == "public" and h:
        navbar_html = get_public_navbar(h, active="achievements")
    else:
        navbar_html = get_navbar("achievements", handle=h)


    # ✅ BODY-ONLY HTML (no <!doctype html> wrapper)
    # IMPORTANT: footer is fixed; on mobile we reduce padding-bottom so it doesn't eat 25% of screen.
    html_out = mobile_page_boost + f"""
<style>
  .ach-body {{
    background:#1a1a1a;
    color:white;
    font-family:sans-serif;
    margin:0;
    padding-top:70px;
    padding-bottom:110px; /* desktop footer clearance */
  }}

  /* Public fixed navbar clearance (shared helper) */
  .ach-body.mam-public-padtop {{ padding-top: 76px; }}
  @media (max-width: 640px) {{
    .ach-body.mam-public-padtop {{ padding-top: 92px; }}
    .ach-body {{ padding-bottom: 84px; }}
  }}

  .container {{ max-width:800px; margin:0 auto; padding:20px; }}
  .badge-card {{ background:#252525; margin-bottom:15px; border-radius:8px; border:1px solid #333; overflow:hidden; }}
  .badge-header {{ display:flex; align-items:center; padding:15px; cursor:pointer; background:#2a2a2a; gap:12px; }}
  .badge-header:hover {{ background:#333; }}
  .progress-bar-bg {{ background:#444; height:12px; border-radius:6px; width:100%; flex-grow:1; margin:0; box-sizing:border-box; }}
  .badge-mid {{ width:100%; }}
  .progress-bar-fill {{ height:100%; border-radius:6px; }}
  .badge-details {{ padding:0 20px 20px 20px; background:#222; max-height:300px; overflow-y:auto; border-top:1px solid #444; }}

  .footer-bar {{
    position: fixed;
    bottom: 0;
    left: 0;
    width: 100%;
    background: #222;
    border-top: 1px solid #444;
    padding: 10px 20px;
    padding-bottom: max(10px, env(safe-area-inset-bottom));
    box-sizing: border-box;
  }}

  details > summary {{ list-style: none; }}
  details > summary::-webkit-details-marker {{ display: none; }}

  .filterbar {{
    background:#222;
    border:1px solid #333;
    border-radius:10px;
    padding:12px;
    margin: 12px 0 18px 0;
    display:flex;
    gap:12px;
    flex-wrap:wrap;
    align-items:center;
  }}

  .filterbar label {{ font-size:12px; color:#aaa; margin-right:6px; }}
  .filterbar select {{
    background:#111;
    color:#fff;
    border:1px solid #444;
    padding:10px 12px;
    border-radius:10px;
    font-size:14px;
    min-width:220px;
  }}

  .hint {{ color:#888; font-size:13px; margin-top:10px; margin-bottom:18px; }}

  /* Airport checklist rows (base) */
  .airport-row {{ font-size: 13px; }}

    /* Runway 360: animate segments after saving */
    body.rwy-animate svg [data-rwy-seg] {{
    stroke-dasharray: 700;
    stroke-dashoffset: 700;
    animation: rwyDraw 900ms ease-out forwards;
    }}
    @keyframes rwyDraw {{ to {{ stroke-dashoffset: 0; }} }}
</style>

<body class="ach-body {'mam-public-padtop' if navbar_mode == 'public' else ''} {animate_class}">
  {navbar_html}

 <div class="container">
  {cert_html}

  <div style="margin-top:8px; margin-bottom:18px;">
    {generate_runway360_section(handle=h, navbar_mode=navbar_mode)}
  </div>

  <!-- -----------------------------
       Bravo, Bravo! (Class B)
       Header + subheading (Runway 360 style)
       ----------------------------- -->
  <div style="margin-top:14px; margin-bottom:18px;">
    <div style="font-size:16px; font-weight:900;">BRAVO, BRAVO!</div>
    <div class="hint" style="margin-top:6px; margin-bottom:12px;">
      Visit all 35 public use U.S. Class B Airports.  (Excludes ADW & NKX)
    </div>

    <details class="badge-card badge-item"
             data-state="BRAVO"
             data-phase="{b_status}">
      <summary class="badge-header">
        <div class="state-pill" style="color:{b_color}">B</div>

        <div class="badge-mid">
          <div class="progress-bar-bg">
            <div class="progress-bar-fill"
                 style="width:{float(bravo_progress.get('pct') or 0):.1f}%;
                        background:{b_color};"></div>
          </div>

          <div class="badge-sub">
            <span class="badge-phase">{b_status}</span>
            <span class="badge-dot">•</span>
            <span>{int(bravo_progress.get('visited') or 0)}/{int(bravo_progress.get('total') or 0)}</span>
            <span class="badge-dot">•</span>
            <span>{float(bravo_progress.get('pct') or 0):.1f}%</span>
            {f'<span class="badge-dot">•</span><span>{_format_date_for_card(bravo_completed_at_iso(h))}</span>' if (bravo_progress.get("complete") and bravo_completed_at_iso(h)) else ""}
          </div>
        </div>

        <span class="badge-icon"
              aria-hidden="true"
              style="color:{b_color}; border:none; background:none; cursor:default;">
          {b_icon}
        </span>

        {f'''
        <a class="badge-icon"
           href="/ai/bravo-card"
           onclick="event.stopPropagation();"
           aria-label="Download Bravo completion card"
           title="Download Bravo completion card"
           style="color:{b_color}">
          ⬇️
        </a>
        ''' if bravo_progress.get("complete") else ""}
      </summary>


      <div class="badge-details">
        <h4 style="color:#aaa; border-bottom:1px solid #444; padding-bottom:5px; margin-top:15px;">
          AIRPORT CHECKLIST
        </h4>
        {bravo_airport_html}
      </div>
    </details>


    <div style="font-size:16px; font-weight:900;">VISITS BY STATE</div>
    <div class="hint">Use the dropdown to review progress by state. Click a state card to see your checklist.</div>

    <div class="filterbar">
      <div>
        <label for="stateSel">State</label>
        <select id="stateSel">
          {state_options_html}
        </select>
      </div>

      <div>
        <label for="phaseSel">Phase</label>
        <select id="phaseSel">
          <option value="ALL">All phases</option>
          <option value="TAKE-OFF">TAKE-OFF</option>
          <option value="CRUISING">CRUISING</option>
          <option value="APPROACH">APPROACH</option>
          <option value="SHORT FINAL">SHORT FINAL</option>
          <option value="COMPLETED">COMPLETED</option>
        </select>
      </div>
    </div>
"""
    for row in badge_rows:
        # CONUS should never be expandable
        is_conus = bool(row.get("is_conus")) or (row.get("state") == "CONUS")

        # Only expandable if it has checklist content and is not CONUS
        is_expandable = (not is_conus) and bool(row.get("airport_html"))

        # Badge/download icon should ONLY appear when COMPLETED
        pct = row.get("percent", 0) or 0

        is_completed = (
            row.get("status") == "COMPLETED"
            or row.get("icon") == "🥇"
            or pct >= 99.999
        )

        # Always show the phase icon for the row (🛫 ✈️ ↘️ 🛬 🏆)
        phase_icon_html = f"""
    <span class="badge-icon"
          aria-hidden="true"
          style="color:{row['color']}; border:none; background:none; cursor:default;">
      {row['icon']}
    </span>
"""

        # Only show the badge/download affordance when COMPLETED
        badge_icon_html = ""
        if row.get("status") == "COMPLETED":
            st = (row.get("state") or "").strip().upper()
            badge_icon_html = f"""
        <a class="badge-icon"
            href="/ai/state-card?state={st}"
            onclick="event.stopPropagation();"
            aria-label="Download state completion card"
            title="Download state completion card"
            style="color:{row['color']}">
            ⬇️
        </a>
"""


        if is_expandable:
            html_out += f"""
<details class="badge-card badge-item"
         data-state="{row['state']}"
         data-phase="{row['status']}">
  <summary class="badge-header">
    <div class="state-pill" style="color:{row['color']}">{row['state']}</div>

    <div class="badge-mid">
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" style="width:{row['percent']:.1f}%; background:{row['color']};"></div>
      </div>

      <div class="badge-sub">
        <span class="badge-phase">{row['status']}</span>
        <span class="badge-dot">•</span>
        <span>{row['visited']}/{row['total']}</span>
        <span class="badge-dot">•</span>
        <span>{row['percent']:.1f}%</span>
      </div>
    </div>

{phase_icon_html}{badge_icon_html}
  </summary>

  <div class="badge-details">
    <h4 style="color:#aaa; border-bottom:1px solid #444; padding-bottom:5px; margin-top:15px;">
      AIRPORT CHECKLIST
    </h4>
    {row['airport_html']}
  </div>
</details>
"""
        else:
            # CONUS (or any non-expandable row): render as a plain card, no details/checklist
            html_out += f"""
<div class="badge-card badge-item"
     data-state="{row['state']}"
     data-phase="{row['status']}">
  <div class="badge-header" style="cursor:default;">
    <div class="state-pill" style="color:{row['color']}">{row['state']}</div>

    <div class="badge-mid">
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" style="width:{row['percent']:.1f}%; background:{row['color']};"></div>
      </div>

      <div class="badge-sub">
        <span class="badge-phase">{row['status']}</span>
        <span class="badge-dot">•</span>
        <span>{row['visited']}/{row['total']}</span>
        <span class="badge-dot">•</span>
        <span>{row['percent']:.1f}%</span>
      </div>
    </div>

{badge_icon_html}
  </div>
</div>
"""

    html_out += f"""
</div>

<div class="footer-bar">
  <div style="max-width: 800px; margin: 0 auto;">
    <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-weight: bold; font-size: 12px;">
      <span>HARD SURFACED - PUBLIC AIRPORT PROGRESS KEY</span>
    </div>

    <div class="legend" style="margin-top:8px; font-size:10px; color:#aaa; display:flex; flex-wrap:wrap; gap:10px;">
      <span><span style="color:#00FFFF; font-weight:800;">🛫</span> TAKE-OFF (0–20%)</span>
      <span><span style="color:#0088FF; font-weight:800;">✈️</span> CRUISING (20–50%)</span>
      <span><span style="color:#FF00FF; font-weight:800;">↘️</span> APPROACH (50–80%)</span>
      <span><span style="color:#00FF00; font-weight:800;">🛬</span> SHORT FINAL (80–99%)</span>
      <span><span style="color:#FFD700; font-weight:800;">🏆</span> COMPLETED (100%)</span>
    </div>
  </div>
</div>

<script>
  const stateSel = document.getElementById('stateSel');
  const phaseSel = document.getElementById('phaseSel');
  const cards = Array.from(document.querySelectorAll('.badge-item')).filter(c => c.getAttribute('data-state') !== 'BRAVO');


  function applyFilters() {{
    const st = stateSel.value;
    const ph = phaseSel.value;

    cards.forEach(c => {{
      const cst = c.getAttribute('data-state');
      const cph = c.getAttribute('data-phase');

      const okState = (st === 'ALL') || (cst === st);
      const okPhase = (ph === 'ALL') || (cph === ph);

      c.style.display = (okState && okPhase) ? '' : 'none';
      if (c.style.display === 'none') c.removeAttribute('open');
    }});
  }}

  stateSel.addEventListener('change', applyFilters);
  phaseSel.addEventListener('change', applyFilters);
  applyFilters();
</script>

</body>
"""

    return html_out

# -----------------------------
# Runway 360 — Share Card + Club List
# -----------------------------

RUNWAY360_CARD_TEMPLATE_PATH = os.path.join(app.static_folder, "runway360_card_template.png")

# Where we store "club roster" entries (global file)
RUNWAY360_CLUB_KEY = "users/_runway360_club.json"

def _read_json_storage(key: str) -> dict:
    try:
        if not storage_backend.exists(key):
            return {}
        raw = storage_backend.read_bytes(key) or b"{}"
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print("_read_json_storage failed:", key, repr(e))
        return {}

def _write_json_storage(key: str, obj: dict) -> None:
    try:
        payload = json.dumps(obj or {}, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            storage_backend.write_bytes(key, payload, content_type="application/json", cache_control="no-store")
        except TypeError:
            storage_backend.write_bytes(key, payload)
    except Exception as e:
        print("_write_json_storage failed:", key, repr(e))

def runway360_is_complete(handle: str) -> bool:
    data = load_runway360(handle)
    done = runway360_completed_set(data)
    return len(done) >= 36

def runway360_completed_at_iso(handle: str) -> str:
    """
    Returns an ISO8601 Z timestamp. If missing and complete, sets it once.
    Stored in runway360 data so every generated card is stable.
    """
    data = load_runway360(handle) or {}
    # Prefer stable stored value
    completed_at = (data.get("completed_at") or "").strip()
    if completed_at:
        return completed_at

    # Only set if actually complete
    if not runway360_is_complete(handle):
        return ""

    completed_at = _now_utc().isoformat().replace("+00:00", "Z")
    try:
        data["completed_at"] = completed_at
        save_runway360(handle, data)
    except Exception as e:
        print("runway360_completed_at_iso: save failed:", repr(e))

    return completed_at

def _format_date_for_card(iso_z: str) -> str:
    # "Jan 17, 2026"
    if not iso_z:
        return ""
    try:
        s = iso_z.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return dt.strftime("%b %d, %Y").replace(" 0", " ")
    except Exception:
        return iso_z[:10]

def _load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    """
    Best-effort font loader that works on:
      - local Windows dev
      - Linux containers (Render)
      - project-bundled fonts (recommended)

    IMPORTANT: If we fall back to ImageFont.load_default(), it will NOT scale with size.
    """
    size = int(max(8, size or 12))

    candidates = []

    # 1) ✅ Preferred: project-bundled fonts (put .ttf files here)
    # Example files you can add:
    #   static/fonts/DejaVuSans.ttf
    #   static/fonts/DejaVuSans-Bold.ttf
    # or Inter:
    #   static/fonts/Inter-Regular.ttf
    #   static/fonts/Inter-Bold.ttf
    try:
        base_dir = getattr(app, "static_folder", None) or os.path.join(os.path.dirname(__file__), "static")
        fonts_dir = os.path.join(base_dir, "fonts")
        if bold:
            candidates += [
                os.path.join(fonts_dir, "DejaVuSans-Bold.ttf"),
                os.path.join(fonts_dir, "Inter-Bold.ttf"),
                os.path.join(fonts_dir, "Arial-Bold.ttf"),
            ]
        candidates += [
            os.path.join(fonts_dir, "DejaVuSans.ttf"),
            os.path.join(fonts_dir, "Inter-Regular.ttf"),
            os.path.join(fonts_dir, "Arial.ttf"),
        ]
    except Exception:
        pass

    # 2) Linux (common container paths)
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]

    # 3) Windows
    win = os.environ.get("WINDIR") or r"C:\Windows"
    if bold:
        candidates += [
            os.path.join(win, "Fonts", "arialbd.ttf"),
            os.path.join(win, "Fonts", "calibrib.ttf"),
            os.path.join(win, "Fonts", "segoeuib.ttf"),
        ]
    candidates += [
        os.path.join(win, "Fonts", "arial.ttf"),
        os.path.join(win, "Fonts", "calibri.ttf"),
        os.path.join(win, "Fonts", "segoeui.ttf"),
    ]

    # Try candidates
    for p in candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size=size)
        except Exception:
            continue

    # Final fallback (WARNING: fixed bitmap size)
    return ImageFont.load_default()

# -----------------------------
# State Completion — Badge Cards (CONUS)
# -----------------------------

STATE_NAMES = {
 "AL":"ALABAMA","AZ":"ARIZONA","AR":"ARKANSAS","CA":"CALIFORNIA","CO":"COLORADO","CT":"CONNECTICUT",
 "DE":"DELAWARE","FL":"FLORIDA","GA":"GEORGIA","ID":"IDAHO","IL":"ILLINOIS","IN":"INDIANA","IA":"IOWA",
 "KS":"KANSAS","KY":"KENTUCKY","LA":"LOUISIANA","ME":"MAINE","MD":"MARYLAND","MA":"MASSACHUSETTS",
 "MI":"MICHIGAN","MN":"MINNESOTA","MS":"MISSISSIPPI","MO":"MISSOURI","MT":"MONTANA","NE":"NEBRASKA",
 "NV":"NEVADA","NH":"NEW HAMPSHIRE","NJ":"NEW JERSEY","NM":"NEW MEXICO","NY":"NEW YORK",
 "NC":"NORTH CAROLINA","ND":"NORTH DAKOTA","OH":"OHIO","OK":"OKLAHOMA","OR":"OREGON","PA":"PENNSYLVANIA",
 "RI":"RHODE ISLAND","SC":"SOUTH CAROLINA","SD":"SOUTH DAKOTA","TN":"TENNESSEE","TX":"TEXAS","UT":"UTAH",
 "VT":"VERMONT","VA":"VIRGINIA","WA":"WASHINGTON","WV":"WEST VIRGINIA","WI":"WISCONSIN","WY":"WYOMING"
}
CONUS_STATES = set(STATE_NAMES.keys())

STATE_COMPLETE_KEY = "users/{handle}/state_complete.json"

def _state_complete_read(handle: str) -> dict:
    key = STATE_COMPLETE_KEY.format(handle=handle)
    obj = _storage_get_json(key)
    return obj if isinstance(obj, dict) else {}

def _state_complete_write(handle: str, obj: dict) -> None:
    key = STATE_COMPLETE_KEY.format(handle=handle)
    _storage_put_json(key, obj or {})

def state_completed_at_iso(handle: str, state: str) -> str:
    """
    Stable completion timestamp per state (set once when first detected complete).
    Stored in R2/user storage for consistency across devices and future downloads.
    """
    handle = (handle or "").strip().lower()
    st = (state or "").strip().upper()
    if not handle or st not in CONUS_STATES:
        return ""

    obj = _state_complete_read(handle)
    when = (obj.get(st) or "").strip()
    if when:
        return when

    # Set only if complete right now
    prog = compute_state_progress(handle, st)
    if not prog.get("complete"):
        return ""

    when = _now_utc().isoformat().replace("+00:00", "Z")
    obj[st] = when
    try:
        _state_complete_write(handle, obj)
    except Exception as e:
        print("state_completed_at_iso write failed:", repr(e))
    return when

def compute_state_progress(handle: str, state: str) -> dict:
    handle = (handle or "").strip().lower()
    st = (state or "").strip().upper()
    if not handle or not st:
        return {"state": st, "total": 0, "visited": 0, "pct": 0, "complete": False}

    df_airports, df_visits = load_data(handle=handle)

    a_state = df_airports[df_airports["state"].astype(str).str.upper() == st].copy()
    total = int(len(a_state))
    if total <= 0:
        return {"state": st, "total": 0, "visited": 0, "pct": 0, "complete": False}

    v = (
        df_visits.get("norm_id", pd.Series([], dtype=str))
        .astype(str).str.strip().str.upper()
        .replace({"": pd.NA}).dropna()
        .unique()
    )
    visited_set = set(v.tolist())

    a_norm = a_state["norm_id"].astype(str).str.strip().str.upper()
    visited = int(a_norm.isin(visited_set).sum())

    complete = (total > 0 and visited >= total)
    pct = 100 if complete else int((visited / total) * 100)  # floor, not round
    return {"state": st, "total": total, "visited": visited, "pct": pct, "complete": complete}

def compute_bravo_progress(*, visited_norm_ids: set[str]) -> dict:
    bravo = get_class_b_norm_ids()
    total = len(bravo)
    if total <= 0:
        return {"total": 0, "visited": 0, "pct": 0, "complete": False}

    visited = len(bravo.intersection(visited_norm_ids or set()))
    complete = (total > 0 and visited >= total)
    pct = 100 if complete else int((visited / total) * 100)

    return {"total": total, "visited": visited, "pct": pct, "complete": complete}


def compute_bravo_progress_for_handle(handle: str) -> dict:
    """
    Bravo progress computed via the same load_data() pipeline as states.
    This is the ONLY place we detect + set completion.
    """
    handle = (handle or "").strip().lower()
    if not handle:
        return {"total": 0, "visited": 0, "pct": 0, "complete": False}

    df_airports, df_visits = load_data(handle=handle)

    v = (
        df_visits.get("norm_id", pd.Series([], dtype=str))
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
        .dropna()
        .unique()
    )
    visited_set = set(v.tolist())

    # --- DEBUG (temporary): proves key-space alignment ---
    bravo = get_class_b_norm_ids()
    hits = bravo.intersection(visited_set)
    print(
        "[bravo][dbg]",
        "handle=", handle,
        "targets=", len(bravo),
        "visited=", len(visited_set),
        "hits=", len(hits),
        "sample_targets=", sorted(list(bravo))[:8],
        "sample_visited=", sorted(list(visited_set))[:8],
    )
    # ----------------------------------------------------

    # ✅ call the pure function
    prog = compute_bravo_progress(visited_norm_ids=visited_set)

    # ✅ Detect + set completion timestamp (set-once)
    if prog.get("complete"):
        set_bravo_completed_date_once(handle)

    return prog

   
STATE_CARD_TEMPLATE_DIR = os.path.join(app.static_folder, "state_cards")

def _state_card_template_path(st: str) -> str:
    st = (st or "").strip().upper()
    if not re.match(r"^[A-Z]{2}$", st):
        raise ValueError(f"Invalid state code: {st!r}")
    path = os.path.join(STATE_CARD_TEMPLATE_DIR, f"{st}.png")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing state card template: {path}")
    return path

def generate_state_badge_png(handle: str, state: str) -> bytes:
    """
    Generates a STATE COMPLETE badge:
    - base asset provides: circle + silhouette + long shadow + logo
    - overlays dynamically: state name + COMPLETE + pill + proof + @handle + date
    """
    handle = (handle or "").strip().lower()
    st = (state or "").strip().upper()
    if not handle:
        raise ValueError("Missing handle")
    if st not in CONUS_STATES:
        raise ValueError("Invalid CONUS state")

    prog = compute_state_progress(handle, st)
    if not prog.get("complete"):
        raise PermissionError("State not complete yet")

    base_path = _state_card_template_path(st)
    # _state_card_template_path already validates and raises FileNotFoundError

    base = Image.open(base_path).convert("RGBA")
    W, H = base.size  # expect 1024x1024

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Typography (tuned to match the badge aesthetic)
    font_state = _load_font(size=int(H * 0.070), bold=True)      # NEW JERSEY
    font_complete = _load_font(size=int(H * 0.060), bold=True)   # COMPLETE
    font_handle = _load_font(size=int(H * 0.060), bold=True)     # @nick
    font_pill = _load_font(size=int(H * 0.034), bold=True)       # pill text
    font_date = _load_font(size=int(H * 0.038), bold=False)      # date

    cx = W // 2

    def centered(text, y, font, fill, shadow=True):
        if shadow:
            draw.text((cx + 2, y + 2), text, font=font, fill=(0, 0, 0, 160), anchor="mm")
        draw.text((cx, y), text, font=font, fill=fill, anchor="mm")

    # -----------------------------
    # Headline positioning (final)
    # -----------------------------
    state_name = STATE_NAMES.get(st, st)
    MAGENTA = (226, 0, 116, 235)
    GOLD    = (236, 201, 120, 245)

    # STATE NAME — top symmetry in black (mirrors date at bottom)
    y_state = int(H * 0.085)
    centered(state_name, y_state, font_state, MAGENTA)

    # COMPLETE — just skirting the blue circle, with black shadow
    y_complete = int(H * 0.155)
    centered("COMPLETE", y_complete, font_complete, GOLD)

    # -----------------------------
    # Magenta pill (proof)
    # -----------------------------

    pill_w = int(W * 0.78)
    pill_h = int(H * 0.070)
    pill_x0 = cx - pill_w // 2
    pill_y0 = int(H * 0.79) - pill_h // 2
    pill_x1 = pill_x0 + pill_w
    pill_y1 = pill_y0 + pill_h
    pill_center_y = (pill_y0 + pill_y1) // 2

    draw.rounded_rectangle(
        (pill_x0, pill_y0, pill_x1, pill_y1),
        radius=34,
        fill=MAGENTA
    )
    draw.rounded_rectangle(
        (pill_x0, pill_y0, pill_x1, pill_y1),
        radius=34,
        outline=(0, 0, 0, 40),
        width=2
    )

    proof = f"LANDED AT ALL {st} PUBLIC AIRPORTS"
    centered(proof, pill_center_y, font_pill, GOLD, shadow=False)

    # Username + stable completion date
    centered(f"@{handle}", int(H * 0.875), font_handle, (255, 255, 255, 235))

    dt = _format_date_for_card(state_completed_at_iso(handle, st))
    if dt:
        centered(dt, int(H * 0.94), font_date, (210, 215, 225, 230), shadow=False)

    out = Image.alpha_composite(base, overlay).convert("RGB")
    bio = BytesIO()
    out.save(bio, format="PNG", optimize=True)
    return bio.getvalue()

def _bravo_card_template_path() -> str:
    path = os.path.join(app.static_folder, "bravo_base.png")  # or bravo-base.png
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing bravo base template: {path}")
    return path

def generate_bravo_badge_png(handle: str) -> bytes:
    """
    Generates BRAVO, BRAVO! badge (state-card style):
    - base asset provides: rings + runway + logo
    - overlays dynamically: BRAVO, BRAVO! + COMPLETE + proof pill + @handle + date
    """
    handle = (handle or "").strip().lower()
    if not handle:
        raise ValueError("Missing handle")

    completed_iso = bravo_completed_at_iso(handle)

    # ✅ Local debug override: allow rendering by setting a stable date once
    # (Never affects production unless you set the env var)
    if not completed_iso:
        if os.environ.get("MAM_DEBUG_BADGES") == "1":
            completed_iso = set_bravo_completed_date_once(handle)
        else:
            raise PermissionError("Bravo not complete yet")

    base_path = _bravo_card_template_path()
    base = Image.open(base_path).convert("RGBA")
    W, H = base.size  # expect 1024x1024

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    cx = W // 2

    def centered(text, y, font, fill, shadow=True):
        if shadow:
            draw.text((cx + 2, y + 2), text, font=font, fill=(0, 0, 0, 160), anchor="mm")
        draw.text((cx, y), text, font=font, fill=fill, anchor="mm")

    def centered_stroke(text, y, font, fill, stroke_w=3):
        # Stroke makes text readable over rings
        draw.text(
            (cx, y),
            text,
            font=font,
            fill=fill,
            anchor="mm",
            stroke_width=stroke_w,
            stroke_fill=(0, 0, 0, 185),
        )

    # Palette (match state cards)
    MAGENTA = (226, 0, 116, 230)
    GOLD    = (236, 201, 120, 245)  # closer to state-card gold

    # Copy state-card hierarchy: tempered title, COMPLETE moved above runway, calmer pill
    title_text = "BRAVO, BRAVO!"
    complete_text = "COMPLETE"
    proof_top = "VISITED ALL PUBLIC-USE"
    proof_bottom = "CLASS B AIRPORTS"

    # Typography (scaled DOWN from v6)
    # (Now that fonts are loading properly, these values will actually take effect.)
    font_title    = _load_font(size=int(H * 0.115), bold=True)   # was too huge / clipping
    font_complete = _load_font(size=int(H * 0.080), bold=True)   # clear, but not dominant

    # Pill text: reduce a bit (still very readable)
    font_pill_top = _load_font(size=int(H * 0.042), bold=True)
    font_pill_bot = _load_font(size=int(H * 0.048), bold=True)

    # Handle/date remain strong but not overpowering
    font_handle   = _load_font(size=int(H * 0.065), bold=True)
    font_date     = _load_font(size=int(H * 0.046), bold=False)

    # -----------------------------
    # Headline placement
    # -----------------------------
    # Title: keep inside the top rings, never clipped
    y_title = int(H * 0.080)
    centered_stroke(title_text, y_title, font_title, MAGENTA, stroke_w=4)

    # COMPLETE: move DOWN into the open space above the runway
    # (This lands it roughly above the runway graphic in your base.)
    y_complete = int(H * 0.305)
    centered_stroke(complete_text, y_complete, font_complete, GOLD, stroke_w=3)

    # -----------------------------
    # Proof pill (2 lines, calmer)
    # -----------------------------
    pill_w = int(W * 0.90)
    pill_h = int(H * 0.110)  # slightly taller than state, but not huge
    pill_x0 = cx - pill_w // 2
    pill_y0 = int(H * 0.715) - pill_h // 2
    pill_x1 = pill_x0 + pill_w
    pill_y1 = pill_y0 + pill_h
    pill_center_y = (pill_y0 + pill_y1) // 2

    radius = int(H * 0.055)

    draw.rounded_rectangle((pill_x0, pill_y0, pill_x1, pill_y1), radius=radius, fill=MAGENTA)
    draw.rounded_rectangle(
        (pill_x0, pill_y0, pill_x1, pill_y1),
        radius=radius,
        outline=(0, 0, 0, 40),
        width=2,
    )

    # Slightly tighter line spacing now that text is smaller
    line_gap = int(H * 0.020)
    centered(proof_top,    pill_center_y - line_gap, font_pill_top, GOLD, shadow=False)
    centered(proof_bottom, pill_center_y + line_gap, font_pill_bot, GOLD, shadow=False)

    # -----------------------------
    # Handle + stable completion date (state-like)
    # -----------------------------
    centered(f"@{handle}", int(H * 0.865), font_handle, (255, 255, 255, 235))

    dt = _format_date_for_card(completed_iso)
    if dt:
        centered(dt, int(H * 0.93), font_date, (210, 215, 225, 230), shadow=False)

    out = Image.alpha_composite(base, overlay).convert("RGB")
    bio = BytesIO()
    out.save(bio, format="PNG", optimize=True)
    return bio.getvalue()

from functools import lru_cache
import json
from datetime import datetime, timezone
AIRPORTS_CSV_PATH = os.environ.get("AIRPORTS_CSV_PATH") or os.path.join(app.root_path, "airports.csv")

@lru_cache(maxsize=1)
def get_class_b_norm_ids() -> set[str]:
    """
    Return the set of norm_id values for airports that have airspace_b == 1
    in the canonical airports.csv (single source of truth).
    """
    path = AIRPORTS_CSV_PATH

    if not os.path.exists(path):
        raise FileNotFoundError(f"[bravo] airports.csv not found at: {path}")

    df = pd.read_csv(path)

    # Defensive: require norm_id
    if "norm_id" not in df.columns:
        raise ValueError(f"[bravo] airports.csv missing required column 'norm_id'. Found: {list(df.columns)}")

    # airspace_b may be named exactly, or you might have it as column O with a specific header.
    # Prefer header 'airspace_b'; if not present, fail loudly (better than silently returning 0).
    if "airspace_b" not in df.columns:
        raise ValueError(
            f"[bravo] airports.csv missing required column 'airspace_b'. "
            f"Found: {list(df.columns)}"
        )

    def _truthy(v) -> bool:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return False
        if isinstance(v, (int, float)):
            return int(v) == 1
        s = str(v).strip().lower()
        return s in ("1", "true", "t", "yes", "y", "x")

    df_b = df[df["airspace_b"].apply(_truthy)]

    out = set(
        df_b["norm_id"]
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"": pd.NA, "nan": pd.NA, "none": pd.NA})
        .dropna()
        .tolist()
    )
    return out

def _put_json_to_storage(key: str, obj: dict) -> None:
    """
    Write JSON using the canonical storage writer (R2/local via storage_backend).
    """
    _write_json_to_storage(key, obj)


def _bravo_complete_key(handle: str) -> str:
    handle = (handle or "").strip().lower()
    return f"users/{handle}/bravo_complete.json"

def bravo_completed_at_iso(handle: str) -> str | None:
    try:
        obj = _load_json_from_storage(_bravo_complete_key(handle))
        if isinstance(obj, dict):
            s = (obj.get("completed_at") or "").strip()
            return s or None
    except Exception:
        pass
    return None

def set_bravo_completed_date_once(handle: str) -> str:
    """
    Set-once completion timestamp for Bravo, stored as YYYY-MM-DD (UTC).
    Emits:
      1) user/public Recent Achievements (right column)
      2) global badge event under events/badges/ (Pilot’s Lounge achievements rail)
    """
    handle = (handle or "").strip().lower()
    if not handle:
        raise ValueError("Missing handle")

    existing = bravo_completed_at_iso(handle)
    if existing:
        return existing

    s = _now_utc().strftime("%Y-%m-%d")
    _put_json_to_storage(_bravo_complete_key(handle), {"completed_at": s})

    # Only publish publicly if community sharing is enabled (non-demo)
    share_on = False
    try:
        share_on = bool(_get_share_activity(handle)) if handle != "demo" else False
    except Exception:
        share_on = False

    # -----------------------------
    # Recent Achievements (right column)
    # -----------------------------
    try:
        ev = {
            "id": f"achv:bravo:{handle}:{s}",            # stable + unique
            "ts": f"{s}T00:00:00Z",
            "type": "bravo_complete",
            "handle": handle,
            "title": "Bravo, Bravo!",
            "subtitle": "Visit all U.S. Class B Airports",
            "badge_url": f"/badge/bravo/{handle}",
            "icon": "🏆",
        }
        _append_recent_achievement_once(handle=handle, event=ev, also_public=share_on)
    except Exception:
        pass

    # -----------------------------
    # Global badge events feed (Pilot's Lounge rail)
    # Written only if sharing is enabled
    # -----------------------------
    try:
        if share_on:
            # Write into the same feed get_global_badge_events() reads: events/badges/
            if _r2_enabled():
                s3 = _r2_client()
                bucket = _r2_bucket()
                if s3 and bucket:
                    ts = f"{s}T00:00:00Z"
                    safe_ts = ts.replace("-", "").replace(":", "")  # sortable key
                    key = f"events/badges/{safe_ts}_{handle}_bravo.json"

                    payload = {
                        "ts": ts,
                        "handle": handle,
                        "badge_label": "Bravo, Bravo! — Class B Airports Completed",
                        "badge_type": "bravo",
                    }

                    s3.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                        ContentType="application/json",
                    )
    except Exception:
        pass

    return s

def generate_runway360_share_card_png(handle: str) -> bytes:
    """
    Runway 360 Share Card (NEW template):
    - Template already includes branding + gold pill text.
    - We only stamp:
        1) handle (NO "@") above the pill
        2) completion date under the pill in M/D/YYYY format (no leading zeros)
    """
    handle = (handle or "").strip().lower()
    if not handle:
        raise ValueError("Missing handle")

    if not os.path.exists(RUNWAY360_CARD_TEMPLATE_PATH):
        raise FileNotFoundError("Missing template at /static/runway360_card_template.png")

    # Load template
    base = Image.open(RUNWAY360_CARD_TEMPLATE_PATH).convert("RGBA")
    W, H = base.size

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def fmt_mdy_slash(iso: str | None) -> str:
        """
        Convert an ISO-ish string to M/D/YYYY (no leading zeros).
        If input is malformed, return empty string (never stamp garbage).
        """
        s = (iso or "").strip()
        if not s:
            return ""
        try:
            # Take ONLY the date portion (YYYY-MM-DD)
            # Handles:
            #  - 2026-05-28
            #  - 2026-05-28T00:00:00Z
            #  - 2026-05-28 00:00:00
            date_part = s.split("T")[0].split(" ")[0]
            y, m, d = date_part.split("-")
            return f"{int(m)}/{int(d)}/{int(y)}"
        except Exception:
            return ""

    def centered_text(
        txt: str,
        y: int,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        shadow: bool = True,
        shadow_alpha: int = 140,
        shadow_dx: int = 2,
        shadow_dy: int = 2,
    ) -> None:
        txt = (txt or "").strip()
        if not txt:
            return
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        x = (W - tw) // 2

        if shadow:
            draw.text((x + shadow_dx, y + shadow_dy), txt, font=font, fill=(0, 0, 0, shadow_alpha))
        draw.text((x, y), txt, font=font, fill=fill)

    # --- Stamp content ---
    handle_text = handle  # NO "@"

    completed_iso = runway360_completed_at_iso(handle)
    date_text = fmt_mdy_slash(completed_iso)

    # Fonts
    font_handle = _load_font(size=int(H * 0.060), bold=True)
    font_date = _load_font(size=int(H * 0.040), bold=False)

    # ------------------------------------------------------------
    # Vertical anchors (explicit green/red zones from template)
    # ------------------------------------------------------------

    pill_top = int(H * 0.705)
    pill_bottom = int(H * 0.755)
    pill_h = max(1, pill_bottom - pill_top)

    # --- GREEN ZONE (handle) ---
    # Between runway glow and pill top
    green_top = pill_top - int(pill_h * 2.2)
    green_bottom = pill_top - int(pill_h * 0.6)
    y_handle = (green_top + green_bottom) // 2

    # --- RED ZONE (date) ---
    # Between pill bottom and inner compass circle
    red_top = pill_bottom + int(pill_h * 0.2)
    red_bottom = pill_bottom + int(pill_h * 1.4)
    y_date = (red_top + red_bottom) // 2

    # Colors (match your gold/space vibe)
    handle_color = (235, 238, 245, 235)  # near-white
    date_color = (210, 215, 225, 225)    # softer gray-white

    centered_text(handle_text, y_handle, font_handle, handle_color, shadow=True, shadow_alpha=160)
    centered_text(date_text, y_date, font_date, date_color, shadow=False)

    out = Image.alpha_composite(base, overlay).convert("RGB")
    bio = BytesIO()
    out.save(bio, format="PNG", optimize=True)
    return bio.getvalue()

def runway360_club_upsert(handle: str) -> None:
    """
    Adds/updates the global club roster entry once a user completes.
    Does not require public sharing — you can filter at render time later.
    """
    handle = (handle or "").strip().lower()
    if not handle or not is_valid_handle(handle):
        return
    if not runway360_is_complete(handle):
        return

    club = _read_json_storage(RUNWAY360_CLUB_KEY) or {}
    club[handle] = {
        "handle": handle,
        "completed_at": runway360_completed_at_iso(handle),
        "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
    }
    _write_json_storage(RUNWAY360_CLUB_KEY, club)

@app.route("/runways/card.png", methods=["GET"])
@login_required
def runway360_card_png():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return Response("Unauthorized", status=401)

    ensure_user_initialized(handle)

    # Must be trial or member (since this is an in-app perk)
    if not has_active_access(handle):
        return redirect("/trial/ended?next=" + quote("/runways/card.png", safe="/=?&"), code=302)

    # Must be complete to download
    if not runway360_is_complete(handle):
        return Response("Runway 360 not complete yet.", status=403)

    # Upsert club roster entry (safe idempotent)
    try:
        runway360_club_upsert(handle)
    except Exception as e:
        print("runway360_card_png: club upsert failed:", repr(e))

    try:
        png = generate_runway360_share_card_png(handle)
        resp = Response(png, mimetype="image/png")
        # Download-friendly filename
        resp.headers["Content-Disposition"] = f'attachment; filename="myairportmap_runway360_{handle}.png"'
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        print("runway360_card_png failed:", repr(e))
        return Response("Failed to generate card.", status=500)

@app.route("/runway360/club", methods=["GET"])
def runway360_club_page():
    club = _read_json_storage(RUNWAY360_CLUB_KEY) or {}
    rows = list(club.values())

    # Sort newest first
    def keyfn(r):
        return (r.get("completed_at") or "")
    rows.sort(key=keyfn, reverse=True)

    def fmt_mdy_slash(iso: str | None) -> str:
        """
        Club date display: M/D/YYYY (no leading zeros).
        Never show raw ISO timestamps on the page.
        """
        s = (iso or "").strip()
        if not s:
            return ""
        try:
            date_part = s.split("T")[0].split(" ")[0]  # YYYY-MM-DD
            y, m, d = date_part.split("-")
            return f"{int(m)}/{int(d)}/{int(y)}"
        except Exception:
            return ""

    # Build table rows
    trs = []
    for r in rows[:500]:  # cap for safety
        h_raw = (r.get("handle") or "").strip().lower()
        if not h_raw:
            continue

        h = _html.escape(h_raw)
        avatar = _html.escape(avatar_url_for_handle(h_raw))
        dt = _html.escape(fmt_mdy_slash(r.get("completed_at", "")) or "")

        map_url = f"/u/{h}/map"
        ach_url = f"/u/{h}/achievements"

        trs.append(f"""
        <tr>
          <td class="td-avatar">
            <img class="avatar"
                 src="{avatar}"
                 onerror="this.onerror=null;this.src='/static/mam-logo.png';"
                 alt="Avatar">
          </td>

          <td class="td-user">
            <a href="{ach_url}" class="handle">@{h}</a>
          </td>

          <td class="td-date">
            {dt}
          </td>

          <td class="td-map">
            <a href="{map_url}" class="maplink">Map</a>
          </td>
        </tr>
        """)

    table_html = ""
    if trs:
        table_html = f"""
        <table class="table club-table">
          <colgroup>
            <col style="width:44px;">
            <col>
            <col style="width:170px;">
            <col style="width:64px;">
          </colgroup>
            <tr>
            <th></th>
            <th>Username</th>
            <th>Runway 360 Date</th>
            <th>Map</th>
            </tr>

          {''.join(trs)}
        </table>
        """
    else:
        table_html = '<div class="muted" style="margin-top:12px;">No members yet.</div>'

    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runway 360 Club · MyAirportMap</title>
  <style>
    body {{ background:#0f1115; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:0; }}
    .wrap {{ max-width:980px; margin:0 auto; padding:22px; }}
    .muted {{ color:#a0a0a0; font-size:14px; }}
    .card {{ background:#151515; border:1px solid #2a2a2a; border-radius:16px; padding:16px; margin-top:14px; }}

    .btn {{
      display:inline-block; padding:10px 12px; border-radius:12px;
      background:#1f1f1f; border:1px solid #3a3a3a; color:#fff; text-decoration:none; font-weight:900;
    }}
    .btn:hover {{ border-color:#666; }}

    .table {{ width:100%; border-collapse:collapse; }}
    .table th {{
      text-align:left; font-size:12px; color:#cfcfcf; letter-spacing:0.04em; text-transform:uppercase;
      padding:10px 8px; border-bottom:1px solid #2a2a2a;
    }}
    .table td {{
      padding:10px 8px; border-bottom:1px solid #2a2a2a; vertical-align:middle;
    }}

    .club-table .avatar {{
      width:34px; height:34px; border-radius:10px; object-fit:cover;
      border:1px solid #2a2a2a; background:#0a0a0a; display:block;
    }}
    .club-table .handle {{
      color:#dbe9ff; text-decoration:none; font-weight:950;
    }}
    .club-table .maplink {{
      color:#9ad; text-decoration:none; font-weight:900;
    }}
    .club-table .maplink:hover {{ text-decoration:underline; }}

    @media (max-width:640px) {{
      html, body {{ overflow-x:hidden; }}
      .wrap {{ padding:18px; }}

      /* ✅ Let the browser size columns naturally */
      .table {{
        table-layout:auto;
        width:100%;
      }}

      /* ✅ Keep headers readable (don’t stack letters) */
      .table th {{
        white-space:nowrap;
        font-size:11px;
      }}

      /* ✅ Do NOT allow per-character breaking */
      .table td {{
        overflow-wrap:normal;
        word-break:normal;
      }}

      /* ✅ Keep @handles on one line */
      .club-table .handle {{
        white-space:nowrap;
        display:inline-block;
      }}

      /* ✅ If table still feels tight, allow horizontal scroll */
      .card {{
        overflow-x:auto;
        -webkit-overflow-scrolling:touch;
      }}
            /* ✅ Lounge + Club: never stack letters */
      a.handle, .handle,
      a.maplink, .maplink,
      .td-user, .td-username {{
        white-space: nowrap !important;
        display: inline-block !important;
        overflow-wrap: normal !important;
        word-break: normal !important;
      }}

      /* ✅ Kill any "break anywhere" coming from elsewhere */
      .table td, .table th {{
        overflow-wrap: normal !important;
        word-break: normal !important;
        hyphens: none !important;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div style="display:flex; justify-content:space-between; align-items:flex-end; gap:12px; flex-wrap:wrap;">
      <div>
        <div style="font-weight:950; font-size:22px; letter-spacing:-0.2px;">Runway 360 Club</div>
        <div class="muted" style="margin-top:6px;">Pilots who completed all 36 runway numbers.</div>
        <div class="muted" style="margin-top:8px;"><b style="color:#fff;">{len(rows)}</b> members</div>
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <a class="btn" href="/logbook">Home</a>
        <a class="btn" href="/sign-in">Sign in</a>
      </div>
    </div>

    <div class="card">
      {table_html}
    </div>
  </div>
</body>
</html>""",
        mimetype="text/html",
    )


RUNWAY360_JOIN_LOG_KEY = "runway360/join_log.json"

def runway360_join_log_read() -> list[dict]:
    obj = _storage_get_json(RUNWAY360_JOIN_LOG_KEY)
    if isinstance(obj, list):
        return obj
    return []

def runway360_join_log_add(handle: str) -> None:
    handle = (handle or "").strip().lower()
    if not handle:
        return
    log = runway360_join_log_read()
    if any((x.get("handle") or "").lower() == handle for x in log):
        return
    log.append({"handle": handle, "joined_at": _now_utc().isoformat().replace("+00:00","Z")})
    # keep it bounded
    log = log[-5000:]
    _storage_put_json(RUNWAY360_JOIN_LOG_KEY, log)

def runway360_join_log_last(n: int = 10) -> list[dict]:
    log = runway360_join_log_read()
    return list(reversed(log))[:n]

# ============================================================
# LOGBOOK MANAGER (manual editor for users w/o export)
# ============================================================

def _visits_schema_df() -> pd.DataFrame:
    """Return an empty visits df in the canonical schema used by this app."""
    return pd.DataFrame(columns=["airport_id", "date_visited", "callsign", "notes"])

def _load_visits_csv(path: str, handle: str | None = None) -> pd.DataFrame:
    raw = _read_visits_bytes(path, handle=handle)
    if raw:
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        except Exception:
            df = _visits_schema_df()
    else:
        df = _visits_schema_df()

    # normalize schema (accept older Map16 variants)
    if "date" in df.columns and "date_visited" not in df.columns:
        df["date_visited"] = df["date"]
    if "aircraft" in df.columns and "callsign" not in df.columns:
        df["callsign"] = df["aircraft"]

    for c in ["airport_id", "date_visited", "callsign", "notes"]:
        if c not in df.columns:
            df[c] = ""
    df = df[["airport_id", "date_visited", "callsign", "notes"]].copy()

    df["airport_id"] = df["airport_id"].astype(str).apply(normalize_airport)
    return df

def _write_visits_csv(df: pd.DataFrame, path: str, handle: str | None = None) -> None:
    out = df.copy()
    for c in ["airport_id", "date_visited", "callsign", "notes"]:
        if c not in out.columns:
            out[c] = ""
    out = out[["airport_id", "date_visited", "callsign", "notes"]]

    buf = io.BytesIO()
    out.to_csv(buf, index=False)
    _write_visits_bytes(path, buf.getvalue(), handle=handle)
    try:
        load_visits.cache_clear()
    except Exception:
        pass

    # Community badge feed: record newly-earned milestones (best-effort)
    try:
        if handle and handle != "demo":
            _record_new_badge_events(handle, df)
    except Exception:
        pass

    # Pilot's Lounge milestones feed: record derived milestones (best-effort)
    try:
        if handle and handle != "demo":
            _record_new_milestone_events(handle, df)
    except Exception:
        pass

def _coerce_date(date_raw: str) -> str:
    """
    Normalize user-entered/parsed date into YYYY-MM-DD.
    Accepts common formats:
      - YYYY-MM-DD
      - MM/DD/YYYY
      - MM/DD/YY
      - YYYY/MM/DD
      - ISO-ish strings like YYYY-MM-DDTHH:MM:SS...
      - 'YYYY-MM-DD 00:00:00'
    Returns "" if unparseable.
    """
    if not date_raw:
        return ""

    s = str(date_raw).strip()
    if not s:
        return ""

    # Handle ISO-ish strings and timestamps by trimming to the date portion
    # e.g. "2025-12-21T00:00:00Z" or "2025-12-21 00:00:00"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        s = s[:10]

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass

    return ""

def _fmt_mmddyyyy(date_raw: str) -> str:
    if not date_raw:
        return ""

    iso = _coerce_date(date_raw)
    if not iso:
        return ""

    return datetime.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%Y")

def _coerce_date_yyyy_mm_dd(date_raw: str) -> str:
    """
    Back-compat shim.
    Some parts of the app expect this name. Normalize into YYYY-MM-DD.
    """
    return _coerce_date(date_raw)

def generate_logbook_manage_content(message: str = "", handle: Optional[str] = None) -> str:
    df_conus = load_airports_cached()  # kept, even if unused here (safe for consistency)

    # Fallback only; private routes should pass handle explicitly
    if not handle:
        handle = "demo"

    # Ensure the user's files exist (demo should be persistent too)
    try:
        ensure_user_initialized(handle)
    except Exception:
        pass

    # -----------------------------
    # "Last import" banner (show once)
    # -----------------------------
    try:
        last_import = session.pop("last_import", None)
    except Exception:
        last_import = None

    last_import_html = ""
    if last_import:
        ts = _html.escape(str(last_import.get("ts", "")))
        ftype = _html.escape(str(last_import.get("type", "")))
        fname = _html.escape(str(last_import.get("filename", "")))
        visits_written = int(last_import.get("visits_written", 0) or 0)
        unique_airports = int(last_import.get("unique_airports", 0) or 0)
        err = last_import.get("error")
        
        warn = last_import.get("warning")
        sample_airports = last_import.get("sample_airports") or []
        sample_line = ""
        if sample_airports:
            sample_line = "<br>Sample airports: <b>" + _html.escape(", ".join(sample_airports[:12])) + "</b>"

        if warn:
            last_import_html += f"""
            <div style="margin-top:10px;background:#3b3418;border:1px solid #6b5a1d;color:#fff2b3;
                            padding:12px 14px;border-radius:10px;">
            <div style="font-weight:900;margin-bottom:6px;">Import safety warning</div>
            <div style="font-size:13px; line-height:1.35;">
              {_html.escape(str(warn))}
            </div>
            </div>
            """


        if err:
            last_import_html = f"""
            <div style="margin-top:12px;background:#3a1e1e;border:1px solid #6a2a2a;color:#ffd7d7;
                        padding:12px 14px;border-radius:10px;">
              <div style="font-weight:900;margin-bottom:6px;">Last import failed</div>
              <div style="font-family:monospace;font-size:12px;opacity:.95;">
                {ts} — {ftype} — {fname}<br>
                {_html.escape(str(err))}
              </div>
            </div>
            """
        else:
            last_import_html = f"""
            <div style="margin-top:12px;background:#1f2a1f;border:1px solid #2f4a2f;color:#d7ffd7;
                        padding:12px 14px;border-radius:10px;">
              <div style="font-weight:900;margin-bottom:6px;">Last import</div>
              <div style="font-family:monospace;font-size:12px;opacity:.95;">
                {ts} — {ftype} — {fname} — {sample_line}<br>
                Visits written: <b>{visits_written}</b> &nbsp;|&nbsp;
                Unique airports: <b>{unique_airports}</b>
              </div>
            </div>
            """

    # -----------------------------
    # Load visits
    # -----------------------------
    path = resolve_visits_csv(handle)
    visits_df = _load_visits_csv(path, handle=handle).reset_index(drop=True)

    # If an undo backup exists, show a small “Undo last delete” affordance.
    undo_html = ""
    try:
        if _read_undo_visits_bytes(path, handle=handle):
            undo_html = """
            <div style="margin-top:12px;background:#1b2430;border:1px solid #2b3b52;color:#dbe9ff;
                    padding:12px 14px;border-radius:10px;">
            <div style="font-weight:900;margin-bottom:6px;">Undo available</div>
            <div style="font-size:13px; line-height:1.35;">
            You can undo your most recent delete.
            </div>
            <form method="post" action="/logbook/manage/undo-delete" style="margin-top:10px;"
                onsubmit="return confirm('Restore the logbook to the state before your most recent delete?');">
            <button class="btn" type="submit">Undo last delete</button>
            </form>
            </div>
        """
    except Exception:
        undo_html = ""

    style_block = """
<style>
  /* Upload overlay (Map36) */
  .upload-overlay{
    position:fixed; inset:0;
    background:rgba(0,0,0,.55);
    display:none; align-items:center; justify-content:center;
    z-index:100000;
  }
  .upload-overlay.show{ display:flex; }
  .upload-modal{
    background:#151515;
    border:1px solid #2a2a2a;
    border-radius:16px;
    padding:18px 18px;
    width:min(420px, calc(100vw - 40px));
    box-shadow:0 18px 50px rgba(0,0,0,.45);
  }
  .upload-row{ display:flex; gap:12px; align-items:center; }
  .spinner{
    width:18px; height:18px; border-radius:999px;
    border:3px solid #3a3a3a;
    border-top-color:#fff;
    animation:spin 0.9s linear infinite;
    flex:0 0 auto;
  }
  @keyframes spin { to { transform:rotate(360deg); } }
  .upload-title{ font-weight:900; font-size:16px; margin:0; }
  .upload-sub{ color:#bdbdbd; font-size:13px; margin-top:4px; line-height:1.35; }
  .btn[disabled]{ opacity:.55; cursor:not-allowed; }

  body { background:#1a1a1a; color:white; font-family:sans-serif; margin:0; padding-top:70px; padding-bottom:50px; }
  .container { max-width:1000px; margin:0 auto; padding:20px; }
  .card { background:#222; border:1px solid #333; border-radius:14px; padding:14px; margin-bottom:16px; }
  .sub { color:#aaa; font-size:13px; margin:6px 0 14px; }
  .grid { display:grid; grid-template-columns: 1.1fr 1.7fr 1.2fr 0.8fr; gap:10px; align-items:end; }
  label { font-size:12px; color:#aaa; display:block; margin-bottom:6px; }
  input, select, textarea { width:100%; box-sizing:border-box; padding:10px; border-radius:10px; background:#111; border:1px solid #444; color:#fff; }
  textarea { min-height:80px; resize:vertical; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px; border-bottom:1px solid #333; vertical-align:top; }
  th { text-align:left; color:#aaa; font-size:12px; }

  /* Buttons (smaller, cleaner) */
  .btn { background:#2b7cff; color:white; border:none; border-radius:10px; padding:8px 10px; cursor:pointer; font-weight:850; font-size:13px; line-height:1; text-decoration:none; display:inline-block; }
  .btn:hover { filter:brightness(1.05); }
  .btn-sm { padding:7px 9px; font-size:13px; }
  .btn-quiet { background:#151515; border:1px solid #3a3a3a; }
  .danger { background:#a33; }

  .btnrow { margin-top:10px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .cardtitle { font-weight:900; margin-bottom:6px; font-size:15px; }
  .msg { margin-bottom:12px; padding:10px 12px; background:#142; border:1px solid #2a5; border-radius:10px; }

  .modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none; align-items:center; justify-content:center; padding:20px; }
  .modal .inner { background:#222; border:1px solid #444; border-radius:14px; max-width:720px; width:100%; padding:14px; }
  .row3 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:10px; }

  /* -----------------------------
     Mobile: Existing Visits as stacked cards (no horiz scroll)
     ----------------------------- */
  .notes-mobile { display:none; }

  @media (max-width: 640px) {
    /* kill horizontal scrolling behavior */
    .visits-card table { overflow: visible; display:block; }
    .visits-card thead { display:none; }

    .visits-card tbody,
    .visits-card tr,
    .visits-card td { display:block; width:100%; }

    .visits-card tr {
      background:#111;
      border:1px solid #2a2a2a;
      border-radius:14px;
      padding:12px;
      margin:10px 0;
    }

    .visits-card td {
      border:0;
      padding:6px 0;
      white-space: normal !important;
      overflow-wrap: normal !important;
      word-break: normal !important;
    }

    /* Hide desktop Notes column on mobile */
    .visits-card td.col-notes { display:none; }

    /* Buttons row */
    .visits-card td.col-actions {
      padding-top:10px;
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      align-items:center;
    }

    /* Comments live under buttons on mobile */
    .notes-mobile {
      display:block;
      width:100%;
      margin-top:10px;
      padding-top:10px;
      border-top:1px solid #222;
      color:#cbd5e1;
      font-size:14px;
      line-height:1.35;
      overflow-wrap:anywhere; /* only breaks truly-long unbroken strings */
      word-break:normal;
    }

    .notes-mobile .label {
      font-size:12px;
      color:#9aa0a6;
      font-weight:800;
      margin-bottom:6px;
      letter-spacing:.2px;
      text-transform:uppercase;
    }
  }
</style>
"""

    # -----------------------------
    # Build table rows (desktop table + mobile cards via CSS)
    # -----------------------------
    rows_html = []
    for idx, row in visits_df.iterrows():
        date = _html.escape(str(row.get("date_visited", "") or ""))
        aid = _html.escape(str(row.get("airport_id", "") or ""))
        cs = _html.escape(str(row.get("callsign", "") or ""))
        notes = str(row.get("notes", "") or "")
        notes_html = _linkify(notes)

        rows_html.append(f"""
        <tr>
          <td class="col-date">{date}</td>
          <td class="col-airport" style="font-weight:800;">{aid}</td>
          <td class="col-aircraft">{cs}</td>

          <!-- Desktop notes column (we'll hide this on mobile via CSS) -->
          <td class="col-notes" style="min-width:240px;">{notes_html}</td>

          <!-- Actions + Mobile notes (mobile-only via CSS) -->
          <td class="col-actions">
            <form method="post" action="/logbook/manage/delete" style="display:inline;">
              <input type="hidden" name="row_index" value="{idx}">
              <button class="btn btn-sm danger" type="submit">Delete</button>
            </form>

            <button class="btn btn-sm" type="button" onclick="openEdit({idx})">Edit</button>

            <div class="notes-mobile">
              <div class="label">Comments</div>
              {notes_html or "<span style='color:#777;'>—</span>"}
            </div>
          </td>
        </tr>
        """)



    table_body = "\n".join(rows_html) if rows_html else "<tr><td colspan='5' style='color:#888;'>No visits yet.</td></tr>"
    msg_html = f'<div class="msg">{_html.escape(message)}</div>' if message else ""
    visits_json = json.dumps(visits_df.to_dict(orient="records"))

    # -----------------------------
    # Share card (trial/paid only)
    # -----------------------------
    base = (request.host_url or "").rstrip("/")
    hub_url = f"{base}/u/{handle}/map"


    # demo always shareable; everyone else must have access (trial active or paid)
    sharing_allowed = (handle == "demo") or has_active_access(handle)

    if sharing_allowed:
        share_html = f"""
        <div class="card" style="margin:14px 0; padding:14px; border:1px solid #2a2a2a; border-radius:14px;">
          <div style="font-weight:800; font-size:16px;">Share your map + achievements</div>
          <div style="color:#aaa; font-size:13px;">
            This link is public. Anyone with it can view your profile and toggle between <b>Map</b> and <b>Achievements</b>.
          </div>

          <div style="margin-top:8px; display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
            <div style="font-weight:700; min-width:78px;">Link</div>
            <a href="{hub_url}" target="_blank" style="word-break:break-all;">{hub_url}</a>
            <button class="btn btn-sm btn-quiet" type="button" onclick="copyText('{hub_url}')">Copy</button>
            <div id="copyMsg" style="color:#7bd; font-size:13px; display:none;">Copied!</div>
          </div>
        </div>
        """
    else:
        share_html = """
        <div class="card" style="margin:14px 0; padding:14px; border:1px solid #2a2a2a; border-radius:14px;">
          <div style="font-weight:900; font-size:16px;">Sharing is a membership feature</div>
          <div style="color:#aaa; font-size:13px; margin-top:6px;">
            Upgrade to share your map and achievements with anyone using your unique link.
          </div>
          <div style="margin-top:12px;">
            <a class="btn" href="/upgrade">Upgrade</a>
          </div>
        </div>
        """

    # -----------------------------
    # Privacy: opt-out toggle (non-demo)
    # -----------------------------
    share_on = _get_share_activity(handle) if handle and handle != "demo" else False
    checked = "checked" if share_on else ""
    privacy_html = ""
    if handle and handle != "demo":
        privacy_html = f"""
        <div class='card' style='margin:14px 0; padding:14px; border:1px solid #2a2a2a; border-radius:14px;'>
          <div style='display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;'>
            <div>
              <div style='font-weight:800; font-size:16px;'>Community sharing</div>
              <div style='color:#aaa; font-size:13px;'>
                If enabled, milestones you earn may appear in the public activity feed.
              </div>
            </div>
            <form method='post' action='/settings/privacy' style='margin:0;'>
              <label style='display:inline-flex; align-items:center; gap:10px; cursor:pointer;'>
                <input type='checkbox' name='share_activity' value='1' {checked} onchange='this.form.submit()' />
                <span style='font-weight:700;'>Share my achievement activity</span>
              </label>
            </form>
          </div>
        </div>
        """

    # -----------------------------
    # Map41: One-time Lounge nudge (after first real activity)
    # -----------------------------
    lounge_nudge_html = ""
    try:
        settings = load_user_settings(handle) or {}
        nudge_shown = bool(settings.get("lounge_nudge_shown"))
    except Exception:
        nudge_shown = False

    try:
        has_activity = bool(len(visits_df) > 0)
    except Exception:
        has_activity = False

    can_share = False
    try:
        can_share = is_paid_user_handle(handle)
    except Exception:
        can_share = False
   
    if (handle and handle != "demo") and has_activity and (not share_on) and (not nudge_shown) and can_share:
        lounge_nudge_html = f"""
        <div class="card" style="margin:14px 0; padding:14px; border:1px solid #2a2a2a; border-radius:14px;">
          <div style="font-weight:900; font-size:16px;">Pilot’s Lounge</div>
            <div style="color:#aaa; font-size:13px; margin-top:6px; line-height:1.35;">
            Want to appear in the Pilot’s Lounge? Turn on <b>Community sharing</b>.<br>
            <span style="opacity:.9;">Opt-in only. You can turn this off anytime.</span>
            </div>
          <form method="post" action="/settings/privacy" style="margin-top:12px;">
            <input type="hidden" name="share_activity" value="1">
            <button class="btn" type="submit">Enable community sharing</button>
          </form>
        </div>
        """
        # mark shown (best-effort, never blocks)
        try:
            save_user_settings(handle, {"lounge_nudge_shown": True})
        except Exception:
            pass

    # -----------------------------
    # Page HTML
    # -----------------------------
    return f"""<!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Airport Visit Manager</title>
    {style_block}
    </head>
    <body>
    {get_navbar("logbook", handle)}
    <div class="container">
    <h1>AIRPORT VISIT MANAGER</h1>
    <div class="sub">Manual create/edit for users without CSV/PDF export. This writes directly to <b>my_visits.csv</b>.</div>

    {msg_html}
    {share_html}
    {privacy_html}
    {lounge_nudge_html}

    <div class="card" id="upload">
      <div class="cardtitle">Uploads</div>
      <div class="sub" style="margin-top:0;">Import ForeFlight exports here: <br>
ForeFlight CSV: Flights Table / Logbook export; <br>
ForeFlight PDF: Complete Logbook Report - 2 page version. <br>
CSV imports Date, Airport, AircraftID, and Pilot Comments. PDFs import flights only; you can add comments below.  </div>
   
<form id="logbookUploadForm" method="post" action="/logbook/manage/upload" enctype="multipart/form-data">
  <div style="max-width:620px;">
    <label>Upload CSV or PDF</label>
    <input type="file" name="file" accept=".csv,.pdf" required>

    <div style="margin-top:12px; padding:12px 14px; border:1px solid #333; border-radius:12px; background:#191919;">
      <div style="font-size:12px; color:#bbb; line-height:1.35;">
        Safety check: if an import parses under <b>3 visits</b>, MyAirportMap will refuse to overwrite your data
        unless you confirm and re-upload.
      </div>

      <label style="display:flex; gap:10px; align-items:center; margin-top:12px; font-size:12px; color:#ddd;">
        <input type="checkbox" name="confirm_small" value="1" style="width:auto; margin:0;">
        I understand this import may overwrite the existing visits.
      </label>

      <div style="margin-top:12px; display:flex; justify-content:left;">
        <button class="btn btn-sm" type="submit" style="min-width:160px;">
          Upload &amp; Import
        </button>
      </div>
    </div>
  </div>
</form>

      <!-- Upload overlay (Map36) -->
      <div id="uploadOverlay" class="upload-overlay" role="alert" aria-live="polite" aria-busy="true">
        <div class="upload-modal">
          <div class="upload-row">
            <div class="spinner" aria-hidden="true"></div>
            <div>
              <div class="upload-title">Importing…</div>
              <div class="upload-sub">Import in progress—please keep this tab open. CSV is quickest. Large PDFs may take a minute or two.</div>
            </div>
          </div>
        </div>
      </div>

      {last_import_html}

      {undo_html}

      <div class="btnrow">
        <a class="btn btn-sm btn-quiet" href="/logbook">Back to Logbook</a>
        <a class="btn btn-sm btn-quiet" href="/download">Download my_visits.csv</a>
        <a class="btn btn-sm btn-quiet" href="/download/foreflight">Download ForeFlight CSV</a>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:900; margin-bottom:6px;">Log a Flight</div>
      <div class="sub">Adds to your MyAirportMap logbook immediately. You can export to ForeFlight anytime. If you upload from ForeFlight later, it may overwrite manual entries—download a backup first.</div>

      <form method="post" action="/logbook/manage/add-flight">
        <div class="grid">
          <div>
            <label>Date (MM/DD/YYYY)</label>
            <input name="Date" placeholder="12/17/1903" autocomplete="off">
          </div>
          <div>
            <label>AircraftID</label>
            <input name="AircraftID" placeholder="N123AB">
          </div>
          <div>
            <label>From</label>
            <input name="From" placeholder="KCDW">
          </div>
          <div>
            <label>To</label>
            <input name="To" placeholder="KMMU" required>
          </div>
          <div style="grid-column: 1 / -1;">
            <label>Pilot Comments (optional)</label>
            <textarea name="PilotComments" placeholder="Optional notes…"></textarea>
          </div>

          <div>
            <button class="btn btn-sm" type="submit">Log Flight</button>
          </div>
        </div>
      </form>
    </div>

    <div class="card visits-card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; gap:10px; flex-wrap:wrap;">
        <div style="font-weight:900;">Existing Visits</div>
        <div style="display:flex; gap:10px; flex-wrap:wrap;">
          <a class="btn btn-sm btn-quiet" href="/download" style="text-decoration:none; display:inline-block;">Download my_visits.csv</a>
          <a class="btn btn-sm btn-quiet" href="/download/foreflight" style="text-decoration:none; display:inline-block;">Download ForeFlight CSV</a>
        </div>
      </div>

      <table>
        <thead>
          <tr><th>Date</th><th>Airport</th><th>Aircraft</th><th>Notes</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {table_body}
        </tbody>
      </table>
    </div>
  </div>

  <div class="modal" id="modal">
    <div class="inner">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <div style="font-weight:900;">Edit Visit</div>
        <button class="btn" type="button" onclick="closeEdit()">Close</button>
      </div>
      <form method="post" action="/logbook/manage/edit">
        <input type="hidden" name="row_index" id="edit_row_index">
        <div class="row3">
          <div>
            <label>Date</label>
            <input name="date_visited" id="edit_date">
          </div>
          <div>
            <label>Airport (identifier)</label>
            <input name="airport_id" id="edit_airport_id" placeholder="KTEB">
          </div>
          <div>
            <label>Aircraft / Callsign</label>
            <input name="callsign" id="edit_callsign">
          </div>
        </div>
        <div style="margin-top:10px;">
          <label>Notes</label>
          <textarea name="notes" id="edit_notes"></textarea>
        </div>
        <div style="margin-top:10px; display:flex; justify-content:flex-end;">
          <button class="btn" type="submit">Save Changes</button>
        </div>
      </form>
    </div>
  </div>

<script>
  const VISITS = {visits_json};

  function openEdit(i) {{
    const v = VISITS[i] || {{}};
    document.getElementById("edit_row_index").value = i;
    document.getElementById("edit_date").value = v.date_visited || "";
    document.getElementById("edit_airport_id").value = v.airport_id || "";
    document.getElementById("edit_callsign").value = v.callsign || "";
    document.getElementById("edit_notes").value = v.notes || "";
    document.getElementById("modal").style.display = "flex";
  }}

  function closeEdit() {{
    document.getElementById("modal").style.display = "none";
  }}

  function copyText(t) {{
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(t).then(() => {{
      const el = document.getElementById("copyMsg");
      if (el) {{
        el.style.display = "block";
        setTimeout(() => {{ el.style.display = "none"; }}, 900);
      }}
    }});
  }}
</script>

<script>
(function () {{
  const form = document.getElementById("logbookUploadForm");
  if (!form) return;

  const overlay = document.getElementById("uploadOverlay");
  const submitBtn = form.querySelector('button[type="submit"]');
  const fileInput = form.querySelector('input[type="file"][name="file"]');

  let submitting = false;

  form.addEventListener("submit", function (e) {{
    if (submitting) {{
      e.preventDefault();
      return false;
    }}

    // basic required file guard (browser should already enforce)
    if (fileInput && !fileInput.value) {{
      return; // let native required message happen
    }}

    submitting = true;
    if (submitBtn) submitBtn.disabled = true;
    if (overlay) overlay.classList.add("show");

    // If something goes wrong and the page doesn't navigate, fail open after 45s
    window.setTimeout(() => {{
      submitting = false;
      if (submitBtn) submitBtn.disabled = false;
      if (overlay) overlay.classList.remove("show");
    }}, 45000);
  }});
}})();
</script>

<div style="
  margin-top:22px;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.10);
  padding: 14px 14px;
  border-radius: 14px;
  color:#cbd5e1;
  font-size: 13px;
  line-height: 1.45;">
  <div style="font-weight:900; color:#fff; margin-bottom:6px;">Important</div>
  MyAirportMap is a post-flight logbook companion. Do not use this Service while operating an aircraft.
  MyAirportMap is not affiliated with, endorsed by, or sponsored by, any other third-party aviation software provider.
  Upload only data you own or have the legal right to use.
</div>

</body>
</html>
"""

# -----------------------------
# Flask
# -----------------------------
from flask import Flask
from flask import request, jsonify

from werkzeug.middleware.proxy_fix import ProxyFix

print("BOOT: app.py loaded OK")
print("[R2_BOOT]",
      "enabled=", storage_backend._r2_enabled(),
      "bucket_set=", bool((os.getenv("R2_BUCKET_NAME") or "").strip()),
      "endpoint_set=", bool((os.getenv("R2_ENDPOINT_URL") or "").strip()),
      "key_set=", bool((os.getenv("R2_ACCESS_KEY_ID") or "").strip()),
      "secret_set=", bool((os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()))

# ============================================================
# Auth token verification helpers - uses python-jose
# ============================================================
import os
import time
import json
import requests
from functools import wraps
from flask import request, jsonify, redirect, Response
from jose import jwk
from jose import jwt
from jose.exceptions import JWTError

# Your first-party auth cookie name (local auth tokens)

# JWKS cache (6 hours)
_JWKS_CACHE = {"keys": None, "ts": 0}

def _get_jwks(force_refresh: bool = False) -> dict:
    url = (os.getenv("CLERK_JWKS_URL") or "").strip()
    if not url:
        return {"keys": []}

    now = int(time.time())
    if (not force_refresh) and _JWKS_CACHE["keys"] and (now - _JWKS_CACHE["ts"] < 21600):
        return _JWKS_CACHE["keys"]

    try:
        jwks = requests.get(url, timeout=10).json()
    except Exception:
        # fall back to cache if present
        if _JWKS_CACHE["keys"]:
            return _JWKS_CACHE["keys"]
        return {"keys": []}

    _JWKS_CACHE["keys"] = jwks
    _JWKS_CACHE["ts"] = now
    return jwks

def _clean_token(tok: str) -> str:
    tok = (tok or "").strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        tok = tok[1:-1].strip()
    return tok

def _get_token_from_request(req) -> str | None:
    # 1) Authorization header (Bearer)
    try:
        ah = (req.headers.get("Authorization") or "").strip()
        if ah.lower().startswith("bearer "):
            tok = ah.split(" ", 1)[1].strip()
            if tok:
                return tok
    except Exception:
        pass

    # 2) Cookie (new canonical)
    try:
        tok = (req.cookies.get(APP_SESSION_COOKIE) or "").strip()
        if tok:
            return tok
    except Exception:
        pass

    return None


def verify_clerk_session(req):
    tok = _get_token_from_request(req)
    if not tok:
        return None

    # Local auth token verification only.
    try:
        payload = decode_access_token(tok)
        if payload and (payload.get("sub") or "").strip():
            return payload
    except Exception:
        return None

def get_clerk_user_id_from_claims(claims: dict) -> str | None:
    return claims.get("sub")  # Clerk user id
    

def _verify_clerk_token_string(token: str):
    """Verify a raw token string (not pulled from request)."""
    class _FakeReq:
        headers = {}
        cookies = {APP_SESSION_COOKIE: token}

    return verify_clerk_session(_FakeReq())

def clerk_sign_in_url(next_path: str = "/logbook") -> str:
    """
    Return the local sign-in URL as a string (NOT a Flask redirect Response).
    Use this for <a href="..."> links.
    """
    nxt = (next_path or "/logbook").strip() or "/logbook"
    if (not nxt.startswith("/")) or nxt.startswith("//"):
        nxt = "/logbook"

    return f"/sign-in?next={quote(nxt, safe='/=?&')}"

# ------------------------------------------------------------
# Auth + cookie separation (CRITICAL)
# ------------------------------------------------------------
import os
import time
from urllib.parse import urlencode
from flask import jsonify, request, redirect

# Flask secret key (for server-side session signing)
app.secret_key = (
    os.environ.get("FLASK_SECRET_KEY")
    or os.environ.get("SECRET_KEY")
    or "dev-change-me"
)

# ✅ Separate cookies: Flask session != Auth token
# Flask defaults to cookie name "session" — do NOT reuse it for auth.
app.config.update(
    SESSION_COOKIE_NAME="mam_web",   # Flask session cookie name
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

# ✅ Auth token cookie name used by verify_clerk_session()
APP_SESSION_COOKIE = "mam_auth"

# ------------------------------------------------------------
# Auth decorator: back-compat alias
# ------------------------------------------------------------
# Back-compat alias (older routes used @require_auth)
require_auth = login_required


# ------------------------------------------------------------
# Reverse proxy headers (https, host)
# ------------------------------------------------------------
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)


@app.route("/billing/stripe-webhook", methods=["POST"])
def billing_stripe_webhook():
    sk = (STRIPE_SECRET_KEY or "").strip()
    wh = (STRIPE_WEBHOOK_SECRET or "").strip()
    if not sk or not wh:
        return Response("Stripe is not configured.", status=500)

    if sk.startswith("whsec_"):
        return Response(
            "Stripe misconfigured: STRIPE_SECRET_KEY is a webhook secret (whsec_...). "
            "Set STRIPE_SECRET_KEY to an sk_live_... key.",
            status=500,
        )

    stripe.api_key = sk
    price_id = (STRIPE_PRICE_ID_ANNUAL or STRIPE_PRICE_ID or "").strip() or None

    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=wh,  # use stripped value
        )
    except Exception as e:
        return Response("Webhook signature verification failed: " + repr(e), status=400)

    event_id = (event.get("id") or "").strip()
    if event_id and _event_seen(event_id):
        return Response("OK (duplicate)", status=200)

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    def _handle_from_metadata(o: dict) -> str:
        md = (o.get("metadata") or {}) if isinstance(o, dict) else {}
        return (md.get("handle") or "").strip()

    processed_ok = False

    try:
        # 1) Initial checkout completion (best for first unlock)
        if etype == "checkout.session.completed":
            handle = _handle_from_metadata(obj)

            # Fallback: client_reference_id = Clerk sub
            clerk_sub = (obj.get("client_reference_id") or "").strip()
            if not handle and clerk_sub:
                handle = (get_handle_for_user(clerk_sub) or "").strip()

            stripe_customer_id = (
                obj.get("customer").strip()
                if isinstance(obj.get("customer"), str)
                else None
            )
            stripe_subscription_id = (
                obj.get("subscription").strip()
                if isinstance(obj.get("subscription"), str)
                else None
            )

            if handle:
                _mark_paid_stripe(
                    handle,
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_subscription_id,
                    stripe_price_id=price_id,
                )
                processed_ok = True
            else:
                # Ack + mark seen so Stripe doesn't retry forever
                if event_id:
                    _event_mark(event_id)
                return Response("OK (missing handle)", status=200)

        # 2) Invoice paid (best for renewals and paid_through)
        elif etype == "invoice.paid":
            stripe_customer_id = (
                obj.get("customer").strip()
                if isinstance(obj.get("customer"), str)
                else None
            )
            stripe_subscription_id = (
                obj.get("subscription").strip()
                if isinstance(obj.get("subscription"), str)
                else None
            )

            # 1) Try handle from invoice metadata (often empty)
            handle = (obj.get("metadata") or {}).get("handle", "").strip()

            # 2) Fallback: retrieve subscription metadata
            if not handle and stripe_subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    md = (sub.get("metadata") or {}) if isinstance(sub, dict) else {}
                    handle = (md.get("handle") or "").strip()
                except Exception:
                    pass

            paid_through = _invoice_paid_through_iso(obj)

            if handle:
                _mark_paid_stripe(
                    handle,
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_subscription_id,
                    stripe_price_id=price_id,
                    paid_through_iso=paid_through,
                )

            processed_ok = True

        # 3) Subscription canceled
        elif etype == "customer.subscription.deleted":
            handle = _handle_from_metadata(obj)

            stripe_customer_id = (
                obj.get("customer").strip()
                if isinstance(obj.get("customer"), str)
                else None
            )
            stripe_subscription_id = (
                obj.get("id").strip()
                if isinstance(obj.get("id"), str)
                else None
            )

            if not handle and stripe_subscription_id:
                try:
                    md = (obj.get("metadata") or {}) if isinstance(obj, dict) else {}
                    handle = (md.get("handle") or "").strip()
                    if not handle:
                        sub = stripe.Subscription.retrieve(stripe_subscription_id)
                        md2 = (sub.get("metadata") or {}) if isinstance(sub, dict) else {}
                        handle = (md2.get("handle") or "").strip()
                except Exception:
                    pass

            if not handle:
                handle = _handle_from_customer_or_sub(stripe_customer_id, stripe_subscription_id)

            if handle:
                _mark_unpaid(handle, reason="subscription_deleted")

            processed_ok = True

        # 4) Payment failed
        elif etype == "invoice.payment_failed":
            stripe_customer_id = (
                obj.get("customer").strip()
                if isinstance(obj.get("customer"), str)
                else None
            )
            stripe_subscription_id = (
                obj.get("subscription").strip()
                if isinstance(obj.get("subscription"), str)
                else None
            )

            handle = _handle_from_metadata(obj)

            if not handle and stripe_subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    md = (sub.get("metadata") or {}) if isinstance(sub, dict) else {}
                    handle = (md.get("handle") or "").strip()
                except Exception:
                    pass

            if not handle:
                handle = _handle_from_customer_or_sub(stripe_customer_id, stripe_subscription_id)

            if handle:
                _entitlements_update(handle, {
                    "unpaid_reason": "invoice_payment_failed",
                    "updated_at": _now_utc().isoformat().replace("+00:00", "Z"),
                })

            processed_ok = True

        else:
            processed_ok = True

    finally:
        if event_id and processed_ok:
            _event_mark(event_id)

    return Response("OK", status=200)


@app.route("/admin/rebuild-stripe-indexes", methods=["POST"])
@login_required
def admin_rebuild_stripe_indexes():
    # Optional: lock this to you only if desired
    # if current_user.handle != "yourhandle":
    #     return Response("Forbidden", status=403)

    stats = rebuild_stripe_indexes()
    return jsonify({"ok": True, **stats})

from urllib.parse import urlencode

from flask import request, jsonify

@app.route("/api/airports", methods=["GET"])
def api_airports():
    """
    Returns GeoJSON for airports in viewport bbox.

    Query:
      bbox=west,south,east,north   (required)
      unvisited=1                 (optional; exclude visited for handle)
      handle=<handle>             (optional; used only if unvisited=1)
      zoom=<int>                  (optional; Leaflet zoom)
      limit=<int>                 (optional; hard cap when zoomed out)

    Map38 rules:
      ✅ Airports come ONLY from canonical load_airports_cached()
      ✅ Visits are only read when unvisited=1 and handle is provided
      ✅ Unvisited is complete at closer zooms (no sampling), capped only when zoomed out
    """
    import hashlib
    import math

    # -----------------------------
    # Parse inputs
    # -----------------------------
    bbox = (request.args.get("bbox") or "").strip()
    if not bbox:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "no_bbox"}})

    try:
        west, south, east, north = [float(x) for x in bbox.split(",")]
    except Exception:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "bad_bbox"}})

    # Dateline not supported (fine for CONUS)
    if west > east:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "dateline"}})

    unvisited = (request.args.get("unvisited") == "1")
    handle = (request.args.get("handle") or "").strip() or None

    # zoom is optional
    z_raw = (request.args.get("zoom") or "").strip()
    zoom = None
    try:
        if z_raw:
            zoom = int(float(z_raw))
    except Exception:
        zoom = None

    # user-provided cap (used only when zoomed out; otherwise ignored)
    try:
        limit = int(request.args.get("limit") or "2500")
        limit = max(50, min(limit, 5000))
    except Exception:
        limit = 2500

    # -----------------------------
    # Defensive: total must always exist
    # -----------------------------
    total = 0
   
    # -----------------------------
    # Zoom policy for UNVISITED overlay
    # -----------------------------
    # - zoom >= 7: return ALL in bbox (no sampling)
    # - zoom == 6: cap to 2000
    # - zoom <= 5: cap to 1200
    effective_limit = limit
    full_mode = False
    if unvisited and (zoom is not None):
        if zoom >= 7:
            full_mode = True
            effective_limit = 5000  # > entire universe (~3270), effectively "no cap"
        elif zoom >= 6:
            effective_limit = min(effective_limit, 2000)
        else:
            effective_limit = min(effective_limit, 1200)

    # -----------------------------
    # ✅ Canonical airports ONLY
    # -----------------------------
    try:
        df_airports = load_airports_cached()
    except Exception as e:
        return jsonify({
            "type": "FeatureCollection",
            "features": [],
            "meta": {"reason": "airports_load_failed", "err": str(e)}
        })

    # Ensure norm_id exists (for visited filtering)
    if "norm_id" not in df_airports.columns:
        try:
            df_airports = df_airports.copy()
            df_airports["norm_id"] = df_airports["airport_id"].astype(str).apply(normalize_id)
        except Exception:
            pass

    # -----------------------------
    # BBox filter (canonical lat/long)
    # -----------------------------
    df = df_airports[
        (pd.to_numeric(df_airports["lat"], errors="coerce") >= south) &
        (pd.to_numeric(df_airports["lat"], errors="coerce") <= north) &
        (pd.to_numeric(df_airports["long"], errors="coerce") >= west) &
        (pd.to_numeric(df_airports["long"], errors="coerce") <= east)
    ].copy()

    # ✅ Total in bbox (before any unvisited filtering / sampling)
    total = int(len(df))

    # -----------------------------
    # Exclude visited if requested (cached visited set)
    # -----------------------------
    if unvisited and handle:
        try:
            visited = get_visited_norm_ids(handle)
            if "norm_id" in df.columns and visited:
                df = df[~df["norm_id"].astype(str).isin(visited)]
        except Exception:
            pass

        # ✅ Total after unvisited filtering (this is the population for sampling/cap)
        total = int(len(df))

    # -----------------------------
    # LOD sampling (only when zoomed out AND over cap)
    # -----------------------------
    # Goal: show "some" airports at low zoom (so toggle doesn't feel broken),
    # while keeping points spatially even and stable (no random flicker).
    #
    # Approach: deterministic "grid bucket" sampling keyed to zoom.
    #
    # We only apply this if NOT full_mode and total > effective_limit.
    sampled = False
    cell_miles = None
    capped = False

    def _cell_miles_for_zoom(z: int | None) -> float | None:
        # Tune these values if needed; conservative defaults.
        if z is None:
            return 120.0
        if z <= 4:
            return 140.0
        if z == 5:
            return 80.0
        if z == 6:
            return 40.0
        # z>=7 => full_mode already handles 7+, but keep consistent:
        return None

    def _grid_sample(df_in: pd.DataFrame, mid_lat: float, cell_miles_in: float) -> pd.DataFrame:
        df0 = df_in.copy()

        # numeric once
        df0["__lat"] = pd.to_numeric(df0["lat"], errors="coerce")
        df0["__lon"] = pd.to_numeric(df0["long"], errors="coerce")
        df0 = df0.dropna(subset=["__lat", "__lon"])
        if df0.empty:
            return df0

        lat_step = cell_miles_in / 69.0
        coslat = max(0.2, math.cos(math.radians(mid_lat)))
        lon_step = cell_miles_in / (69.0 * coslat)

        lat_bin = (df0["__lat"] / lat_step).astype(int)
        lon_bin = (df0["__lon"] / lon_step).astype(int)
        df0["__bin"] = lat_bin.astype(str) + ":" + lon_bin.astype(str)

        # deterministic pick per bin
        if "airport_id" in df0.columns:
            df0 = df0.sort_values(["__bin", "airport_id"], kind="mergesort")
        else:
            df0 = df0.sort_values(["__bin", "name"], kind="mergesort")

        df0 = df0.drop_duplicates("__bin", keep="first")
        return df0.drop(columns=["__lat", "__lon", "__bin"])

    if (not full_mode) and (total > effective_limit):
        # Apply spatial LOD sampling first (stable)
        cell_miles = _cell_miles_for_zoom(zoom)
        if cell_miles is not None:
            sampled = True
            mid_lat = (south + north) / 2.0
            df = _grid_sample(df, mid_lat=mid_lat, cell_miles_in=cell_miles)

        # After grid sampling, still enforce hard cap as a backstop
        if len(df) > effective_limit:
            capped = True
            # deterministic trim: stable order
            if "airport_id" in df.columns:
                df = df.sort_values(["airport_id"], kind="mergesort").head(effective_limit).copy()
            else:
                df = df.sort_values(["name"], kind="mergesort").head(effective_limit).copy()

    total_after = int(len(df))

    # -----------------------------
    # Build GeoJSON (properties match current JS: expects boolean 'towered')
    # -----------------------------
    features = []
    for _, r in df.iterrows():
        try:
            lon = float(r["long"])
            lat = float(r["lat"])
        except Exception:
            continue

        status = str(r.get("towered_status") or "")
        is_towered = status.lower().startswith("towered")

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "airport_id": str(r.get("airport_id") or ""),
                "name": str(r.get("name") or ""),
                "state": str(r.get("state") or ""),
                "towered": bool(is_towered),
                "towered_status": status,
            }
        })

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "total_in_bbox": total,
            "total_after_sampling": total_after,
            "returned": len(features),
            "limit": effective_limit,
            "zoom": zoom,

            # Back-compat: keep existing key name
            "unvisited": bool(unvisited and handle),

            # ✅ added clarity
            "unvisited_requested": bool(unvisited),
            "unvisited_applied": bool(unvisited and handle),
            "handle_present": bool(handle),

            "source": "canonical_airports_csv",
            "sampled": bool(sampled),
            "cell_miles": cell_miles,
            "capped": bool(capped),
            "full_mode": bool(full_mode),
        }
    })

@app.route("/api/visits")
def route_api_visits():
    """
    BBOX-driven visits loader for progressive map hydration.

    Query:
      - bbox=west,south,east,north  (required)
      - handle=<handle>            (required)
      - mode=first|all             (required)
      - limit=<int>                (optional, default 400)
      - cursor=<int>               (optional, default 0)

    Returns:
      { "items": [ {id, lat, lon, towered, popup_html} ... ],
        "next_cursor": <int or null>,
        "meta": {...}
      }
    """
    try:
        bbox = (request.args.get("bbox") or "").strip()
        handle = (request.args.get("handle") or "").strip()
        mode = (request.args.get("mode") or "").strip().lower()

        if not bbox or not handle or mode not in {"first", "all"}:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": "missing/invalid params"}}), 400

        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": "invalid bbox"}}), 400

        west, south, east, north = [float(x) for x in parts]

        # limit/cursor (bounded)
        try:
            limit = int(request.args.get("limit") or "400")
        except Exception:
            limit = 400
        if limit < 50:
            limit = 50
        if limit > 1200:
            limit = 1200

        try:
            cursor = int(request.args.get("cursor") or "0")
        except Exception:
            cursor = 0
        if cursor < 0:
            cursor = 0

        # Load data (your load_data is fast per your MAP_TIMING logs)
        visits_csv = resolve_visits_csv(handle)
        df_airports, df_visits = load_data(visits_csv=visits_csv, handle=handle)

        if df_visits is None or df_visits.empty or df_airports is None or df_airports.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # --- Canonical tower status (same logic as map) ---
        def _canon_towered_status(v) -> str:
            s = str(v or "").strip().lower()
            if not s or s in {"nan", "none", "null"}:
                return "Non-Towered"
            if ("non" in s and "tower" in s) or ("no tower" in s):
                return "Non-Towered"
            if s in {"no", "n", "false", "0", "uncontrolled", "ctaf", "untowered", "none"}:
                return "Non-Towered"
            if s in {"towered", "twr", "yes", "y", "true", "1", "controlled", "ct", "c"}:
                return "Towered"
            if ("tower" in s and "non" not in s) or ("twr" in s):
                return "Towered"
            return "Non-Towered"

        if "towered_status" not in df_airports.columns:
            df_airports["towered_status"] = "Non-Towered"
        df_airports["towered_status"] = df_airports["towered_status"].map(_canon_towered_status)

        # Merge visits -> airport metadata/coords (same columns as your map)
        merge_cols = ["norm_id", "airport_id", "name", "state", "lat", "long", "towered_status"]
        for c in merge_cols:
            if c not in df_airports.columns:
                # fail-closed: if missing columns, return empty rather than crash the map
                return jsonify({"items": [], "next_cursor": None, "meta": {"error": f"airports missing {c}"}})

        df_vis_plot = pd.merge(
            df_visits,
            df_airports[merge_cols],
            on="norm_id",
            how="left",
            suffixes=("", "_apt"),
        )
        df_vis_plot = df_vis_plot.dropna(subset=["lat", "long"]).copy()

        if df_vis_plot.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # Repair towered_status if merge didn't carry it
        if "towered_status" not in df_vis_plot.columns:
            df_vis_plot["towered_status"] = None
        df_vis_plot["towered_status"] = (
            df_vis_plot["towered_status"]
            .fillna("Non-Towered")
            .map(_canon_towered_status)
        )

        # Filter to bbox (load what they see)
        df_vis_plot["lat"] = pd.to_numeric(df_vis_plot["lat"], errors="coerce")
        df_vis_plot["long"] = pd.to_numeric(df_vis_plot["long"], errors="coerce")
        df_vis_plot = df_vis_plot.dropna(subset=["lat", "long"]).copy()

        df_vis_plot = df_vis_plot[
            (df_vis_plot["lat"] >= south) & (df_vis_plot["lat"] <= north) &
            (df_vis_plot["long"] >= west) & (df_vis_plot["long"] <= east)
        ].copy()

        if df_vis_plot.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # First visit per airport (same semantics as your map)
        if mode == "first":
            if "date_visited" in df_vis_plot.columns:
                df_vis_plot = df_vis_plot.sort_values("date_visited")
            df_vis_plot = df_vis_plot.drop_duplicates("norm_id", keep="first")

        # Paging
        df_vis_plot = df_vis_plot.reset_index(drop=False).rename(columns={"index": "_rowid"})
        total = int(len(df_vis_plot))
        start = cursor
        end = min(cursor + limit, total)
        page = df_vis_plot.iloc[start:end].copy()


        # Popup builder (duplicated for surgical isolation)
        def create_popup_html(
            airport_id: str,
            name: str,
            state: str,
            status: str,
            visit_date: str,
            callsign: str,
            notes: str,
            is_first_visit: bool = False,
        ) -> str:
            status = (status or "").strip() or "Non-Towered"
            header_color = "#0044cc" if status == "Towered" else "#cc00cc"
            title = "First Visit" if is_first_visit else "Visit Details"

            safe_airport_id = _html.escape(airport_id or "")
            safe_name = _html.escape(name or "")
            safe_state = _html.escape(state or "")
            safe_status = _html.escape(status or "")
            safe_visit_date = _html.escape(visit_date or "")
            safe_callsign = _html.escape(callsign or "")
            raw_notes = (notes or "").strip()
            safe_notes = (linkify_text(raw_notes) or "").replace("\n", "<br>")


            out = f"""
            <div style="font-family:sans-serif; min-width:180px;">
              <div style="background:{header_color}; color:white; padding:8px; font-weight:bold;">
                {safe_airport_id} <span style="font-weight:normal;">({safe_state})</span>
              </div>
              <div style="padding:10px; color:#333;">
                <div style="font-weight:bold;">{safe_name}</div>
                <div style="font-size:11px; color:#666;">{safe_status}</div>
                <hr style="margin:8px 0; border-top:1px solid #eee;">
                <div style="font-size:10px; color:#888;">{title}</div>
                <div style="display:flex; justify-content:space-between;"><span>Date:</span><b>{safe_visit_date}</b></div>
                <div style="display:flex; justify-content:space-between;"><span>Aircraft:</span><b>{safe_callsign}</b></div>
            """
            if raw_notes and raw_notes.lower() not in {"nan", "none"}:
                out += f"""
                <div style="margin-top:8px; background:#f9f9f9; padding:5px; font-style:italic;">
                  {safe_notes}
                </div>
                """

            out += "</div></div>"
            return out

        items = []
        is_first = (mode == "first")

        for _, vr in page.iterrows():
            status = str(vr.get("towered_status") or "Non-Towered")
            towered = True if status == "Towered" else False

            disp_id = str(vr.get("airport_id", "") or "")
            popup_html = create_popup_html(
                airport_id=disp_id,
                name=str(vr.get("name", "") or ""),
                state=str(vr.get("state", "") or ""),
                status=status,
                visit_date=str(vr.get("date_visited", "") or ""),
                callsign=str(vr.get("callsign", "") or ""),
                notes=str(vr.get("notes", "") or ""),
                is_first_visit=is_first,
            )

            # stable-ish id for client-side de-dupe
            if is_first:
                vid = str(vr.get("norm_id") or disp_id or vr.get("_rowid"))
            else:
                vid = str(vr.get("_rowid"))

            items.append({
                "id": vid,
                "lat": float(vr["lat"]),
                "lon": float(vr["long"]),
                "towered": towered,
                "popup_html": popup_html,
            })

        next_cursor = end if end < total else None
        return jsonify({
            "items": items,
            "next_cursor": next_cursor,
            "meta": {
                "mode": mode,
                "count": len(items),
                "total_in_bbox": total,
                "bbox": {"w": west, "s": south, "e": east, "n": north},
            }
        })
    except Exception as e:
        # fail-closed: never crash the app
        try:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": str(e)[:200]}}), 200
        except Exception:
            return ("", 200)

# --- Auth entrypoints (single source of truth) ---

@app.route("/home")
def home_redirect():
    return redirect("/login", code=302)


def _debug_trial_state(handle: str) -> None:
    try:
        e = _read_entitlements(handle) or {}
        print("trial_state:",
              handle,
              "is_paid=", bool(e.get("is_paid")),
              "trial_started_at=", e.get("trial_started_at"),
              "trial_expires_at=", e.get("trial_expires_at"))
    except Exception as ex:
        print("trial_state debug failed:", repr(ex))


@app.route("/trial/ended")
def trial_ended():
    nxt = (request.args.get("next") or "/app").strip() or "/app"
    if not nxt.startswith("/"):
        nxt = "/app"
    return redirect("/upgrade?next=" + quote(nxt, safe="/=?&"))

@app.route("/achievements/locked")
def achievements_locked():
    next_path = (request.args.get("next") or "/achievements").strip() or "/achievements"
    if not next_path.startswith("/"):
        next_path = "/achievements"

    up = "/upgrade?next=" + quote(next_path, safe="/=?&")
    cur = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path  # (kept, unused)

    html_out = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Achievements Locked</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background: #0f1115;
      color: #fff;
      margin: 0;
      font-size: 16px;
      line-height: 1.55;
    }}

    .wrap {{
      max-width: 820px;
      margin: 32px auto;
      padding: 0 16px;
    }}

    .card {{
      background: #171a21;
      border: 1px solid #2a2f3a;
      border-radius: 16px;
      padding: 18px;
    }}

    h1 {{
      font-size: 26px;
      margin: 0 0 10px;
      line-height: 1.2;
    }}

    .muted {{
      color: #aab2c0;
      font-size: 16px;
      line-height: 1.55;
    }}

    .row {{
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 12px;
    }}

    .btn {{
      display: inline-block;
      padding: 14px 16px;
      border-radius: 14px;
      text-decoration: none;
      font-weight: 800;
      font-size: 15px;
    }}

    .primary {{
      background: #2b7cff;
      color: #fff;
    }}

    .ghost {{
      background: #111;
      color: #fff;
    }}

    @media (max-width: 640px) {{
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Achievements are a membership feature</h1>
      <p class="muted">Earn and display achievements as your flying history grows.</p>
      <div class="row">
        <a class="btn primary" href="{up}">Upgrade to unlock</a>
        <a class="btn ghost" href="/logbook/manage#upload">Upload logbook</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

    return Response(html_out, mimetype="text/html")


# ============================================================
# LOGBOOK HELPERS (ported from Map6; minimal changes)
# ============================================================

# ForeFlight CSV import "sentinel" (first row). ForeFlight includes many trailing commas;
# we validate the first two fields rather than strict string equality so we don't break
# when ForeFlight adjusts column count/formatting.
FORE_FLIGHT_SENTINEL_A = "ForeFlight Logbook Import"
FORE_FLIGHT_SENTINEL_B = "This row is required for importing into ForeFlight. Do not delete or modify."
FORE_FLIGHT_SENTINEL_ROW = f"{FORE_FLIGHT_SENTINEL_A}\t{FORE_FLIGHT_SENTINEL_B}"


FORE_FLIGHT_COLUMNS = [
    "Date","AircraftID","From","To","Route","TimeOut","TimeOff","TimeOn","TimeIn",
    "OnDuty","OffDuty","TotalTime","PIC","SIC","Night","Solo","CrossCountry","PICUS",
    "MultiPilot","IFR","Examiner","NVG","NVG Ops","Distance","ActualInstrument",
    "SimulatedInstrument","HobbsStart","HobbsEnd","TachStart","TachEnd","Holds",
    "Approach1","Approach2","Approach3","Approach4","Approach5","Approach6",
    "DualGiven","DualReceived","SimulatedFlight","GroundTraining","GroundTrainingGiven",
    "InstructorName","InstructorComments","Person1","Person2","Person3","Person4",
    "Person5","Person6","PilotComments","Flight Review (FAA)","IPC (FAA)","Checkride (FAA)",
    "FAA 61.58 (FAA)","NVG Proficiency (FAA)","Takeoff Day","Takeoff Night",
    "Landing Full-Stop Day","Landing Full-Stop Night","DayTakeoffs","DayLandingsFullStop",
    "NightTakeoffs","NightLandingsFullStop","AllLandings"
]

MAP24_EDITABLE = {"Date","AircraftID","From","To","Route","PilotComments"}

def _norm(v) -> str:
    return "" if v is None else str(v).strip()

def write_foreflight_import_csv_bytes(rows, *, preserve_extra_columns=True, extra_columns=None) -> bytes:
    # Header = canonical + any extra columns (future-proof and preserves user-uploaded extras)
    header = list(FORE_FLIGHT_COLUMNS)
    if preserve_extra_columns:
        seen = set(header)
        if extra_columns:
            for c in extra_columns:
                c = str(c)
                if c not in seen:
                    header.append(c); seen.add(c)
        for r in rows:
            for c in r.keys():
                c = str(c)
                if c not in seen:
                    header.append(c); seen.add(c)

    out = io.StringIO(newline="")
    # Row 1: Sentinel (TAB separated, exact)
    out.write(FORE_FLIGHT_SENTINEL_ROW + "\n")

    writer = csv.DictWriter(out, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()

    for r in rows:
        row_out = {c: "" for c in header}
        for k, v in r.items():
            row_out[str(k)] = _norm(v)
        writer.writerow(row_out)

    return out.getvalue().encode("utf-8")

def read_foreflight_import_csv_bytes(data: bytes):
    """
    Returns: (sentinel_line, header_cols, rows)

    Upload rules (CSV):
    - The ForeFlight sentinel row may appear anywhere near the top (not always line 1).
    - For ForeFlight "full export" files that contain multiple tables (Aircraft Table, Flights Table, etc.),
      we must parse the *Flights Table* section (not the Aircraft Table).
    - For non-ForeFlight CSVs (no sentinel), parse as a normal CSV with the first non-empty row as header.

    Notes:
    - We validate the sentinel by checking the first two fields only (comma OR tab).
    - We exclude the sentinel row (and any table label rows like "Flights Table") from DictReader parsing.
    """
    text = data.decode("utf-8", errors="replace")
    raw_lines = text.splitlines()
    if not raw_lines:
        raise ValueError("Empty CSV")

    def _sentinel_ok(line: str) -> bool:
        line = (line or "").lstrip("\ufeff").strip("\r\n")
        if not line.strip():
            return False
        for delim in [",", "\t"]:
            try:
                fields = next(csv.reader([line], delimiter=delim))
            except Exception:
                continue
            if (
                len(fields) >= 2
                and fields[0].strip() == FORE_FLIGHT_SENTINEL_A
                and fields[1].strip() == FORE_FLIGHT_SENTINEL_B
            ):
                return True
        return False

    def _is_table_label(line: str, name: str) -> bool:
        """
        ForeFlight table labels look like: 'Flights Table , , , , ...'
        Accept minor variations in spacing/case/punctuation.
        """
        s = (line or "").lstrip("\ufeff").strip()
        if not s:
            return False
        # Normalize: collapse spaces, strip trailing commas
        s_norm = re.sub(r"\s+", " ", s).strip()
        # First token before comma is the label
        head = s_norm.split(",")[0].strip().lower()
        return head == name.lower()

    # Find first non-empty line
    first_nonempty = None
    for i, ln in enumerate(raw_lines):
        if (ln or "").strip():
            first_nonempty = i
            break
    if first_nonempty is None:
        raise ValueError("Empty CSV")

    # 1) Locate sentinel row in a small window near the top.
    sentinel_idx = None
    scan_limit = min(len(raw_lines), first_nonempty + 50)
    for i in range(first_nonempty, scan_limit):
        if _sentinel_ok(raw_lines[i]):
            sentinel_idx = i
            break

    sentinel = ""
    data_start_idx = first_nonempty
    if sentinel_idx is not None:
        sentinel = raw_lines[sentinel_idx]
        data_start_idx = sentinel_idx + 1

    # 2) If this is a ForeFlight export (sentinel present), prefer the Flights Table section.
    # Scan forward for a "Flights Table" label; header is the next non-empty line after that label.
    if sentinel:
        flights_label_idx = None
        for i in range(data_start_idx, len(raw_lines)):
            if _is_table_label(raw_lines[i], "Flights Table") or _is_table_label(raw_lines[i], "Flight Table"):
                flights_label_idx = i
                break
        if flights_label_idx is not None:
            data_start_idx = flights_label_idx + 1

    # 3) Skip blank lines to find header row.
    header_idx = None
    for i in range(data_start_idx, len(raw_lines)):
        if (raw_lines[i] or "").strip():
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Missing CSV header row")

    # If header row is itself a table label, skip it.
    if _is_table_label(raw_lines[header_idx], "Flights Table") or _is_table_label(raw_lines[header_idx], "Flight Table"):
        for j in range(header_idx + 1, len(raw_lines)):
            if (raw_lines[j] or "").strip():
                header_idx = j
                break

    # Feed DictReader only the header + data lines.
    remaining = "\n".join(raw_lines[header_idx:])
    f = io.StringIO(remaining)

    # Prefer comma delimiter; ForeFlight flight table is comma CSV.
    reader = csv.DictReader(f)
    if not reader.fieldnames:
        raise ValueError("Missing CSV header row")

    header = list(reader.fieldnames)
    rows = []
    for row in reader:
        rows.append({k: (v if v is not None else "") for k, v in row.items()})

    return sentinel, header, rows


# Noise filter for route parsing (fixes / keeps Map6 behavior)
IGNORED_WAYPOINTS = {
    'LOCAL', 'VFR', 'IFR', 'TOUCH', 'GO', 'ROUND', 'PRACTICE', 'TOUR', 'SIGHT',
    'SEEING', 'ABORT', 'PATTERN', 'TOTAL', 'PIC', 'SIC', 'SOLO', 'XC', 'NIGHT',
    'LANDINGS', 'APPROACH', 'STOP', 'FULL', 'TAXI', 'BACK', 'HOLD',  "IFR","VFR","TOUCH","GO","ARPT","AIRPORT","TRAINING",
    "DCT","DIRECT", "RWY", "RUNWAY",
    'DIXIE',  # non-airport fix you don't want counted
    'SBJ'     # nav fix, not an airport
}

# Compatibility alias used by Map6-style logbook code
VISITS_PATH = VISITS_CSV

def _debug_log_csv_state(handle: str, label: str) -> None:
    try:
        ff = _read_foreflight_bytes(resolve_foreflight_csv(handle), handle=handle)
        visits = _read_visits_bytes(resolve_visits_csv(handle), handle=handle)

        ff_lines = ff.decode("utf-8", errors="ignore").splitlines()[:3] if ff else []
        visits_lines = visits.decode("utf-8", errors="ignore").splitlines()[:3] if visits else []

        print(f"[DEBUG:{label}] handle={handle}")
        print("  foreflight:", ff_lines)
        print("  visits:", visits_lines)
    except Exception as e:
        print(f"[DEBUG:{label}] error:", e)

def clean_route_points(route_str: str) -> list[str]:
    """Extract airport-like tokens from a ForeFlight 'Route' string.

    Map36 behavior:
    - Split on ANY non-alnum.
    - Run every token through _token_to_valid_airport(), which is *fail-closed* to our airport-id set.
      (This removes IFR waypoint noise like FLOAT/TINNI/etc.)
    """
    if not route_str:
        return []

    clean = re.sub(r"[^A-Z0-9]", " ", str(route_str).upper())
    parts = [p for p in clean.split() if p]

    out: list[str] = []
    for p in parts:
        apt = _token_to_valid_airport(p)
        if apt:
            out.append(apt)
    return out

def _is_flight_table_page(text: str) -> bool:
    t = (text or "").upper()
    return ("TYPE OF PILOTING TIME" in t and "DATE" in t and "AIRCRAFT" in t and "ROUTE" in t)

def _is_remarks_page(text: str) -> bool:
    t = (text or "").upper()
    return ("ADDITIONAL COMMENTS AND REMARKS" in t) or ("ADDITIONAL COMMENTS" in t and "REMARKS" in t)

def _parse_remarks_page(text: str) -> list:
    """
    Returns list of dicts: [{"time": float|None, "note": str}, ...]
    ForeFlight often formats remarks like:
      1.3 Pilot: ...
      Pilot: continuation...
    """
    notes = []
    cur = None
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = re.match(r"^(\d+(?:\.\d+)?)\s+PILOT:\s*(.*)$", ln, flags=re.I)
        if m:
            cur = {"time": float(m.group(1)), "note": m.group(2).strip()}
            notes.append(cur)
            continue
        m2 = re.match(r"^PILOT:\s*(.*)$", ln, flags=re.I)
        if m2:
            # continuation or time-less note
            if cur is None:
                cur = {"time": None, "note": m2.group(1).strip()}
                notes.append(cur)
            else:
                tail = m2.group(1).strip()
                if tail:
                    cur["note"] = (cur["note"] + " " + tail).strip()
            continue
        # If we are in a note, treat unlabelled lines as continuation (wrapped text)
        if cur is not None:
            # stop if it looks like a column header line
            if ln.lower().startswith("actual ") or ln.lower().startswith("sim "):
                continue
            cur["note"] = (cur["note"] + " " + ln).strip()
    # clean: enforce "first LETTER after time numbers" rule
    cleaned = []
    for n in notes:
        txt = (n.get("note") or "").strip()
        if not txt:
            continue
        # drop leading non-letters
        txt2 = re.sub(r"^[^A-Za-z]+", "", txt).strip()
        cleaned.append({"time": n.get("time"), "note": txt2 or txt})
    return cleaned

def _extract_route_and_total(rest: str) -> tuple:
    """
    From the remainder after (TYPE), split into route text + total time.
    total_time is the first float-like token encountered.
    """
    if not rest:
        return "", None
    tokens = rest.split()
    route_tokens = []
    total = None
    for tok in tokens:
        t = tok.strip()
        if _FLOAT_RE.match(t):
            total = float(t)
            break
        route_tokens.append(t)
    route = " ".join(route_tokens).strip()
    return route, total

def parse_foreflight_complete_logbook_pdf(pdf_path: str) -> pd.DataFrame:
    """
    Parses ForeFlight Complete Logbook Report (2-Page) PDF.
    Returns df with columns: airport_id, date_visited, callsign, notes
    """
    import pdfplumber  # local import
    rows = []
    # Work page-by-page, pairing flight-table page with the following remarks page if present.
    with pdfplumber.open(pdf_path) as pdf:
        prev_flights = None  # list[dict] for prior flight table page
        for pi, page in enumerate(pdf.pages):
            text = (
                page.extract_text(layout=False, x_tolerance=2, y_tolerance=2)
                or page.extract_text(layout=False)
                or ""
            )

            if _is_flight_table_page(text):
                flights = []
                current = None
                pre_lines = []  # spillover route lines before first dated row on a page
                for raw in text.splitlines():
                    ln = raw.strip()
                    if not ln:
                        continue
                    m = _DATE_RE.match(ln)
                    if m:
                        date = m.group(1)
                        tail = m.group(2).upper()
                        rest = (m.group(4) or "").strip()
                        route, total = _extract_route_and_total(rest)
                        current = {
                            "date": date,
                            "callsign": tail,
                            "route": route,
                            "total": total,
                            "note": ""
                        }
                        # Apply any pre-date spillover lines (route wraps ABOVE the date row)
                        if pre_lines:
                            spill = " ".join(pre_lines).strip()
                            if spill:
                                if not current["route"]:
                                    current["route"] = spill
                                else:
                                    current["route"] = (spill + " " + current["route"]).strip()
                            pre_lines = []
                        flights.append(current)
                        continue
                    # route spillover lines
                    if current is None:
                        if ln.upper().startswith("TOTALS "):
                            continue
                        if re.fullmatch(r"\d{1,4}", ln):
                            continue
                        if "-" in ln or re.fullmatch(r"[A-Z0-9]{2,5}", ln):
                            pre_lines.append(ln)
                        continue
                    if ln.upper().startswith("TOTALS "):
                        continue
                    if re.fullmatch(r"\d{1,4}", ln):
                        continue
                    current["route"] = (current.get("route","") + " " + ln).strip()
                prev_flights = flights
                continue
            if _is_remarks_page(text) and prev_flights:
                # NOTE: For reliability, we intentionally DO NOT import PDF remarks/notes.
                # ForeFlight's 2-page PDF often cannot be matched to the correct flight row deterministically.
                # We still emit the flights from the prior flight-table page with blank notes.
                for f in prev_flights:
                    stops = clean_route_points(f.get("route",""))
                    if not stops:
                        continue
                    for apt in sorted(set(stops)):
                        rows.append({
                            "airport_id": apt,
                            "date_visited": f.get("date",""),
                            "callsign": f.get("callsign",""),
                            "notes": ""
                        })
                prev_flights = None
                continue

        # If the file ends on a flight table page without a remarks page, still emit those flights (notes blank)
        if prev_flights:
            for f in prev_flights:
                stops = clean_route_points(f.get("route",""))
                if not stops:
                    continue
                for apt in sorted(set(stops)):
                    rows.append({
                        "airport_id": apt,
                        "date_visited": f.get("date",""),
                        "callsign": f.get("callsign",""),
                        "notes": ""
                    })

    df = pd.DataFrame(rows, columns=["airport_id","date_visited","callsign","notes"])
    # drop rows where airport_id is empty or not in known list
    if not df.empty:
        df["airport_id"] = df["airport_id"].astype(str).str.upper()
        df = df[df["airport_id"].astype(str).str.len() > 0].copy()
    return df


def detect_column(df: pd.DataFrame, keywords: List[str]) -> Optional[str]:
    """Heuristically detect a column by keywords in its name."""
    for col in df.columns:
        cl = str(col).lower().replace(" ", "").replace("_", "")
        for kw in keywords:
            if kw in cl:
                return col
    return None

def normalize_airport(code: str) -> str:
    if not code:
        return ""
    code = str(code).strip().upper()
    # US ICAO -> FAA-ish mapping for your dataset
    if len(code) == 4 and code.startswith("K") and code[1:].isalpha():
        return code[1:]
    return code

def _route_airports_with_endpoints(route: str) -> list[str]:
    """
    Return airports from route tokens, plus ensure the first and last airport-looking tokens
    are included (From/To behavior).
    """
    toks = clean_route_points(route)  # MUST be regex-scrub version, not .split()
    if not toks:
        return []
    # ensure endpoints are included explicitly
    endpoints = [toks[0], toks[-1]]
    out = []
    seen = set()
    for t in endpoints + toks:
        apt = _token_to_valid_airport(t) if "_token_to_valid_airport" in globals() else normalize_airport(t)
        if apt and apt not in seen:
            seen.add(apt)
            out.append(apt)
    return out

_TOKEN_RE = re.compile(r"[^A-Z0-9]+")

@functools.lru_cache(maxsize=1)
def _airport_ids_set() -> set[str]:
    """
    Cached set of valid airport identifiers from airports.csv.
    Uses airport_id values only (not norm_id) to avoid accepting waypoint-like tokens.
    """
    df = load_airports_cached()
    s: set[str] = set()
    if "airport_id" in df.columns:
        for v in df["airport_id"].dropna().astype(str).str.strip().str.upper():
            v = _TOKEN_RE.sub("", v)
            if v:
                s.add(v)
    return s

def _token_to_valid_airport(tok: str) -> str | None:
    """
    Normalize a token to a *known* airport_id in our dataset.

    Map42 safety posture (fail-closed):
      ✅ Accept only:
         - FAA-style: 3 letters/digits (e.g., "LAX", "0A9")
         - US ICAO-style: "K" + 3 letters/digits (e.g., "KLAX")
      ❌ Reject everything else (e.g., "MLAX", "EGLL", "LFPG") to prevent
         international/ambiguous codes from mapping to similarly-named US airports.

    Notes:
      - Always prefers FAA "XXX" if both "XXX" and "KXXX" exist in our dataset.
      - Keeps legacy FAA -> ICAO fallback within the strict allowlist.
    """
    if not tok:
        return None

    t = _TOKEN_RE.sub("", str(tok).strip().upper())
    if not t:
        return None
    if t in IGNORED_WAYPOINTS:
        return None

    valid = _airport_ids_set()
    if not valid:
        return None

    # --- Strict allowlist gating ---
    is_faa = (len(t) == 3 and t.isalnum())
    is_us_icao = (len(t) == 4 and t.startswith("K") and t[1:].isalnum())
    if not (is_faa or is_us_icao):
        return None

    # --- Normalize and validate within dataset ---

    # US ICAO -> FAA (preferred)
    if is_us_icao:
        faa = t[1:]
        if faa in valid:
            return faa
        return t if t in valid else None

    # FAA (exact) or FAA -> ICAO fallback (still within allowlist)
    if t in valid:
        return t
    cand = "K" + t
    return cand if cand in valid else None

def parse_foreflight_logbook_csv(df_or_text) -> pd.DataFrame:
    """Parse ForeFlight CSV exports into my_visits-style rows.

    Accepts either:
    - a raw header=None DataFrame (older behavior), OR
    - the decoded CSV text (preferred; supports 'Chelsea CSV' that may not include a 'Flights Table' marker).

    Returns DataFrame with columns: airport_id, date_visited, callsign, notes
    """
    # -----------------------------
    # 1) Normalize input into an iterable of dict rows (like csv.DictReader yields)
    # -----------------------------
    rows: list[dict] = []

    # If caller passed text, parse directly.
    if isinstance(df_or_text, str):
        s = df_or_text

        import csv as _csv

        lines = s.splitlines()
        # If this is the multi-section ForeFlight report CSV, locate 'Flights Table'
        start_idx = None
        for i, line in enumerate(lines):
            if "Flights Table" in str(line):
                start_idx = i + 1  # header should be next line
                break

        if start_idx is not None and start_idx < len(lines):
            data_lines = lines[start_idx:]
        else:
            # Fallback: treat the file as a plain flights table export with a normal header.
            data_lines = lines

        # Guard: need at least a header
        data_lines = [ln for ln in data_lines if ln is not None]
        if not data_lines:
            return pd.DataFrame(columns=["airport_id", "date_visited", "callsign", "notes"])

        reader = _csv.DictReader(data_lines)
        rows = [dict(r) for r in reader if r and any((v or "").strip() for v in r.values())]

    else:
        # Older behavior: a raw DataFrame with header=None
        df_raw = df_or_text
        if df_raw is None or getattr(df_raw, "empty", True):
            return pd.DataFrame(columns=["airport_id", "date_visited", "callsign", "notes"])

        flights_marker_idx = df_raw[df_raw.iloc[:, 0].astype(str).str.contains("Flights Table", na=False)].index
        if len(flights_marker_idx) == 0:
            # Fallback: try treating row 0 as the header (some exports are just a plain table)
            try:
                header = [str(x).strip() for x in list(df_raw.iloc[0])]
                data = df_raw.iloc[1:].reset_index(drop=True)
                flights = pd.DataFrame(data.iloc[:, :len(header)].values, columns=header)
                rows = flights.to_dict(orient="records")
            except Exception:
                raise ValueError("Unrecognized ForeFlight CSV format (no 'Flights Table' marker and no usable header).")
        else:
            flights_marker_idx = int(flights_marker_idx[0])
            header_idx = flights_marker_idx + 1
            header = list(df_raw.iloc[header_idx])
            data = df_raw.iloc[header_idx + 1:].reset_index(drop=True)
            flights = pd.DataFrame(data.iloc[:, :len(header)].values, columns=header)
            rows = flights.to_dict(orient="records")

    # -----------------------------
    # 2) Convert flight rows -> visit rows
    # -----------------------------
    collected: list[dict] = []
    for r in rows:
        # Date normalization: ForeFlight exports often use YYYY-MM-DD
        date_raw = (r.get("Date") or r.get("date") or "").strip() if isinstance(r.get("Date") or r.get("date"), str) else (r.get("Date") or r.get("date"))
        if date_raw in (None, "", "nan"):
            continue

        date_str = str(date_raw).strip()
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            date_str = dt.strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
        except Exception:
            # keep as-is (already MM/DD/YYYY, etc.)
            pass

        callsign = str(r.get("AircraftID", "") or "").strip().upper()
        route = r.get("Route") or ""

        # Notes/comments
        notes_parts = []
        for _col in ("PilotComments", "InstructorComments", "Comments", "Remarks", "Notes"):
            _v = r.get(_col)
            if isinstance(_v, str):
                _vv = _v.strip().strip('"')
                if _vv:
                    notes_parts.append(_vv)
        _seen = set()
        notes = " | ".join([p for p in notes_parts if not (p in _seen or _seen.add(p))]).strip()

        # Always include departure/arrival
        for col in ("From", "To"):
            val = r.get(col)
            if isinstance(val, str) and val.strip():
                apt = _token_to_valid_airport(val)
                if apt:
                    collected.append({"airport_id": apt, "date_visited": date_str, "callsign": callsign, "notes": notes})

        # Route-based (will be filtered by strict token validator)
        if isinstance(route, str) and route.strip():
            for apt in clean_route_points(route):
                collected.append({"airport_id": apt, "date_visited": date_str, "callsign": callsign, "notes": notes})

    df_out = pd.DataFrame(collected, columns=["airport_id", "date_visited", "callsign", "notes"])
    if df_out.empty:
        return df_out

    df_out["airport_id"] = df_out["airport_id"].astype(str).str.strip().str.upper()
    df_out["date_visited"] = df_out["date_visited"].astype(str).str.strip()
    df_out["callsign"] = df_out["callsign"].astype(str).fillna("").astype(str)
    df_out["notes"] = df_out["notes"].astype(str).fillna("").astype(str)

    # De-dupe: one airport per day
    df_out = df_out.drop_duplicates(subset=["airport_id", "date_visited"], keep="first").reset_index(drop=True)
    return df_out

def generate_logbook_content(*args, **kwargs) -> str:
    return "LOGBOOK TEMP DISABLED"

  
from flask import Response, redirect, request, send_file, send_from_directory, make_response  # keep imports together



# =========================
# Canonical host + Auth entrypoints
# =========================

APP_CANONICAL_HOST = (os.getenv("APP_CANONICAL_HOST") or "app.myairportmap.com").strip().lower()
APP_BASE_URL = (os.getenv("APP_BASE_URL") or f"https://{APP_CANONICAL_HOST}").strip()
_canonical_env = (os.getenv("APP_ENFORCE_CANONICAL_HOST") or "").strip().lower()
if _canonical_env:
    APP_ENFORCE_CANONICAL = _canonical_env in {"1", "true", "yes", "on"}
else:
    APP_ENFORCE_CANONICAL = bool((os.getenv("RENDER") or "").strip())

# Which paths should ALWAYS be served from the canonical host:
_CANONICAL_PREFIXES = (
    "/app",
    "/login",
    "/sign-in",
    "/sign-up",
    "/sign-out",
    "/onboard",
    "/billing",
    "/api",
    "/logbook",
    "/runways",
    "/u/",
    "/auth",     # add explicitly for /auth/debug
    "/_debug",
)

def _normalize_host(raw: str) -> str:
    """
    Normalize Host / X-Forwarded-Host for canonical comparisons.
    - Strips proxy comma lists
    - Strips :port (common behind proxies)
    - Lowercases
    """
    h = (raw or "").split(",")[0].strip().lower()
    if not h:
        return ""
    # strip :port (avoid mangling IPv6 literals)
    if h.count(":") == 1:
        host_part, port_part = h.rsplit(":", 1)
        if port_part.isdigit():
            h = host_part
    return h

def _is_onrender_host(host: str) -> bool:
    h = _normalize_host(host)
    return h.endswith(".onrender.com")

def _is_https_request() -> bool:
    xf_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if xf_proto:
        return xf_proto == "https"
    return request.scheme == "https"

@app.before_request
def enforce_canonical_host_for_app_routes():
    if not APP_ENFORCE_CANONICAL:
        return None

    path = request.path or "/"

    raw_host = request.headers.get("X-Forwarded-Host") or request.host or ""
    host = _normalize_host(raw_host)

    # Special-case: never serve "/" from the onrender hostname.
    # Keeps "/" free on the canonical host for a future landing page.
    # IMPORTANT: allow HEAD / to pass through for Render health checks.
    if (
        path == "/"
        and request.method != "HEAD"
        and host
        and _is_onrender_host(host)
    ):
        qs = request.query_string.decode("utf-8") if request.query_string else ""
        target = f"{APP_BASE_URL}{path}" + (f"?{qs}" if qs else "")
        return redirect(target, code=302)

    # Only enforce canonical host / https for selected prefixes
    if not any(path.startswith(p) for p in _CANONICAL_PREFIXES):
        return None

    qs = request.query_string.decode("utf-8") if request.query_string else ""
    target = f"{APP_BASE_URL}{path}" + (f"?{qs}" if qs else "")

    if host and host != APP_CANONICAL_HOST:
        return redirect(target, code=302)

    if not _is_https_request():
        return redirect(target, code=302)

    return None


@app.get("/robots.txt")
def robots_txt():
    # Keep it simple until landing page + SEO decisions are finalized
    return Response("User-agent: *\nDisallow:\n", mimetype="text/plain", status=200)

from time import perf_counter

@app.before_request
def _req_t0():
    request._t0 = perf_counter()

@app.after_request
def _log_req(resp):
    try:
        ms = int((perf_counter() - getattr(request, "_t0", perf_counter())) * 1000)
    except Exception:
        ms = -1

    loc = resp.headers.get("Location", "")
    # Never log tokens. This is safe: path + status + location.
    print(f"[REQ] {request.method} {request.path} status={resp.status_code} ms={ms} loc={loc[:180]}")
    return resp


# ------------------------------------------------------------
# Local sign-up entrypoint using auth service logic
# ------------------------------------------------------------

def _render_sign_up_page(next_path: str, error: str | None = None, email: str = "", first_name: str = "", last_name: str = "") -> str:
    safe_next = _html.escape(next_path)
    safe_email = _html.escape(email)
    safe_first = _html.escape(first_name)
    safe_last = _html.escape(last_name)
    error_html = f'<div style="color:#b84;margin:0 0 18px;">{_html.escape(error)}</div>' if error else ""

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Create your account</title>
  <style>
        *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family:system-ui, sans-serif; margin:0; padding:20px; background:#f6f7fb; color:#111; }}
        .card {{ width:100%; max-width:420px; margin:0 auto; background:#fff; border-radius:16px; padding:26px; box-shadow:0 16px 40px rgba(15,23,42,0.12); }}
    .title {{ margin:0 0 10px; font-size:24px; line-height:1.1; }}
    .sub {{ margin:0 0 22px; color:#666; font-size:15px; line-height:1.5; }}
    .field {{ width:100%; margin-bottom:14px; }}
    .field input {{ width:100%; padding:12px 14px; border:1px solid #ccd6e3; border-radius:12px; font-size:15px; }}
    .btn {{ width:100%; background:#111; color:#fff; border:none; border-radius:12px; padding:14px; font-size:15px; font-weight:700; cursor:pointer; }}
    .help {{ margin-top:16px; font-size:14px; color:#555; }}
    .help a {{ color:#111; text-decoration:none; font-weight:700; }}
  </style>
</head>
<body>
  <div class="card">
    <h1 class="title">Create account</h1>
    <p class="sub">Use your email and password to register for MyAirportMap.</p>
    {error_html}
    <form method="POST" action="/sign-up">
      <input type="hidden" name="next" value="{safe_next}">
      <div class="field"><input type="email" name="email" placeholder="Email" value="{safe_email}" required autocomplete="email"></div>
      <div class="field"><input type="text" name="first_name" placeholder="First name" value="{safe_first}" required autocomplete="given-name"></div>
      <div class="field"><input type="text" name="last_name" placeholder="Last name" value="{safe_last}" required autocomplete="family-name"></div>
      <div class="field"><input type="password" name="password" placeholder="Password" required autocomplete="new-password"></div>
      <button type="submit" class="btn">Create account</button>
    </form>
    <p class="help">Already have an account? <a href="/sign-in?next={_html.escape(next_path)}">Sign in</a></p>
  </div>
</body>
</html>'''

@app.route("/sign-up", methods=["GET", "POST"])
def sign_up_route():
    nxt = (request.values.get("next") or "/app").strip() or "/app"
    if (not nxt.startswith("/")) or nxt.startswith("//"):
        nxt = "/app"

    while nxt.endswith("?") or nxt.endswith("&"):
        nxt = nxt[:-1]
    nxt = nxt.replace("?&", "?")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not email or not first_name or not last_name or not password:
            error = "Please complete all form fields."
            return Response(_render_sign_up_page(nxt, error, email, first_name, last_name), mimetype="text/html", status=400)

        try:
            payload = RegisterRequest(email=email, first_name=first_name, last_name=last_name, password=password)
            db = SessionLocal()
            try:
                result = auth_register(payload, db=db)
            finally:
                db.close()

            if isinstance(result, dict) and result.get("success"):
                return redirect(f"/sign-in?next={quote(nxt, safe='/=?&')}", code=302)

            error = result.get("message") if isinstance(result, dict) else "Unable to create account."
        except HTTPException as exc:
            error = str(getattr(exc, "detail", exc))
        except Exception:
            error = "Unable to create account. Please try again."

        return Response(_render_sign_up_page(nxt, error, email, first_name, last_name), mimetype="text/html", status=400)

    return Response(_render_sign_up_page(nxt), mimetype="text/html", status=200)

def sign_up_redirect(path: str = "/logbook"):
    p = (path or "/logbook").strip() or "/logbook"

    # Normalize absolute URLs to a safe relative path + query
    if p.startswith("http://") or p.startswith("https://"):
        try:
            u = urllib.parse.urlparse(p)
            p = (u.path or "/logbook") + (("?" + u.query) if u.query else "")
        except Exception:
            p = "/logbook"

    # Must be an internal path and not protocol-relative
    if (not p.startswith("/")) or p.startswith("//"):
        p = "/logbook"

    # Prevent loops back to login routes
    if p in ("/login", "/sign-in", "/sign-up") or p.startswith("/login?") or p.startswith("/sign-in?") or p.startswith("/sign-up?"):
        p = "/logbook"

    q_next = urllib.parse.quote(p, safe="/=?&")
    fresh = "1" if request.cookies.get("mam_signed_out") == "1" else "0"

    return redirect(f"/sign-up?next={q_next}&fresh={fresh}", code=302)


def sign_up_href(path: str = "/logbook") -> str:
    p = (path or "/logbook").strip() or "/logbook"

    if p.startswith("http://") or p.startswith("https://"):
        try:
            u = urllib.parse.urlparse(p)
            p = (u.path or "/logbook") + (("?" + u.query) if u.query else "")
        except Exception:
            p = "/logbook"

    if (not p.startswith("/")) or p.startswith("//"):
        p = "/logbook"

    if p in ("/login", "/sign-in", "/sign-up") or p.startswith("/login?") or p.startswith("/sign-in?") or p.startswith("/sign-up?"):
        p = "/logbook"

    q_next = urllib.parse.quote(p, safe="/=?&")
    fresh = "1" if request.cookies.get("mam_signed_out") == "1" else "0"

    return f"/sign-up?next={q_next}&fresh={fresh}"

# Backwards compatibility alias
clerk_sign_up_redirect = sign_up_redirect
clerk_sign_up_href = sign_up_href

def _runtime_base_url() -> str:
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}".rstrip("/")

# ------------------------------------------------------------
# Local sign-in entrypoint using auth service logic
# ------------------------------------------------------------

def _render_sign_in_page(next_path: str, error: str | None = None, email: str = "") -> str:
    safe_next = _html.escape(next_path)
    safe_email = _html.escape(email)
    error_html = f'<div style="color:#b84;margin:0 0 18px;">{_html.escape(error)}</div>' if error else ""

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in</title>
  <style>
        *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family:system-ui, sans-serif; margin:0; padding:20px; background:#f6f7fb; color:#111; }}
        .card {{ width:100%; max-width:420px; margin:0 auto; background:#fff; border-radius:16px; padding:26px; box-shadow:0 16px 40px rgba(15,23,42,0.12); }}
    .title {{ margin:0 0 10px; font-size:24px; line-height:1.1; }}
    .sub {{ margin:0 0 22px; color:#666; font-size:15px; line-height:1.5; }}
    .field {{ width:100%; margin-bottom:14px; }}
    .field input {{ width:100%; padding:12px 14px; border:1px solid #ccd6e3; border-radius:12px; font-size:15px; }}
    .btn {{ width:100%; background:#111; color:#fff; border:none; border-radius:12px; padding:14px; font-size:15px; font-weight:700; cursor:pointer; }}
    .help {{ margin-top:16px; font-size:14px; color:#555; }}
    .help a {{ color:#111; text-decoration:none; font-weight:700; }}
  </style>
</head>
<body>
  <div class="card">
    <h1 class="title">Sign in</h1>
    <p class="sub">Use your email and password to continue.</p>
    {error_html}
    <form method="POST" action="/sign-in">
      <input type="hidden" name="next" value="{safe_next}">
      <div class="field"><input type="email" name="email" placeholder="Email" value="{safe_email}" required autocomplete="email"></div>
      <div class="field"><input type="password" name="password" placeholder="Password" required autocomplete="current-password"></div>
      <button type="submit" class="btn">Sign in</button>
    </form>
    <p class="help">Don't have an account? <a href="/sign-up?next={_html.escape(next_path)}">Create one</a></p>
  </div>
</body>
</html>'''

@app.route("/sign-in", methods=["GET", "POST"])
def sign_in_route():
    nxt = (request.values.get("next") or "/app").strip() or "/app"
    if (not nxt.startswith("/")) or nxt.startswith("//"):
        nxt = "/app"

    while nxt.endswith("?") or nxt.endswith("&"):
        nxt = nxt[:-1]
    nxt = nxt.replace("?&", "?")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not email or not password:
            error = "Please enter both email and password."
            return Response(_render_sign_in_page(nxt, error, email), mimetype="text/html", status=400)

        try:
            payload = LoginRequest(email=email, password=password)
            db = SessionLocal()
            try:
                result = auth_login(payload, db=db)
            finally:
                db.close()

            if isinstance(result, dict) and result.get("success"):
                token = (result.get("data") or {}).get("access_token") or ""
                resp = redirect(nxt, code=302)
                _cookie_secure = not app.debug and os.environ.get("COOKIE_SECURE", "1") != "0"
                resp.set_cookie(
                    APP_SESSION_COOKIE,
                    token,
                    httponly=True,
                    secure=_cookie_secure,
                    samesite="Lax",
                    path="/",
                    max_age=60 * 60 * 24 * 7,
                )
                resp.delete_cookie("mam_signed_out", path="/")
                return resp

            error = result.get("message") if isinstance(result, dict) else "Unable to sign in."
        except HTTPException as exc:
            error = str(getattr(exc, "detail", exc))
        except Exception:
            error = "Unable to sign in. Please try again."

        return Response(_render_sign_in_page(nxt, error, email), mimetype="text/html", status=400)

    return Response(_render_sign_in_page(nxt), mimetype="text/html", status=200)



# ---------------------------
# Root stub (single definition)
# ---------------------------
@app.route("/", methods=["GET", "HEAD"])
def root_route():
    if request.method == "HEAD":
        return ("", 200)

    # Authenticated users -> /app, otherwise -> /sign-in
    try:
        claims = verify_clerk_session(request)  # best-effort
        if claims and (claims.get("sub") or "").strip():
            return redirect("/app", code=302)
    except Exception:
        pass

    return redirect("/sign-in?next=/app", code=302)


# --- /login (canonical alias) ---
# Keep endpoint unique to avoid collisions with any legacy def login() elsewhere.
@app.get("/login", endpoint="login_route")
def login_route():
    # Always funnel through the canonical local sign-in route
    return redirect("/sign-in?next=/app", code=302)

@app.route("/welcome")
@login_required
def welcome():
    page = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Welcome | MyAirportMap</title>

  <style>
    :root {{
      --bravo-blue: #005589;
      --magenta: #E20074;
      --text: #0b1220;
      --muted: rgba(11, 18, 32, 0.72);
      --card: #ffffff;
      --soft: #f6f8fb;
      --border: rgba(0,0,0,0.10);
    }}

    body {{
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background: #fff;
      color: var(--text);
    }}

    .wrap {{
      max-width: 860px;
      margin: 28px auto 40px;
      padding: 0 16px;
    }}

    .brand {{
      display: flex;
      justify-content: center;
      margin: 8px 0 14px;
    }}

    .brand img {{
      width: 120px;
      height: auto;
      border-radius: 18px;
    }}

    .hero {{
      text-align: center;
      margin-bottom: 18px;
    }}

    .hero h1 {{
      font-size: 34px;
      margin: 0 0 8px;
      letter-spacing: -0.02em;
      color: var(--bravo-blue);
      font-weight: 900;
    }}

    .hero .sub {{
      font-size: 20px;
      line-height: 1.5;
      color: var(--muted);
      margin: 0 auto;
      max-width: 720px;
    }}

    .grid {{
      margin-top: 22px;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
    }}

    .card {{
      display: block;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px 16px 14px;
      text-decoration: none;
      color: var(--text);
      box-shadow: 0 10px 24px rgba(0, 85, 137, 0.06);
      transition: transform 0.12s ease, filter 0.12s ease;
    }}

    .card:hover {{
      transform: translateY(-2px);
      filter: brightness(1.02);
    }}

    .card .title {{
      font-weight: 900;
      font-size: 18px;
      margin: 0 0 6px;
      letter-spacing: -0.01em;
    }}

    .card .desc {{
      font-weight: 650;
      font-size: 14px;
      line-height: 1.35;
      color: var(--muted);
      margin: 0;
    }}

    .primary {{
      border-color: rgba(0, 85, 137, 0.35);
      background: linear-gradient(180deg, rgba(0,85,137,0.06), rgba(0,85,137,0.02));
    }}

    .primary .title {{ color: var(--bravo-blue); }}

    .demo {{
      border-color: rgba(226, 0, 116, 0.35);
      background: linear-gradient(180deg, rgba(226,0,116,0.06), rgba(226,0,116,0.02));
    }}

    .demo .title {{ color: var(--magenta); }}

    .footerRow {{
      margin-top: 16px;
      display: flex;
      gap: 14px;
      align-items: center;
      justify-content: center;
      flex-wrap: wrap;
    }}

    .link {{
      text-decoration: none;
      font-weight: 850;
      color: var(--text);
    }}

    .mutelink {{
      text-decoration: none;
      font-weight: 800;
      color: rgba(11, 18, 32, 0.70);
    }}

    .note {{
      margin-top: 20px;
      font-size: 13px;
      color: rgba(11, 18, 32, 0.65);
      text-align: center;
    }}

    /* ✅ Mobile readability (welcome-only) */
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}

    @media (max-width: 640px) {{
      .wrap {{ margin-top: 18px; }}
      .hero h1 {{ font-size: 28px; line-height: 1.15; }}
      .hero .sub {{ font-size: 19px; line-height: 1.55; }}
      .card .title {{ font-size: 20px; }}
      .card .desc {{ font-size: 18px; line-height: 1.45; }}
    }}
  </style>
</head>

<body>
  <div class="wrap">

    <div class="brand">
      <img src="/static/brandmark.png" alt="MyAirportMap" loading="eager">
    </div>

    <div class="hero">
      <h1>{BRAND_HEADLINE}</h1>
      <div class="sub">{BRAND_STATEMENT}</div>
    </div>

    <!-- ✅ Three simple choices. Relative links help preserve Clerk session. -->
    <div class="grid">
      <a class="card primary" href="/logbook">
        <div class="title">Enter and/or upload airport visits</div>
        <p class="desc">Get your airports pinned and your map ready in minutes.</p>
      </a>

      <a class="card" href="/map">
        <div class="title">Open my map</div>
        <p class="desc">See your visited airports and filter your flights.</p>
      </a>

      <a class="card demo" href="/u/myairportmap-demo">
        <div class="title">View demo map</div>
        <p class="desc">See a real example (read-only), no changes to your data.</p>
      </a>
    </div>

    <div class="footerRow">
      <a class="link" href="/welcome/dismiss">Continue &gt;</a>
      <a class="mutelink" href="/sign-out">Sign out</a>
    </div>

    <div class="note">{BADGE_TIE_IN}</div>
  </div>
</body>
</html>
    """
    return Response(page, mimetype="text/html")



@app.route("/welcome/dismiss")
@login_required
def welcome_dismiss():
    # Always land in the private experience
    resp = make_response(redirect("/logbook", code=302))
    resp.set_cookie(
        "welcome_dismissed",
        "1",
        max_age=60 * 60 * 24 * 30,  # 30 days
        samesite="Lax",
        secure=True,
        path="/",
    )
    return resp


def _full_url(path: str) -> str:
    # Always create absolute URL on canonical host
    path = (path or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return APP_BASE_URL.rstrip("/") + path


@app.get("/_debug/env")
def _debug_env():
    safe = {
        "host": request.host,
        "scheme": request.scheme,
        "APP_BASE_URL": APP_BASE_URL,
        "APP_CANONICAL_HOST": APP_CANONICAL_HOST,
        "APP_ENFORCE_CANONICAL_HOST": APP_ENFORCE_CANONICAL,
        "APP_SESSION_COOKIE": APP_SESSION_COOKIE,
    }
    return safe, 200

@app.route("/app")
@login_required
def route_app():
    """
    Post-login router:
      1) Require MyAirportMap user name selection (once)
      2) Require Terms acceptance
      3) Then go to /logbook
    """
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return redirect("/sign-in?next=/app", code=302)

    chosen = (get_handle_for_user(user_id) or "").strip()

    # If user name not chosen (or still default "user_..."), send them to onboarding
    if (not chosen) or chosen.startswith("user_"):
        return redirect("/onboard/handle", code=302)

    # Terms gate
    if not tos_accepted_for_user(user_id):
        return redirect("/onboard/terms?next=/app", code=302)

    return redirect("/logbook", code=302)


@app.route("/onboard/plan", methods=["GET"])
@login_required
def onboard_plan_page():
    """
    Legacy route: previously showed 'Free vs Pro'.
    New rules:
      - There is no free. Only trial (30 days) or member.
      - If active access -> /logbook
      - If trial ended  -> /trial/ended -> /upgrade
    """
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return redirect("/sign-in?next=/app", code=302)

    # Require Terms (per user_id)
    if not tos_accepted_for_user(user_id):
        return redirect("/onboard/terms?next=/onboard/plan", code=302)

    # Require chosen handle
    handle = (get_handle_for_user(user_id) or "").strip().lower()
    if not handle:
        return redirect("/onboard/handle", code=302)

    # Ensure trial anchor exists (safe + idempotent)
    try:
        ensure_trial_initialized(handle)
    except Exception as e:
        print("onboard_plan: ensure_trial_initialized failed:", repr(e))

    # Route based on access state
    if has_active_access(handle):
        return redirect("/logbook", code=302)

    return redirect("/trial/ended?next=" + quote("/logbook", safe="/=?&"), code=302)


@app.route("/onboard/terms", methods=["GET", "POST"])
@login_required
def onboard_terms():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return redirect("/sign-in?next=/logbook", code=302)

    # ✅ Stable default: /logbook
    nxt = (request.args.get("next") or "/logbook").strip() or "/logbook"
    if (not nxt.startswith("/")) or nxt.startswith("//"):
        nxt = "/logbook"

    # If already accepted, continue immediately
    if tos_accepted_for_user(user_id):
        return redirect(nxt, code=302)

    if request.method == "POST":
        # ✅ This is where the crash was occurring before
        set_tos_accepted_for_user(user_id)
        return redirect(nxt, code=302)

    # Best-effort navbar (do not let navbar failures break onboarding)
    navbar_html = ""
    try:
        navbar_html = get_navbar("terms", handle=current_user_handle())
    except Exception:
        pass

    return Response(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Terms acknowledgement • MyAirportMap</title>
</head>
<body style="background:#0f1115;color:#fff;font-family:system-ui;margin:0;">
  {navbar_html}

  <div style="max-width:760px;margin:0 auto;padding:26px;padding-top:90px;">
    <h1 style="margin:0 0 10px;font-size:26px;letter-spacing:-0.2px;">
      One quick acknowledgement
    </h1>

    <div style="color:#b9c0cc;margin-bottom:18px;line-height:1.45;">
      Please review the Terms, then acknowledge to continue.
      <a href="/terms?back=/onboard/terms?next={_html.escape(quote(nxt))}"
         style="color:#fff;font-weight:900;text-decoration:underline;text-underline-offset:3px;">
        View Terms
      </a>
    </div>

    <form method="POST"
          style="background:#141820;
                 border:1px solid rgba(255,255,255,0.10);
                 border-radius:16px;
                 padding:16px;">

      <label style="display:flex;gap:10px;align-items:flex-start;
                    color:#d7dbe3;font-weight:850;">
        <input type="checkbox" required style="margin-top:4px;">
        <span>I have read and agree to the MyAirportMap Terms.</span>
      </label>

      <button type="submit"
        style="margin-top:14px;
               padding:10px 14px;
               border-radius:12px;
               background:#ffffff;
               border:none;
               color:#0f1115;
               font-weight:900;
               cursor:pointer;">
        Continue
      </button>
    </form>

    <div style="margin-top:14px;
                color:rgba(255,255,255,0.55);
                font-size:12.5px;
                line-height:1.35;">
      Not for use while operating an aircraft. MyAirportMap is intended to extend
      the flying experience after you leave the airport.
    </div>
  </div>
</body>
</html>
""", mimetype="text/html")


@app.route("/onboard/handle", methods=["GET"])
@login_required
def onboard_handle_page():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    nxt = (request.args.get("next") or "/app").strip() or "/app"
    if not nxt.startswith("/"):
        nxt = "/app"

    existing = (get_handle_for_user(user_id) or "").strip() if user_id else ""
    # If user already picked a real user name, go to Terms gate (or onward)
    if existing and not existing.startswith("user_"):
        return redirect(f"/onboard/terms?next={quote(nxt)}", code=302)

    return Response(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Choose user name • MyAirportMap</title>
</head>
<body style="background:#0f0f0f;color:#fff;font-family:system-ui;margin:0;">
  {get_navbar("home")}
  <div style="max-width:760px;margin:0 auto;padding:26px;padding-top:90px;">
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;">
      <img src="/logo.png" style="height:54px;width:auto;border-radius:12px;" alt="MyAirportMap">
      <div>
        <div style="font-size:22px;font-weight:800;">Choose your MyAirportMap user name</div>
        <div style="color:#aaa;margin-top:2px;">This becomes part of your publically shareable profile URL.</div>
      </div>
    </div>

    <div style="background:#151515;border:1px solid #333;border-radius:16px;padding:16px;">
      <label style="display:block;color:#aaa;font-weight:700;margin-bottom:8px;">User name</label>
      <input id="handle" placeholder="e.g., flywithfrank, sr22pilot" maxlength="20"
             style="width:100%;padding:12px 14px;border-radius:12px;border:1px solid #333;background:#0f0f0f;color:#fff;font-size:16px;outline:none;">

      <div id="preview" style="margin-top:10px;color:#aaa;">
        Full member's public map page will be:<span style="color:#fff;">/u/<span id="hprev">…</span></span>
      </div>

      <div id="msg" style="margin-top:10px;color:#f6b73c;display:none;"></div>

      <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;">
        <button onclick="submitHandle()"
                style="padding:10px 14px;border-radius:12px;background:#1f1f1f;border:1px solid #3a3a3a;color:#fff;font-weight:800;cursor:pointer;">
          Continue
        </button>
        <button onclick="randomHandle()"
                style="padding:10px 14px;border-radius:12px;background:#0f0f0f;border:1px solid #3a3a3a;color:#ddd;font-weight:700;cursor:pointer;">
          Random
        </button>
        <a href="/sign-out"
           style="padding:10px 14px;border-radius:12px;background:#0f0f0f;border:1px solid #3a3a3a;color:#ddd;font-weight:800;cursor:pointer;text-decoration:none;display:inline-block;">
          Sign out
        </a>
      </div>

      <div style="margin-top:12px;color:#777;font-size:13px;">
        3–20 chars, letters/numbers/underscore/dash.
      </div>
    </div>
  </div>

  <script>
    const inp = document.getElementById("handle");
    const hprev = document.getElementById("hprev");
    const msg = document.getElementById("msg");

    function sanitize(s){{ return (s||"").toLowerCase().replace(/[^a-z0-9_-]/g,""); }}

    inp.addEventListener("input", ()=>{{
      const v = sanitize(inp.value);
      inp.value = v;
      hprev.textContent = v || "…";
      msg.style.display="none";
    }});

    function randomHandle(){{
      const choices = ["pilot","cirrus","runway","tower","pattern","aviate","airspeed"];
      const n = Math.floor(Math.random()*900)+100;
      inp.value = choices[Math.floor(Math.random()*choices.length)] + n;
      inp.dispatchEvent(new Event("input"));
    }}

    async function submitHandle(){{
      const v = sanitize(inp.value);
      if(v.length < 3){{ msg.textContent="User name is too short."; msg.style.display="block"; return; }}

      const res = await fetch("/api/onboard/handle", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{handle: v}})
      }});

      const data = await res.json().catch(()=>({{}}));
      if(!res.ok){{
        msg.textContent = data.error || "Could not save user name.";
        msg.style.display="block";
        return;
      }}

      window.location.href = "/onboard/terms?next={_html.escape(quote(nxt))}";
    }}
  </script>
</body>
</html>
""", mimetype="text/html")

@app.route("/terms", methods=["GET"])
def terms_page():
    """
    Public Terms page (linked from navbar + profile).

    UX: single button to return to prior page. We do NOT try to be clever with
    continue/back flows here.
    """
    # Prefer explicit ?next=, then HTTP referrer, then a safe default
    next_path = (request.args.get("next") or "").strip()
    if next_path and not next_path.startswith("/"):
        next_path = ""

    back_href = next_path or (request.referrer or "/")

    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Terms • MyAirportMap</title>
</head>
<body style="background:#0f1115;color:#fff;font-family:system-ui;margin:0;">
  <div style="max-width:980px;margin:0 auto;padding:24px;">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
      <img src="/logo.png" alt="MyAirportMap" style="height:34px;width:auto;border-radius:10px;">
      <div style="font-weight:950;font-size:18px;letter-spacing:-0.2px;">MyAirportMap • Terms</div>
    </div>

    <div style="background:#141820;border:1px solid rgba(255,255,255,0.10);border-radius:16px;padding:18px;">
      <div style="color:#b9c0cc;margin-bottom:14px;">
        Please review the Terms of Use below.
      </div>

      {render_terms_html()}

      <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;">
        <a href="{_html.escape(back_href)}"
           style="padding:10px 14px;border-radius:12px;background:#ffffff;border:none;color:#0f1115;font-weight:950;text-decoration:none;display:inline-block;">
          Back
        </a>
      </div>
    </div>
  </div>
</body>
</html>""",
        mimetype="text/html",
        status=200,
    )


@app.route("/api/onboard/handle", methods=["POST"])
@login_required
def api_onboard_handle():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return {"error": "Unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    safe = _sanitize_username(payload.get("handle") or "")

    if len(safe) < 3 or len(safe) > 20:
        return {"error": "User name must be 3–20 characters."}, 400

    try:
        # your existing validation / uniqueness logic
        set_handle_for_user(user_id, safe)

        # NEW: durable mapping (survives restarts)
        set_handle_for_user_durable(user_id, safe)

    except ValueError as e:
        return {"error": str(e)}, 400

    _ensure_visits_csv_for_username(safe)
    return {"ok": True, "handle": safe}
 
# ------------------------------------------------------------
# Profile: change MyAirportMap user name
# ------------------------------------------------------------

@app.route("/profile", methods=["GET"])
@login_required
def profile_page():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()

    # Canonical handle first (from the authenticated session)
    current = (current_user_handle() or "").strip().lower()

    # Fallback to mapping if needed
    if (not current) and user_id:
        mapped = (get_handle_for_user(user_id) or "").strip().lower()
        if mapped and (not mapped.startswith("user_")):
            current = mapped

    handle = current

    # Avatar is always served by /avatar/<handle>
    # ✅ Cache-bust on profile page so refresh always shows the latest upload
    safe = _safe_handle_for_avatar(handle or "")
    try:
        import time as _time
        v = str(int(_time.time()))
    except Exception:
        v = "1"

    avatar_src = (f"/avatar/{safe}?v={v}" if safe else "/static/mam-logo.png")
    avatar_src_esc = _html.escape(avatar_src)

    # Navbar must use the SAME handle
    navbar = get_navbar("profile", handle=handle)

    # 90-day gate for handle changes
    eligible, next_ts = _can_change_handle(user_id) if user_id else (True, None)
    next_date = _fmt_utc_date(next_ts or 0) if next_ts else ""

    # Load saved cert selections (by current handle)
    prefs = _load_json_from_storage(_profile_prefs_key(current)) if current else {}
    selected = prefs.get("achievements_certs") or []
    if not isinstance(selected, list):
        selected = []
    selected = _validate_cert_keys([str(x) for x in selected])
    selected_json = json.dumps(selected)

    # Handle save button behavior: disappears unless eligible
    handle_save_btn = ""
    handle_gate_note = ""
    if eligible:
        handle_save_btn = """
        <button onclick="saveHandle()"
                style="padding:10px 14px;border-radius:12px;background:#ffffff;border:none;color:#0f1115;font-weight:900;cursor:pointer;">
          Save user name
        </button>
        """
    else:
        handle_gate_note = f"""
        <div style="margin-top:10px;color:#98a2b3;font-size:13px;line-height:1.35;">
          User name changes are available every {PROFILE_HANDLE_COOLDOWN_DAYS} days. Next change: <span style="color:#fff;">{_html.escape(next_date)}</span>
        </div>
        """

    # Template vars used in HTML
    current_esc = _html.escape(current)
    prev_text = current_esc or "…"

    return Response(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Profile • MyAirportMap</title>
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0" />
  <meta http-equiv="Pragma" content="no-cache" />
  <meta http-equiv="Expires" content="0" />
</head>
<body style="background:#0f1115;color:#fff;font-family:system-ui;margin:0;">
  {navbar}
  <div style="max-width:760px;margin:0 auto;padding:26px;padding-top:90px;">
    <h1 style="margin:0 0 10px;font-size:26px;letter-spacing:-0.2px;">Profile</h1>
    <div style="color:#b9c0cc;margin-bottom:18px;">Manage your MyAirportMap user name, avatar, and FAA ratings.</div>

    <div style="background:#141820;border:1px solid rgba(255,255,255,0.10);border-radius:16px;padding:16px;">

      <!-- Label row with avatar (no navbar impact) -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
        <div style="width:44px;height:44px;border-radius:999px;overflow:hidden;background:#ffffff;display:flex;align-items:center;justify-content:center;flex:0 0 auto;">
            <img id="avatar_top"
                src="{avatar_src_esc}"
                onerror="this.onerror=null;this.src='/static/mam-logo.png';"
                alt="Avatar"
                style="width:100%;height:100%;object-fit:cover;display:block;">
        </div>
        <label style="display:block;color:#98a2b3;font-weight:800;margin:0;">MyAirportMap user name</label>
      </div>

      <input id="uname" value="{current_esc}" maxlength="20"
             placeholder="e.g., cfi-barbara, piperpilot, dannydecathalon"
             style="width:100%;padding:12px 14px;border-radius:12px;border:1px solid rgba(255,255,255,0.14);background:#0f1115;color:#fff;font-size:16px;outline:none;">

      <div style="margin-top:10px;color:#98a2b3;">
        Your user name will be part of the URL for full members: <span style="color:#fff;">/u/<span id="prev">{prev_text}</span></span>
      </div>

      <div id="msg" style="margin-top:10px;color:#f6b73c;display:none;"></div>

      <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;align-items:center;">
        {handle_save_btn}
        <a href="/app"
           style="padding:10px 14px;border-radius:12px;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.16);color:#fff;font-weight:900;cursor:pointer;text-decoration:none;display:inline-block;">
          Back to app
        </a>
      </div>

      <div style="margin-top:12px;color:#98a2b3;font-size:13px;line-height:1.35;">
        3–20 chars, letters/numbers/underscore/dash.
      </div>

      {handle_gate_note}

      <!-- Divider -->
      <div style="margin:18px 0;border-top:1px solid rgba(255,255,255,0.10);"></div>

      <div id="avatar_block" style="margin-top:16px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.10);">
        <div style="font-weight:900;margin-bottom:6px;">Avatar</div>
        <div style="color:#98a2b3;font-size:13px;line-height:1.35;margin-bottom:10px;">
          Choose your avatar from your files. Upload a square image. It will be shown across the app and public pages.
        </div>

        <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;">

          <!-- Preview -->
          <div style="width:56px;height:56px;border-radius:999px;overflow:hidden;background:#fff;display:flex;align-items:center;justify-content:center;flex:0 0 auto;">
            <img id="avatar_img"
                src="{avatar_src_esc}"
                onerror="this.onerror=null;this.src='/static/mam-logo.png';"
                alt="Avatar"
                style="width:100%;height:100%;object-fit:cover;display:block;">
          </div>

          <input id="avatar_file"
                 type="file"
                 accept="image/jpeg,image/png,image/webp"
                 style="color:#b9c0cc;font-weight:800;">

          <button id="avatar_btn"
                  type="button"
                  onclick="if (window.uploadAvatar) window.uploadAvatar(); return false;"
                  style="padding:10px 14px;border-radius:12px;background:rgba(255,255,255,0.08);
                         border:1px solid rgba(255,255,255,0.16);color:#fff;font-weight:900;cursor:pointer;">
            Upload avatar
          </button>
        </div>

        <div id="avatar_msg" style="margin-top:10px;color:#f6b73c;display:none; font-weight:800;"></div>
      </div>

      <!-- Divider -->
      <div style="margin:18px 0;border-top:1px solid rgba(255,255,255,0.10);"></div>

      <!-- Achievements certifications (checkboxes) -->
      <div>
        <div style="font-weight:900;margin-bottom:6px;">Achievements certifications</div>
        <div style="color:#98a2b3;font-size:13px;line-height:1.35;margin-bottom:12px;">
          If you choose, select your highest FAA Certificate levels which will appear on your Achievements page. Uncheck all to hide it.
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
          <div>
            <div style="color:#b9c0cc;font-weight:900;margin:0 0 8px;">ATP</div>
            { _cert_checkbox("atp_asel","ASEL") }
            { _cert_checkbox("atp_amel","AMEL") }
            { _cert_checkbox("atp_ases","ASES") }
            { _cert_checkbox("atp_ames","AMES") }
            { _cert_checkbox("atp_heli","Helicopter") }
          </div>

          <div>
            <div style="color:#b9c0cc;font-weight:900;margin:0 0 8px;">CPL</div>
            { _cert_checkbox("cpl_asel","ASEL") }
            { _cert_checkbox("cpl_amel","AMEL") }
            { _cert_checkbox("cpl_ases","ASES") }
            { _cert_checkbox("cpl_ames","AMES") }
            { _cert_checkbox("cpl_heli","Helicopter") }
          </div>

          <div>
            <div style="color:#b9c0cc;font-weight:900;margin:0 0 8px;">PPL</div>
            { _cert_checkbox("ppl_asel","ASEL") }
            { _cert_checkbox("ppl_amel","AMEL") }
            { _cert_checkbox("ppl_ases","ASES") }
            { _cert_checkbox("ppl_ames","AMES") }
            { _cert_checkbox("ppl_heli","Helicopter") }
           </div>

           <div>
            <div style="color:#b9c0cc;font-weight:900;margin:0 0 8px;">CFI</div>
            { _cert_checkbox("cfi","CFI") }
            { _cert_checkbox("cfi_i","CFI-I") }
            { _cert_checkbox("mei","MEI") }
            { _cert_checkbox("cfi_heli","Helicopter") }

            { _cert_checkbox("instrument","Instrument Rated") }
           </div>

           <div style="grid-column:1 / -1;">
           <div style="color:#b9c0cc;font-weight:900;margin:0 0 8px;">Other</div>
           <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                { _cert_checkbox("flight_attendant","Flight Attendant") }
                { _cert_checkbox("dispatcher","Aircraft Dispatcher") }
                { _cert_checkbox("student_pilot","Student Pilot") }
                { _cert_checkbox("uas","UAS") }

                { _cert_checkbox("dpe","Designated Pilot Examiner (DPE)") }
                { _cert_checkbox("ap","A&P Mechanic") }
                { _cert_checkbox("atc","Air Traffic Controller") }
                { _cert_checkbox("flight_engineer","Flight Engineer") }
           </div>
           </div>
          </div>
        </div>

        <div id="cert_msg" style="margin-top:10px;color:#f6b73c;display:none;"></div>

        <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;">
          <button onclick="saveCerts()"
                  style="padding:10px 14px;border-radius:12px;background:rgba(255,255,255,0.92);border:none;color:#0f1115;font-weight:900;cursor:pointer;">
            Save certifications
          </button>
        </div>
      </div>

    </div>
  </div>

<script>
  const inp     = document.getElementById("uname");
  const prev    = document.getElementById("prev");
  const msg     = document.getElementById("msg");

  // Certifications UI
  const certMsg = document.getElementById("cert_msg");

  // Avatar UI
  const avatarMsg  = document.getElementById("avatar_msg");
  const avatarFile = document.getElementById("avatar_file");

  // prechecked cert keys from server
  const PRECHECK = {selected_json} || [];

  function sanitize(s) {{
    return (s || "")
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "");
  }}

  function showMsg(el, text, ms) {{
    if (!el) return;
    el.textContent = text || "";
    el.style.display = text ? "block" : "none";
    if (ms) setTimeout(() => {{ try {{ el.style.display = "none"; }} catch (e) {{}} }}, ms);
  }}

  // --- Username live preview ---
  if (inp) {{
    inp.addEventListener("input", () => {{
      const v = sanitize(inp.value);
      inp.value = v;
      if (prev) prev.textContent = v || "…";
      if (msg) msg.style.display = "none";
    }});
  }}

  // --- Apply prechecks ---
  (function initCerts() {{
    try {{
      if (Array.isArray(PRECHECK)) {{
        for (const k of PRECHECK) {{
          const el = document.querySelector('input[data-cert="' + k + '"]');
          if (el) el.checked = true;
        }}
      }}
    }} catch (e) {{}}
  }})();

  async function saveHandle() {{
    const v = sanitize(inp && inp.value);
    if (!v || v.length < 3) {{
      showMsg(msg, "User name is too short.");
      return;
    }}

    const res = await fetch("/api/profile/username", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ handle: v }}),
    }});

    const data = await res.json().catch(() => ({{}}));
    if (!res.ok) {{
      showMsg(msg, data.error || "Could not save user name.");
      return;
    }}

    window.location.href = "/app";
  }}

  function gatherCerts() {{
    const out = [];
    document.querySelectorAll("input[data-cert]").forEach((el) => {{
      try {{
        if (el.checked) out.push(el.getAttribute("data-cert"));
      }} catch (e) {{}}
    }});
    return out;
  }}

  async function saveCerts() {{
    showMsg(certMsg, "");

    const certs = gatherCerts();
    const res = await fetch("/api/profile/achievements-certs", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ certs }}),
    }});

    const data = await res.json().catch(() => ({{}}));
    if (!res.ok) {{
      showMsg(certMsg, data.error || "Could not save certifications.");
      return;
    }}

    showMsg(certMsg, "Saved.", 1200);
  }}

  // --- Avatar upload ---
  async function uploadAvatar() {{
    if (!avatarFile) return;

    showMsg(avatarMsg, "");
    const f = avatarFile.files && avatarFile.files[0];
    if (!f) {{
      showMsg(avatarMsg, "Choose an image first.");
      return;
    }}

    const okTypes = ["image/jpeg", "image/png", "image/webp"];
    if (f.type && !okTypes.includes(f.type)) {{
      showMsg(avatarMsg, "Avatar must be a JPG, PNG, or WebP image.");
      return;
    }}
    if (f.size && f.size > 3 * 1024 * 1024) {{
      showMsg(avatarMsg, "Avatar too large (max 3MB).");
      return;
    }}

    const btn = document.getElementById("avatar_btn");
    const oldText = btn ? btn.textContent : "";
    if (btn) {{
      btn.disabled = true;
      btn.style.opacity = "0.75";
      btn.style.cursor = "default";
      btn.textContent = "Uploading…";
    }}

    try {{
      const fd = new FormData();
      fd.append("avatar", f);

      const res = await fetch("/api/profile/avatar", {{
        method: "POST",
        body: fd,
      }});

      const data = await res.json().catch(() => ({{}}));
      if (!res.ok) {{
        showMsg(avatarMsg, (data && data.error) ? data.error : "Could not upload avatar.");
        return;
      }}

      showMsg(avatarMsg, "Avatar uploaded.", 1200);

      try {{
        if (data && data.avatar_url) {{
          const url = data.avatar_url + "?v=" + Date.now();

          const topImg = document.getElementById("avatar_top");
          if (topImg) topImg.src = url;

          const prevImg = document.getElementById("avatar_img");
          if (prevImg) prevImg.src = url;

          const navImg = document.querySelector(".pill .navavatar img");
          if (navImg) navImg.src = url;

          const pubImg = document.querySelector(".mam-public-avatar img");
          if (pubImg) pubImg.src = url;
        }}
      }} catch (e) {{}}

      try {{ avatarFile.value = ""; }} catch (e) {{}}
    }} finally {{
      if (btn) {{
        btn.disabled = false;
        btn.style.opacity = "1";
        btn.style.cursor = "pointer";
        btn.textContent = oldText || "Upload avatar";
      }}
    }}
  }}

  // Expose for onclick hooks
  window.saveHandle = saveHandle;
  window.saveCerts = saveCerts;
  window.uploadAvatar = uploadAvatar;
</script>
</body>
</html>""", mimetype="text/html")


def _cert_checkbox(key: str, label: str) -> str:
    """
    Small HTML helper used by /profile page render.
    """
    k = _html.escape(key)
    lab = _html.escape(label)
    return f"""
      <label style="display:flex;align-items:center;gap:10px;margin:0 0 10px;cursor:pointer;">
        <input type="checkbox" data-cert="{k}"
               style="width:18px;height:18px;accent-color:#ffffff;cursor:pointer;">
        <span style="color:#fff;font-weight:800;">{lab}</span>
      </label>
    """

@app.route("/api/profile/username", methods=["POST"])
@login_required
def api_profile_username():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        return {"error": "Unauthorized"}, 401

    # 90-day handle change gate
    eligible, next_ts = _can_change_handle(user_id)
    if not eligible:
        return {
            "error": f"User name can be changed every {PROFILE_HANDLE_COOLDOWN_DAYS} days. Next change: {_fmt_utc_date(next_ts or 0)}."
        }, 429

    payload = request.get_json(silent=True) or {}
    safe = _sanitize_username(payload.get("handle") or "")

    if len(safe) < 3 or len(safe) > 20:
        return {"error": "User name must be 3–20 characters."}, 400

    # get old handle for prefs migration
    old = (get_handle_for_user(user_id) or "").strip().lower()
    if old.startswith("user_"):
        old = ""

    try:
        set_handle_for_user(user_id, safe)
    except ValueError as e:
        return {"error": str(e)}, 400

    # persist change timestamp + migrate prefs (best effort)
    _set_handle_change_ts(user_id)
    if old:
        _migrate_profile_prefs(old, safe)

    _ensure_visits_csv_for_username(safe)
    return {"ok": True, "handle": safe}

from flask import Response, send_from_directory

@app.route("/avatar/<handle>")
def route_avatar(handle: str):
    h = _safe_handle_for_avatar(handle)

    # Always return something, and NEVER allow caching.
    # Reason: before an avatar exists, browsers may cache the fallback logo,
    # which makes it look like uploads "don't persist" when navbar uses /avatar/<handle>.
    def _nocache(resp):
        try:
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        except Exception:
            pass
        return resp

    if not h:
        return _nocache(send_from_directory("static", "mam-logo.png"))

    key = avatar_key(h)
    ct_key = avatar_ct_key(h)

    # Prefer: attempt read directly (exists() can be flaky / inconsistent across backends)
    try:
        data = storage_backend.read_bytes(key) or b""
        if data:
            ct = "image/jpeg"
            try:
                raw = storage_backend.read_bytes(ct_key) or b""
                guess = raw.decode("utf-8", errors="ignore").strip().lower()
                if is_safe_image_content_type(guess):
                    ct = guess
            except Exception:
                pass

            return _nocache(Response(data, mimetype=ct))
    except Exception:
        pass

    # Fallback: always show MAM logo (no blanks, no initials)
    return _nocache(send_from_directory("static", "mam-logo.png"))

@app.route("/api/profile/avatar", methods=["POST"])
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

# ------------------------------------------------------------
# Onboarding Step: Choose MyAirportMap user name (internal: handle)
# ------------------------------------------------------------

def _sanitize_username(raw: str) -> str:
    raw = (raw or "").strip().lower()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    return safe

def _ensure_visits_csv_for_username(username: str) -> None:
    key = user_visits_key(username)
    if not storage_backend.exists(key):
        empty = "airport_id,date_visited,callsign,notes\n".encode("utf-8")
        storage_backend.write_bytes(key, empty, content_type="text/csv", cache_control="no-store")



def _validate_username(safe: str) -> None:
    if not safe:
        raise ValueError("User name is required.")
    if len(safe) < 3:
        raise ValueError("User name is too short (min 3).")
    if len(safe) > 20:
        raise ValueError("User name is too long (max 20).")
    if safe.startswith("user_"):
        raise ValueError("That user name is reserved. Pick something custom.")

    # light reserved list (avoid obvious collisions + impersonation/system handles)
    reserved = {
        # app routes / obvious collisions
        "app", "map", "logbook", "welcome", "admin", "api", "static", "profile", "upgrade",
        "settings", "billing", "payments",

        # brand / staff-ish
        "support", "help", "staff", "moderator", "mod", "official", "myairportmap", "mam",

        # auth/payments tech words
        "stripe", "clerk", "oauth", "login", "signin", "signup",

        # aviation / org impersonation (exact handles)
        "faa", "tsa", "atc",
        "united", "delta", "american", "southwest", "jetblue", "alaska", "spirit", "frontier",
    }
    if safe in reserved:
        raise ValueError("That user name is reserved. Choose another.")

    # -----------------------------
    # Foul / abusive guard
    # - deterministic, local-only
    # - do NOT reveal which word triggered (avoid gaming)
    # -----------------------------
    blocked_exact = {
        # tiny exact list for the most obvious
        "fuck", "fuk", "shit", "cunt",
    }

    # Substring tokens; handle is already sanitized to [a-z0-9_-]
    blocked_substrings = [
        # profanity
        "fuck", "fuk", "fck", "shit", "cunt", "bitch", "asshole", "dick", "pussy", "fucker",
        # sexual/explicit
        "porn", "xxx", "sex",
        # hate/slurs (include what you explicitly want disallowed)
        "nigger", "faggot", "kike", "spic", "chink",
        # violence/terror (brand safety)
        "isis", "nazis", "nazi", "kkk",
    ]

    # Reduce common obfuscation: "f_u-c_k" -> "fuck"
    condensed = safe.replace("_", "").replace("-", "")

    if safe in blocked_exact or condensed in blocked_exact:
        raise ValueError("That user name isn’t available. Choose another.")

    for tok in blocked_substrings:
        if tok in safe or tok in condensed:
            raise ValueError("That user name isn’t available. Choose another.")


@app.route("/api/profile/achievements-certs", methods=["POST"])
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
                    meta={"cert_key": k},
                )
            except Exception:
                pass

    return {"ok": True, "certs": certs, "added": added}

@app.route("/api/lounge/search")
def lounge_search():
    q = (request.args.get("q") or "").lower().strip()
    q = q.replace("@", "")

    if len(q) < 3:
        return {"items": []}

    d = _directory_read()
    out = []

    for u in d.values():
        if q in u["handle"]:
            out.append({
                "handle": u["handle"],
                "avatar_url": u["avatar_url"],
                "airports": u["airports"],
                "map": f"/@{u['handle']}",
                "achievements": f"/@{u['handle']}/achievements",
            })
        if len(out) >= 20:
            break

    return {"items": out}

@app.route("/api/lounge/spotlight", methods=["GET"])
def api_lounge_spotlight():
    d = _directory_read_cached()
    items = [v for v in d.values() if isinstance(v, dict) and v.get("handle")]
    if not items:
        return jsonify({"items": []})

    snap = _lounge_load_snapshot()
    ts = float(snap.get("ts") or 0.0)
    seats = snap.get("seats") if isinstance(snap.get("seats"), list) else []

    now = time.time()
    if (now - ts) >= LOUNGE_TTL_SECONDS or len(seats) != LOUNGE_SEATS:
        new_seats = _choose_next_lounge_seats(items, seats)
        _lounge_save_snapshot({"ts": now, "seats": new_seats})
        seats = new_seats

    out = []
    for u in seats:
        h = (u.get("handle") or "").strip().lower()
        if not h:
            continue
        out.append({
            "handle": h,
            "avatar_url": u.get("avatar_url") or f"/avatar/{h}",
            "airports": int(u.get("airports") or 0),
            "map": f"/u/{h}/map",
            "achievements": f"/u/{h}/achievements",
        })

    return jsonify({"items": out})


@app.route("/api/lounge/search", methods=["GET"])
def api_lounge_search():
    q = (request.args.get("q") or "").strip().lower()
    if q.startswith("@"):
        q = q[1:]
    q = re.sub(r"[^a-z0-9_-]", "", q)

    if len(q) < 3:
        return jsonify({"items": []})

    d = _directory_read_cached()
    items = [v for v in d.values() if isinstance(v, dict) and v.get("handle")]

    matches = []
    for u in items:
        h = (u.get("handle") or "").strip().lower()
        if q in h:  # substring: larry finds barronlarry, larrybarron
            matches.append({
                "handle": h,
                "avatar_url": u.get("avatar_url") or f"/avatar/{h}",
                "airports": int(u.get("airports") or 0),
                "map": f"/u/{h}/map",
                "achievements": f"/u/{h}/achievements",
            })
        if len(matches) >= 20:
            break

    return jsonify({"items": matches})

@app.route("/loading")
def route_loading():
    """
    Lightweight interstitial so users get immediate feedback while slow pages render.
    Usage: /loading?next=/map
    """
    next_path = (request.args.get("next") or "/app").strip() or "/app"
    if not next_path.startswith("/"):
        next_path = "/app"

     # --- TRACE: /loading interstitial usage ---
    try:
        print(f"[TRACE] /loading hit -> next={next_path} ua={request.headers.get('User-Agent','')[:80]}")
    except Exception:
        pass
    # --- END TRACE ---

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loading…</title>
  <style>
    body {{
      margin:0;
      background:#0f1115; color:#fff;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;
      display:flex; align-items:center; justify-content:center;
      min-height:100vh;
    }}
    .wrap {{
      text-align:center;
      padding:20px;
      max-width:520px;
    }}
    .spinner {{
      width:44px; height:44px;
      border-radius:999px;
      border:4px solid rgba(255,255,255,0.18);
      border-top-color: rgba(255,255,255,0.92);
      margin:0 auto 14px;
      animation: spin 0.9s linear infinite;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .title {{
      font-weight:950;
      font-size:18px;
      margin-bottom:6px;
    }}
    .muted {{
      color:#aab2c0;
      font-size:14px;
      line-height:1.45;
    }}
    a {{ color:#dbe9ff; text-decoration:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="spinner" aria-hidden="true"></div>
    <div class="title" id="ltitle">Loading…</div>
    <div class="muted">
      Building your map. If you’re not redirected automatically,
      <a href="{_html.escape(next_path)}">click here</a>.
    </div>
  </div>

  <script>
    (function () {{
      const steps = [
        "Loading…",
        "Loading airports…",
        "Plotting visits…",
        "Finalizing…"
      ];
      let i = 0;
      const el = document.getElementById("ltitle");

      const ticker = setInterval(function () {{
        i = (i + 1) % steps.length;
        if (el) el.textContent = steps[i];
      }}, 450);

      // Allow one paint frame before navigation
      setTimeout(function () {{
        clearInterval(ticker);
        window.location.href = "{_html.escape(next_path)}";
      }}, 80);
    }})();
  </script>
</body>
</html>
"""


@app.route("/me")
@login_required
def me():
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = claims.get("sub", "unknown")
    return Response(f"<h1>Signed in</h1><p>Clerk user: {user_id}</p>", mimetype="text/html")

@app.route("/map", strict_slashes=False)
@login_required
def map_page():
    """
    Private map for logged-in users.
    This must never route to /map/locked (there is no such route).
    """
    handle = (current_user_handle() or "").strip()
    if not handle:
        return redirect("/sign-in?next=/map", code=302)

    # Make sure their data scaffolding exists
    ensure_user_initialized(handle)

    # Render ONCE (Folium returns a complete HTML page)
    try:
        from time import perf_counter
    except Exception:
        perf_counter = None

    if perf_counter:
        t0 = perf_counter()
        html_out = generate_map_content(handle=handle, navbar_mode="owner")
        dt_ms = int((perf_counter() - t0) * 1000)
        try:
            qs = request.query_string.decode("utf-8")
        except Exception:
            qs = ""
        try:
            print(f"[TRACE] /map render ms={dt_ms} handle={handle} qs={qs}")
        except Exception:
            pass
    else:
        html_out = generate_map_content(handle=handle, navbar_mode="owner")

    return Response(html_out, mimetype="text/html")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon"
    )

@app.route("/index.html")
def route_map():
    # legacy path support
    qs = request.query_string.decode("utf-8")
    return redirect("/map" + (f"?{qs}" if qs else ""))

@app.route("/u/<handle>")
def public_profile(handle: str):
    # Hard stop on invalid handles (prevents weird Windows paths and bad writes)
    if not is_valid_handle(handle):
        return Response("<h2>Not found</h2>", mimetype="text/html", status=404)

    ensure_user_initialized(handle)

    blocked = require_public_share_access_or_owner(handle)
    if blocked:
        return blocked

    safe = _html.escape(handle)

    return Response(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>@{safe} • MyAirportMap</title>
  <style>
    body {{ margin:0; padding:0; font-family:system-ui, -apple-system, Segoe UI, Roboto, Arial; background:#fff; color:#111; }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 92px 16px 32px; }}
    .card {{ border:1px solid #eee; border-radius:16px; padding:18px; background:#fff; }}
    .btns {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:8px; }}
    .btn {{ display:inline-block; padding:12px 14px; border-radius:12px; text-decoration:none; font-weight:800; }}
    .btn.primary {{ background:#111; color:#fff; }}
    .btn.secondary {{ background:#f3f3f3; color:#111; }}
    .muted {{ opacity:0.85; line-height:1.5; }}
    @media (max-width: 520px) {{
      .wrap {{ padding-top: 88px; }}
      .btn {{ width:100%; text-align:center; }}
    }}
  </style>
</head>
<body>
  {get_public_navbar(handle, "map")}
  <div class="wrap">
    <div class="card"><center>
      <h2 style="margin:0 0 8px;">@{safe}</h2>
      <div class="muted">View this pilot’s MyAirportMap profile. Use the buttons below to switch between the map and achievements.</div>
      <div class="btns">
        <a class="btn primary" href="/u/{safe}/map">Map</a>
        <a class="btn secondary" href="/u/{safe}/achievements">Achievements</a>
       </center>
      </div>
    </div>
  </div>
</body>
</html>
""", mimetype="text/html")

@app.route("/logo.png")
def serve_logo():
    return send_from_directory(BASE_DIR, "logo.png")

@app.route("/settings/privacy", methods=["POST"])
@login_required
def route_settings_privacy():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return redirect("/app", code=302)

    # Checkbox semantics:
    # - checked => field present
    # - unchecked => field missing
    enabled = bool(request.form.get("share_activity"))

    _set_share_activity(handle, enabled)

    # Optional: clear legacy key so nothing else "reads the wrong thing"
    try:
        s = _read_json_r2(_settings_key(handle)) or {}
        if "public_share_enabled" in s:
            s.pop("public_share_enabled", None)
            _write_json_r2(_settings_key(handle), s)
    except Exception:
        pass

    # Debug proof (remove after confirmed)
    try:
        after = _get_share_activity(handle)
        print("[PRIVACY] handle=", handle, "enabled=", enabled, "after=", after, "key=", _settings_key(handle))
    except Exception:
        pass

    return redirect("/logbook/manage", code=302)

@app.route("/achievements")
@login_required
def route_achievements():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        # Logged-in guard should usually prevent this, but keep it safe.
        return redirect("/sign-in?next=/achievements")

    # Gate: achievements require active access (as you had)
    if not has_active_access(handle):
        cur = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
        if not cur.startswith("/"):
            cur = "/achievements"
        return redirect("/trial/ended?next=" + quote(cur, safe="/=?&"), code=302)

    # ✅ FORCE the correct per-user visits path (R2/local) into the badge generator
    visits_path = resolve_visits_csv(handle)

    # --- Map41: Achievements certifications (checkbox-driven, achievements-only) ---
    cert_line = ""
    try:
        prefs = _load_json_from_storage(_profile_prefs_key(handle))
        selected = prefs.get("achievements_certs") or []
        if not isinstance(selected, list):
            selected = []
        selected = _validate_cert_keys([str(x) for x in selected])
        cert_line = format_certifications_line(selected, username=handle)
    except Exception:
        cert_line = ""

    # Optional debug (remove later if you want)
    print("[ACH] handle=", handle, "visits_path=", visits_path, "active=", has_active_access(handle))

    body_html = generate_badges_content(
        visits_csv=visits_path,
        handle=handle,
        navbar_mode="owner",
        certifications_line=cert_line,  # ✅ Map41 additive param
    )

    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Achievements · MyAirportMap</title>

  <!-- ✅ Map40: prevent fixed navbar overlap on all sizes -->
  <style>
    body {{ padding-top: var(--mam-nav-h, 96px) !important; }}
  </style>

</head>
{body_html}
</html>""",
        mimetype="text/html",
    )


@app.get("/download")
@login_required
def download_visits():
    handle = (current_user_handle() or "").strip()
    if not handle:
        return redirect("/app")

    path = resolve_visits_csv(handle)
    data = _read_visits_bytes(path, handle=handle) or b""

    resp = send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name="my_visits.csv",
        max_age=0,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/logbook", methods=["GET"], endpoint="logbook_root")
@login_required
def route_logbook_root():
    """Private logbook dashboard: search + recent visits + community badges. Heavy editing/uploads live in /logbook/manage."""

    # Canonical handle for ALL data access
    handle = current_user_handle()

    try:
        emit_milestone_once(handle, "joined", "Joined MyAirportMap")
    except Exception:
        pass

    # Display-only label (never used for storage keys)
    try:
        display = (current_user_display_handle() or "").strip()
    except Exception:
        display = ""


    # Display-only label (never used for storage keys)
    try:
        display = (current_user_display_handle() or "").strip()
    except Exception:
        display = ""

    # ✅ Map40: /logbook must ALWAYS be reachable (it's how new users become "not new").
    # Do NOT redirect to /welcome from here; instead keep a flag for optional inline messaging.
    show_welcome_banner = False
    try:
        show_welcome_banner = bool(should_show_welcome())
    except Exception:
        show_welcome_banner = False

    q = (request.args.get("q") or "").strip()

    visits_path = resolve_visits_csv(handle)
    df_visits = _load_visits_csv(visits_path, handle=handle).reset_index(drop=True)

    # First airport (when they have at least 1 visit)
    if not df_visits.empty and "airport_id" in df_visits.columns:
        first_airport = str(df_visits.iloc[0].get("airport_id", "")).strip().upper()
        if first_airport:
            emit_milestone_once(handle, "first_airport", "Logged first airport", {"airport_id": first_airport})

    # First state (optional; only if helper exists in this codebase)
    try:
        fn_state = globals().get("derive_first_state_from_visits")
        st = fn_state(df_visits) if callable(fn_state) else None
        if st:
            emit_milestone_once(handle, "first_state", "Unlocked first state", {"state": str(st)})
    except Exception:
        pass

    # First upload (best proxy: they have a non-empty visits csv where previously they didn’t)
    # Since we’re not backfilling, we just treat "df_visits not empty" as "first upload happened"
    if not df_visits.empty:
        emit_milestone_once(handle, "first_upload", "Uploaded a logbook")

    # Search (best-effort across key columns)
    search_rows_html = ""
    if q:
        q_up = q.upper()
        cols = [c for c in ["airport_id", "date_visited", "callsign", "notes"] if c in df_visits.columns]
        if cols:
            mask = False
            for c in cols:
                mask = mask | df_visits[c].astype(str).str.upper().str.contains(q_up, na=False)
            hits = df_visits.loc[mask].copy()
        else:
            hits = df_visits.copy()
        hits = hits.tail(200).reset_index(drop=True)
        if hits.empty:
            search_rows_html = f"<div class=\"muted\">No matches for <b>{_html.escape(q)}</b>.</div>"
        else:
            rows = []
            for _, r in hits.iterrows():
                rows.append(
                    f"<tr><td>{_html.escape(str(r.get('date_visited','')))}</td>"
                    f"<td><b>{_html.escape(str(r.get('airport_id','')))}</b></td>"
                    f"<td>{_html.escape(str(r.get('callsign','')))}</td>"
                    f"<td>{_linkify(str(r.get('notes','')))}</td></tr>"
                )
            search_rows_html = (
                "<table class=\"table\"><tr><th>Date</th><th>Airport</th><th>Callsign</th><th>Notes</th></tr>"
                + "".join(rows)
                + "</table>"
            )

    # -----------------------------
    # Backward-compat alias
    # (old name used by earlier MapXX code / templates)
    # -----------------------------
    def get_global_activity_events(*args, **kwargs):
        return get_global_badge_events(*args, **kwargs)

    # -----------------------------
    # Pilot’s Lounge: Milestones feed (global, read-only)
    # Layout: avatar | username | milestone
    # No per-flight activity. No repetition.
    # -----------------------------
    milestones = []
    try:
        fn = globals().get("get_global_milestone_events")
        milestones = fn(limit=20) if callable(fn) else []
    except Exception:
        milestones = []

    ms_rows = []
    for e in (milestones or []):
        h_raw = str(e.get("handle", "")).strip().lower()
        if not h_raw:
            continue
        h = _html.escape(h_raw)

        label = (str(e.get("label", "")) or "").strip() or "Milestone"
        label = _html.escape(label)

        # Optional tiny context (kept quiet)
        airport_id = (str(e.get("airport_id", "")) or "").strip().upper()
        state = (str(e.get("state", "")) or "").strip().upper()
        extra = ""
        if airport_id:
            extra = f" ({_html.escape(airport_id)}"
            if state:
                extra += f", {_html.escape(state)}"
            extra += ")"
        label = label + extra

        ms_rows.append(
            f"""
            <div style="display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid #2a2a2a;">
              <a href="/u/{h}" style="display:inline-flex; align-items:center; text-decoration:none;">
                <img src="/avatar/{h}" alt="@{h}"
                     style="width:28px; height:28px; border-radius:999px; border:1px solid #333; background:#0a0a0a; object-fit:cover;">
              </a>
              <a href="/u/{h}" style="color:#fff; text-decoration:none; font-weight:800;">@{h}</a>
              <div style="color:#cfcfcf; font-size:14px; flex:1;">{label}</div>
            </div>
            """
        )

    milestones_html = (
        "<div class=\"muted\">No milestones yet.</div>"
        if not ms_rows
        else "<div>" + "".join(ms_rows) + "</div>"
    )

    # -----------------------------
    # Pilot’s Lounge / Global feed filtering
    # -----------------------------
    def _is_achievement_badge(label_raw: str) -> bool:
        s = (label_raw or "").strip().lower()
        if not s:
            return False

        # State completion
        if "state" in s and ("complete" in s or "completed" in s or "100%" in s):
            return True

        # Runway 360
        if ("runway 360" in s or "runway360" in s) and ("club" in s or "complete" in s or "completed" in s or "36/36" in s
            ):
            return True


        # Bravo, Bravo! (Class B completion)
        if (
            "bravo" in s
            and ("class b" in s or "class-b" in s or "b airports" in s or "airspace b" in s or "bravo, bravo" in s)
            and ("complete" in s or "completed" in s or "100%" in s)
        ):
            return True

        # Ratings / certificates
        if "rating" in s or "certificate" in s or "certification" in s:
            return True

        return False
    
    global_events = []
    try:
        global_events = get_global_activity_events(limit=80) or []
    except Exception as e:
        print("[global_events][err]", repr(e))
        global_events = []

    ge_rows = []
    for e in (global_events or []):
        label_raw = str(e.get("badge_label", "") or "")
        if not _is_achievement_badge(label_raw):
            continue

        h_raw = str(e.get("handle", "")).strip().lower()
        if not h_raw:
            continue

        h = _html.escape(h_raw)
        label = _html.escape(label_raw.strip())

        ge_rows.append(f"<tr><td><a href=\"/u/{h}/map\">@{h}</a></td><td>{label}</td></tr>")

    ge_html = (
        "<div class=\"muted\">No achievements yet.</div>"
        if not ge_rows
        else (
            "<table class=\"table\">"
            "<tr><th>Pilot</th><th>Achievement</th></tr>"
            + "".join(ge_rows)
            + "</table>"
        )
    )


    # Display text only
    handle_display = _html.escape((display or "").lstrip("@") or handle)

    return Response(
 
        f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pilot's Lounge</title>

<style>
  /* -----------------------------
     Pilot's Lounge (Map41)
     ----------------------------- */

  body {{
    background:#0f0f0f;
    color:#fff;
    font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    margin:0;
    padding-top:70px;
  }}

  .container {{
    max-width:1000px;
    margin:0 auto;
    padding:18px;
  }}

  h1 {{
    margin:0 0 6px;
    font-size:28px;
  }}

  .muted {{
    color:#a0a0a0;
    font-size:14px;
  }}

  .row {{
    display:flex;
    gap:12px;
    flex-wrap:wrap;
    align-items:center;
  }}

  .card {{
    background:#151515;
    border:1px solid #2a2a2a;
    border-radius:18px;
    padding:14px;
    margin:12px 0;
  }}

  .btn {{
    display:inline-block;
    padding:10px 12px;
    border-radius:12px;
    background:#1f1f1f;
    border:1px solid #3a3a3a;
    color:#fff;
    text-decoration:none;
    font-weight:800;
  }}
  .btn:hover {{ border-color:#666; }}

  .input {{
    width:100%;
    box-sizing:border-box;
    padding:12px 14px;
    border-radius:14px;
    background:#0a0a0a;
    border:1px solid #333;
    color:#fff;
    font-size:15px;
    outline:none;
  }}

  .grid2 {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
  }}

  @media (max-width:640px) {{
    .grid2 {{ grid-template-columns:1fr; }}
    body {{ padding-top:76px; }}
    h1 {{ font-size:24px; }}
  }}

  .table {{
    width:100%;
    border-collapse:collapse;
    font-size:14px;
  }}
  .table th,
  .table td {{
    border-bottom:1px solid #2a2a2a;
    padding:10px 8px;
    vertical-align:middle;
  }}
  .table th {{
    color:#cfcfcf;
    text-align:left;
    font-size:12px;
    letter-spacing:0.04em;
    text-transform:uppercase;
  }}

  /* Lounge-specific */
  .lounge-empty {{
    color:#a0a0a0;
    font-size:14px;
    margin-top:8px;
  }}

  .lounge-table img {{
    width:34px;
    height:34px;
    border-radius:10px;
    object-fit:cover;
    border:1px solid #2a2a2a;
    background:#0a0a0a;
    display:block;
  }}

  .lounge-table .pill {{
    display:inline-block;
    padding:6px 10px;
    border-radius:999px;
    background:#0a0a0a;
    border:1px solid #2a2a2a;
    font-weight:800;
    font-size:13px;
    color:#d7dbe3;
    min-width:52px;
    text-align:center;
  }}

  .lounge-table a {{
    color:#9ad;
    text-decoration:none;
    font-weight:800;
  }}
  .lounge-table a:hover {{ text-decoration:underline; }}

@media (max-width:640px) {{
  .table th,
  .table td {{ padding:9px 6px; }}

  html, body {{ overflow-x: hidden; }}
  .container {{ max-width: 100%; }}

  /* ✅ Let the browser size columns naturally (prevents stacked letters) */
  .table {{
    table-layout: auto;
    width: 100%;
    max-width: 100%;
    box-sizing: border-box;
  }}
  .table th, .table td {{ box-sizing: border-box; }}

  /* ✅ Never break inside words/handles */
  .lounge-table td {{
    overflow-wrap: normal;
    word-break: normal;
    hyphens: none;
  }}

  /* ✅ Keep handles + links on one line */
  .lounge-table a,
  .lounge-table b,
  .lounge-handle {{
    white-space: nowrap;
    display: inline-block;
  }}

  /* ✅ If something is still tight, reduce header font slightly */
  .table th {{
    font-size: 11px;
    white-space: nowrap;
  }}
}}
</style>


</head>
<body>
  {get_navbar("logbook", handle=handle)}
  <div class="container">
    <h1>Pilot's Lounge <span style="font-size:14px; color:#a0a0a0; font-weight:700;">@{handle_display}</span></h1>
    <div class="muted">Private dashboard. Uploads/edits live in <a href="/logbook/manage" style="color:#9ad;">Manage</a>.</div>

    <div class="card">
      <div class="row">
        <a class="btn" href="/logbook/manage">Manage (Enter/Upload) Visits</a>
        <a class="btn" href="/download">Download my_visits.csv</a>
        <a class="btn" href="/share">Share</a>
      </div>
      <div style="height:10px;"></div>
      <form method="get" action="/logbook">
        <input class="input" name="q" value="{_html.escape(q)}" placeholder="Search: airport, date, callsign, notes…" />
      </form>
      <div style="height:10px;"></div>
      {search_rows_html}
    </div>

    <div class="card">
      <div style="font-weight:900; margin-bottom:8px;">Currently in the MyAirportMap Pilot’s Lounge</div>
      <div class="muted" style="margin-bottom:10px;">
        Opt-in pilots only · Four seats in the lounge · One user departs every 6 hours · Sequence is random
      </div>

      <div id="lounge-spotlight-wrap">
        <div class="lounge-empty">Loading…</div>
      </div>

      <hr style="margin:14px 0;">

      <div style="font-weight:900; margin-bottom:8px;">Find a pilot</div>
      <input id="lounge-search" class="input" placeholder="Search handle (min 3 characters)" autocomplete="off" />
      <div id="lounge-results-wrap" style="margin-top:10px;">
        <div class="lounge-empty">Type at least 3 characters to search.</div>
      </div>
    </div>

<script>
(function () {{
  function esc(s) {{
    return String(s || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }}

  function renderTable(items) {{
    if (!items || !items.length) {{
      return '<div class="lounge-empty">No pilots found.</div>';
    }}

    const rows = items.map(u => {{
      const handle = esc(u.handle);
      const avatar = esc(u.avatar_url || "");
      const airports = Number(u.airports || 0);

      // Map41: explicit /map route
      const mapUrl = esc(u.map || ("/u/" + handle + "/map"));

      return `
        <tr>
          <td class="lounge-td-avatar">
            <img class="lounge-avatar"
                 src="${{avatar}}"
                 onerror="this.onerror=null;this.src='/static/mam-logo.png';"
                 alt="Avatar">
          </td>

        <td class="lounge-td-user">
        <a class="lounge-handle" href="/u/${{handle}}/achievements">@${{handle}}</a>
        </td>


          <td class="lounge-td-airports">
            <span class="pill">${{airports}}</span>
          </td>

          <td class="lounge-td-map">
            <a href="${{mapUrl}}">Map</a>
          </td>
        </tr>
      `;
    }}).join("");

    return `
      <table class="table lounge-table">
        <colgroup>
            <col style="width:44px;">
            <col>
            <col style="width:140px;">
            <col style="width:64px;">
        </colgroup>

        <tr>
          <th></th>
          <th>Username</th>
          <th>Airports</th>
          <th>Map</th>
        </tr>

        ${{rows}}
      </table>
    `;
  }}

  async function loadSpotlight() {{
    const wrap = document.getElementById("lounge-spotlight-wrap");
    try {{
      const r = await fetch("/api/lounge/spotlight", {{ cache: "no-store" }});
      const j = await r.json();
      wrap.innerHTML = renderTable(j.items || []);
    }} catch (e) {{
      wrap.innerHTML = '<div class="lounge-empty">Unable to load lounge pilots.</div>';
      console.error(e);
    }}
  }}

  let t = null;
  async function doSearch(q) {{
    const wrap = document.getElementById("lounge-results-wrap");
    q = (q || "").trim();

    if (q.startsWith("@")) q = q.slice(1);
    if (q.length < 3) {{
      wrap.innerHTML = '<div class="lounge-empty">Type at least 3 characters to search.</div>';
      return;
    }}

    wrap.innerHTML = '<div class="lounge-empty">Searching…</div>';

    try {{
      const r = await fetch("/api/lounge/search?q=" + encodeURIComponent(q), {{ cache: "no-store" }});
      const j = await r.json();
      wrap.innerHTML = renderTable(j.items || []);
    }} catch (e) {{
      wrap.innerHTML = '<div class="lounge-empty">Search failed. Try again.</div>';
      console.error(e);
    }}
  }}

  document.addEventListener("DOMContentLoaded", function () {{
    loadSpotlight();

    const inp = document.getElementById("lounge-search");
    if (!inp) return;

    inp.addEventListener("input", function () {{
      const q = inp.value || "";
      clearTimeout(t);
      t = setTimeout(() => doSearch(q), 220);
    }});
  }});
}})();
</script>

    <div class="grid2">
        <div class="card">
        <div style="font-weight:900; margin-bottom:8px;">Milestones</div>
        <div class="muted" style="margin-bottom:8px;">Highlights shared in the MyAirportMap Lounge.</div>
        {milestones_html}
      </div>

      <div class="card">
        <div style="font-weight:900; margin-bottom:8px;">Recent Achievements</div>
        <div class="muted" style="margin-bottom:8px;">States · Runway 360 · Ratings (shared only).</div>
        {ge_html}
      </div>
    </div>
  </div>
</body></html>""",
        mimetype="text/html",
    )

@app.route("/logbook/manage", methods=["GET"])
@login_required
def route_logbook_manage():
    handle = current_user_handle()
    content = generate_logbook_manage_content(handle=handle)

    return Response(
        f"""<!doctype html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <!-- ✅ Map40: prevent fixed navbar overlap -->
    <style>
        body {{ padding-top: var(--mam-nav-h, 96px) !important; }}
    </style>
    </head>
    {content}
    </html>""",
        mimetype="text/html",
    )


@app.get("/_debug/ping")
def _debug_ping():
    return "pong", 200

@app.get("/download/foreflight")
@login_required
def download_foreflight():
    handle = (current_user_handle() or "").strip()
    if not handle:
        return redirect("/app")

    ff_path = resolve_foreflight_csv(handle)
    raw = _read_foreflight_bytes(ff_path, handle=handle)

    if not raw:
        raw = write_foreflight_import_csv_bytes([])
        _write_foreflight_bytes(ff_path, raw, handle=handle)

    return Response(
        raw,
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="foreflight_logbook.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/logbook/manage/upload", methods=["POST"])
@login_required
def route_logbook_manage_upload():
    handle = current_user_handle()
    claims = getattr(request, "clerk_claims", {}) or {}
    print("[UPLOAD] start user_id=", (claims.get("sub") or "")[:12], " handle=", handle, " host=", request.host)

    # ✅ MUST exist before try/except uses it
    last_import = {
        "ts": "",
        "type": "",
        "filename": "",
        "visits_written": 0,
        "unique_airports": 0,
        "error": None,
    }

    try:
        try:
            ensure_user_initialized(handle)
        except Exception:
            pass

        ts = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        last_import["ts"] = ts
        up = request.files.get("file")

        if not up or not getattr(up, "filename", ""):
            raise ValueError("No file uploaded.")

        filename = (up.filename or "").strip()
        last_import["filename"] = filename
        ext = os.path.splitext(filename.lower())[1].strip()

        df_out = pd.DataFrame(columns=["airport_id", "date_visited", "callsign", "notes"])

        if ext == ".csv":
            last_import["type"] = "CSV"
            raw_bytes = up.read() or b""
            if not raw_bytes:
                raise ValueError("Uploaded CSV was empty.")
            try:
                s = raw_bytes.decode("utf-8-sig", errors="strict")
            except Exception:
                s = raw_bytes.decode("latin-1", errors="replace")
            df_out = parse_foreflight_logbook_csv(s)

        elif ext == ".pdf":
            last_import["type"] = "PDF"
            raw_bytes = up.read() or b""
            if not raw_bytes:
                raise ValueError("Uploaded PDF was empty.")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name
            try:
                df_out = parse_foreflight_complete_logbook_pdf(tmp_path)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        else:
            raise ValueError("Unsupported file type. Please upload a .csv or .pdf.")

        for c in ["airport_id", "date_visited", "callsign", "notes"]:
            if c not in df_out.columns:
                df_out[c] = ""

        df_out["airport_id"] = df_out["airport_id"].astype(str).str.strip().str.upper()
        df_out["date_visited"] = df_out["date_visited"].astype(str).str.strip()
        df_out["callsign"] = df_out["callsign"].astype(str).fillna("").astype(str)
        df_out["notes"] = df_out["notes"].astype(str).fillna("").astype(str)

        if not df_out.empty:
            df_out = df_out.drop_duplicates(
                subset=["airport_id", "date_visited"],
                keep="first"
            ).reset_index(drop=True)

        parsed_n = int(len(df_out))
        print("[UPLOAD] parsed rows=", parsed_n,
              " unique_airports=", int(df_out["airport_id"].nunique()) if parsed_n else 0)

        # HARD SAFETY
        if parsed_n == 0:
            last_import["error"] = "Parsed 0 rows from upload. Did NOT overwrite my_visits.csv."
            session["last_import"] = last_import
            return redirect("/logbook/manage#upload")

        if parsed_n < 3 and (request.form.get("confirm_small", "") != "1"):
            last_import["warning"] = (
                f"Parsed only {parsed_n} visit(s). For safety, MyAirportMap DID NOT overwrite my_visits.csv. "
                "Re-upload the same file and check the confirmation box to proceed."
            )
            last_import["visits_parsed"] = parsed_n
            last_import["unique_airports"] = int(df_out["airport_id"].nunique())
            session["last_import"] = last_import
            return redirect("/logbook/manage#upload")

        path = resolve_visits_csv(handle)
        _write_visits_csv(df_out, path, handle=handle)

        # ✅ Cache invalidation only after a successful write
        _map_cache_clear()

        # ✅ Map41: refresh lounge directory stats after visits import
        # (handles opt-in removal when share_off; updates airports count when share_on)
        _directory_refresh_for_handle(handle)

        try:
            emit_milestone_once(
                handle,
                "first_logbook_upload",
                "First logbook upload",
                meta={"filename": filename, "type": last_import.get("type", "")},
            )
        except Exception:
            pass

        last_import["visits_written"] = parsed_n
        last_import["unique_airports"] = int(df_out["airport_id"].nunique())
        session["last_import"] = last_import

        print("[UPLOAD] wrote visits to path=", path, " rows=", parsed_n)
        return redirect("/logbook/manage#upload")

    except Exception as e:
        last_import["error"] = str(e)
        session["last_import"] = last_import
        return redirect("/logbook/manage#upload")


@app.route("/logbook/manage/add", methods=["POST"])
@login_required
def route_logbook_manage_add():
    date_visited = _coerce_date(request.form.get("date_visited", "") or request.form.get("date", ""))
    airport_id = normalize_airport(request.form.get("airport_id", ""))
    # optional validation: warn if airport isn't in df_conus
    df_conus, _ = load_data()
    valid_ids = set(df_conus["airport_id"].astype(str).str.upper())
    warning = ""
    if airport_id and airport_id not in valid_ids:
        warning = f"Saved, but '{airport_id}' was not found in airports database."
    callsign = (request.form.get("callsign", "") or "").strip()
    notes = (request.form.get("notes", "") or "").strip()

    if not airport_id:
        return Response(generate_logbook_manage_content("Airport is required."), mimetype="text/html")

    handle = current_user_handle()
    # Private logging should remain usable even if the trial ended.

    path = resolve_visits_csv(handle)

    df = _load_visits_csv(path, handle=handle)
    df.loc[len(df)] = {
        "airport_id": airport_id,
        "date_visited": date_visited,
        "callsign": callsign,
        "notes": notes,
    }
    _write_visits_csv(df, path, handle=handle)
    try:
        _directory_refresh_for_handle(handle)
    except Exception:
        pass

    if warning:
        return redirect("/logbook/manage?msg=" + quote(warning, safe=""))

    return redirect("/logbook/manage")


def _is_logged_in() -> bool:
    claims = getattr(request, "clerk_claims", {}) or {}
    if (claims.get("sub") or "").strip():
        return True
    # Public-route best-effort (do NOT attach)
    try:
        c = verify_clerk_session(request)
        return bool((c.get("sub") or "").strip()) if c else False
    except Exception:
        return False


@app.post("/logbook/manage/add-flight")
@login_required
def route_logbook_manage_add_flight():
    # Canonical handle resolution (auth-required route)
    handle = current_user_handle()
    # Private logging should remain usable even if the trial ended.

    # ---- gather inputs (single entry) ----
    date_in = (request.form.get("Date") or "").strip()
    if not date_in:
        date_in = datetime.now().strftime("%m/%d/%Y")
    aircraft = (request.form.get("AircraftID") or "").strip().upper()
    frm = normalize_airport((request.form.get("From") or "").strip().upper())
    to  = normalize_airport((request.form.get("To") or "").strip().upper())
    route = ""  # Route entry removed from manual UI (still supported via imports)
    comments = (request.form.get("PilotComments") or "").strip()

    date_norm = _coerce_date(date_in)

    # ✅ validate before any I/O
    if (not frm) and (not to):
        return Response(
            generate_logbook_manage_content("Enter at least From or To.", handle=handle),
            mimetype="text/html",
        )

    # ---- 1) Append flight to ForeFlight logbook ----
    ff_path = resolve_foreflight_csv(handle)
    raw = _read_foreflight_bytes(ff_path, handle=handle)

    if raw:
        _, header_cols, ff_rows = read_foreflight_import_csv_bytes(raw)
    else:
        header_cols, ff_rows = None, []

    new_row = {c: "" for c in FORE_FLIGHT_COLUMNS}
    new_row["Date"] = date_norm
    new_row["AircraftID"] = aircraft
    new_row["From"] = frm
    new_row["To"] = to
    new_row["Route"] = route
    new_row["PilotComments"] = comments
    ff_rows.append(new_row)

    ff_bytes = write_foreflight_import_csv_bytes(
        ff_rows,
        preserve_extra_columns=True,
        extra_columns=header_cols,
    )
    _write_foreflight_bytes(ff_path, ff_bytes, handle=handle)

    # ---- 2) Derive visits for map and merge into my_visits.csv ----
    new_visits = foreflight_rows_to_visits_df([new_row])

    visits_path = resolve_visits_csv(handle)
    visits_existing = _load_visits_csv(visits_path, handle=handle)

    merged = pd.concat([visits_existing, new_visits], ignore_index=True)
    if not merged.empty:
        merged["airport_id"] = merged["airport_id"].astype(str).str.upper()
        merged["date_visited"] = merged["date_visited"].astype(str).str.strip()
        merged = merged.drop_duplicates(
            subset=["airport_id", "date_visited"],
            keep="first",
        ).reset_index(drop=True)

    _write_visits_csv(merged, visits_path, handle=handle)
    try:
        _directory_refresh_for_handle(handle)
    except Exception:
        pass

    return redirect("/logbook/manage?msg=Flight logged (map updated)")

@app.route("/logbook/manage/edit", methods=["POST"])
@login_required
def route_logbook_manage_edit():
    idx_raw = request.form.get("row_index", "")
    try:
        idx = int(idx_raw)
    except Exception:
        return Response(generate_logbook_manage_content("Invalid edit request."), mimetype="text/html")

    date_visited = _coerce_date_yyyy_mm_dd(request.form.get("date_visited", ""))
    airport_id = normalize_airport(request.form.get("airport_id", ""))
    callsign = (request.form.get("callsign", "") or "").strip()
    notes = (request.form.get("notes", "") or "").strip()

    handle = current_user_handle()
    path = resolve_visits_csv(handle)
    df = _load_visits_csv(path, handle=handle).reset_index(drop=True)

    if idx < 0 or idx >= len(df):
        return Response(generate_logbook_manage_content("Edit row not found.", handle=handle), mimetype="text/html")
    if not airport_id:
        return Response(generate_logbook_manage_content("Airport is required.", handle=handle), mimetype="text/html")

    df.loc[idx, "airport_id"] = airport_id
    df.loc[idx, "date_visited"] = date_visited
    df.loc[idx, "callsign"] = callsign
    df.loc[idx, "notes"] = notes

    _write_visits_csv(df, path, handle=handle)
    try:
        _directory_refresh_for_handle(handle)
    except Exception:
        pass

    return redirect("/logbook/manage")

@app.route("/logbook/manage/delete", methods=["POST"])
@login_required
def route_logbook_manage_delete():
    idx_raw = request.form.get("row_index", "")
    try:
        idx = int(idx_raw)
    except Exception:
        return Response(generate_logbook_manage_content("Invalid delete request."), mimetype="text/html")

    handle = current_user_handle()
    path = resolve_visits_csv(handle)

    df = _load_visits_csv(path, handle=handle).reset_index(drop=True)
    if idx < 0 or idx >= len(df):
        return Response(generate_logbook_manage_content("Delete row not found.", handle=handle), mimetype="text/html")

    # Undo (Option A): backup the whole file before we mutate it
    try:
        raw_before = _read_visits_bytes(path, handle=handle)
        if raw_before:
            _write_undo_visits_bytes(path, raw_before, handle=handle)
    except Exception:
        # Never block deletes due to backup failure
        pass

    df = df.drop(index=idx).reset_index(drop=True)
    _write_visits_csv(df, path, handle=handle)
    try:
        _directory_refresh_for_handle(handle)
    except Exception:
        pass

    return redirect("/logbook/manage")

def is_owner_view(handle: str) -> bool:
    """
    True if the current request is from the owner of this profile.
    Owner means: authenticated + current_user_handle() matches the URL handle.
    """
    try:
        viewer = (current_user_handle() or "").strip().lower()
    except Exception:
        viewer = ""
    return bool(viewer) and viewer == (handle or "").strip().lower()

def render_trial_ended_page(next_path: str = "/") -> str:
    next_path = (next_path or "/").strip() or "/"
    if not next_path.startswith("/"):
        next_path = "/"
    up = "/upgrade?next=" + quote(next_path, safe="/=?&")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Upgrade</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background:#0f1115; color:#fff; margin:0; }}
    .wrap {{ max-width:820px; margin:50px auto; padding:0 16px; }}
    .card {{ background:#171a21; border:1px solid #2a2f3a; border-radius:16px; padding:18px; }}
    .btn {{ display:inline-block; padding:12px 14px; border-radius:12px; text-decoration:none; font-weight:800; }}
    .primary {{ background:#2b7cff; color:#fff; }}
    .muted {{ color:#aab2c0; font-size:14px; line-height:1.5; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0 0 10px; font-size:28px;">Upgrade to unlock public sharing</h1>
      <p class="muted" style="margin:0 0 10px;">
        Public profiles (map + achievements) are available after upgrading.
      </p>
      <div class="row">
        <a class="btn primary" href="{up}">Upgrade</a>
        <a class="muted" href="/logbook" style="text-decoration:none;">Back to Logbook</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

def require_public_share_access_or_owner(handle: str):
    """
    Gate for /u/<handle> public-facing pages.

    Map41 Option A (public is public):
      - demo allowed
      - owner can always preview
      - public viewers must have:
          1) sharing enabled (opt-in)
          2) paid membership (no trial access to public pages)
    """
    h = (handle or "").strip().lower()
    if not h:
        return Response("<h2>Not found</h2>", mimetype="text/html", status=404)

    # Special demo handle is always public
    if h == "demo":
        return None

    if not is_valid_handle(h):
        return Response("<h2>Not found</h2>", mimetype="text/html", status=404)

    # Owner preview is always allowed (even if sharing is OFF)
    try:
        if is_owner_view(h):
            return None
    except Exception:
        pass

    # Public viewers: must be explicitly shared (opt-in)
    try:
        share_ok = is_public_share_enabled(h)
    except Exception:
        share_ok = False

    if not share_ok:
        safe = _html.escape(h)
        return Response(
            f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>@{safe} · Private</title>
  <style>
    body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#fff; color:#111; }}
    .wrap {{ max-width: 860px; margin: 0 auto; padding: 92px 16px 28px; }}
    .card {{ border:1px solid #eee; border-radius:16px; padding:18px; }}
    .muted {{ opacity:.82; line-height:1.5; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h2 style="margin:0 0 8px;">@{safe} is private</h2>
      <div class="muted">This pilot hasn’t enabled community sharing.</div>
    </div>
  </div>
</body></html>""",
            mimetype="text/html",
            status=404,
        )

    # Public viewers: must be paid (no trial access)
    if not is_paid_user_handle(h):
        cur = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
        return Response(render_trial_ended_page(next_path=cur), mimetype="text/html", status=402)

    return None


def load_user_settings(handle: str) -> dict:
    """
    Load per-user settings from users/<handle>/settings.json.

    Works in both:
      - Local filesystem (via storage_backend)
      - R2 (via storage_backend)

    Returns {} if missing or invalid.
    """
    h = (handle or "").strip().lower()
    if not h:
        return {}

    key = _settings_key(h)

    try:
        raw = storage_backend.read_bytes(key)  # returns bytes or None
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_user_settings(handle: str, patch: dict) -> dict:
    """
    Merge-update settings.json for a handle.
    Safe + best-effort: never throws.
    """
    h = (handle or "").strip().lower()
    if not h:
        return {}

    cur = load_user_settings(h) or {}
    if isinstance(patch, dict):
        cur.update(patch)

    try:
        storage_backend.write_bytes(
            _settings_key(h),
            json.dumps(cur, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            cache_control="no-store",
        )
    except Exception:
        pass

    return cur

@app.route("/logbook/manage/undo-delete", methods=["POST"])
@login_required
def route_logbook_manage_undo_delete():
    handle = current_user_handle()
    path = resolve_visits_csv(handle)

    raw = None
    try:
        raw = _read_undo_visits_bytes(path, handle=handle)
    except Exception:
        raw = None

    if not raw:
        return Response(
            generate_logbook_manage_content("Nothing to undo (no backup found).", handle=handle),
            mimetype="text/html",
        )

    # Restore and clear backup
    try:
        _write_visits_bytes(path, raw, handle=handle)
        try:
            _directory_refresh_for_handle(handle)
        except Exception:
            pass
    finally:
        _delete_undo_visits(path, handle=handle)

    return redirect("/logbook/manage?msg=Undo complete")

@app.route("/health")
def health():
    return {"ok": True, "app": APP_TITLE}

@app.route("/share", methods=["GET", "POST"])
@login_required
def route_share():
    # Canonical handle for storage/access + public URL slug
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return redirect("/app", code=302)

    # Display-only (nice for UI, never used for entitlement/storage)
    try:
        display = (current_user_display_handle() or "").strip()
    except Exception:
        display = ""

    if not is_paid_user_handle(handle):
        return redirect("/share/locked", code=302)

    save_msg = ""
    save_ok = None  # None/True/False

    # --- POST: save toggle ---
    if request.method == "POST":
        enabled = bool(request.form.get("share_activity"))  # checked => True, missing => False
        try:
            _set_share_activity(handle, enabled)
            after = _get_share_activity(handle)
            save_ok = (bool(after) == bool(enabled))
            save_msg = "Saved." if save_ok else "Save failed (value did not persist)."
        except Exception:
            save_ok = False
            save_msg = "Save failed (exception)."
        return redirect("/share", code=303)

    # --- GET: read current state ---
    share_enabled = False
    try:
        share_enabled = bool(_get_share_activity(handle))
    except Exception:
        share_enabled = False

    public_profile_path = f"/u/{handle}"
    public_profile_url_abs = APP_BASE_URL.rstrip("/ommunity sharing") + public_profile_path
    handle_display = _html.escape((display or "").lstrip("@") or handle)
    checked_attr = "checked" if share_enabled else ""

    # --- DEBUG: show where we read/write and what is stored ---
    dbg_r2 = bool(_r2_enabled())
    settings_key = _settings_key(handle)
    settings_data = {}
    dbg_share_activity = None
    dbg_public_share_enabled = None

    if not dbg_r2:
        settings_data = _read_json_r2(settings_key) or {}
        dbg_share_activity = settings_data.get("share_activity", None)
        dbg_public_share_enabled = settings_data.get("public_share_enabled", None)

    share_status_line = (
        "Your public profile is live. Copy and share this link anywhere."
        if share_enabled
        else "Community sharing is off. Your public pages are private until you enable sharing."
    )

    debug_block = ""
    if not dbg_r2:
        debug_block = f"""
      <div style=\"height:14px;\"></div>
      <div class=\"muted\">
        <span class=\"pill\">debug</span>
        r2_enabled=<b>{str(dbg_r2)}</b> - key=<span class=\"mono\">{_html.escape(settings_key)}</span><br>
        share_activity=<b>{_html.escape(str(dbg_share_activity))}</b> - legacy public_share_enabled=<b>{_html.escape(str(dbg_public_share_enabled))}</b>
      </div>
      """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Share · MyAirportMap</title>
  <style>
    body {{
      margin:0;
      padding-top:70px;
      background:#0f1115;
      color:#fff;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;
    }}
    .wrap {{ max-width:820px; margin:24px auto; padding:0 16px; }}
    .card {{
      background:#171a21;
      border:1px solid #2a2f3a;
      border-radius:16px;
      padding:16px;
      margin:18px 0;
    }}
    .muted {{ opacity:.85; line-height:1.5; color:#aab2c0; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .btn {{
      display:inline-block;
      padding:12px 14px;
      border-radius:12px;
      text-decoration:none;
      background:#2b7cff;
      color:#fff;
      font-weight:850;
      border:none;
      cursor:pointer;
    }}
    .btn2 {{
      text-decoration:none;
      color:#dbe9ff;
      opacity:.9;
      font-weight:750;
    }}
    .toggle {{
      display:flex;
      gap:10px;
      align-items:flex-start;
    }}
    .toggle input {{
      margin-top:3px;
      transform: scale(1.1);
    }}
    .pill {{
      display:inline-block;
      padding:2px 10px;
      border-radius:999px;
      font-size:12px;
      font-weight:800;
      border:1px solid rgba(255,255,255,0.14);
      background:rgba(255,255,255,0.06);
      color:#dbe9ff;
    }}
  </style>
</head>
<body>
  {get_navbar("share", handle=handle)}

  <div class="wrap">
    <h2 style="margin:0 0 8px;">
      Share your map
      <span style="font-size:14px; color:#aab2c0; font-weight:750;">
        @{handle_display}
      </span>
    </h2>

    <div class="muted">{share_status_line}</div>

    <div class="card">
      <div style="font-weight:900; margin-bottom:8px;">Community sharing</div>
      <div class="muted" style="margin-bottom:12px;">
        For paid members, if enabled, your <b>Map</b> and <b>Achievements</b> pages can be viewed by others with the below link and
        and you may appear in a Pilot’s Lounge seat.
      </div>

      <form method="post" action="/share">
        <label class="toggle">
          <input type="checkbox" name="share_activity" value="1" {checked_attr}>
          <span>
            <div style="font-weight:850;">Make my profile public</div>
            <div class="muted" style="margin-top:4px;">
              Leave this off if MyAirportMap is just for you.
            </div>
          </span>
        </label>
        <div style="height:12px;"></div>
        <button class="btn" type="submit">Save</button>
      </form>

            {debug_block}
    </div>

    <div class="card">
      <div style="font-weight:900; margin-bottom:8px;">Public profile link</div>
      <div class="muted" style="margin-bottom:10px;">
        Anyone with this link can view your profile and toggle between your <b>Map</b> and <b>Achievements</b>.
      </div>
      <div class="mono" style="
          padding:10px 12px;
          border-radius:10px;
          background:#0b0f19;
          border:1px solid rgba(255,255,255,0.10);
          overflow:auto;">
        {_html.escape(public_profile_url_abs)}
      </div>
    </div>

    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
      <a class="btn" href="{public_profile_path}">View public profile</a>
      <a class="btn2" href="/logbook">Back to logbook</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/share/locked")
@login_required
def route_share_locked():
    cur = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    up = "/upgrade?next=" + quote(cur, safe="/=?&")

    return f"""<!doctype html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Share Locked</title>
</head>
<body>
  <div style="max-width: 820px; margin: 40px auto; padding: 0 18px; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;">
    <h1 style="font-size: 30px; margin: 0 0 10px;">Unlock your public map</h1>

    <div style="font-size: 16px; line-height: 1.5; opacity: 0.9; margin-bottom: 16px;">
      {BRAND_HEADLINE} is a logbook companion designed for the ground — visualize, celebrate, and share where you’ve flown.
    </div>

    <div style="border:1px solid #eee; border-radius: 14px; padding: 16px; margin: 18px 0;">
      <div style="font-weight:700; margin-bottom: 10px;">Membership unlocks:</div>
      <ul style="margin: 0; padding-left: 18px; line-height: 1.7;">
        <li>Public map link (share anywhere)</li>
        <li>Earn and display achievements</li>
        <li>Runway 360 Club progress</li>
      </ul>
      <div style="margin-top: 12px; font-size: 13px; opacity: 0.65;">{BADGE_TIE_IN}</div>
    </div>

    <div style="display:flex; gap: 10px; align-items:center;">
      <a href="{up}" style="display:inline-block; padding: 10px 14px; border-radius: 12px; text-decoration:none; background:#111; color:#fff; font-weight:650;">
        Upgrade
      </a>
      <a href="/logbook" style="text-decoration:none; color:#111; opacity: 0.8;">Not now</a>
    </div>
  </div>
</body>
</html>
"""

def _get_upgrade_viewer_handle() -> str | None:
    """
    Best-effort: if the user is logged in, show the logged-in navbar + @handle.
    If not logged in, show a public-ish navbar (no account dropdown).

    IMPORTANT:
    - This function must NOT attach request.clerk_claims.
    - Canonical claim attachment belongs in login_required() only.
    """
    try:
        claims = verify_clerk_session(request)  # best-effort; no redirects
        uid = (claims.get("sub") or "").strip() if claims else ""
        if uid:
            return "demo" if uid == "demo" else get_or_create_handle_for_user(uid)
    except Exception:
        pass
    return None


@app.get("/demo")
def demo():
    return redirect("/u/demo/map", code=302)

@app.route("/u/<handle>/map")
def public_user_map(handle: str):
    if not is_valid_handle(handle):
        return Response("<h2>Not found</h2>", mimetype="text/html", status=404)

    ensure_user_initialized(handle)

    blocked = require_public_share_access_or_owner(handle)
    if blocked:
        try:
            print(f"[TRACE] /u/{handle}/map BLOCKED type={type(blocked)}")
        except Exception:
            pass
        return blocked

    path = resolve_visits_csv(handle)
    raw = _read_visits_bytes(path, handle=handle)
    if not raw:
        return Response(
            f"<h2>Profile not found</h2><p>No visits CSV found for @{_html.escape(handle)}</p>",
            mimetype="text/html",
            status=404,
        )

    filter_state = request.args.get("state") or None

    # Render ONCE + trace timing (public)
    try:
        from time import perf_counter
    except Exception:
        perf_counter = None

    wrap_mode = "unknown"
    if perf_counter:
        t0 = perf_counter()
        html_out = generate_map_content(
            filter_state=filter_state,
            visits_csv=path,
            handle=handle,
            navbar_mode="public",
        )
        dt_ms = int((perf_counter() - t0) * 1000)
    else:
        html_out = generate_map_content(
            filter_state=filter_state,
            visits_csv=path,
            handle=handle,
            navbar_mode="public",
        )
        dt_ms = -1

    # --- Public avatar enforcement (Map41 stability) ---
    # Shared pages must show the owner's avatar; if the public navbar doesn't include it,
    # we inject it into the Menu pill (quiet, non-social, map-first).
    safe = _safe_handle_for_avatar(handle or "")
    avatar_src = f"/avatar/{safe}" if safe else "/static/mam-logo.png"
    avatar_src_js = avatar_src.replace("\\", "\\\\").replace("'", "\\'")

    inject_js = f"""
<script>
(function () {{
  try {{
    var url = '{avatar_src_js}' + '?v=' + Date.now();

    // If a public avatar node exists, refresh it
    document.querySelectorAll('.mam-public-avatar img').forEach(function(img) {{
      try {{
        img.src = url;
      }} catch (e) {{}}
    }});

    // If the Menu pill avatar exists (private-like pill), refresh it
    document.querySelectorAll('.pill .navavatar img').forEach(function(img) {{
      try {{
        img.src = url;
      }} catch (e) {{}}
    }});

    // If NO avatar exists on the shared page, insert one into the Menu pill
    var hasAny = !!(document.querySelector('.mam-public-avatar img') || document.querySelector('.pill .navavatar img'));
    if (!hasAny) {{
      var pill = document.querySelector('.pill');
      if (pill) {{
        var span = document.createElement('span');
        span.className = 'navavatar mam-public-avatar';
        span.setAttribute('aria-hidden', 'true');
        span.style.cssText = 'width:24px;height:24px;border-radius:999px;overflow:hidden;background:#fff;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;margin-right:8px;';

        var img = document.createElement('img');
        img.src = url;
        img.alt = 'Avatar';
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
        img.onerror = function() {{
          try {{ this.onerror = null; this.src = '/static/mam-logo.png'; }} catch (e) {{}}
        }};

        span.appendChild(img);
        pill.insertBefore(span, pill.firstChild);
      }}
    }}
  }} catch (e) {{}}
}})();
</script>
"""

    # If generate_map_content already returns a full HTML doc, do NOT double-wrap.
    # If it returns BODY-ONLY, wrap it so viewport is guaranteed.
    lower = (html_out or "").lstrip().lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        full_html = html_out
        wrap_mode = "full-doc"
    else:
        title = f"@{handle} · Map"
        full_html = render_public_page(title=title, body_html=html_out)
        wrap_mode = "wrapped"

    # Ensure our injector is present near the end of <body>
    try:
        l2 = (full_html or "").lower()
        bi = l2.rfind("</body>")
        if bi != -1:
            full_html = full_html[:bi] + inject_js + full_html[bi:]
        else:
            full_html = (full_html or "") + inject_js
    except Exception:
        full_html = (full_html or "") + inject_js

    # Server trace (safe, minimal)
    try:
        qs = request.query_string.decode("utf-8")
    except Exception:
        qs = ""
    try:
        if dt_ms >= 0:
            print(f"[TRACE] /u/{handle}/map render ms={dt_ms} wrap={wrap_mode} qs={qs}")
        else:
            print(f"[TRACE] /u/{handle}/map wrap={wrap_mode} qs={qs}")
    except Exception:
        pass
    try:
        print(f"[TRACE] /u/{handle}/map OK bytes={len(full_html or '')}")
    except Exception:
        pass

    resp = Response(full_html, mimetype="text/html")
    return attach_trial_cookie(resp, None)

@app.route("/u/<handle>/achievements")
def public_user_achievements(handle: str):
    if not is_valid_handle(handle):
        return Response("<h2>Not found</h2>", mimetype="text/html", status=404)

    ensure_user_initialized(handle)

    blocked = require_public_share_access_or_owner(handle)
    if blocked:
        return blocked

    path = resolve_visits_csv(handle)
    raw = _read_visits_bytes(path, handle=handle)
    if not raw:
        return Response(
            f"<h2>Profile not found</h2><p>No visits CSV found for @{_html.escape(handle)}</p>",
            mimetype="text/html",
            status=404,
        )

    # Owner-only hint (injected into body)
    hint_html = ""
    is_owner = is_owner_view(handle)
    cookies = request.cookies or {}
    if is_owner and not cookies.get("owner_public_hint_dismissed"):
        hint_html = """
<div id="owner-public-hint" style="
    background:#121212;border-bottom:1px solid #222;color:#ddd;
    padding:10px 16px;line-height:1.35;">
  <div style="max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:12px;">
    <div style="flex:1;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <span style="display:inline-block;padding:2px 8px;border:1px solid #2a2a2a;border-radius:999px;color:#fff;font-weight:700;">
        Public view
      </span>
      <span style="opacity:.92;">This is what others see when you share your profile.</span>
      <a href="/app" style="color:#9ecbff;text-decoration:none;margin-left:6px;">Back to your dashboard</a>
    </div>
    <button type="button" onclick="dismissOwnerPublicHint()" aria-label="Dismiss" style="
        background:none;border:none;color:#888;cursor:pointer;font-size:18px;line-height:1;">×</button>
  </div>
</div>
<script>
  function dismissOwnerPublicHint() {
    document.cookie = "owner_public_hint_dismissed=1; path=/; max-age=31536000";
    const el = document.getElementById("owner-public-hint");
    if (el) el.remove();
  }
</script>
"""

    # --- Map41: Achievements certifications line (public view) ---
    cert_line = ""
    try:
        prefs = _load_json_from_storage(_profile_prefs_key(handle))
        selected = prefs.get("achievements_certs") or []
        if not isinstance(selected, list):
            selected = []
        selected = _validate_cert_keys([str(x) for x in selected])
        cert_line = format_certifications_line(selected, username=handle)
    except Exception:
        cert_line = ""

    body_html = generate_badges_content(
        visits_csv=path,
        handle=handle,
        navbar_mode="public",
        certifications_line=cert_line,
    )

    # Inject hint INSIDE <body> right after the opening tag
    if hint_html:
        lower = body_html.lower()
        bi = lower.find("<body")
        if bi != -1:
            gt = body_html.find(">", bi)
            if gt != -1:
                body_html = body_html[:gt + 1] + hint_html + body_html[gt + 1:]
        else:
            body_html = hint_html + body_html

    # Ensure body has pad-top helper for the fixed public navbar
    if "<body" in body_html:
        import re
        m = re.search(r"<body([^>]*)>", body_html, flags=re.IGNORECASE)
        if m:
            tag = m.group(0)
            attrs = m.group(1) or ""
            if re.search(r'\bclass\s*=\s*"', attrs, flags=re.IGNORECASE):
                body_html = re.sub(
                    r'(<body[^>]*\bclass\s*=\s*")',
                    r'\1mam-public-padtop ',
                    body_html,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                new_tag = tag[:-1] + ' class="mam-public-padtop">'
                body_html = body_html.replace(tag, new_tag, 1)

    # Canonical public wrapper (viewport meta always present)
    title = f"@{handle} · Achievements"
    full_html = render_public_page(title=title, body_html=body_html)

    # --- Public avatar enforcement (Map41 stability) ---
    safe = _safe_handle_for_avatar(handle or "")
    avatar_src = f"/avatar/{safe}" if safe else "/static/mam-logo.png"
    avatar_src_js = avatar_src.replace("\\", "\\\\").replace("'", "\\'")

    inject_js = f"""
<script>
(function () {{
  try {{
    var url = '{avatar_src_js}' + '?v=' + Date.now();

    // If a public avatar node exists, refresh it
    document.querySelectorAll('.mam-public-avatar img').forEach(function(img) {{
      try {{ img.src = url; }} catch (e) {{}}
    }});

    // If the Menu pill avatar exists, refresh it
    document.querySelectorAll('.pill .navavatar img').forEach(function(img) {{
      try {{ img.src = url; }} catch (e) {{}}
    }});

    // If NO avatar exists on the shared page, insert one into the Menu pill
    var hasAny = !!(document.querySelector('.mam-public-avatar img') || document.querySelector('.pill .navavatar img'));
    if (!hasAny) {{
      var pill = document.querySelector('.pill');
      if (pill) {{
        var span = document.createElement('span');
        span.className = 'navavatar mam-public-avatar';
        span.setAttribute('aria-hidden', 'true');
        span.style.cssText = 'width:24px;height:24px;border-radius:999px;overflow:hidden;background:#fff;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;margin-right:8px;';

        var img = document.createElement('img');
        img.src = url;
        img.alt = 'Avatar';
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
        img.onerror = function() {{
          try {{ this.onerror = null; this.src = '/static/mam-logo.png'; }} catch (e) {{}}
        }};

        span.appendChild(img);
        pill.insertBefore(span, pill.firstChild);
      }}
    }}
  }} catch (e) {{}}
}})();
</script>
"""
    try:
        l2 = (full_html or "").lower()
        bi = l2.rfind("</body>")
        if bi != -1:
            full_html = full_html[:bi] + inject_js + full_html[bi:]
        else:
            full_html = (full_html or "") + inject_js
    except Exception:
        full_html = (full_html or "") + inject_js

    resp = Response(full_html, mimetype="text/html")
    return attach_trial_cookie(resp, None)

@app.route("/ai/state-card", methods=["GET"])
@login_required
def ai_state_card_png():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return Response("Unauthorized", status=401)

    ensure_user_initialized(handle)

    # Must be trial or member (same gate as Runway360)
    if not has_active_access(handle):
        nxt = quote(
            request.full_path[:-1] if request.full_path.endswith("?") else request.full_path,
            safe="/=?&"
        )
        return redirect("/trial/ended?next=" + nxt, code=302)

    st = (request.args.get("state") or "").strip().upper()
    if st not in CONUS_STATES:
        return Response("Invalid state (CONUS only).", status=400)

    prog = compute_state_progress(handle, st)
    if not prog.get("complete"):
        return Response("State not complete yet.", status=403)

    try:
        png = generate_state_badge_png(handle, st)
        resp = Response(png, mimetype="image/png")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="myairportmap_{st.lower()}_complete_{handle}.png"'
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except FileNotFoundError as e:
        print("ai_state_card_png missing template:", repr(e))
        return Response("Missing state base template.", status=500)
    except Exception as e:
        print("ai_state_card_png failed:", repr(e))
        return Response("Failed to generate card.", status=500)

@app.route("/ai/bravo-card", methods=["GET"])
@login_required
def ai_bravo_card_png():
    handle = (current_user_handle() or "").strip().lower()
    if not handle:
        return Response("Unauthorized", status=401)

    ensure_user_initialized(handle)

    # Must be trial or member (same gate as state cards / Runway360)
    if not has_active_access(handle):
        nxt = quote(
            request.full_path[:-1] if request.full_path.endswith("?") else request.full_path,
            safe="/=?&"
        )
        return redirect("/trial/ended?next=" + nxt, code=302)

    # Verify completion
    try:
        prog = compute_bravo_progress_for_handle(handle)
    except Exception as e:
        print("ai_bravo_card_png progress failed:", repr(e))
        return Response("Failed to compute progress.", status=500)

    if not prog.get("complete"):
        return Response("Bravo not complete yet.", status=403)

    # Must have a stable completion date (Achievements sets it once; but be safe)
    try:
        completed_iso = bravo_completed_at_iso(handle)
        if not completed_iso:
            completed_iso = set_bravo_completed_date_once(handle)

        png = generate_bravo_badge_png(handle=handle)
        resp = Response(png, mimetype="image/png")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="myairportmap_bravo_bravo_complete_{handle}.png"'
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except FileNotFoundError as e:
        print("ai_bravo_card_png missing template:", repr(e))
        return Response("Missing bravo base template.", status=500)
    except Exception as e:
        print("ai_bravo_card_png failed:", repr(e))
        return Response("Failed to generate card.", status=500)

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return Response("ok", status=200, mimetype="text/plain")


from urllib.parse import urlencode

def _delete_cookie_dual(resp, name: str):
    # host-only delete
    resp.delete_cookie(name, path="/")
    # parent-domain delete (best-effort for Clerk cookies)
    resp.delete_cookie(name, path="/", domain=".myairportmap.com")
    return resp

from urllib.parse import urlencode

@app.route("/sign-out")
def sign_out():
    resp = redirect("/signed-out", code=302)

    # Clear app cookies (host-only)
    resp.delete_cookie("mam_auth", path="/")
    resp.delete_cookie("mam_web", path="/")
    resp.delete_cookie("session", path="/")

    # Belt-and-suspenders deletes (if any older cookie used a domain attribute)
    resp.delete_cookie("mam_auth", path="/", domain=".myairportmap.com")
    resp.delete_cookie("mam_web", path="/", domain=".myairportmap.com")
    resp.delete_cookie("session", path="/", domain=".myairportmap.com")

    # Brake cookie blocks legacy auth redirect loops
    resp.set_cookie(
        "mam_signed_out",
        "1",
        max_age=300,
        secure=True,
        samesite="Lax",
        path="/",
    )

    return resp

@app.route("/signed-out")
def signed_out():
    resp = make_response("""
<h2>You are signed out</h2>
<p><a href="/sign-in?next=/app&fresh=1">Sign in again</a></p>
""")

    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"

    # Clear the brake on landing
    resp.delete_cookie("mam_signed_out", path="/")
    resp.delete_cookie("mam_signed_out", path="/", domain=".myairportmap.com")

    return resp

@app.get("/_debug/auth")
def _debug_auth():
    tok = _get_token_from_request(request)
    claims = verify_clerk_session(request) if tok else None
    return jsonify({
        "host": request.host,
        "path": request.path,
        "scheme": request.scheme,
        "headers": {
            "Host": request.headers.get("Host"),
            "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto"),
        },
        "cookie_names": sorted(list(request.cookies.keys())),
        "token_present": bool(tok),
        "token_preview": (tok[:20] + "…" + tok[-10:]) if tok else None,
        "claims_present": bool(claims),
        "claims_iss": (claims or {}).get("iss"),
        "claims_azp": (claims or {}).get("azp"),
        "claims_sub": (claims or {}).get("sub"),
    }), 200


import datetime as dt
import os
from flask import jsonify, request
from jose import jwt

@app.get("/auth/debug")
def auth_debug():
    """
    Debug endpoint for Clerk session verification (python-jose).
    Safe diagnostics only.
    """
    import datetime as _dt

    # ---- Request headers snapshot (safe) ----
    headers_snap = {
        "Host": request.headers.get("Host"),
        "X-Forwarded-Host": request.headers.get("X-Forwarded-Host"),
        "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto"),
    }
    cookie_names = sorted(list(request.cookies.keys()))

    # ---- Token detection ----
    tok = None
    token_source = None

    # 1) Authorization header (if present)
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        tok = auth.split(" ", 1)[1].strip()
        if tok:
            token_source = "header:Authorization"

    # 2) Cookie via canonical helper (preferred)
    if not tok:
        tok = _get_token_from_request(request) or ""
        if tok:
            token_source = f"cookie:{APP_SESSION_COOKIE}"

    now_epoch = int(time.time())
    now_utc = _dt.datetime.utcnow().isoformat() + "Z"

    # ---- Unverified claims (payload) ----
    unverified = {}
    unverified_hdr = {}
    verify_error = None

    if tok:
        try:
            unverified = jwt.get_unverified_claims(tok) or {}
        except Exception as e:
            unverified = {}
            verify_error = f"unverified_claims_error: {repr(e)}"

    # ---- Unverified header (kid) ----
    if tok:
        try:
            unverified_hdr = jwt.get_unverified_header(tok) or {}
        except Exception:
            unverified_hdr = {}

    unverified_kid = (unverified_hdr.get("kid") or "").strip()

    # ---- JWKS diagnostics (safe) ----
    jwks_keys_count = 0
    try:
        jwks = _get_jwks() or {}
        jwks_keys_count = len((jwks.get("keys") or []))
    except Exception:
        jwks_keys_count = 0

    # ---- Verification ----
    claims = None
    if tok:
        try:
            claims = verify_clerk_session(request)
        except Exception as e:
            claims = None
            verify_error = f"verify_exception: {repr(e)}"

    if tok and claims is None and verify_error is None:
        verify_error = "verify_clerk_session returned None"

    # ---- Exp / timing ----
    verified_sub = (claims or {}).get("sub")
    unverified_exp = unverified.get("exp")
    exp_in_seconds = None
    token_expired = None
    if isinstance(unverified_exp, int):
        exp_in_seconds = unverified_exp - now_epoch
        token_expired = (now_epoch >= unverified_exp)

    return jsonify({
        # ---- Request context ----
        "host": request.host,
        "path": request.path,
        "scheme": request.scheme,
        "headers": headers_snap,
        "cookie_names": cookie_names,

        # ---- Runtime config ----
        "app_session_cookie_name": APP_SESSION_COOKIE,
        "issuer_expected": (os.getenv("CLERK_ISSUER") or "").strip() or None,
        "jwks_url": (os.getenv("CLERK_JWKS_URL") or "").strip() or None,
        "audience_expected": (os.getenv("CLERK_AUDIENCE") or "").strip() or None,
        "azp_allowlist": [
            s.strip()
            for s in (os.getenv("CLERK_AUTHORIZED_PARTIES") or "").split(",")
            if s.strip()
        ],

        # ---- Token presence ----
        "token_present": bool(tok),
        "token_source": token_source,
        "token_preview": (tok[:24] + "…") if tok else None,

        # ---- Time ----
        "now_epoch": now_epoch,
        "now_utc": now_utc,

        # ---- Unverified payload ----
        "unverified_sub": unverified.get("sub"),
        "unverified_iss": unverified.get("iss"),
        "unverified_aud": unverified.get("aud"),
        "unverified_exp": unverified_exp,
        "unverified_kid": unverified_kid,

        "exp_in_seconds": exp_in_seconds,
        "token_expired": token_expired,

        # ---- JWKS ----
        "jwks_keys_count": jwks_keys_count,

        # ---- Verification result ----
        "verified": bool((verified_sub or "").strip()),
        "verified_sub": verified_sub,
        "claims_iss": (claims or {}).get("iss"),
        "claims_azp": (claims or {}).get("azp"),

        # ---- Errors ----
        "verify_error": verify_error,
    }), 200

@app.route("/debug/me")
@login_required
def debug_me():
    handle = (current_user_handle() or "").strip().lower()
    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = claims.get("sub") or ""

    e = _read_entitlements(handle) or {}

    return {
        "user_id": user_id,
        "handle": handle,
        "is_paid": bool(e.get("is_paid")),
        "trial_started_at": e.get("trial_started_at"),
        "trial_expires_at": e.get("trial_expires_at"),
        "trial_days_left": trial_days_left(handle),
        "tos_accepted": tos_accepted_for_user(user_id),
    }

def _debug_generate_bravo_card(handle: str, out_path: str = "bravo_test.png"):
    png = generate_bravo_badge_png(handle)
    with open(out_path, "wb") as f:
        f.write(png)
    print(f"[DEBUG] wrote {out_path}")

@app.route("/_debug/bravo-card")
def debug_bravo_card():
    png = generate_bravo_badge_png("myairportmap-demo")
    return Response(png, mimetype="image/png")


_SWAGGER_SCHEMA: dict | None = None


def _build_openapi_schema() -> dict:
    global _SWAGGER_SCHEMA
    if _SWAGGER_SCHEMA is not None:
        return _SWAGGER_SCHEMA

    api_app = FastAPI(title="MyAirportMap API", version="1.0.0")
    api_app.include_router(auth_api.router)
    api_app.include_router(user_api.router)
    api_app.include_router(airports_api.router)
    api_app.include_router(visits_api.router)
    api_app.include_router(achievements_router_api.router)
    api_app.include_router(runway360_api.router)
    api_app.include_router(export_api.router)
    api_app.include_router(upload_api.router)
    api_app.include_router(certifications_api.router)
    api_app.include_router(subcription_api.router)

    schema = get_openapi(
        title=api_app.title,
        version=api_app.version,
        routes=api_app.routes,
        description="OpenAPI schema served from Flask app-web-2",
    )

    # Flask API serves several routers under /api/* to avoid clashing with
    # existing page routes. Achievements is explicitly served under
    # /achievements/achievements.
    path_remap_prefixes = {
        "/achievements": "/achievements/achievements",
        "/certifications": "/certifications/certifications",
        "/export": "/export/export",
        "/runway360": "/runway360/runway360",
        "/subscription": "/subscription/subscription",
        "/upload": "/upload/upload",
        "/users": "/users/users",
        "/visits": "/visits/visits",
        "/auth": "/auth/auth",
        "/airports": "/airports/airports",
    }

    remapped_paths = {}
    for path, value in (schema.get("paths") or {}).items():
        new_path = path
        for src_prefix, dst_prefix in path_remap_prefixes.items():
            if path == src_prefix:
                new_path = dst_prefix
                break
            if path.startswith(src_prefix + "/"):
                new_path = path.replace(src_prefix, dst_prefix, 1)
                break
        remapped_paths[new_path] = value
        print(f"[DEBUG] remapped OpenAPI path: {path} -> {new_path}")
    schema["paths"] = remapped_paths

    _SWAGGER_SCHEMA = schema
    return _SWAGGER_SCHEMA


@app.route("/openapi.json", methods=["GET"])
def openapi_json_route():
        return jsonify(_build_openapi_schema())


@app.route("/docs", methods=["GET"])
def swagger_ui_route():
        html = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>MyAirportMap API Docs</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\" />
    <style>body { margin: 0; background: #fafafa; } #swagger-ui { max-width: 1200px; margin: 0 auto; }</style>
</head>
<body>
    <div id=\"swagger-ui\"></div>
    <script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script>
    <script>
        window.ui = SwaggerUIBundle({
            url: '/openapi.json',
            dom_id: '#swagger-ui',
            deepLinking: true,
            presets: [SwaggerUIBundle.presets.apis],
            layout: 'BaseLayout'
        });
    </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")


if __name__ == "__main__":
    print(f"--- {APP_TITLE} ---")
    print(f"BASE_DIR: {BASE_DIR}")
    try:
        load_data()
    except Exception as e:
        print("!! Startup data load error:", repr(e))
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
