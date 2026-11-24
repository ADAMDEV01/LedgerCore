"""Add organization workspaces and tenant isolation.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "accounts", "accounting_periods", "document_sequences", "journal_entries",
    "journal_entry_seals", "ledger_lines", "invoices", "invoice_payments",
    "audit_logs", "idempotency_records", "users",
)
RLS_TABLES = tuple(table for table in TENANT_TABLES if table != "users")


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (1, 'Default Organization', 'default', CURRENT_TIMESTAMP)"
        )
    )

    # Backfill every existing account and financial record into one preserved workspace.
    for table in TENANT_TABLES:
        op.add_column(
            table,
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        )
        op.execute(sa.text(f"UPDATE {table} SET organization_id = 1 WHERE organization_id IS NULL"))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("organization_id", existing_type=sa.Integer(), nullable=False)
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])

    # Numbering and accounting-period keys are local to each organization.
    with op.batch_alter_table("accounting_periods") as batch:
        batch.drop_constraint("pk_accounting_periods", type_="primary")
        batch.create_primary_key("pk_accounting_periods", ["organization_id", "period"])
    with op.batch_alter_table("document_sequences") as batch:
        batch.drop_constraint("pk_document_sequences", type_="primary")
        batch.create_primary_key("pk_document_sequences", ["organization_id", "name"])
    with op.batch_alter_table("journal_entry_seals") as batch:
        batch.drop_constraint("pk_journal_entry_seals", type_="primary")
        batch.create_primary_key("pk_journal_entry_seals", ["organization_id", "entry_id"])

    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_accounts_code", type_="unique")
        batch.create_unique_constraint("uq_accounts_org_code", ["organization_id", "code"])
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint("uq_journal_entries_reference_no", type_="unique")
        batch.create_unique_constraint(
            "uq_journal_org_reference", ["organization_id", "reference_no"]
        )
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("uq_invoices_invoice_number", type_="unique")
        batch.create_unique_constraint(
            "uq_invoices_org_number", ["organization_id", "invoice_number"]
        )
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_constraint("uq_audit_logs_seq", type_="unique")
        batch.create_unique_constraint("uq_audit_org_seq", ["organization_id", "seq"])
    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_constraint("uq_idempotency_actor_operation_key", type_="unique")
        batch.create_unique_constraint(
            "uq_idempotency_org_actor_operation_key",
            ["organization_id", "actor", "operation", "key"],
        )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in RLS_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::integer) "
                "WITH CHECK (organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::integer)"
            )
        op.execute("""
            CREATE OR REPLACE FUNCTION ledgercore_enforce_tenant_references()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_TABLE_NAME = 'ledger_lines' THEN
                    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.entry_id AND organization_id = NEW.organization_id)
                       OR NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id AND organization_id = NEW.organization_id) THEN
                        RAISE EXCEPTION 'cross-organization ledger line reference';
                    END IF;
                ELSIF TG_TABLE_NAME = 'journal_entry_seals' THEN
                    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.entry_id AND organization_id = NEW.organization_id) THEN
                        RAISE EXCEPTION 'cross-organization journal seal reference';
                    END IF;
                ELSIF TG_TABLE_NAME = 'invoices' THEN
                    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.journal_entry_id AND organization_id = NEW.organization_id) THEN
                        RAISE EXCEPTION 'cross-organization invoice journal reference';
                    END IF;
                ELSIF TG_TABLE_NAME = 'invoice_payments' THEN
                    IF NOT EXISTS (SELECT 1 FROM invoices WHERE id = NEW.invoice_id AND organization_id = NEW.organization_id)
                       OR NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.journal_entry_id AND organization_id = NEW.organization_id) THEN
                        RAISE EXCEPTION 'cross-organization invoice payment reference';
                    END IF;
                ELSIF TG_TABLE_NAME = 'journal_entries' AND NEW.reverses_entry_id IS NOT NULL THEN
                    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND organization_id = NEW.organization_id) THEN
                        RAISE EXCEPTION 'cross-organization reversal reference';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;
        """)
        for table in ("ledger_lines", "journal_entry_seals", "invoices", "invoice_payments", "journal_entries"):
            op.execute(
                f"CREATE TRIGGER {table}_tenant_reference_guard BEFORE INSERT OR UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION ledgercore_enforce_tenant_references()"
            )
        # New tenant tables must receive the same least-privilege DML grants as earlier migrations.
        role_exists = bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app'")
        ).scalar_one_or_none()
        if role_exists:
            op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO ledger_app")
            op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_app")
            op.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE ledger_migrator IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledger_app"
            )
    elif bind.dialect.name == "sqlite":
        # SQLite is used for local development only; keep cross-workspace references guarded.
        guards = {
            "ledger_lines": "EXISTS (SELECT 1 FROM journal_entries WHERE id=NEW.entry_id AND organization_id<>NEW.organization_id) OR EXISTS (SELECT 1 FROM accounts WHERE id=NEW.account_id AND organization_id<>NEW.organization_id)",
            "journal_entry_seals": "EXISTS (SELECT 1 FROM journal_entries WHERE id=NEW.entry_id AND organization_id<>NEW.organization_id)",
            "invoices": "EXISTS (SELECT 1 FROM journal_entries WHERE id=NEW.journal_entry_id AND organization_id<>NEW.organization_id)",
            "invoice_payments": "EXISTS (SELECT 1 FROM invoices WHERE id=NEW.invoice_id AND organization_id<>NEW.organization_id) OR EXISTS (SELECT 1 FROM journal_entries WHERE id=NEW.journal_entry_id AND organization_id<>NEW.organization_id)",
            "journal_entries": "NEW.reverses_entry_id IS NOT NULL AND EXISTS (SELECT 1 FROM journal_entries WHERE id=NEW.reverses_entry_id AND organization_id<>NEW.organization_id)",
        }
        for table, condition in guards.items():
            op.execute(
                f"CREATE TRIGGER {table}_tenant_reference_guard BEFORE INSERT ON {table} "
                f"WHEN {condition} BEGIN SELECT RAISE(ABORT, 'cross-organization reference'); END"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        for table in ("ledger_lines", "journal_entry_seals", "invoices", "invoice_payments", "journal_entries"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_tenant_reference_guard ON {table}")
        op.execute("DROP FUNCTION IF EXISTS ledgercore_enforce_tenant_references()")
    elif bind.dialect.name == "sqlite":
        for table in ("ledger_lines", "journal_entry_seals", "invoices", "invoice_payments", "journal_entries"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_tenant_reference_guard")

    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_constraint("uq_idempotency_org_actor_operation_key", type_="unique")
        batch.create_unique_constraint(
            "uq_idempotency_actor_operation_key", ["actor", "operation", "key"]
        )
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_constraint("uq_audit_org_seq", type_="unique")
        batch.create_unique_constraint("uq_audit_logs_seq", ["seq"])
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("uq_invoices_org_number", type_="unique")
        batch.create_unique_constraint("uq_invoices_invoice_number", ["invoice_number"])
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint("uq_journal_org_reference", type_="unique")
        batch.create_unique_constraint("uq_journal_entries_reference_no", ["reference_no"])
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_accounts_org_code", type_="unique")
        batch.create_unique_constraint("uq_accounts_code", ["code"])
    with op.batch_alter_table("journal_entry_seals") as batch:
        batch.drop_constraint("pk_journal_entry_seals", type_="primary")
        batch.create_primary_key("pk_journal_entry_seals", ["entry_id"])
    with op.batch_alter_table("document_sequences") as batch:
        batch.drop_constraint("pk_document_sequences", type_="primary")
        batch.create_primary_key("pk_document_sequences", ["name"])
    with op.batch_alter_table("accounting_periods") as batch:
        batch.drop_constraint("pk_accounting_periods", type_="primary")
        batch.create_primary_key("pk_accounting_periods", ["period"])

    for table in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("organization_id")
    op.drop_table("organizations")
