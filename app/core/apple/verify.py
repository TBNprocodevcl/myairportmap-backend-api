import jwt
import time
from app.core.config import settings
from cryptography.hazmat.primitives import serialization

def create_apple_token():
    with open(settings.PRIVATE_KEY_PATH, "rb") as f:
        key_data = f.read()
        private_key = serialization.load_pem_private_key(key_data, password=None)
        print("Key hợp lệ!")

    headers = {
        "alg": "ES256", # Thêm dòng này vào headers
        "kid": settings.APPLE_KEY_ID,
        "typ": "JWT"
    }

    payload = {
        "iss": settings.APPLE_ISSUER_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 1200,
        "aud": "appstoreconnect-v1",
        "bid": settings.APPLE_BUNDLE_ID
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers=headers
    )
    print(f"DEBUG TOKEN: {token}")

    return token