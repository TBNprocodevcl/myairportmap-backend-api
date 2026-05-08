"""
Import users from Airport Timesheet CSV into the DB.
- Extracts email, first_name, last_name from each row
- Skips users whose email already exists
- Creates user with is_first_login=True (no real password needed)
- Assigns a random secure temp password (user will set their own on first login)
"""

import csv
import json
import os
import sys
import secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.models.user import User
from app.core.security import hash_password

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Airport Timesheet - User Production.csv")


def normalize_handle(email: str) -> str:
    return email.split("@")[0].lower()


def generate_unique_handle(db, base_handle: str) -> str:
    handle = base_handle
    counter = 1
    while db.query(User).filter(User.handle == handle).first():
        handle = f"{base_handle}{counter}"
        counter += 1
    return handle


def main():
    db = SessionLocal()
    skipped = []
    created = []

    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)  # skip header row

            for row in reader:
                if not row or not row[0].strip():
                    continue

                email = row[0].strip().lower()

                # Parse JSON metadata if present
                first_name = ""
                last_name = ""
                if len(row) > 1 and row[1].strip():
                    try:
                        data = json.loads(row[1])
                        first_name = data.get("first_name") or ""
                        last_name = data.get("last_name") or ""
                    except json.JSONDecodeError:
                        pass

                # Skip if already exists
                existing = db.query(User).filter(User.email == email).first()
                if existing:
                    print(f"[SKIP]  {email} — already exists")
                    skipped.append(email)
                    continue

                base_handle = normalize_handle(email)
                handle = generate_unique_handle(db, base_handle)

                temp_password = secrets.token_urlsafe(16)

                user = User(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    handle=handle,
                    avatar_url=f"https://api.dicebear.com/7.x/initials/svg?seed={handle}",
                    password=hash_password(temp_password),
                    is_first_login=True,
                )
                db.add(user)
                db.flush()
                created.append(email)
                print(f"[CREATE] {email} ({first_name} {last_name})")

        db.commit()

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()

    print(f"\nDone. Created: {len(created)}, Skipped: {len(skipped)}")


if __name__ == "__main__":
    main()
