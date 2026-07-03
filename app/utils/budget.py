"""
Budget manager — enforces daily and cumulative spending limits.
"""

from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import BudgetEntry


# ---------------------------------------------------------------------------
# Spend tracking
# ---------------------------------------------------------------------------
def record_spend(
    db: Session,
    amount_eur: float,
    category: str = "gas",
    memo: str = "",
) -> BudgetEntry:
    """Record a spending event."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    entry = BudgetEntry(
        date=today,
        category=category,
        amount_eur=round(amount_eur, 6),
        memo=memo,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def daily_spend(db: Session) -> float:
    """Return total EUR spent today."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    result = (
        db.query(func.sum(BudgetEntry.amount_eur))
        .filter(BudgetEntry.date == today)
        .scalar()
    )
    return float(result or 0.0)


def total_spend(db: Session) -> float:
    """Return total EUR spent all time."""
    result = db.query(func.sum(BudgetEntry.amount_eur)).scalar()
    return float(result or 0.0)


# ---------------------------------------------------------------------------
# Guard checks
# ---------------------------------------------------------------------------
def within_daily_cap(db: Session) -> bool:
    """True if today's spend is under the daily cap."""
    return daily_spend(db) < settings.DAILY_GAS_CAP_EUR


def within_stop_loss(db: Session) -> bool:
    """True if cumulative spend is under the stop-loss threshold."""
    return total_spend(db) < settings.STOP_LOSS_EUR


def can_spend(db: Session, amount_eur: float = 0.0) -> bool:
    """Full guard: check daily cap + stop-loss + proposed amount."""
    if not within_stop_loss(db):
        return False
    if daily_spend(db) + amount_eur > settings.DAILY_GAS_CAP_EUR:
        return False
    return True
