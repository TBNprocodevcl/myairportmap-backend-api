from app.db.session import SessionLocal
from app.models.user import User
from passlib.context import CryptContext

db = SessionLocal()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str):
    return pwd_context.hash(password)

def avatar_url_for_handle(handle: str):
    return f"https://api.dicebear.com/7.x/initials/svg?seed={handle}"

def normalize_handle(email: str):
    return email.split("@")[0].lower()

def seed_users():
    raw_users = [
        {"email": "admin@gmail.com", "password": "123456"},
        {"email": "test@gmail.com", "password": "123456"},
        {"email": "demo@gmail.com", "password": "123456"},
    ]

    for u in raw_users:
        exists = db.query(User).filter(User.email == u["email"]).first()
        if exists:
            continue

        handle = normalize_handle(u["email"])

        user = User(
            email=u["email"],
            password=hash_password(u["password"]),
            handle=handle,
            avatar_url=avatar_url_for_handle(handle),
        )

        db.add(user)

    db.commit()
    print("✅ Seed users done")

if __name__ == "__main__":
    seed_users()