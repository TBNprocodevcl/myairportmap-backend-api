from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
print("👉 DATABASE_URL:", settings.DATABASE_URL)

engine = create_engine(settings.DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)