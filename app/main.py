import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError

from app.api.router import api_router
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.errors import DomainError
from app.seed import seed_database

STATIC_DIR = Path(__file__).parent / "static"
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Vercel functions are ephemeral and may cold-start concurrently; production seeding
    # must be run explicitly once by an operator, never during function startup.
    if settings.SEED_ON_STARTUP and os.getenv("VERCEL") != "1":
        seed_database()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.VERSION,
        description="Double-entry accounting API with an append-only, hash-chained audit trail.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith(DOC_PATHS):
            response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.ENVIRONMENT == "production" and settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StaleDataError)
    async def stale_data_handler(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": "The record was modified concurrently. Reload and retry."}, status_code=409
        )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.VERSION}

    @app.get("/health/ready", tags=["health"])
    def ready() -> JSONResponse:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
