from flask import Flask
from flasgger import Swagger
from api.airports_api import airports_api
from api.assets_api import assets_api
from api.auth_api import auth_api
from api.health import health_api
from api.billing import billing_api
from api.logbook import logbook_api
from api.profile_api import profile_api
from app import APP_TITLE, BASE_DIR, load_data


app = Flask(__name__)
swagger = Swagger(app)  # Đây là Swagger UI

# Đăng ký tất cả Blueprint
app.register_blueprint(airports_api)
app.register_blueprint(assets_api)
app.register_blueprint(auth_api)
app.register_blueprint(health_api)
app.register_blueprint(billing_api)
app.register_blueprint(logbook_api)
app.register_blueprint(profile_api)

if __name__ == "__main__":
    print(f"--- {APP_TITLE} ---")
    print(f"BASE_DIR: {BASE_DIR}")
    try:
        load_data()
    except Exception as e:
        print("!! Startup data load error:", repr(e))
    app.run(debug=True)