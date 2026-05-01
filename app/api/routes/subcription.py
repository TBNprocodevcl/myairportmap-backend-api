import jwt
import requests
import base64
import json
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.android.verify import verify_android_subscription, service
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


def _decode_android_pubsub_payload(body: dict):
    message = body.get("message")
    if not message:
        raise HTTPException(400, "Missing message")

    encoded_data = message.get("data")
    if not encoded_data:
        raise HTTPException(400, "Missing message.data")

    try:
        decoded_bytes = base64.b64decode(encoded_data)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid Pub/Sub payload")

    return payload


def _is_android_subscription_active(result: dict, now: datetime):
    expiry_ms = result.get("expiryTimeMillis")
    if not expiry_ms:
        return False, None

    expiration_date = datetime.fromtimestamp(
        int(expiry_ms) / 1000,
        tz=timezone.utc
    )

    payment_state = result.get("paymentState")
    cancel_reason = result.get("cancelReason")

    is_active = (
        expiration_date > now
        and payment_state in [1, 2]
        and cancel_reason is None
    )
    return is_active, expiration_date

@router.post("/subscription/verify")
def verify_subscription(
    data: VerifySubscriptionRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    if data.platform == "ios":
        return handle_ios(data, db, user)

    elif data.platform == "android":
        return handle_android(data, db, user)

    else:
        raise HTTPException(400, "Unsupported platform")
    
# @router.post("/subscription/verify")
def handle_ios(
    data,
    db,
    user
):
    if not data.original_transaction_id:
        raise HTTPException(400, "Missing original_transaction_id")
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
        if t.get("expiresDate") and not t.get("revocationDate")
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
    db_user = db.query(User).filter(User.id == user.id).first()

    # 🔁 tránh duplicate
    original_transaction_id = latest["originalTransactionId"]
    existing = db.query(Subscription).filter(
        Subscription.original_transaction_id == original_transaction_id
    ).first()

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

def handle_android(data, db, user):
    if not data.purchase_token:
        raise HTTPException(400, "Missing purchase_token")
    result = verify_android_subscription(
        package_name=settings.ANDROID_PACKAGE_NAME,
        product_id=data.product_id,
        purchase_token=data.purchase_token
    )


    expiry_ms = result.get("expiryTimeMillis")
    start_ms = result.get("startTimeMillis")
    cancel_reason = result.get("cancelReason")

    if not expiry_ms:
        raise HTTPException(400, "Invalid subscription")

    if result.get("acknowledgementState") == 0:
        try:
            service.purchases().subscriptions().acknowledge(
                packageName=settings.ANDROID_PACKAGE_NAME,
                subscriptionId=data.product_id,
                token=data.purchase_token,
                body={}
            ).execute()
        except Exception as e:
            print("ACK ERROR:", str(e))

    expiration_date = datetime.fromtimestamp(
        int(expiry_ms) / 1000,
        tz=timezone.utc
    )

    purchase_date = datetime.fromtimestamp(
        int(start_ms) / 1000,
        tz=timezone.utc
    ) if start_ms else None

    now = datetime.now(timezone.utc)
    payment_state = result.get("paymentState")

    is_active = (
        expiration_date > now
        and payment_state in [1, 2]  # 1: purchased, 2: trial
    )
    db_user = db.query(User).filter(User.id == user.id).first()

    purchase_token = data.purchase_token

    existing = db.query(Subscription).filter(
        Subscription.original_transaction_id == purchase_token
    ).first()

    if existing:
        if existing.user_id and existing.user_id != db_user.id:
            raise HTTPException(400, "Already owned")

        existing.user_id = db_user.id
        existing.expiration_date = expiration_date
        existing.status = "active" if is_active else "expired"

    else:
        sub = Subscription(
            id=uuid4(),
            user_id=db_user.id,
            product_id=data.product_id,
            transaction_id=result.get("orderId"),
            original_transaction_id=purchase_token,
            purchase_date=purchase_date,
            expiration_date=expiration_date,
            platform="android",
            status="active" if is_active else "expired"
        )
        db.add(sub)

    db.commit()

    return {
        "success": True,
        "data": {
            "is_premium": is_active,
            "expiration_date": expiration_date
        }
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
    print("Received Apple webhook:", body)
    signed_payload = body.get("signedPayload")
    if not signed_payload:
        raise HTTPException(400, "Missing payload")

    # ⚠️ DEV: chưa verify signature (PROD phải verify)
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

    # decode transaction
    try:
        tx = jwt.decode(signed_tx, options={"verify_signature": False})
    except Exception:
        raise HTTPException(400, "Invalid transaction")

    original_transaction_id = tx.get("originalTransactionId")
    transaction_id = tx.get("transactionId")

    expires_ms = tx.get("expiresDate")
    purchase_ms = tx.get("purchaseDate")
    revocation_ms = tx.get("revocationDate")

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

    # 🔍 tìm subscription
    sub = db.query(Subscription).filter(
        Subscription.original_transaction_id == original_transaction_id
    ).first()

    if not sub:
        # webhook đến trước verify → bỏ qua
        print("Webhook before verify → skip")
        return {"success": True}

    user = db.query(User).get(sub.user_id) if sub.user_id else None

    # =========================
    # 🎯 HANDLE EVENTS
    # =========================

    if notification_type in ["INITIAL_BUY", "DID_RENEW"]:
        sub.expiration_date = expiration_date
        sub.status = "active"

        if user:
            user.is_paid = True
            user.premium_expire_at = expiration_date

    elif notification_type in ["EXPIRED", "DID_FAIL_TO_RENEW"]:
        sub.status = "expired"

        if user:
            user.is_paid = False

    elif notification_type in ["REFUND", "REVOKE"]:
        sub.status = "revoked"
        sub.expiration_date = now

        if user:
            user.is_paid = False

    elif notification_type == "GRACE_PERIOD":
        # vẫn còn quyền sử dụng tạm thời
        sub.status = "grace"

    elif notification_type == "PRICE_INCREASE":
        print("User needs to accept price increase")

    # =========================
    # 🔄 sync trạng thái chuẩn
    # =========================

    if expiration_date:
        is_active = expiration_date > now and not revocation_ms

        sub.status = "active" if is_active else sub.status

        if user:
            user.is_paid = is_active
            user.premium_expire_at = expiration_date

    # log
    print("APPLE WEBHOOK:", notification_type, transaction_id)

    db.commit()

    return {"success": True}


@router.post("/webhook/android")
async def android_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    payload = _decode_android_pubsub_payload(body)

    package_name = payload.get("packageName")
    if package_name != settings.ANDROID_PACKAGE_NAME:
        raise HTTPException(400, "Invalid package")

    subscription_notification = payload.get("subscriptionNotification")
    if not subscription_notification:
        # testNotification / oneTimeProductNotification
        return {"success": True}

    purchase_token = subscription_notification.get("purchaseToken")
    product_id = subscription_notification.get("subscriptionId")
    notification_type = subscription_notification.get("notificationType")

    if not purchase_token or not product_id:
        raise HTTPException(400, "Missing purchase token or product id")

    sub = db.query(Subscription).filter(
        Subscription.original_transaction_id == purchase_token
    ).first()

    if not sub:
        print("ANDROID WEBHOOK: subscription not found, skip", purchase_token)
        return {"success": True}

    result = verify_android_subscription(
        package_name=settings.ANDROID_PACKAGE_NAME,
        product_id=product_id,
        purchase_token=purchase_token
    )

    now = datetime.now(timezone.utc)
    is_active, expiration_date = _is_android_subscription_active(result, now)

    start_ms = result.get("startTimeMillis")
    if start_ms:
        sub.purchase_date = datetime.fromtimestamp(
            int(start_ms) / 1000,
            tz=timezone.utc
        )

    if expiration_date:
        sub.expiration_date = expiration_date

    sub.product_id = product_id
    sub.transaction_id = result.get("orderId") or sub.transaction_id
    sub.status = "active" if is_active else "expired"

    user = db.query(User).get(sub.user_id) if sub.user_id else None
    if user:
        user.is_paid = is_active
        user.premium_expire_at = expiration_date

    # Keep event log for debugging RTDN flow and event mapping.
    print("ANDROID WEBHOOK:", {
        "notification_type": notification_type,
        "purchase_token": purchase_token,
        "product_id": product_id,
        "is_active": is_active
    })

    db.commit()

    return {"success": True}