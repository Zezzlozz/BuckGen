"""
Unit tests for the Budget Manager (app/utils/budget.py).

Tests cover:
  - Recording spend events
  - Daily and total spend aggregation
  - Guard checks (daily cap, stop-loss, combined can_spend)
"""

from datetime import UTC, datetime

from app.db.models import BudgetEntry
from app.utils.budget import (
    can_spend,
    daily_spend,
    record_spend,
    total_spend,
    within_daily_cap,
    within_stop_loss,
)


class TestRecordSpend:
    """record_spend — creates a BudgetEntry and persists it."""

    def test_creates_entry(self, db_session):
        entry = record_spend(db_session, 5.0, "gas", "test")
        assert isinstance(entry, BudgetEntry)
        assert entry.id is not None
        assert entry.amount_eur == 5.0
        assert entry.category == "gas"
        assert entry.memo == "test"
        # date should be today's YYYY-MM-DD
        assert entry.date == datetime.now(UTC).strftime("%Y-%m-%d")

    def test_rounds_amount(self, db_session):
        entry = record_spend(db_session, 3.14159265, "gas")
        assert entry.amount_eur == 3.141593

    def test_custom_category(self, db_session):
        entry = record_spend(db_session, 2.0, "defi", "swap fee")
        assert entry.category == "defi"

    def test_multiple_entries_independent(self, db_session):
        e1 = record_spend(db_session, 10.0, "gas")
        e2 = record_spend(db_session, 20.0, "llm")
        assert e1.id != e2.id


class TestDailySpend:
    """daily_spend — aggregates today's total."""

    def test_no_spend_returns_zero(self, db_session):
        assert daily_spend(db_session) == 0.0

    def test_single_spend(self, db_session):
        record_spend(db_session, 15.0, "gas")
        assert daily_spend(db_session) == 15.0

    def test_multiple_spends_summed(self, db_session):
        record_spend(db_session, 10.0, "gas")
        record_spend(db_session, 20.0, "llm")
        record_spend(db_session, 5.0, "defi")
        assert daily_spend(db_session) == 35.0

    def test_ignores_yesterday_spend(self, db_session):
        """Entry with yesterday's date should NOT be counted in today's total."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        entry = BudgetEntry(
            date=yesterday, category="gas", amount_eur=999.0, memo="old"
        )
        db_session.add(entry)
        db_session.commit()
        assert daily_spend(db_session) == 0.0


class TestTotalSpend:
    """total_spend — cumulative all-time sum regardless of date."""

    def test_zero_when_empty(self, db_session):
        assert total_spend(db_session) == 0.0

    def test_sum_all_entries(self, db_session):
        record_spend(db_session, 10.0, "gas")
        record_spend(db_session, 20.0, "gas")
        assert total_spend(db_session) == 30.0

    def test_includes_old_dates(self, db_session):
        from datetime import datetime, timedelta

        from app.db.models import BudgetEntry

        old_date = (datetime.now(UTC) - timedelta(days=30)).strftime(
            "%Y-%m-%d"
        )
        db_session.add(
            BudgetEntry(date=old_date, category="gas", amount_eur=100.0, memo="old")
        )
        db_session.commit()
        assert total_spend(db_session) == 100.0


class TestGuardChecks:
    """within_daily_cap, within_stop_loss, can_spend."""

    def test_within_daily_cap_true_when_below(self, db_session, mock_settings):
        record_spend(db_session, 10.0, "gas")
        assert within_daily_cap(db_session) is True

    def test_within_daily_cap_false_when_exceeded(self, db_session, mock_settings):
        record_spend(db_session, 60.0, "gas")  # cap is 50
        assert within_daily_cap(db_session) is False

    def test_within_stop_loss_true_when_below(self, db_session, mock_settings):
        record_spend(db_session, 100.0, "gas")
        assert within_stop_loss(db_session) is True

    def test_within_stop_loss_false_when_exceeded(self, db_session, mock_settings):
        record_spend(db_session, 600.0, "gas")  # stop-loss is 500
        assert within_stop_loss(db_session) is False

    def test_can_spend_approves_small_tx(self, db_session, mock_settings):
        assert can_spend(db_session, 5.0) is True

    def test_can_spend_denies_when_over_daily_cap(self, db_session, mock_settings):
        record_spend(db_session, 48.0, "gas")
        # 48 + 5 = 53 > 50
        assert can_spend(db_session, 5.0) is False

    def test_can_spend_denies_when_over_stop_loss(self, db_session, mock_settings):
        record_spend(db_session, 499.0, "gas")
        # within stop loss
        assert within_stop_loss(db_session) is True
        # this push it over
        assert can_spend(db_session, 2.0) is False

    def test_can_spend_zero_amount_always_ok(self, db_session, mock_settings):
        assert can_spend(db_session, 0.0) is True
