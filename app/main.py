from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from app.api.routes import auth,airports, user, visits, achievements, runway360, export, upload, certifications

app = FastAPI()

app.include_router(auth.router, prefix="/auth")
app.include_router(user.router, prefix="/users")
app.include_router(airports.router, prefix="/airports")
app.include_router(visits.router, prefix="/visits")
app.include_router(achievements.router, prefix="/achievements")
app.include_router(runway360.router, prefix="/runway360")
app.include_router(export.router, prefix="/export")
app.include_router(upload.router, prefix="/upload")
app.include_router(certifications.router, prefix="/certifications")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount(
    "/.well-known",
    StaticFiles(directory="well-known"),
    name="well-known"
)