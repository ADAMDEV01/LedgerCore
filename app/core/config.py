from typing import Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "LedgerCore"
    VERSION: str = "4.1.0"
    ENVIRONMENT: Literal["development", "test", "production"] = "development"

    # Required: the app refuses to start without a strong key. No default on purpose.
    SECRET_KEY: str = Field(min_length=32)
    DATABASE_URL: str = "sqlite:///./data/ledgercore.db"
    BASE_CURRENCY: str = "USD"

    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 7
    COOKIE_SECURE: Optional[bool] = None

    LOGIN_MAX_FAILURES: int = 5
    LOGIN_WINDOW_MINUTES: int = 15

    INITIAL_ADMIN_EMAIL: str = "admin@ledgercore.io"
    INITIAL_ADMIN_PASSWORD: Optional[str] = None
    DEMO_VIEWER_EMAIL: Optional[str] = None
    DEMO_VIEWER_PASSWORD: Optional[str] = None
    SEED_ON_STARTUP: bool = True
    SEED_DEMO_DATA: bool = False

    @model_validator(mode="after")
    def _reject_placeholder_secret(self) -> "Settings":
        if "REPLACE" in self.SECRET_KEY.upper():
            raise ValueError("SECRET_KEY still contains the placeholder value")
        if self.ENVIRONMENT == "production" and self.COOKIE_SECURE is False:
            raise ValueError("COOKIE_SECURE=false is not allowed in production")
        if self.ENVIRONMENT == "production" and not self.DATABASE_URL.startswith(
            ("postgres://", "postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("Production requires PostgreSQL; SQLite is not supported")
        if bool(self.DEMO_VIEWER_EMAIL) != bool(self.DEMO_VIEWER_PASSWORD):
            raise ValueError("DEMO_VIEWER_EMAIL and DEMO_VIEWER_PASSWORD must be configured together")
        return self

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.ENVIRONMENT == "production"


settings = Settings()  # type: ignore[call-arg]
