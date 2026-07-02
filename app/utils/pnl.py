"""
Profit & Loss tracking — records revenue by module and provides
P&L summaries for data-driven optimization decisions.

Pairs with budget.py (spending tracking) for full ROI calculation.
"""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import RevenueEntry, BudgetEntry

logger = logging.getLogger("buckgen.pnl")


# ---------------------------------------------------------------------------
# Revenue recording
# ---------------------------------------------------------------------------
def record_revenue(
    db: Session,
    module: str,
    amount_eur: float,
    currency: str = "EUR",
    source: str = "",
    memo: str = "",
) -> RevenueEntry:
    """
    Record a revenue event from a module.

    Args:
        db: Database session
        module: Module name (bounties, arbitrage, airdrops, tasks)
        amount_eur: Revenue amount in EUR (positive value)
        currency: Currency code (default EUR)
        source: Source identifier (bounty URL, arb pair, airdrop name)
        memo: Optional description

    Returns:
        Created RevenueEntry.
    """
    entry = RevenueEntry(
        module=module,
        amount_eur=round(amount_eur, 6),
        currency=currency,
        source=source,
        memo=memo,
        earned_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    logger.info("[pnl] Revenue +EUR %.2f from %s (%s)", amount_eur, module, source[:40])
    return entry


# ---------------------------------------------------------------------------
# P&L queries
# ---------------------------------------------------------------------------
def module_revenue(
    db: Session,
    module: str | None = None,
    hours: int | None = None,
) -> float:
    """
    Total revenue for a module (or all modules if None).

    Args:
        db: Database session
        module: Module name or None for all modules
        hours: Optional lookback window

    Returns:
        Total EUR revenue.
    """
    query = db.query(func.sum(RevenueEntry.amount_eur))

    if module:
        query = query.filter(RevenueEntry.module == module)
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        query = query.filter(RevenueEntry.earned_at >= cutoff)

    result = query.scalar()
    return float(result or 0.0)


def module_spend(
    db: Session,
    module: str | None = None,
    hours: int | None = None,
) -> float:
    """
    Total spending for a module (filtered by category).

    Args:
        db: Database session
        module: Module name — maps to BudgetEntry category
        hours: Optional lookback window

    Returns:
        Total EUR spent.
    """
    query = db.query(func.sum(BudgetEntry.amount_eur))

    if module:
        # Map module names to budget categories
        module_to_category = {
            "bounties": "llm",
            "arbitrage": "gas",
            "airdrops": "gas",
            "tasks": "gas",
            "defi": "gas",
        }
        category = module_to_category.get(module, "gas")
        query = query.filter(BudgetEntry.category == category)

    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        # BudgetEntry.date is YYYY-MM-DD string, so time-based filtering is approximate
        date_cutoff = cutoff.strftime("%Y-%m-%d")
        query = query.filter(BudgetEntry.date >= date_cutoff)

    result = query.scalar()
    return float(result or 0.0)


def module_pnl(
    db: Session,
    module: str,
    hours: int | None = None,
) -> dict:
    """
    Calculate P&L for a specific module.

    Args:
        db: Database session
        module: Module name
        hours: Optional lookback window

    Returns:
        dict with revenue, spend, profit, and ROI.
    """
    rev = module_revenue(db, module, hours)
    spend = module_spend(db, module, hours)
    profit = rev - spend
    roi = (profit / spend * 100.0) if spend > 0 else 0.0

    return {
        "module": module,
        "revenue_eur": round(rev, 2),
        "spend_eur": round(spend, 2),
        "profit_eur": round(profit, 2),
        "roi_pct": round(roi, 1),
    }


def pnl_summary(
    db: Session,
    hours: int | None = None,
) -> dict:
    """
    Full P&L summary across all modules.

    Args:
        db: Database session
        hours: Optional lookback window

    Returns:
        dict with total P&L, per-module breakdown, and top sources.
    """
    modules = ["bounties", "arbitrage", "airdrops", "tasks", "defi"]
    per_module = [module_pnl(db, m, hours) for m in modules]

    total_rev = sum(m["revenue_eur"] for m in per_module)
    total_spend = sum(m["spend_eur"] for m in per_module)

    # Top revenue sources
    query = db.query(RevenueEntry).order_by(RevenueEntry.amount_eur.desc())
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        query = query.filter(RevenueEntry.earned_at >= cutoff)
    top_sources = query.limit(5).all()

    return {
        "total_revenue_eur": round(total_rev, 2),
        "total_spend_eur": round(total_spend, 2),
        "total_profit_eur": round(total_rev - total_spend, 2),
        "total_roi_pct": round(
            ((total_rev - total_spend) / total_spend * 100.0)
            if total_spend > 0
            else 0.0,
            1,
        ),
        "per_module": per_module,
        "top_sources": [
            {
                "module": s.module,
                "amount_eur": s.amount_eur,
                "source": s.source[:60],
                "earned_at": s.earned_at.isoformat(),
            }
            for s in top_sources
        ],
        "period_hours": hours or "all_time",
    }
