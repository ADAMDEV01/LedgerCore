from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["application settings"])


@router.get("/settings")
def application_settings() -> dict[str, str]:
    """Expose non-sensitive runtime settings needed to keep UI and API consistent."""
    return {"base_currency": settings.BASE_CURRENCY}
