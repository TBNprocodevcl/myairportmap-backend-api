
"""
Storage backend abstraction for Map20.

Supports:
- Local filesystem (default)
- Cloudflare R2 (S3-compatible) when R2_* env vars are present.

Keys:
- For local mode, keys are treated as absolute paths (or relative to BASE_DIR where used).
- For R2 mode, keys are treated as object keys (e.g., "users/<handle>/my_visits.csv").
"""
from __future__ import annotations

import os
from typing import Optional

_R2_CLIENT = None

def _r2_enabled() -> bool:
    return bool(os.getenv("R2_BUCKET_NAME") and (os.getenv("R2_ENDPOINT_URL") or os.getenv("R2_ACCOUNT_ID")))

def _r2_client():
    global _R2_CLIENT
    if _R2_CLIENT is not None:
        return _R2_CLIENT

    import boto3
    from botocore.config import Config

    endpoint_url = os.getenv("R2_ENDPOINT_URL")
    if not endpoint_url:
        account_id = os.environ["R2_ACCOUNT_ID"]
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    _R2_CLIENT = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return _R2_CLIENT

def exists(key_or_path: str) -> bool:
    if _r2_enabled():
        s3 = _r2_client()
        bucket = os.environ["R2_BUCKET_NAME"]
        try:
            s3.head_object(Bucket=bucket, Key=key_or_path)
            return True
        except Exception:
            return False
    return os.path.exists(key_or_path)

def read_bytes(key_or_path: str) -> bytes:
    if _r2_enabled():
        s3 = _r2_client()
        bucket = os.environ["R2_BUCKET_NAME"]
        try:
            obj = s3.get_object(Bucket=bucket, Key=key_or_path)
            return obj["Body"].read()
        except Exception:
            # Treat missing keys as empty (callers interpret as "not found")
            return b""
    with open(key_or_path, "rb") as f:
        return f.read()

def write_bytes(key_or_path: str, data: bytes, content_type: Optional[str] = None, cache_control: Optional[str] = None) -> None:
    if _r2_enabled():
        s3 = _r2_client()
        bucket = os.environ["R2_BUCKET_NAME"]
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        s3.put_object(Bucket=bucket, Key=key_or_path, Body=data, **extra)
        return

    # local
    os.makedirs(os.path.dirname(key_or_path), exist_ok=True)
    with open(key_or_path, "wb") as f:
        f.write(data)
