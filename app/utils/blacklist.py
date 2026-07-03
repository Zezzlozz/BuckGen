"""
Blacklist tracker — prevents the agent from wasting resources
on sources that don't pay reliably.
"""

from sqlalchemy.orm import Session

from app.db.models import BlacklistEntry


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
def is_blacklisted(db: Session, source_type: str, source_id: str) -> bool:
    """Check whether a source is currently blacklisted."""
    return (
        db.query(BlacklistEntry)
        .filter(
            BlacklistEntry.source_type == source_type,
            BlacklistEntry.source_id == source_id,
        )
        .first()
        is not None
    )


def get_blacklisted_sources(
    db: Session, source_type: str | None = None
) -> list[BlacklistEntry]:
    """Return all blacklisted entries, optionally filtered by type."""
    q = db.query(BlacklistEntry)
    if source_type:
        q = q.filter(BlacklistEntry.source_type == source_type)
    return q.all()


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------
def blacklist_source(
    db: Session,
    source_type: str,
    source_id: str,
    reason: str = "",
) -> BlacklistEntry:
    """Add a source to the blacklist (or increment its failure count)."""
    existing = (
        db.query(BlacklistEntry)
        .filter(
            BlacklistEntry.source_type == source_type,
            BlacklistEntry.source_id == source_id,
        )
        .first()
    )
    if existing:
        existing.failed_attempts = (existing.failed_attempts or 0) + 1
        if reason:
            existing.reason = reason
        db.commit()
        return existing

    entry = BlacklistEntry(
        source_type=source_type,
        source_id=source_id,
        reason=reason,
        failed_attempts=1,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def remove_blacklist(db: Session, source_type: str, source_id: str) -> bool:
    """Remove a source from the blacklist.  Returns True if removed."""
    entry = (
        db.query(BlacklistEntry)
        .filter(
            BlacklistEntry.source_type == source_type,
            BlacklistEntry.source_id == source_id,
        )
        .first()
    )
    if entry:
        db.delete(entry)
        db.commit()
        return True
    return False
