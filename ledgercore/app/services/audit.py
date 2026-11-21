"""Tamper-evident audit trail.

Every row stores the hash of the previous row, so editing or deleting history anywhere breaks
every later hash. verify_chain() recomputes the whole chain. The database triggers installed by
the migration additionally refuse UPDATE/DELETE outright.
"""

import hashlib
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.constants import SEQ_AUDIT
from app.models import AuditLog, DocumentSequence
from app.services.sequences import next_value

GENESIS_HASH = "0" * 64


def _ts(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def compute_hash(
    prev_hash: str,
    seq: int,
    actor: str,
    action: str,
    entity: str,
    details: str,
    created_at: datetime,
) -> str:
    material = "\x1f".join([prev_hash, str(seq), actor, action, entity, details, _ts(created_at)])
    return hashlib.sha256(material.encode()).hexdigest()


def log_audit(db: Session, actor: str, action: str, entity: str, details: str) -> AuditLog:
    seq = next_value(db, SEQ_AUDIT)
    prev: Optional[str] = GENESIS_HASH
    if seq > 1:
        prev = db.scalar(select(AuditLog.row_hash).where(AuditLog.seq == seq - 1))
        if prev is None:
            raise RuntimeError(f"audit chain is broken: missing row {seq - 1}")
    now = clock.utcnow()
    row = AuditLog(
        seq=seq,
        actor=actor,
        action=action,
        entity=entity,
        details=details,
        created_at=now,
        prev_hash=prev,
        row_hash=compute_hash(prev, seq, actor, action, entity, details, now),
    )
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session) -> dict[str, object]:
    prev_hash = GENESIS_HASH
    expected_seq = 1
    checked = 0
    rows = db.scalars(select(AuditLog).order_by(AuditLog.seq)).yield_per(500)
    for row in rows:
        expected = compute_hash(
            prev_hash, row.seq, row.actor, row.action, row.entity, row.details, row.created_at
        )
        if row.seq != expected_seq or row.prev_hash != prev_hash or row.row_hash != expected:
            return {"valid": False, "checked": checked, "first_invalid_seq": row.seq}
        prev_hash = row.row_hash
        expected_seq += 1
        checked += 1
    # The transactional sequence is an independent high-water mark. It makes deletion of
    # the newest audit row detectable; a hash chain alone cannot see a missing tail row.
    sequence = db.get(DocumentSequence, SEQ_AUDIT)
    recorded_tail = sequence.last_value if sequence is not None else 0
    if checked != recorded_tail:
        return {"valid": False, "checked": checked, "first_invalid_seq": expected_seq}
    return {"valid": True, "checked": checked, "first_invalid_seq": None}
