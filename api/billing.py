from flask import Blueprint, request, jsonify
from urllib.parse import quote
import stripe
import os

from app import (
    current_user_handle,
    ensure_stripe_customer_for_current_user,
    is_paid_user_handle,
    login_required,
)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
APP_BASE_URL = os.environ.get("APP_BASE_URL")

billing_api = Blueprint("billing_api", __name__)


@billing_api.route("/api/billing/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session_api():

    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    handle = (current_user_handle() or "").strip().lower()

    # check user
    if not user_id or not handle:
        return jsonify({"error": "Unauthorized"}), 401

    # check stripe config
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID or not APP_BASE_URL:
        return jsonify({"error": "Stripe not configured"}), 500

    next_path = (request.json.get("next") if request.is_json else None) or "/logbook"

    stripe.api_key = STRIPE_SECRET_KEY

    success_url = f"{APP_BASE_URL}/billing/success?next={quote(next_path)}"
    cancel_url = f"{APP_BASE_URL}/upgrade?next={quote(next_path)}"

    customer_id = ensure_stripe_customer_for_current_user(handle=handle)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[
            {
                "price": STRIPE_PRICE_ID,   # ✅ sửa lỗi ở đây
                "quantity": 1
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user_id,
        metadata={
            "handle": handle,
            "user_id": user_id
        }
    )

    return jsonify({
        "checkout_url": session.url,
        "session_id": session.id
    })


@billing_api.route("/api/billing/subscription", methods=["GET"])
@login_required
def api_billing_subscription():

    claims = getattr(request, "clerk_claims", {}) or {}
    user_id = (claims.get("sub") or "").strip()
    handle = (current_user_handle() or "").strip().lower()

    if not user_id or not handle:
        return jsonify({"error": "Unauthorized"}), 401

    is_premium = is_paid_user_handle(handle)

    return jsonify({
        "user_id": user_id,
        "handle": handle,
        "is_premium": is_premium
    })