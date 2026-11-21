"""Ensure the runtime PostgreSQL role cannot bypass ledger triggers."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import engine

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL roles")


def _as_app_role(statement: str) -> None:
    with engine.connect() as connection:
        connection.execute(text("SET ROLE ledger_app"))
        with pytest.raises(DBAPIError):
            connection.execute(text(statement))
        connection.rollback()


def test_runtime_role_is_not_privileged_or_a_schema_owner():
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolbypassrls, r.rolinherit, "
                "has_schema_privilege('ledger_app', 'public', 'CREATE') AS can_create "
                "FROM pg_roles r WHERE r.rolname = 'ledger_app'"
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolcreatedb is False
    assert row.rolcreaterole is False
    assert row.rolbypassrls is False
    assert row.rolinherit is False
    assert row.can_create is False


def test_runtime_role_cannot_drop_ledger_triggers_or_truncate_entries():
    _as_app_role("DROP TRIGGER IF EXISTS journal_entries_append_only ON journal_entries")
    _as_app_role("TRUNCATE TABLE journal_entries")


def test_runtime_role_retains_required_dml_access():
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT has_table_privilege('ledger_app', 'journal_entries', 'SELECT') AS can_select, "
                "has_table_privilege('ledger_app', 'journal_entries', 'INSERT') AS can_insert, "
                "has_table_privilege('ledger_app', 'journal_entries', 'UPDATE') AS can_update, "
                "has_table_privilege('ledger_app', 'journal_entries', 'DELETE') AS can_delete, "
                "has_table_privilege('ledger_app', 'journal_entries', 'TRUNCATE') AS can_truncate"
            )
        ).one()
    assert row.can_select and row.can_insert and row.can_update and row.can_delete
    assert row.can_truncate is False
