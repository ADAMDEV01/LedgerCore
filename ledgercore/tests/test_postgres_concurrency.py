"""Production database race tests; CI runs these against PostgreSQL 16."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import SessionLocal, engine
from app.core.errors import AuthenticationError, ConflictError
from app.models import Account, RefreshToken, User
from app.services import auth as auth_service
from app.services.ledger import LineInput, post_entry
from app.services.periods import close_period

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks")


def test_only_one_concurrent_refresh_request_consumes_the_old_token():
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "cfo@ledgertest.io"))
        raw = auth_service.create_refresh_token(db, user.id)
        db.commit()

    barrier = Barrier(2)

    def rotate():
        with SessionLocal() as db:
            barrier.wait(timeout=5)
            try:
                _, successor = auth_service.rotate_refresh_token(db, raw)
                db.commit()
                return ("success", successor)
            except AuthenticationError:
                db.rollback()
                return ("rejected", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rotate(), range(2)))

    successes = [token for status, token in results if status == "success"]
    assert len(successes) == 1
    with SessionLocal() as db:
        old = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == auth_service.hash_token(raw)))
        successor = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == auth_service.hash_token(successes[0]))
        )
        assert old.revoked_at is not None
        # The concurrent replay revokes the entire family, including the winning successor.
        assert successor.revoked_at is not None


def test_period_closure_serializes_with_a_concurrent_posting_check():
    period = "2098-07"
    closer = SessionLocal()
    try:
        close_period(closer, period, "cfo@ledgertest.io")
        started = Event()

        def posting_check():
            with SessionLocal() as db:
                started.set()
                try:
                    cash_id = db.scalars(select(Account.id).where(Account.code == "1000")).one()
                    capital_id = db.scalars(select(Account.id).where(Account.code == "3000")).one()
                    post_entry(
                        db,
                        entry_date=date(2098, 7, 15),
                        description="Must not post after close",
                        lines=[
                            LineInput(cash_id, debit=Decimal("10.00")),
                            LineInput(capital_id, credit=Decimal("10.00")),
                        ],
                        actor="race-test@example.test",
                    )
                    db.commit()
                    return "posted"
                except ConflictError:
                    db.rollback()
                    return "closed"

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(posting_check)
            assert started.wait(timeout=5)
            time.sleep(0.15)  # let the competing transaction reach the locked period row
            closer.commit()
            assert future.result(timeout=5) == "closed"
    finally:
        closer.close()


def test_concurrent_line_insert_cannot_race_with_entry_sealing():
    """A line insert that starts while sealing is uncommitted must wait, then fail."""
    seal_ready = Event()
    insert_started = Event()
    reference = f"R{uuid4().hex[:12]}"

    def insert_after_seal_begins():
        with SessionLocal() as db:
            insert_started.set()
            try:
                account_id = db.scalar(select(Account.id).where(Account.code == "5000"))
                db.execute(
                    text(
                        "INSERT INTO ledger_lines (entry_id, account_id, debit, credit) "
                        "VALUES (:entry_id, :account_id, 1.00, 0.00)"
                    ),
                    {"entry_id": entry_id, "account_id": account_id},
                )
                db.commit()
                return "inserted"
            except DBAPIError:
                db.rollback()
                return "rejected"

    with SessionLocal() as sealing:
        debit_account = sealing.scalar(select(Account.id).where(Account.code == "1000"))
        credit_account = sealing.scalar(select(Account.id).where(Account.code == "3000"))
        entry_id = sealing.execute(
            text(
                "INSERT INTO journal_entries "
                "(reference_no, entry_date, accounting_period, description, source, posted_by, created_at) "
                "VALUES (:reference, '2098-08-15', '2098-08', 'seal race test', 'MANUAL', "
                "'race@example.test', CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"reference": reference},
        ).scalar_one()
        sealing.execute(
            text(
                "INSERT INTO ledger_lines (entry_id, account_id, debit, credit) "
                "VALUES (:entry_id, :account_id, 20.00, 0.00)"
            ),
            {"entry_id": entry_id, "account_id": debit_account},
        )
        sealing.execute(
            text(
                "INSERT INTO ledger_lines (entry_id, account_id, debit, credit) "
                "VALUES (:entry_id, :account_id, 0.00, 20.00)"
            ),
            {"entry_id": entry_id, "account_id": credit_account},
        )
        sealing.execute(
            text(
                "INSERT INTO journal_entry_seals (entry_id, sealed_at) "
                "VALUES (:entry_id, CURRENT_TIMESTAMP)"
            ),
            {"entry_id": entry_id},
        )
        seal_ready.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(insert_after_seal_begins)
            assert seal_ready.wait(timeout=5)
            assert insert_started.wait(timeout=5)
            time.sleep(0.15)  # the line trigger should be waiting on the parent row lock
            sealing.commit()
            assert future.result(timeout=5) == "rejected"
