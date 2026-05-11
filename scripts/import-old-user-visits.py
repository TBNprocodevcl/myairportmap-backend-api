"""
Import legacy user visits from Cloudflare R2 into the DB.

Source layout on bucket:
- users/<handle>/my_visits.csv

Behavior:
- Maps file owner by <handle> to users.handle in DB
- Normalizes airport code (KJFK -> JFK)
- Skips rows with invalid date/airport
- Skips airports that do not exist in airports table
- De-duplicates against existing visits for each user

Usage:
  python scripts/import-old-user-visits.py
  python scripts/import-old-user-visits.py --handle ngoc
  python scripts/import-old-user-visits.py --dry-run

Required environment variables (preferred):
- R2_ACCESS_KEY_ID
- R2_SECRET_ACCESS_KEY

Optional environment variables:
- R2_BUCKET_NAME (default: mapusers)
- R2_ACCOUNT_ID (used to construct endpoint if R2_ENDPOINT_URL is missing)
- R2_ENDPOINT_URL (overrides endpoint built from account id)

Fallback:
- If R2 vars are missing, script will try reading ACCOUNT_ID / ACCESS_KEY /
    SECRET_KEY / BUCKET_NAME from getdata.py.
"""

import argparse
import csv
import io
import os
import sys
from datetime import datetime
from typing import Dict, Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import boto3
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "boto3 is required. Install it with: pip install boto3"
    ) from exc

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.airport import Airport
from app.models.user import User
from app.models.visit import Visit


def normalize_airport_id(code: Optional[str]) -> Optional[str]:
    if not code:
        return None

    code = code.strip().upper()
    if not code:
        return None

    # US ICAO -> FAA/IATA style used in this project
    if len(code) == 4 and code.startswith("K"):
        return code[1:]

    return code


def parse_date(date_str: Optional[str]):
    if not date_str:
        return None

    raw = date_str.strip()
    if not raw:
        return None

    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    return None


def build_s3_client():
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    endpoint_url = os.getenv("R2_ENDPOINT_URL")
    account_id = os.getenv("R2_ACCOUNT_ID")

    if not access_key or not secret_key or (not endpoint_url and not account_id):
        try:
            import getdata as legacy_r2

            account_id = account_id or getattr(legacy_r2, "ACCOUNT_ID", None)
            access_key = access_key or getattr(legacy_r2, "ACCESS_KEY", None)
            secret_key = secret_key or getattr(legacy_r2, "SECRET_KEY", None)
            endpoint_url = endpoint_url or getattr(legacy_r2, "ENDPOINT_URL", None)
        except Exception:
            # Keep env values as-is and fail with a clear message below if still missing.
            pass

    if not access_key or not secret_key:
        raise RuntimeError(
            "Missing R2 credentials. Set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY."
        )

    if not endpoint_url:
        if not account_id:
            raise RuntimeError(
                "Missing endpoint configuration. Set R2_ENDPOINT_URL or R2_ACCOUNT_ID."
            )
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    return boto3.client(
        service_name="s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def resolve_bucket_name() -> str:
    bucket_name = os.getenv("R2_BUCKET_NAME")
    if bucket_name:
        return bucket_name

    try:
        import getdata as legacy_r2

        legacy_bucket = getattr(legacy_r2, "BUCKET_NAME", None)
        if legacy_bucket:
            return legacy_bucket
    except Exception:
        pass

    return "mapusers"


def iter_visit_keys(s3_client, bucket_name: str) -> Iterable[str]:
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Prefix="users/"):
        for obj in page.get("Contents", []):
            key = obj.get("Key") or ""
            if key.count("/") >= 2 and key.endswith("/my_visits.csv"):
                yield key


def extract_handle_from_key(key: str) -> Optional[str]:
    # users/<handle>/my_visits.csv
    parts = key.split("/")
    if len(parts) != 3:
        return None
    if parts[0] != "users" or parts[2] != "my_visits.csv":
        return None
    return parts[1].strip().lower() or None


def load_existing_visit_set(db: Session, user_id):
    rows = (
        db.query(Visit.airport_id, Visit.date_visited, Visit.callsign, Visit.notes)
        .filter(Visit.user_id == user_id)
        .all()
    )
    return {(a, d, c, n) for a, d, c, n in rows}


def parse_args():
    parser = argparse.ArgumentParser(description="Import old user visits from R2")
    parser.add_argument(
        "--handle",
        help="Only import one user handle (example: ngoc)",
    )
    parser.add_argument(
        "--limit-users",
        type=int,
        default=0,
        help="Limit number of users/files to process (0 = no limit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate data without writing to DB",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    bucket_name = resolve_bucket_name()

    s3 = build_s3_client()
    db: Session = SessionLocal()

    summary: Dict[str, int] = {
        "files_seen": 0,
        "files_processed": 0,
        "rows_total": 0,
        "inserted": 0,
        "duplicates": 0,
        "invalid_date": 0,
        "invalid_airport": 0,
        "unknown_airport": 0,
        "user_not_found": 0,
    }

    try:
        users = db.query(User.id, User.handle).all()
        users_by_handle = {
            (handle or "").lower(): user_id
            for user_id, handle in users
            if handle
        }

        known_airports = {code for (code,) in db.query(Airport.airport_id).all()}

        processed_users = 0

        for key in iter_visit_keys(s3, bucket_name):
            summary["files_seen"] += 1

            handle = extract_handle_from_key(key)
            if not handle:
                continue

            if args.handle and handle != args.handle.strip().lower():
                continue

            user_id = users_by_handle.get(handle)
            if not user_id:
                summary["user_not_found"] += 1
                print(f"[SKIP USER] handle={handle} key={key} (not found in DB)")
                continue

            if args.limit_users > 0 and processed_users >= args.limit_users:
                break

            response = s3.get_object(Bucket=bucket_name, Key=key)
            raw_bytes = response["Body"].read()
            text = raw_bytes.decode("utf-8-sig", errors="replace")

            reader = csv.DictReader(io.StringIO(text))
            existing = load_existing_visit_set(db, user_id)

            file_inserted = 0
            file_rows = 0

            for row in reader:
                file_rows += 1
                summary["rows_total"] += 1

                airport_id = normalize_airport_id(row.get("airport_id"))
                date_visited = parse_date(row.get("date_visited"))
                callsign = (row.get("callsign") or "").strip() or None
                notes = (row.get("notes") or "").strip() or None

                if not airport_id:
                    summary["invalid_airport"] += 1
                    continue

                if not date_visited:
                    summary["invalid_date"] += 1
                    continue

                if airport_id not in known_airports:
                    summary["unknown_airport"] += 1
                    continue

                dedup_key = (airport_id, date_visited, callsign, notes)
                if dedup_key in existing:
                    summary["duplicates"] += 1
                    continue

                if not args.dry_run:
                    db.add(
                        Visit(
                            user_id=user_id,
                            airport_id=airport_id,
                            date_visited=date_visited,
                            callsign=callsign,
                            notes=notes,
                        )
                    )

                existing.add(dedup_key)
                file_inserted += 1
                summary["inserted"] += 1

                if not args.dry_run and file_inserted % 500 == 0:
                    db.flush()

            if not args.dry_run:
                db.commit()

            summary["files_processed"] += 1
            processed_users += 1
            print(
                f"[DONE] handle={handle} key={key} rows={file_rows} inserted={file_inserted}"
            )

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
