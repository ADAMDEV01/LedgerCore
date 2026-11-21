from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app import models  # noqa: F401
from app.core.database import Base

ROOT = Path(__file__).resolve().parent.parent


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_upgrade_creates_every_model_table_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path}/migrate.db"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    command.downgrade(_config(url), "base")
    assert set(inspect(create_engine(url)).get_table_names()) <= {"alembic_version"}


def test_migrated_schema_matches_the_models(tmp_path):
    """Parity check: the hand-written migration must not drift from models.py.

    On PostgreSQL (CI) the full autogenerate diff must be empty. SQLite reports cosmetic
    differences for some types, so there only tables and columns are compared.
    """
    url = f"sqlite:///{tmp_path}/parity.db"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    structural = {"add_table", "remove_table", "add_column", "remove_column"}
    problems = [d for d in diff if d[0] in structural]
    assert problems == []


def test_migration_installs_append_only_triggers(tmp_path):
    url = f"sqlite:///{tmp_path}/triggers.db"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs (seq, actor, action, entity, details, created_at, prev_hash, row_hash) "
                "VALUES (1, 'a', 'X', 'e', 'd', '2026-01-01 00:00:00', 'p', 'h')"
            )
        )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE audit_logs SET actor = 'tampered'"))


def test_migration_protects_audit_sequence_high_water_mark(tmp_path):
    url = f"sqlite:///{tmp_path}/sequence-guard.db"
    command.upgrade(_config(url), "head")
    migrated_engine = create_engine(url)
    with migrated_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO document_sequences (name, last_value) VALUES ('AUDIT', 5)")
        )
    with pytest.raises(DBAPIError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("UPDATE document_sequences SET last_value = 0 WHERE name = 'AUDIT'")
            )
    with pytest.raises(DBAPIError):
        with migrated_engine.begin() as connection:
            connection.execute(text("DELETE FROM document_sequences WHERE name = 'AUDIT'"))
    migrated_engine.dispose()
