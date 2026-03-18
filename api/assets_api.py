from flask import Blueprint, send_from_directory
import os

assets_api = Blueprint("assets_api", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@assets_api.route("/api/assets/logo", methods=["GET"])
def serve_logo():
    """
    Serve the main logo image
    ---
    tags:
      - Assets
    responses:
      200:
        description: Returns the logo image
        content:
          image/png:
            schema:
              type: string
              format: binary
    """
    return send_from_directory(BASE_DIR, "logo.png")


@assets_api.route("/favicon.ico")
def favicon():
    """
    Serve the favicon
    ---
    tags:
      - Assets
    responses:
      200:
        description: Returns the favicon
        content:
          image/vnd.microsoft.icon:
            schema:
              type: string
              format: binary
    """
    return send_from_directory(
        os.path.join(assets_api.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon"
    )