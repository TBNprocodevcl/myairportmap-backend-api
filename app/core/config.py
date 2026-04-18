from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    DATABASE_URL = os.getenv("DATABASE_URL")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "fallback-secret")
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID")
    RESET_PASSWORD_URL: str = os.getenv("RESET_PASSWORD_URL")
    SENDGRID_API_KEY: str = os.getenv("SENDGRID_API_KEY")
    EMAIL_FROM: str = os.getenv("EMAIL_FROM")
    BASE_URL: str = os.getenv("BASE_URL")
    APPLE_KEY_ID: str = os.getenv("APPLE_KEY_ID")
    APPLE_ISSUER_ID: str = os.getenv("APPLE_ISSUER_ID")
    APPLE_BUNDLE_ID: str = os.getenv("APPLE_BUNDLE_ID")
    APPLE_SHARED_SECRET: str = os.getenv("APPLE_SHARED_SECRET")
    PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
    DEMO_FLAG = os.getenv("DEMO_FLAG", "false").lower() == "true"
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    ANDROID_PACKAGE_NAME = os.getenv("ANDROID_PACKAGE_NAME")

settings = Settings()