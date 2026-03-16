from flask import Blueprint, send_from_directory
import os

assets_api = Blueprint("assets_api", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@assets_api.route("/api/assets/logo", methods=["GET"])
def serve_logo():
    return send_from_directory(BASE_DIR, "logo.png")

@assets_api.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(assets_api.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon"
    )
