"""FastAPI application entry point for EBrowserAgent."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.errors import safe_request_validation_error
from app.api.routes import router
from app.config import get_settings

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(router, prefix="/api")

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


app.add_exception_handler(RequestValidationError, safe_request_validation_error)


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    """Serve the plain HTML frontend from the same origin as the API."""

    return FileResponse(FRONTEND_DIR / "index.html")
