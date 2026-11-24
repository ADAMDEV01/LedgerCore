import os
import tempfile

# Environment must be set before the app is imported: settings are read at import time.
_TMP = tempfile.mkdtemp(prefix="ledgercore-test-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-test-secret-key-0123456789")
os.environ["ENVIRONMENT"] = "test"
os.environ["SEED_ON_STARTUP"] = "false"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["INITIAL_ADMIN_EMAIL"] = "cfo@ledgertest.io"
os.environ["INITIAL_ADMIN_PASSWORD"] = "Correct-Horse-Battery-9"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP}/test.db"
os.environ.pop("COOKIE_SECURE", None)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402,F401
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.db_triggers import install_database_triggers  # noqa: E402
from app.seed import seed_database  # noqa: E402
from tests.helpers import PASSWORD, create_user, login, make_client  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database() -> None:
    """Every test starts from an empty schema plus the seeded chart of accounts and CFO."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install_database_triggers(connection)  # same DDL the migrations ship
        if engine.dialect.name == "postgresql":
            tenant_tables = (
                "accounts", "accounting_periods", "document_sequences", "journal_entries",
                "journal_entry_seals", "ledger_lines", "invoices", "invoice_payments",
                "audit_logs", "idempotency_records",
            )
            for table in tenant_tables:
                connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
                connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
                connection.execute(
                    text(
                        f"CREATE POLICY tenant_isolation ON {table} "
                        "USING (organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::integer) "
                        "WITH CHECK (organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::integer)"
                    )
                )
            connection.execute(
                text(
                    "DO $ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app') "
                    "THEN EXECUTE 'CREATE ROLE ledger_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT'; "
                    "END IF; END $$"
                )
            )
            connection.execute(text("GRANT USAGE ON SCHEMA public TO ledger_app"))
            connection.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ledger_app"
                )
            )
            connection.execute(
                text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_app")
            )
            connection.execute(
                text("REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM ledger_app")
            )
    seed_database()


@pytest.fixture
def db() -> Session:
    with SessionLocal() as session:
        yield session  # type: ignore[misc]


@pytest.fixture
def anon():  # type: ignore[no-untyped-def]
    return make_client()


@pytest.fixture
def cfo():  # type: ignore[no-untyped-def]
    return login(make_client(), "cfo@ledgertest.io", PASSWORD)


@pytest.fixture
def accountant():  # type: ignore[no-untyped-def]
    create_user("accountant@ledgertest.io", "ACCOUNTANT")
    return login(make_client(), "accountant@ledgertest.io", PASSWORD)


@pytest.fixture
def viewer():  # type: ignore[no-untyped-def]
    create_user("viewer@ledgertest.io", "VIEWER")
    return login(make_client(), "viewer@ledgertest.io", PASSWORD)
