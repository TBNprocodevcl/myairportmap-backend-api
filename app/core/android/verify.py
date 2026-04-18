from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.core.config import settings

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]

credentials = service_account.Credentials.from_service_account_file(
    settings.GOOGLE_SERVICE_ACCOUNT_FILE,
    scopes=SCOPES
)

service = build("androidpublisher", "v3", credentials=credentials)

def verify_android_subscription(package_name, product_id, purchase_token):
    result = service.purchases().subscriptions().get(
        packageName=package_name,
        subscriptionId=product_id,
        token=purchase_token
    ).execute()

    return result