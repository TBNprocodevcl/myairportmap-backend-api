from fastapi import FastAPI
from app.api.routes import auth,airports, user

app = FastAPI()

app.include_router(auth.router, prefix="/auth")
app.include_router(user.router, prefix="/users")
app.include_router(airports.router, prefix="/airports")
