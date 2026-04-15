import jwt
import requests
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.apple.verify import create_apple_token
from app.core.config import settings
from app.db.session import get_db
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.apple import VerifySubscriptionRequest

router = APIRouter(tags=["apple"])

APPLE_API = "https://api.storekit.itunes.apple.com/inApps/v1/subscriptions/"
APPLE_API_SANDBOX = "https://api.storekit-sandbox.itunes.apple.com/inApps/v1/subscriptions/"
def decode_apple_jwt(token: str):
    return jwt.decode(
        token,
        options={"verify_signature": False}  # Apple verify bằng server khác
    )
@router.post("/subscription/verify")
def verify_subscription(
    data: VerifySubscriptionRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    token = create_apple_token()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    res = requests.get(
        APPLE_API + data.original_transaction_id,
        headers=headers
    )
    if res.status_code in [401, 404]:
        print("Switch to SANDBOX")
        res = requests.get(APPLE_API_SANDBOX + data.original_transaction_id, headers=headers)

    if res.status_code != 200:
        print("APPLE ERROR:", res.status_code, res.text)
        raise HTTPException(400, "Apple API error")

    result = res.json()

    data_list = result.get("data", [])
    print("APPLE DATA:", data_list)
    if not data_list:
        raise HTTPException(400, "No subscription data")

    # 🔥 lấy transaction mới nhất
    transactions = data_list[0].get("lastTransactions", [])

    if not transactions:
        raise HTTPException(400, "No transactions")

    decoded_transactions = []

    for t in transactions:
        payload = decode_apple_jwt(t["signedTransactionInfo"])
        print("Decoded transaction:", payload)
        decoded_transactions.append({
            "transactionId": payload.get("transactionId"),
            "originalTransactionId": payload.get("originalTransactionId"),
            "productId": payload.get("productId"),
            "purchaseDate": payload.get("purchaseDate"),
            "expiresDate": payload.get("expiresDate"),
            "revocationDate": payload.get("revocationDate"),
        })

    valid_transactions = [
        t for t in decoded_transactions
        if t.get("expiresDate")
    ]

    if not valid_transactions:
        raise HTTPException(400, "No valid subscription")

    latest = max(
        valid_transactions,
        key=lambda x: int(x["expiresDate"])
    )

    product_id = latest["productId"]
    # ✅ check product
    if product_id != data.product_id:
        raise HTTPException(400, "Product mismatch")

    # ✅ check bundle
    if result.get("bundleId") != settings.APPLE_BUNDLE_ID:
        raise HTTPException(400, "Invalid bundle")

    # ❌ check revoked
    if latest.get("revocationDate"):
        raise HTTPException(400, "Transaction revoked")

    # ⏱ time
    expiration_date = datetime.fromtimestamp(
        int(latest["expiresDate"]) / 1000,
        tz=timezone.utc
    )

    purchase_date = datetime.fromtimestamp(
        int(latest["purchaseDate"]) / 1000,
        tz=timezone.utc
    )

    now = datetime.now(timezone.utc)
    is_active = expiration_date > now

    # 🔁 tránh duplicate
    original_transaction_id = latest["originalTransactionId"]
    existing_owner = db.query(Subscription).filter(
        Subscription.original_transaction_id == original_transaction_id,
        Subscription.user_id.isnot(None),
        Subscription.user_id != db_user.id
    ).first()

    if existing_owner:
        raise HTTPException(400, "Subscription already owned by another account")
    existing = db.query(Subscription).filter(
        Subscription.original_transaction_id == original_transaction_id
    ).first()
    db_user = db.query(User).filter(User.id == user.id).first()

    if existing:
        print("Existing subscription found:", existing.id)
        if existing.user_id and existing.user_id != db_user.id:
            raise HTTPException(400, "Subscription already owned by another account")

        if not existing.user_id:
            existing.user_id = db_user.id

        existing.expiration_date = expiration_date
        existing.status = "active" if is_active else "expired"
    else:
        print("Creating new subscription")
        sub = Subscription(
            id=uuid4(),
            user_id=db_user.id,
            product_id=product_id,
            transaction_id=latest["transactionId"],
            original_transaction_id=latest["originalTransactionId"],
            purchase_date=purchase_date,
            expiration_date=expiration_date,
            platform=data.platform,
            status="active" if is_active else "expired"
        )
        db.add(sub)
        
   
    # 🔓 unlock premium
    # db_user.is_paid = db.query(Subscription).filter(
    #     Subscription.user_id == db_user.id,
    #     Subscription.expiration_date > now
    # ).first() is not None
    # db_user.premium_expire_at = expiration_date
    db.commit()
    db.refresh(db_user)
    print("USER ID:", user.id)
    print("SUBS:", db.query(Subscription).all())


    if settings.DEMO_FLAG:
        data = {
            "is_premium": db.query(Subscription).filter(
                Subscription.user_id == db_user.id
            ).first() is not None,
            "expiration_date": expiration_date
        }
    else:
        data = {
            "is_premium": db.query(Subscription).filter(
                Subscription.user_id == db_user.id,
                Subscription.expiration_date > now
            ).first() is not None,
            "expiration_date": expiration_date
        }

    return {
        "success": True,
        "data": data
    }

@router.get("/subscription/status")
def get_subscription_status(
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    now = datetime.now(timezone.utc)

    sub = db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.expiration_date > now
    ).order_by(Subscription.expiration_date.desc()).first()

    return {
        "success": True,
        "data": {
            "is_premium": sub is not None,
            "expiration_date": sub.expiration_date if sub else None
        }
    }

@router.post("/webhook")
async def apple_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()

    signed_payload = body.get("signedPayload")
    if not signed_payload:
        raise HTTPException(400, "Missing payload")

    # ⚠️ DEV: chưa verify signature
    try:
        decoded = jwt.decode(
            signed_payload,
            options={"verify_signature": False}
        )
    except Exception:
        raise HTTPException(400, "Invalid payload")

    notification_type = decoded.get("notificationType")
    data = decoded.get("data", {})

    signed_tx = data.get("signedTransactionInfo")
    if not signed_tx:
        return {"success": True}

    # decode nested JWT
    tx = jwt.decode(signed_tx, options={"verify_signature": False})

    transaction_id = tx.get("transactionId")
    original_transaction_id = tx.get("originalTransactionId")
    product_id = tx.get("productId")

    expires_ms = tx.get("expiresDate")
    purchase_ms = tx.get("purchaseDate")

    expiration_date = None
    purchase_date = None

    if expires_ms:
        expiration_date = datetime.fromtimestamp(
            int(expires_ms) / 1000,
            tz=timezone.utc
        )

    if purchase_ms:
        purchase_date = datetime.fromtimestamp(
            int(purchase_ms) / 1000,
            tz=timezone.utc
        )

    now = datetime.now(timezone.utc)
    is_active = expiration_date and expiration_date > now

    # 🔍 tìm subscription theo original_transaction_id
    sub = db.query(Subscription).filter(
        Subscription.original_transaction_id == original_transaction_id
    ).first()

    user = None
    if sub:
        user = db.query(User).get(sub.user_id)

    # =========================
    # 🎯 HANDLE EVENTS
    # =========================

    if notification_type in ["INITIAL_BUY", "DID_RENEW"]:
        if sub:
            sub.expiration_date = expiration_date
            sub.status = "active"
        if not sub:
            print("Webhook before verify → skip")
            return {"success": True}

        if user:
            user.is_paid = True
            user.premium_expire_at = expiration_date

    elif notification_type in ["EXPIRED", "DID_FAIL_TO_RENEW"]:
        if sub:
            sub.status = "expired"

        if user:
            user.is_paid = False

    elif notification_type in ["REFUND", "REVOKE"]:
        if sub:
            sub.status = "revoked"

        if user:
            user.is_paid = False

    # debug log
    print("APPLE WEBHOOK:", notification_type, transaction_id)

    db.commit()

    return {"success": True}