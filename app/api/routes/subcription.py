import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime
from uuid import uuid4

from app.db.session import get_db
from app.schemas.apple import VerifyReceiptRequest
from app.models.subscription import Subscription
from app.models.user import User
from app.core.security import get_current_user

router = APIRouter()

APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"
APPLE_PROD_URL = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_SHARED_SECRET = "YOUR_SHARED_SECRET"
APPLE_ROOT_CERTS = "https://www.apple.com/certificateauthority/AppleRootCA-G3.cer"


@router.post("/subscription/verify")
def verify_subscription(
    data: VerifyReceiptRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    payload = {
        "receipt-data": data.receipt_data,
        "password": APPLE_SHARED_SECRET
    }

    # 1. call Apple
    res = requests.post(APPLE_PROD_URL, json=payload)
    result = res.json()

    # 2. sandbox fallback
    if result.get("status") == 21007:
        res = requests.post(APPLE_SANDBOX_URL, json=payload)
        result = res.json()

    if result.get("status") != 0:
        raise HTTPException(400, "Invalid receipt")

    # 3. get latest receipt
    receipts = result.get("latest_receipt_info", [])

    if not receipts:
        raise HTTPException(400, "No subscription found")

    latest = sorted(
        receipts,
        key=lambda x: int(x["expires_date_ms"]),
        reverse=True
    )[0]

    expiration_date = datetime.fromtimestamp(
        int(latest["expires_date_ms"]) / 1000
    )

    # 4. save DB
    sub = Subscription(
        id=uuid4(),
        user_id=user.id,
        product_id=latest["product_id"],
        transaction_id=latest["transaction_id"],
        original_transaction_id=latest["original_transaction_id"],
        purchase_date=datetime.fromtimestamp(int(latest["purchase_date_ms"]) / 1000),
        expiration_date=expiration_date,
        platform="ios",
        status="active" if expiration_date > datetime.utcnow() else "expired"
    )

    db.add(sub)
    db.commit()

    # 5. response
    return {
        "success": True,
        "data": {
            "is_premium": expiration_date > datetime.utcnow(),
            "expiration_date": expiration_date
        }
    }

@router.post("/apple/webhook")
async def apple_webhook(request: Request):
    body = await request.json()

    signed_payload = body.get("signedPayload")

    if not signed_payload:
        raise HTTPException(400, "Missing payload")

    # 1. Decode JWT (không verify signature đơn giản version)
    try:
        decoded = jwt.decode(
            signed_payload,
            options={"verify_signature": False}
        )
    except Exception:
        raise HTTPException(400, "Invalid payload")

    notification_type = decoded.get("notificationType")
    data = decoded.get("data", {})

    # 2. Extract subscription info
    renewal_info = data.get("signedRenewalInfo")
    transaction_info = data.get("signedTransactionInfo")

    # decode nested JWT
    tx = jwt.decode(transaction_info, options={"verify_signature": False})

    original_transaction_id = tx.get("originalTransactionId")
    product_id = tx.get("productId")

    # 3. Handle events
    if notification_type in ["DID_RENEW", "INITIAL_BUY"]:
        print("ACTIVE SUBSCRIPTION")

    elif notification_type == "EXPIRED":
        print("EXPIRED SUBSCRIPTION")

    elif notification_type == "REFUND":
        print("REFUND -> revoke access")

    return {"success": True}