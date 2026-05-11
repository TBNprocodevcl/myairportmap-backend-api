"""
Create one legacy test user account in the database.

Usage:
	python scripts/import-1-old-user.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.user import User


EMAIL = "truongngoc2812002@gmail.com"
FIRST_NAME = "Truong"
LAST_NAME = "Ngoc"
TEMP_PASSWORD = "OldUser@123"


def normalize_handle(email: str) -> str:
	return email.split("@", 1)[0].lower()


def is_valid_email(email: str) -> bool:
	return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def generate_unique_handle(db, base_handle: str) -> str:
	handle = base_handle
	counter = 1
	while db.query(User).filter(User.handle == handle).first():
		handle = f"{base_handle}{counter}"
		counter += 1
	return handle


def main() -> None:
	email = EMAIL.strip().lower()

	if not is_valid_email(email):
		raise ValueError(f"Invalid email: {email}")

	db = SessionLocal()
	try:
		existing = db.query(User).filter(User.email == email).first()
		if existing:
			print(f"[SKIP] {email} already exists")
			return

		handle = generate_unique_handle(db, normalize_handle(email))

		user = User(
			email=email,
			first_name=FIRST_NAME,
			last_name=LAST_NAME,
			handle=handle,
			avatar_url=f"https://api.dicebear.com/7.x/initials/svg?seed={handle}",
			password=hash_password(TEMP_PASSWORD),
			is_first_login=True,
		)

		db.add(user)
		db.commit()

		print("[CREATE] User created successfully")
		print(f"  email: {email}")
		print(f"  handle: {handle}")
		print(f"  temp_password: {TEMP_PASSWORD}")
		print("  is_first_login: True")
	except Exception:
		db.rollback()
		raise
	finally:
		db.close()


if __name__ == "__main__":
	main()
