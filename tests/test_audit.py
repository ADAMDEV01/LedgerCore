import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core import clock
from app.core.database import engine
from app.core.errors import AppendOnlyViolation
from app.models import AuditLog, DocumentSequence, JournalEntry, LedgerLine
from app.services.audit import GENESIS_HASH, verify_chain
from tests.helpers import API, post_je


def test_business_actions_are_audited_in_order(cfo):
    post_je(cfo, "1000", "3000", "10.00")
    items = cfo.get(f"{API}/audit-logs").json()["items"]
    actions = [row["action"] for row in reversed(items)]
    assert "LOGIN" in actions and "JOURNAL_POSTED" in actions
    assert actions.index("LOGIN") < actions.index("JOURNAL_POSTED")


def test_chain_links_each_row_to_the_previous_hash(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    rows = list(db.scalars(select(AuditLog).order_by(AuditLog.seq)))
    assert rows[0].prev_hash == GENESIS_HASH
    for previous, current in zip(rows, rows[1:], strict=False):
        assert current.prev_hash == previous.row_hash
        assert current.seq == previous.seq + 1


def test_verify_endpoint_reports_an_intact_chain(cfo):
    post_je(cfo, "1000", "3000", "10.00")
    result = cfo.get(f"{API}/audit-logs/verify").json()
    assert result["valid"] is True and result["checked"] >= 2


def test_a_forged_row_is_detected_by_chain_verification(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    count = db.scalar(select(func.count()).select_from(AuditLog))
    db.add(
        AuditLog(
            organization_id=1, seq=count + 1, actor="intruder", action="FORGED", entity="x", details="y",
            created_at=clock.utcnow(), prev_hash=GENESIS_HASH, row_hash="f" * 64,
        )
    )
    db.commit()  # INSERT is allowed (append-only); the broken link is what gives it away
    result = verify_chain(db)
    assert result["valid"] is False and result["first_invalid_seq"] == count + 1
    assert cfo.get(f"{API}/audit-logs/verify").json()["valid"] is False


def test_deleting_the_last_audit_row_is_detected_by_sequence_high_water_mark(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    last_seq = db.get(DocumentSequence, (1, "AUDIT")).last_value
    if engine.dialect.name == "sqlite":
        # Simulate a privileged attacker who has already disabled the append-only trigger.
        db.execute(text("DROP TRIGGER IF EXISTS audit_logs_no_delete"))
    elif engine.dialect.name == "postgresql":
        # A database owner/superuser can disable triggers. Model that threat explicitly:
        # the runtime ledger_app role cannot do this, but an operator with table ownership can.
        db.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_append_only"))
        try:
            db.execute(text("DELETE FROM audit_logs WHERE organization_id = 1 AND seq = :seq"), {"seq": last_seq})
        finally:
            db.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_append_only"))
    if engine.dialect.name != "postgresql":
        db.execute(text("DELETE FROM audit_logs WHERE seq = :seq"), {"seq": last_seq})
    db.commit()
    result = verify_chain(db)
    assert result["valid"] is False
    assert result["first_invalid_seq"] == last_seq


def test_orm_refuses_to_update_audit_rows(cfo, db):
    row = db.scalars(select(AuditLog)).first()
    row.details = "rewritten history"
    with pytest.raises(AppendOnlyViolation):
        db.commit()


def test_orm_refuses_to_update_or_delete_ledger_rows(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    line = db.scalars(select(LedgerLine)).first()
    line.debit = line.debit + 1
    with pytest.raises(AppendOnlyViolation):
        db.flush()
    db.rollback()
    entry = db.scalars(select(JournalEntry)).first()
    db.delete(entry)
    with pytest.raises(AppendOnlyViolation):
        db.flush()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET actor = 'x'",
        "DELETE FROM audit_logs",
        "UPDATE ledger_lines SET debit = debit + 1",
        "DELETE FROM ledger_lines",
        "UPDATE journal_entries SET description = 'x'",
        "DELETE FROM journal_entries",
    ],
)
def test_database_triggers_block_raw_sql_tampering(cfo, db, statement):
    post_je(cfo, "1000", "3000", "10.00")  # make sure every table has at least one row
    with pytest.raises(DBAPIError):
        db.execute(text(statement))


def test_database_rejects_raw_sql_line_inserts_after_entry_is_sealed(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    entry = db.scalars(select(JournalEntry)).first()
    account_id = db.scalar(text("SELECT id FROM accounts WHERE code = '5000'"))
    with pytest.raises(DBAPIError, match="sealed"):
        db.execute(
            text(
                "INSERT INTO ledger_lines (organization_id, entry_id, account_id, debit, credit) "
                "VALUES (1, :entry_id, :account_id, 1.00, 0.00)"
            ),
            {"entry_id": entry.id, "account_id": account_id},
        )


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="deferred database constraints require PostgreSQL")
def test_postgres_rejects_unbalanced_entry_written_with_raw_sql():
    """Application validation can be bypassed; the deferred DB trigger must not be."""
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.current_organization_id', '1', true)"))
            entry_id = connection.execute(
                text(
                    "INSERT INTO journal_entries "
                    "(organization_id, reference_no, entry_date, accounting_period, description, source, posted_by, created_at) "
                    "VALUES (1, 'RAW-UNBALANCED', '2026-11-21', '2026-11', 'raw SQL test', 'MANUAL', "
                    "'attacker@example.test', CURRENT_TIMESTAMP) RETURNING id"
                )
            ).scalar_one()
            debit_account = connection.execute(
                text("SELECT id FROM accounts WHERE code = '1000'")
            ).scalar_one()
            credit_account = connection.execute(
                text("SELECT id FROM accounts WHERE code = '3000'")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO ledger_lines (organization_id, entry_id, account_id, debit, credit) "
                    "VALUES (1, :entry_id, :account_id, 100.00, 0.00)"
                ),
                {"entry_id": entry_id, "account_id": debit_account},
            )
            connection.execute(
                text(
                    "INSERT INTO ledger_lines (organization_id, entry_id, account_id, debit, credit) "
                    "VALUES (1, :entry_id, :account_id, 0.00, 80.00)"
                ),
                {"entry_id": entry_id, "account_id": credit_account},
            )
            connection.execute(
                text(
                    "INSERT INTO journal_entry_seals (organization_id, entry_id, sealed_at) "
                    "VALUES (1, :entry_id, CURRENT_TIMESTAMP)"
                ),
                {"entry_id": entry_id},
            )


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="deferred database constraints require PostgreSQL")
def test_postgres_rejects_journal_entry_without_a_seal():
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.current_organization_id', '1', true)"))
            connection.execute(
                text(
                    "INSERT INTO journal_entries "
                    "(organization_id, reference_no, entry_date, accounting_period, description, source, posted_by, created_at) "
                    "VALUES (1, 'RAW-UNSEALED', '2026-11-21', '2026-11', 'missing seal', 'MANUAL', "
                    "'attacker@example.test', CURRENT_TIMESTAMP)"
                )
            )


def test_audit_high_water_mark_cannot_be_lowered_or_deleted(cfo, db):
    post_je(cfo, "1000", "3000", "10.00")
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE document_sequences SET last_value = 0 WHERE organization_id = 1 AND name = 'AUDIT'"))
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM document_sequences WHERE organization_id = 1 AND name = 'AUDIT'"))
    db.rollback()
