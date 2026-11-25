from fastapi import APIRouter

from app.api import accounts, admin, auth, invoices, journal, meta, reports, users

api_router = APIRouter(prefix="/api/v1")
for module in (auth, meta, users, accounts, journal, invoices, reports, admin):
    api_router.include_router(module.router)
