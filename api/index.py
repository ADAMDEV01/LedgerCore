"""Vercel ASGI entry point for the LedgerCore FastAPI application."""

from app.main import app

__all__ = ["app"]
