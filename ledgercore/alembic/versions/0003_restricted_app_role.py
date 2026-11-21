"""grant runtime role only DML privileges

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    role_exists = bind.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app'")
    ).scalar_one_or_none()
    if not role_exists:
        # Local development and SQLite-based migration checks do not create runtime roles.
        return

    op.execute("GRANT USAGE ON SCHEMA public TO ledger_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ledger_app"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_app")
    op.execute("REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM ledger_app")
    op.execute(
        "REVOKE ALL ON TABLE alembic_version FROM ledger_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE ledger_migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledger_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE ledger_migrator IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO ledger_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE ledger_migrator IN SCHEMA public "
        "REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM ledger_app"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app'")).scalar_one_or_none():
        op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM ledger_app")
        op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM ledger_app")
