"""
Unit tests for Scheduled Job Definitions (app/scheduler/jobs.py).

Tests cover all 5 async APScheduler job functions:
  - scan_bounties: fetches, deduplicates, scores, inserts
  - check_gas_balances: wallet balance check, low-gas alert
  - check_prices: price+arbitrage scan, snapshot storage, high-conf alert
  - farm_airdrops: airdrop farming pipeline
  - self_heal: system health check + recovery

Each test mocks all external module imports (lazy or module-level).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.scheduler.jobs import (
    check_gas_balances,
    check_prices,
    farm_airdrops,
    scan_bounties,
    self_heal,
)


def _mock_db_session():
    """Return a tuple (mock_db, mock_session_gen) for ``next(get_session())``."""
    db = MagicMock()
    session_gen = iter([db])  # real iterator so next() works reliably
    return db, session_gen


def run_async(coro):
    """Run an async function synchronously for testing."""
    import asyncio

    return asyncio.run(coro)


# =============================================================================
# scan_bounties
# =============================================================================


class TestScanBounties:
    """scan_bounties — GitHub Issues bounty scanner."""

    def test_empty_list_returns_early(self):
        with (
            patch(
                "app.scheduler.jobs.gitcoin.fetch_open_bounties",
                AsyncMock(return_value=[]),
            ),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            result = run_async(scan_bounties())
        assert result is None

    def test_all_existing_skips_insert(self):
        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = [("existing_1",)]

        raw = MagicMock()
        norm = {
            "external_id": "existing_1",
            "title": "Test",
            "description": "desc",
            "reward_amount": 100,
            "reward_currency": "USD",
            "experience_level": "beginner",
            "url": "https://github.com/owner/repo/issues/1",
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.scheduler.jobs.gitcoin.fetch_open_bounties",
                AsyncMock(return_value=[raw]),
            ),
            patch("app.scheduler.jobs.gitcoin.normalize_bounty", return_value=norm),
            patch("app.scheduler.jobs.monitor.record_success"),
        ):
            run_async(scan_bounties())
        db.add.assert_not_called()

    def test_inserts_new_bounties(self):
        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = []

        raw = MagicMock()
        norm = {
            "external_id": "new_1",
            "title": "New Bounty",
            "description": "A test bounty",
            "reward_amount": 500,
            "reward_currency": "USD",
            "experience_level": "intermediate",
            "url": "https://github.com/owner/repo/issues/2",
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.scheduler.jobs.gitcoin.fetch_open_bounties",
                AsyncMock(return_value=[raw]),
            ),
            patch("app.scheduler.jobs.gitcoin.normalize_bounty", return_value=norm),
            patch("app.scheduler.jobs.score_bounty", AsyncMock(return_value=0.85)),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_bounty_found", AsyncMock()),
            patch("app.scheduler.jobs.is_blacklisted", return_value=False),
        ):
            run_async(scan_bounties())
        db.add.assert_called_once()
        bounty = db.add.call_args[0][0]
        assert bounty.external_id == "new_1"
        assert bounty.score == 0.85
        db.commit.assert_called_once()

    def test_low_score_no_notification(self):
        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = []

        raw = MagicMock()
        norm = {
            "external_id": "low_1",
            "title": "Low value",
            "description": "desc",
            "reward_amount": 10,
            "reward_currency": "USD",
            "experience_level": "beginner",
            "url": "https://github.com/owner/repo/issues/3",
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.scheduler.jobs.gitcoin.fetch_open_bounties",
                AsyncMock(return_value=[raw]),
            ),
            patch("app.scheduler.jobs.gitcoin.normalize_bounty", return_value=norm),
            patch("app.scheduler.jobs.score_bounty", AsyncMock(return_value=0.3)),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_bounty_found", AsyncMock()) as mock_notify,
            patch("app.scheduler.jobs.is_blacklisted", return_value=False),
        ):
            run_async(scan_bounties())
        mock_notify.assert_not_called()

    def test_exception_handled(self):
        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.side_effect = ValueError(
            "DB error"
        )

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.scheduler.jobs.gitcoin.fetch_open_bounties",
                AsyncMock(return_value=[MagicMock()]),
            ),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            run_async(scan_bounties())
        db.rollback.assert_called_once()
        db.close.assert_called_once()


# =============================================================================
# check_gas_balances
# =============================================================================


class TestCheckGasBalances:
    """check_gas_balances — wallet balance monitor."""

    def test_no_active_wallets(self):
        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = []

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            run_async(check_gas_balances())

    def test_all_wallets_have_gas(self):
        wallet = MagicMock()
        wallet.id = 1
        wallet.address = "0xabc"
        wallet.chain = "ethereum"
        wallet.is_active = True

        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = [wallet]

        bal = MagicMock()
        bal.has_gas = True
        bal.balance_eth = 1.5
        bal.balance_wei = 1500000000000000000
        bal.symbol = "ETH"
        bal.error = ""

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch("app.modules.rpc.get_balance", return_value=bal),
            patch("app.scheduler.jobs.monitor.record_success"),
        ):
            run_async(check_gas_balances())
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_low_gas_triggers_alert(self):
        wallet = MagicMock()
        wallet.id = 1
        wallet.address = "0xabc"
        wallet.chain = "ethereum"
        wallet.is_active = True

        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = [wallet]

        bal = MagicMock()
        bal.has_gas = False
        bal.balance_eth = 0.0001
        bal.balance_wei = 100000000000000
        bal.symbol = "ETH"
        bal.error = ""

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch("app.modules.rpc.get_balance", return_value=bal),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(check_gas_balances())
        mock_alert.assert_called_once()

    def test_exception_handled(self):
        wallet = MagicMock()
        wallet.id = 1
        wallet.address = "0xabc"
        wallet.chain = "ethereum"
        wallet.is_active = True

        db, session_gen = _mock_db_session()
        db.query.return_value.filter.return_value.all.return_value = [wallet]

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch("app.modules.rpc.get_balance", side_effect=ValueError("RPC fail")),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            run_async(check_gas_balances())
        db.close.assert_called_once()


# =============================================================================
# check_prices
# =============================================================================


class TestCheckPrices:
    """check_prices — price + arbitrage monitor."""

    def test_successful_check_with_opportunities(self):
        db, session_gen = _mock_db_session()

        result = {
            "pairs_checked": 10,
            "tickers_fetched": 80,
            "arbitrage_opportunities": 2,
            "errors": [],
            "top_opportunities": [
                {
                    "pair": "BTC/USDT",
                    "buy_at": "binance",
                    "buy_price": 100.0,
                    "sell_at": "kraken",
                    "sell_price": 101.5,
                    "net_profit_pct": 1.2,
                    "estimated_profit_eur": 5.0,
                    "confidence": 0.85,
                },
            ],
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.prices.check_all_prices", AsyncMock(return_value=result)
            ),
            patch("app.modules.prices.fetch_all_tickers", return_value={}),
            patch("app.modules.prices.store_ticker_snapshots", return_value=10),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(check_prices())
        mock_alert.assert_called_once()
        db.close.assert_called_once()

    def test_no_high_confidence_no_alert(self):
        db, session_gen = _mock_db_session()

        result = {
            "pairs_checked": 10,
            "tickers_fetched": 80,
            "arbitrage_opportunities": 1,
            "errors": [],
            "top_opportunities": [
                {"pair": "ETH/USDT", "confidence": 0.3},
            ],
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.prices.check_all_prices", AsyncMock(return_value=result)
            ),
            patch("app.modules.prices.fetch_all_tickers", return_value={}),
            patch("app.modules.prices.store_ticker_snapshots", return_value=0),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(check_prices())
        mock_alert.assert_not_called()

    def test_exception_handled(self):
        db, session_gen = _mock_db_session()

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.prices.check_all_prices",
                AsyncMock(side_effect=ValueError("price fail")),
            ),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            run_async(check_prices())
        db.rollback.assert_called_once()
        db.close.assert_called_once()


# =============================================================================
# farm_airdrops
# =============================================================================


class TestFarmAirdrops:
    """farm_airdrops — airdrop farming pipeline."""

    def test_successful_farm(self):
        db, session_gen = _mock_db_session()

        result = {
            "airdrops_discovered": 3,
            "wallets_created": 5,
            "faucet_claims_succeeded": 10,
            "faucet_claims_attempted": 12,
            "registrations": 2,
            "errors": [],
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.airdrop.farm_opportunities", AsyncMock(return_value=result)
            ),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(farm_airdrops())
        mock_alert.assert_called_once()
        db.close.assert_called_once()

    def test_no_faucet_claims_no_alert(self):
        db, session_gen = _mock_db_session()

        result = {
            "airdrops_discovered": 1,
            "wallets_created": 0,
            "faucet_claims_succeeded": 0,
            "faucet_claims_attempted": 0,
            "registrations": 0,
            "errors": [],
        }

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.airdrop.farm_opportunities", AsyncMock(return_value=result)
            ),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(farm_airdrops())
        mock_alert.assert_not_called()

    def test_exception_handled(self):
        db, session_gen = _mock_db_session()

        with (
            patch("app.scheduler.jobs.get_session", return_value=session_gen),
            patch(
                "app.modules.airdrop.farm_opportunities",
                AsyncMock(side_effect=RuntimeError("farm oops")),
            ),
            patch("app.scheduler.jobs.monitor.record_error"),
            patch("app.scheduler.jobs.notify_error", AsyncMock()),
        ):
            run_async(farm_airdrops())
        db.rollback.assert_called_once()
        db.close.assert_called_once()


# =============================================================================
# self_heal
# =============================================================================


class TestSelfHeal:
    """self_heal — periodic system health check and recovery."""

    def test_all_modules_healthy(self):
        summary = {
            "modules_ok": 5,
            "modules_total": 5,
            "modules_degraded": 0,
            "modules_down": 0,
            "errors_last_hour": 0,
        }
        with (
            patch("app.scheduler.jobs.monitor.get_summary", return_value=summary),
            patch("app.scheduler.jobs.monitor.record_success"),
        ):
            run_async(self_heal())

    def test_recovers_degraded_modules(self):
        summary = {
            "modules_ok": 3,
            "modules_total": 5,
            "modules_degraded": 2,
            "modules_down": 0,
            "errors_last_hour": 3,
        }
        with (
            patch("app.scheduler.jobs.monitor.get_summary", return_value=summary),
            patch(
                "app.scheduler.jobs.monitor.recover_all",
                AsyncMock(return_value={"mod1": "ok"}),
            ),
            patch("app.scheduler.jobs.monitor.record_success"),
        ):
            run_async(self_heal())

    def test_many_down_triggers_alert(self):
        summary = {
            "modules_ok": 1,
            "modules_total": 5,
            "modules_degraded": 1,
            "modules_down": 3,
            "errors_last_hour": 10,
        }
        with (
            patch("app.scheduler.jobs.monitor.get_summary", return_value=summary),
            patch("app.scheduler.jobs.monitor.recover_all", AsyncMock(return_value={})),
            patch("app.scheduler.jobs.monitor.record_success"),
            patch("app.scheduler.jobs.notify_alert", AsyncMock()) as mock_alert,
        ):
            run_async(self_heal())
        mock_alert.assert_called_once()

    def test_exception_handled(self):
        with (
            patch(
                "app.scheduler.jobs.monitor.get_summary",
                side_effect=ValueError("monitor fail"),
            ),
            patch("app.scheduler.jobs.monitor.record_error"),
        ):
            run_async(self_heal())
