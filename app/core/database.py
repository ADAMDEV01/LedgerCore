import os
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria
from sqlalchemy.pool import NullPool

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
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, info={"organization_id": 1}
)


def get_db() -> Iterator[Session]:
    """One session per request. Handlers commit explicitly; anything uncommitted rolls back."""
    db = SessionLocal()
    db.info["organization_id"] = 1
    try:
        yield db
    finally:
        db.close()


_TENANT_MODEL_NAMES = (
    "Account", "AccountingPeriod", "DocumentSequence", "JournalEntry",
    "JournalEntrySeal", "LedgerLine", "Invoice", "InvoicePayment",
    "AuditLog", "IdempotencyRecord",
)


def set_tenant_context(session: Session, organization_id: int) -> None:
    if organization_id < 1:
        raise ValueError("organization_id must be positive")
    session.info["organization_id"] = organization_id
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        session.execute(
            text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
            {"organization_id": str(organization_id)},
        )


@event.listens_for(Session, "after_begin")
def _set_postgres_tenant_context(session: Session, transaction, connection) -> None:
    if connection.dialect.name == "postgresql":
        from sqlalchemy import text
        connection.execute(
            text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
            {"organization_id": str(session.info.get("organization_id", 1))},
        )


@event.listens_for(Session, "do_orm_execute")
def _scope_tenant_queries(state) -> None:
    if not state.is_orm_statement:
        return
    organization_id = state.session.info.get("organization_id")
    if not organization_id:
        return
    from app import models
    tenant_models = tuple(getattr(models, name) for name in _TENANT_MODEL_NAMES)
    if state.is_select:
        for model in tenant_models:
            state.statement = state.statement.options(
                with_loader_criteria(
                    model,
                    lambda entity: entity.organization_id == organization_id,
                    include_aliases=True,
                )
            )
    elif state.is_update or state.is_delete:
        mapper = state.bind_mapper
        if mapper is not None and mapper.class_ in tenant_models + (models.User,):
            state.statement = state.statement.where(mapper.class_.organization_id == organization_id)


@event.listens_for(Session, "before_flush")
def _stamp_tenant_rows(session: Session, _flush_context, _instances) -> None:
    organization_id = session.info.get("organization_id", 1)
    from app import models
    tenant_models = tuple(getattr(models, name) for name in _TENANT_MODEL_NAMES) + (models.User,)
    for obj in session.new:
        if isinstance(obj, tenant_models):
            current = getattr(obj, "organization_id", None)
            if current is not None and current != organization_id:
                raise ValueError("Cannot create a record for another organization")
            obj.organization_id = organization_id
    for obj in session.dirty:
        if isinstance(obj, tenant_models) and obj.organization_id != organization_id:
            raise ValueError("Cannot modify a record owned by another organization")
