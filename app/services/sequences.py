from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import DocumentSequence


def next_value(db: Session, name: str) -> int:
    """Atomically take the next number of a gapless sequence.

    A single UPDATE ... RETURNING takes the row lock in PostgreSQL until the surrounding
    transaction ends, so concurrent writers queue up and a rollback also rolls the number back
    (no gaps, no duplicates).
    """
    organization_id = int(db.info.get("organization_id", 1))
    stmt = (
        update(DocumentSequence)
        .where(
            DocumentSequence.organization_id == organization_id,
            DocumentSequence.name == name,
        )
        .values(last_value=DocumentSequence.last_value + 1)
        .returning(DocumentSequence.last_value)
        .execution_options(synchronize_session=False)
    )
    value = db.execute(stmt).scalar_one_or_none()
    if value is None:  # first use of this sequence
        db.add(DocumentSequence(organization_id=organization_id, name=name, last_value=1))
        db.flush()
        return 1
    return int(value)
