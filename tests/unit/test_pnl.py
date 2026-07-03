"""
Unit tests for P&L Tracking (app/utils/pnl.py).

Tests cover:
  - Recording revenue events
  - Querying revenue aggregated by module / time window
  - Querying spend aggregated by module category mapping
  - Module P&L calculation (revenue - spend, ROI)
  - Full P&L summary across all modules
"""

import pytest
from datetime import datetime, timezone, timedelta

from app.utils.pnl import (
    record_revenue,
    module_revenue,
    module_spend,
    module_pnl,
    pnl_summary,
)
from app.db.models import RevenueEntry, BudgetEntry


class TestRecordRevenue:
    """record_revenue — creates RevenueEntry and persists it."""

    def test_creates_entry(self, db_session):
        entry = record_revenue(db_session, "bounties", 100.0, source="github.com/test")
        assert isinstance(entry, RevenueEntry)
        assert entry.id is not None
        assert entry.amount_eur == 100.0
        assert entry.module == "bounties"
        assert entry.source == "github.com/test"
        assert entry.earned_at is not None

    def test_rounds_amount(self, db_session):
        entry = record_revenue(db_session, "arbitrage", 3.14159265)
        assert entry.amount_eur == 3.141593

    def test_custom_currency(self, db_session):
        entry = record_revenue(db_session, "airdrops", 0.5, currency="ETH")
        assert entry.currency == "ETH"

    def test_multiple_entries(self, db_session):
        e1 = record_revenue(db_session, "bounties", 100.0)
        e2 = record_revenue(db_session, "arbitrage", 50.0)
        assert e1.id != e2.id


class TestModuleRevenue:
    """module_revenue — aggregates revenue, optionally filtered."""

    def test_no_revenue_returns_zero(self, db_session):
        assert module_revenue(db_session) == 0.0

    def test_total_across_all_modules(self, db_session):
        record_revenue(db_session, "bounties", 100.0)
        record_revenue(db_session, "arbitrage", 50.0)
        assert module_revenue(db_session) == 150.0

    def test_filter_by_module(self, db_session):
        record_revenue(db_session, "bounties", 100.0)
        record_revenue(db_session, "arbitrage", 50.0)
        assert module_revenue(db_session, module="bounties") == 100.0
        assert module_revenue(db_session, module="arbitrage") == 50.0
        assert module_revenue(db_session, module="airdrops") == 0.0

    def test_filter_by_hours_window(self, db_session):
        record_revenue(db_session, "bounties", 100.0)
        # Manually insert an old entry
        old_entry = RevenueEntry(
            module="bounties",
            amount_eur=500.0,
            earned_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        db_session.add(old_entry)
        db_session.commit()

        # Only count revenue in last 24 hours
        assert module_revenue(db_session, hours=24) == 100.0

    def test_filter_by_module_and_hours(self, db_session):
        record_revenue(db_session, "arbitrage", 30.0)
        old = RevenueEntry(
            module="arbitrage",
            amount_eur=200.0,
            earned_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        db_session.add(old)
        db_session.commit()
        assert module_revenue(db_session, module="arbitrage", hours=24) == 30.0


class TestModuleSpend:
    """module_spend — aggregates spend by module category mapping."""

    def test_no_spend_returns_zero(self, db_session):
        assert module_spend(db_session) == 0.0

    def test_total_across_categories(self, db_session):
        db_session.add(
            BudgetEntry(date="2025-01-01", category="gas", amount_eur=10.0, memo="")
        )
        db_session.add(
            BudgetEntry(date="2025-01-01", category="llm", amount_eur=5.0, memo="")
        )
        db_session.commit()
        assert module_spend(db_session) == 15.0

    def test_module_category_mapping(self, db_session):
        # 'bounties' module maps to 'llm' category
        db_session.add(
            BudgetEntry(date="2025-01-01", category="llm", amount_eur=8.0, memo="")
        )
        db_session.commit()
        assert module_spend(db_session, module="bounties") == 8.0
        # 'arbitrage' maps to 'gas'
        assert module_spend(db_session, module="arbitrage") == 0.0

    def test_module_spend_hours_filter(self, db_session):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%d"
        )
        db_session.add(BudgetEntry(date=today, category="gas", amount_eur=3.0, memo=""))
        db_session.add(
            BudgetEntry(date=yesterday, category="gas", amount_eur=20.0, memo="")
        )
        db_session.commit()
        # Only the entry within last 24h
        assert module_spend(db_session, hours=24) == 3.0


class TestModulePnl:
    """module_pnl — calculates profit and ROI for a specific module."""

    def test_no_activity(self, db_session):
        result = module_pnl(db_session, "arbitrage")
        assert result["module"] == "arbitrage"
        assert result["revenue_eur"] == 0.0
        assert result["spend_eur"] == 0.0
        assert result["profit_eur"] == 0.0
        assert result["roi_pct"] == 0.0

    def test_profitable_module(self, db_session):
        # Revenue: 100 EUR, Spend: 10 EUR (via 'gas' category for 'arbitrage')
        record_revenue(db_session, "arbitrage", 100.0)
        db_session.add(
            BudgetEntry(date="2025-01-01", category="gas", amount_eur=10.0, memo="")
        )
        db_session.commit()

        result = module_pnl(db_session, "arbitrage")
        assert result["revenue_eur"] == 100.0
        assert result["spend_eur"] == 10.0
        assert result["profit_eur"] == 90.0
        assert result["roi_pct"] == 900.0  # (90/10)*100

    def test_loss_making_module(self, db_session):
        record_revenue(db_session, "bounties", 10.0)  # maps to 'llm'
        db_session.add(
            BudgetEntry(date="2025-01-01", category="llm", amount_eur=50.0, memo="")
        )
        db_session.commit()

        result = module_pnl(db_session, "bounties")
        assert result["profit_eur"] == -40.0
        assert result["roi_pct"] == -80.0  # (-40/50)*100

    def test_zero_spend_roi(self, db_session):
        """ROI should be 0 (not divide-by-zero) when spend is 0."""
        record_revenue(db_session, "bounties", 10.0)
        result = module_pnl(db_session, "bounties")
        assert result["profit_eur"] == 10.0
        assert result["roi_pct"] == 0.0


class TestPnlSummary:
    """pnl_summary — full P&L across all modules."""

    def test_empty_summary(self, db_session):
        result = pnl_summary(db_session)
        assert result["total_revenue_eur"] == 0.0
        assert result["total_spend_eur"] == 0.0
        assert result["total_profit_eur"] == 0.0
        assert len(result["per_module"]) == 5
        assert result["per_module"][0]["module"] == "bounties"
        assert result["top_sources"] == []

    def test_summary_with_data(self, db_session):
        record_revenue(db_session, "bounties", 100.0, source="issue/1")
        record_revenue(db_session, "arbitrage", 50.0, source="BTC/USDT")
        db_session.add(
            BudgetEntry(date="2025-01-01", category="llm", amount_eur=20.0, memo="")
        )
        db_session.add(
            BudgetEntry(date="2025-01-01", category="gas", amount_eur=5.0, memo="")
        )
        db_session.commit()

        result = pnl_summary(db_session)
        assert result["total_revenue_eur"] == 150.0
        assert result["total_spend_eur"] == 25.0
        assert result["total_profit_eur"] == 125.0
        # ROI: (150-25)/25*100 = 500.0
        assert result["total_roi_pct"] == 500.0

        assert len(result["top_sources"]) == 2
        assert result["top_sources"][0]["module"] == "bounties"

    def test_summary_hours_filter(self, db_session):
        record_revenue(db_session, "bounties", 100.0)
        old = RevenueEntry(
            module="bounties",
            amount_eur=999.0,
            earned_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        db_session.add(old)
        db_session.commit()

        result_24h = pnl_summary(db_session, hours=24)
        assert result_24h["total_revenue_eur"] == 100.0

        result_all = pnl_summary(db_session)
        assert result_all["total_revenue_eur"] == 1099.0

    def test_top_sources_limited_to_five(self, db_session):
        for i in range(10):
            record_revenue(db_session, "bounties", float(i + 1), source=f"src/{i}")
        result = pnl_summary(db_session)
        assert len(result["top_sources"]) == 5
