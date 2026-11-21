import os
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def normalize_database_url(url: str) -> str:
    """Accept Heroku/Render style URLs and route them to the psycopg 3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def make_engine(url: str) -> Engine:
    url = normalize_database_url(url)
    if url.startswith("sqlite"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:" and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    if os.getenv("VERCEL") == "1":
        # Serverless instances should not retain idle DB connections between invocations.
        return create_engine(url, pool_pre_ping=True, poolclass=NullPool)
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """One session per request. Handlers commit explicitly; anything uncommitted rolls back."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
