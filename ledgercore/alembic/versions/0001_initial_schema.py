"""initial schema with append-only ledger triggers

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

from app.core.db_triggers import drop_append_only_triggers, install_append_only_triggers

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

MONEY = sa.Numeric(14, 2)
RATE = sa.Numeric(12, 6)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("attempted_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempts"),
    )
    op.create_index(
        "ix_login_attempts_email_attempted_at", "login_attempts", ["email", "attempted_at"]
    )
    op.create_index("ix_login_attempts_ip_attempted_at", "login_attempts", ["ip", "attempted_at"])
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("account_type", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("code", name="uq_accounts_code"),
    )
    op.create_table(
        "accounting_periods",
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("closed_by", sa.String(254), nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("period", name="pk_accounting_periods"),
    )
    op.create_table(
        "document_sequences",
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_document_sequences"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reference_no", sa.String(20), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("accounting_period", sa.String(7), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("posted_by", sa.String(254), nullable=False),
        sa.Column("reverses_entry_id", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["journal_entries.id"],
            name="fk_journal_entries_reverses_entry_id_journal_entries",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entries"),
        sa.UniqueConstraint("reference_no", name="uq_journal_entries_reference_no"),
        sa.UniqueConstraint("reverses_entry_id", name="uq_journal_entries_reverses_entry_id"),
    )
    op.create_index("ix_journal_entries_entry_date", "journal_entries", ["entry_date"])
    op.create_index(
        "ix_journal_entries_accounting_period", "journal_entries", ["accounting_period"]
    )
    op.create_table(
        "ledger_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("debit", MONEY, nullable=False),
        sa.Column("credit", MONEY, nullable=False),
        sa.CheckConstraint(
            "(debit > 0 AND credit = 0) OR (debit = 0 AND credit > 0)", name="ck_ledger_lines_one_sided"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["journal_entries.id"], name="fk_ledger_lines_entry_id_journal_entries"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_ledger_lines_account_id_accounts"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_lines"),
    )
    op.create_index("ix_ledger_lines_entry_id", "ledger_lines", ["entry_id"])
    op.create_index("ix_ledger_lines_account_id", "ledger_lines", ["account_id"])
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.String(20), nullable=False),
        sa.Column("client_name", sa.String(160), nullable=False),
        sa.Column("client_email", sa.String(254), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("exchange_rate", RATE, nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("tax_amount", MONEY, nullable=False),
        sa.Column("total_amount", MONEY, nullable=False),
        sa.Column("base_total", MONEY, nullable=False),
        sa.Column("base_settled", MONEY, nullable=False),
        sa.Column("amount_paid", MONEY, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(254), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name="fk_invoices_journal_entry_id_journal_entries",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint("invoice_number", name="uq_invoices_invoice_number"),
    )
    op.create_table(
        "invoice_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("payment_rate", RATE, nullable=False),
        sa.Column("cash_base", MONEY, nullable=False),
        sa.Column("ar_cleared_base", MONEY, nullable=False),
        sa.Column("fx_difference", MONEY, nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("recorded_by", sa.String(254), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name="fk_invoice_payments_invoice_id_invoices"
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name="fk_invoice_payments_journal_entry_id_journal_entries",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoice_payments"),
    )
    op.create_index("ix_invoice_payments_invoice_id", "invoice_payments", ["invoice_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(254), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity", sa.String(60), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.UniqueConstraint("seq", name="uq_audit_logs_seq"),
    )
    install_append_only_triggers(op.get_bind())


def downgrade() -> None:
    drop_append_only_triggers(op.get_bind())
    for index, table in (
        ("ix_invoice_payments_invoice_id", "invoice_payments"),
        ("ix_ledger_lines_account_id", "ledger_lines"),
        ("ix_ledger_lines_entry_id", "ledger_lines"),
        ("ix_journal_entries_accounting_period", "journal_entries"),
        ("ix_journal_entries_entry_date", "journal_entries"),
        ("ix_login_attempts_ip_attempted_at", "login_attempts"),
        ("ix_login_attempts_email_attempted_at", "login_attempts"),
        ("ix_refresh_tokens_family_id", "refresh_tokens"),
    ):
        op.drop_index(index, table_name=table)
    for table in (
        "audit_logs",
        "invoice_payments",
        "invoices",
        "ledger_lines",
        "journal_entries",
        "document_sequences",
        "accounting_periods",
        "accounts",
        "login_attempts",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)
