"""database-enforced journal sealing and durable request idempotency

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""

import sqlalchemy as sa
from alembic import op

from app.core.db_triggers import drop_ledger_integrity_triggers, install_ledger_integrity_triggers

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail closed rather than silently sealing historic entries that were already corrupt.
    invalid_entry = op.get_bind().execute(
        sa.text(
            "SELECT je.id FROM journal_entries je "
            "LEFT JOIN ledger_lines ll ON ll.entry_id = je.id "
            "GROUP BY je.id "
            "HAVING COUNT(ll.id) < 2 "
            "OR COALESCE(SUM(ll.debit), 0) <> COALESCE(SUM(ll.credit), 0) "
            "LIMIT 1"
        )
    ).first()
    if invalid_entry is not None:
        raise RuntimeError(
            f"Cannot enable database ledger enforcement: journal entry {invalid_entry[0]} is unbalanced"
        )

    op.create_table(
        "journal_entry_seals",
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["journal_entries.id"], name="fk_journal_entry_seals_entry_id_journal_entries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("entry_id", name="pk_journal_entry_seals"),
    )
    # Existing posted entries are already immutable through the original UPDATE/DELETE guards;
    # seal them before enabling the INSERT guard so they retain the same protection.
    op.execute(
        sa.text(
            "INSERT INTO journal_entry_seals (entry_id, sealed_at) "
            "SELECT id, created_at FROM journal_entries"
        )
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=254), nullable=False),
        sa.Column("operation", sa.String(length=120), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "actor", "operation", "key", name="uq_idempotency_actor_operation_key"
        ),
    )
    op.create_index(
        "ix_idempotency_records_created_at", "idempotency_records", ["created_at"]
    )
    install_ledger_integrity_triggers(op.get_bind())


def downgrade() -> None:
    drop_ledger_integrity_triggers(op.get_bind())
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_table("journal_entry_seals")
