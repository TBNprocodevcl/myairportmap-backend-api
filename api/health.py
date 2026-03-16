from flask import Blueprint

health_api = Blueprint("health_api", __name__)

@health_api.route("/health")
def health():
    return {"ok": True}