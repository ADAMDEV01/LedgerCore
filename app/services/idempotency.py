"""Durable idempotency for financial writes."""

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import ConflictError
from app.models import IdempotencyRecord


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin_request(
    db: Session,
    *,
    actor: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> tuple[IdempotencyRecord, bool]:
    """Reserve a key atomically; return (record, replayed).

    A uniqueness conflict waits for an in-flight transaction to finish on PostgreSQL. The
    winner stores the response in the same transaction as the financial operation, so a crash
    rolls back both and a retry can safely become the new winner.
    """
    key = key.strip()
    if not 16 <= len(key) <= 255:
        raise ConflictError("Idempotency-Key must contain between 16 and 255 characters")
    request_hash = _fingerprint(payload)
    values = {
        "actor": actor,
        "operation": operation,
        "key": key,
        "request_hash": request_hash,
        "response_payload": None,
        "created_at": clock.utcnow(),
        "completed_at": None,
    }
    dialect = db.get_bind().dialect.name
    statement: Any
    if dialect == "postgresql":
        statement = pg_insert(IdempotencyRecord).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(IdempotencyRecord).values(**values)
    else:
        raise RuntimeError(f"Idempotency is not implemented for database dialect {dialect}")

    inserted_id = db.execute(
        statement.on_conflict_do_nothing(
            index_elements=["actor", "operation", "key"]
        ).returning(IdempotencyRecord.id)
    ).scalar_one_or_none()
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor == actor,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )
    if record is None:
        raise RuntimeError("Idempotency reservation disappeared")
    if inserted_id is not None:
        return record, False
    if record.request_hash != request_hash:
        raise ConflictError("This Idempotency-Key was already used with a different request")
    if record.response_payload is None or record.completed_at is None:
        raise ConflictError("A request with this Idempotency-Key is still being processed; retry")
    return record, True


def complete_request(
    db: Session, record: IdempotencyRecord, response_payload: dict[str, Any]
) -> None:
    record.response_payload = response_payload
    record.completed_at = clock.utcnow()
    db.flush()
