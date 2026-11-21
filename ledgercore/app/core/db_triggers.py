"""Database-level append-only and double-entry enforcement.

PostgreSQL is the production authority: deferred constraint triggers validate a complete,
sealed journal entry at transaction commit. SQLite mirrors the seal/append protections for the
local test suite; its trigger engine does not support deferred constraint triggers, so full
balance-at-commit guarantees are exercised against PostgreSQL in CI.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

APPEND_ONLY_TABLES = ("audit_logs", "ledger_lines", "journal_entries")

_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only';
END;
$$ LANGUAGE plpgsql
"""

_PG_LINE_GUARD = """
CREATE OR REPLACE FUNCTION reject_lines_for_sealed_entry() RETURNS trigger AS $$
BEGIN
    -- Serialize line insertion with sealing on the parent row. A plain EXISTS check would
    -- allow a concurrent INSERT that passed just before another transaction sealed the entry.
    PERFORM id FROM journal_entries WHERE id = NEW.entry_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM journal_entry_seals WHERE entry_id = NEW.entry_id
    ) THEN
        RAISE EXCEPTION USING MESSAGE = 'journal entry is sealed';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PG_SEAL_GUARD = """
CREATE OR REPLACE FUNCTION lock_entry_before_sealing() RETURNS trigger AS $$
BEGIN
    -- Use the same parent-row lock as ledger-line INSERTs to close the check/insert race.
    PERFORM id FROM journal_entries WHERE id = NEW.entry_id FOR UPDATE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PG_SEQUENCE_GUARD = """
CREATE OR REPLACE FUNCTION protect_document_sequence() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING MESSAGE = 'document sequence high-water marks cannot be deleted';
    END IF;
    IF NEW.last_value < OLD.last_value THEN
        RAISE EXCEPTION USING MESSAGE = 'document sequence high-water marks cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PG_BALANCE_FUNCTION = """
CREATE OR REPLACE FUNCTION assert_journal_entry_is_sealed_and_balanced() RETURNS trigger AS $$
DECLARE
    target_entry_id INTEGER;
    line_count BIGINT;
    debit_total NUMERIC(14, 2);
    credit_total NUMERIC(14, 2);
BEGIN
    IF TG_TABLE_NAME = 'journal_entries' THEN
        target_entry_id := NEW.id;
    ELSE
        target_entry_id := NEW.entry_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM journal_entry_seals WHERE entry_id = target_entry_id
    ) THEN
        RAISE EXCEPTION USING MESSAGE = 'journal entry must be sealed before commit';
    END IF;

    SELECT COUNT(*), COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
      INTO line_count, debit_total, credit_total
      FROM ledger_lines
     WHERE entry_id = target_entry_id;

    IF line_count < 2 OR debit_total <> credit_total THEN
        RAISE EXCEPTION USING MESSAGE = 'journal entry must contain at least two balanced lines';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def install_append_only_triggers(connection: Connection) -> None:
    """Install UPDATE/DELETE guards for the original append-only tables."""
    if connection.dialect.name == "postgresql":
        connection.execute(text(_PG_FUNCTION))
        for table in APPEND_ONLY_TABLES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"))
            connection.execute(
                text(
                    f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
                )
            )
    elif connection.dialect.name == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for verb in ("UPDATE", "DELETE"):
                name = f"{table}_no_{verb.lower()}"
                connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {name} BEFORE {verb} ON {table} BEGIN "
                        f"SELECT RAISE(ABORT, '{table} is append-only'); END"
                    )
                )


def install_ledger_integrity_triggers(connection: Connection) -> None:
    """Require balanced, sealed entries and forbid lines being appended after sealing."""
    if connection.dialect.name == "postgresql":
        connection.execute(text(_PG_LINE_GUARD))
        connection.execute(text(_PG_SEAL_GUARD))
        connection.execute(text(_PG_SEQUENCE_GUARD))
        connection.execute(text(_PG_BALANCE_FUNCTION))
        connection.execute(text("DROP TRIGGER IF EXISTS ledger_lines_insert_guard ON ledger_lines"))
        connection.execute(
            text(
                "CREATE TRIGGER ledger_lines_insert_guard BEFORE INSERT ON ledger_lines "
                "FOR EACH ROW EXECUTE FUNCTION reject_lines_for_sealed_entry()"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS journal_entries_balance_check ON journal_entries"))
        connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER journal_entries_balance_check "
                "AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION assert_journal_entry_is_sealed_and_balanced()"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS journal_entry_seals_balance_check ON journal_entry_seals"))
        connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER journal_entry_seals_balance_check "
                "AFTER INSERT ON journal_entry_seals DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION assert_journal_entry_is_sealed_and_balanced()"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS journal_entry_seals_insert_guard ON journal_entry_seals"))
        connection.execute(
            text(
                "CREATE TRIGGER journal_entry_seals_insert_guard BEFORE INSERT ON journal_entry_seals "
                "FOR EACH ROW EXECUTE FUNCTION lock_entry_before_sealing()"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS document_sequences_monotonic ON document_sequences"))
        connection.execute(
            text(
                "CREATE TRIGGER document_sequences_monotonic BEFORE UPDATE OR DELETE ON document_sequences "
                "FOR EACH ROW EXECUTE FUNCTION protect_document_sequence()"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS journal_entry_seals_append_only ON journal_entry_seals"))
        connection.execute(
            text(
                "CREATE TRIGGER journal_entry_seals_append_only BEFORE UPDATE OR DELETE ON journal_entry_seals "
                "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
            )
        )
    elif connection.dialect.name == "sqlite":
        connection.execute(text("DROP TRIGGER IF EXISTS ledger_lines_insert_guard"))
        connection.execute(
            text(
                "CREATE TRIGGER ledger_lines_insert_guard BEFORE INSERT ON ledger_lines "
                "WHEN EXISTS (SELECT 1 FROM journal_entry_seals WHERE entry_id = NEW.entry_id) "
                "BEGIN SELECT RAISE(ABORT, 'journal entry is sealed'); END"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS journal_entry_seals_balance_check"))
        connection.execute(
            text(
                "CREATE TRIGGER journal_entry_seals_balance_check BEFORE INSERT ON journal_entry_seals "
                "WHEN (SELECT COUNT(*) FROM ledger_lines WHERE entry_id = NEW.entry_id) < 2 "
                "OR (SELECT COALESCE(SUM(debit), 0) FROM ledger_lines WHERE entry_id = NEW.entry_id) "
                "<> (SELECT COALESCE(SUM(credit), 0) FROM ledger_lines WHERE entry_id = NEW.entry_id) "
                "BEGIN SELECT RAISE(ABORT, 'journal entry must contain at least two balanced lines'); END"
            )
        )
        for verb in ("UPDATE", "DELETE"):
            name = f"journal_entry_seals_no_{verb.lower()}"
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
            connection.execute(
                text(
                    f"CREATE TRIGGER {name} BEFORE {verb} ON journal_entry_seals BEGIN "
                    "SELECT RAISE(ABORT, 'journal entry seals are append-only'); END"
                )
            )
        connection.execute(text("DROP TRIGGER IF EXISTS document_sequences_no_decrease"))
        connection.execute(
            text(
                "CREATE TRIGGER document_sequences_no_decrease BEFORE UPDATE ON document_sequences "
                "WHEN NEW.last_value < OLD.last_value BEGIN "
                "SELECT RAISE(ABORT, 'document sequence high-water marks cannot decrease'); END"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS document_sequences_no_delete"))
        connection.execute(
            text(
                "CREATE TRIGGER document_sequences_no_delete BEFORE DELETE ON document_sequences "
                "BEGIN SELECT RAISE(ABORT, 'document sequence high-water marks cannot be deleted'); END"
            )
        )


def install_database_triggers(connection: Connection) -> None:
    """Install all triggers after metadata-created schemas (used by tests)."""
    install_append_only_triggers(connection)
    install_ledger_integrity_triggers(connection)


def drop_ledger_integrity_triggers(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        for trigger, table in (
            ("ledger_lines_insert_guard", "ledger_lines"),
            ("journal_entries_balance_check", "journal_entries"),
            ("journal_entry_seals_balance_check", "journal_entry_seals"),
            ("journal_entry_seals_insert_guard", "journal_entry_seals"),
            ("journal_entry_seals_append_only", "journal_entry_seals"),
            ("document_sequences_monotonic", "document_sequences"),
        ):
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
        connection.execute(text("DROP FUNCTION IF EXISTS reject_lines_for_sealed_entry()"))
        connection.execute(text("DROP FUNCTION IF EXISTS lock_entry_before_sealing()"))
        connection.execute(text("DROP FUNCTION IF EXISTS protect_document_sequence()"))
        connection.execute(text("DROP FUNCTION IF EXISTS assert_journal_entry_is_sealed_and_balanced()"))
    elif connection.dialect.name == "sqlite":
        for trigger in (
            "ledger_lines_insert_guard",
            "journal_entry_seals_balance_check",
            "journal_entry_seals_no_update",
            "journal_entry_seals_no_delete",
            "document_sequences_no_decrease",
            "document_sequences_no_delete",
        ):
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))


def drop_append_only_triggers(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"))
        connection.execute(text("DROP FUNCTION IF EXISTS forbid_mutation()"))
    elif connection.dialect.name == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for verb in ("UPDATE", "DELETE"):
                connection.execute(text(f"DROP TRIGGER IF EXISTS {table}_no_{verb.lower()}"))
