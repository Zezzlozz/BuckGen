"""
Unit tests for the DeFi Execution Module (app/modules/defi.py).

Tests cover:
  - Trade exchange initialization with API keys
  - CEX-CEX arbitrage execution (mocked ccxt)
  - Budget guard enforcement before trades
  - Insufficient balance handling
  - CoinGecko reference skip
  - Unprofitable trade detection
  - Revenue recording on success
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from app.modules.defi import (
    _get_trade_exchange,
    execute_cex_arbitrage,
    execute_arbitrage,
    _trade_exchanges,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_trade_exchanges():
    """Clear cached trade exchange instances before each test."""
    _trade_exchanges.clear()
    yield
    _trade_exchanges.clear()


# =============================================================================
# Trade exchange initialization
# =============================================================================


class TestGetTradeExchange:
    """_get_trade_exchange creates ccxt instances with trade keys."""

    @patch("ccxt.binance")
    def test_creates_exchange_with_keys(self, mock_binance_class, mock_settings):
        mock_instance = MagicMock()
        mock_instance.markets = {"BTC/USDT": {}, "ETH/USDT": {}}
        mock_binance_class.return_value = mock_instance

        ex = _get_trade_exchange("binance")
        assert ex is mock_instance
        call_kwargs = mock_binance_class.call_args[0][0]
        assert call_kwargs["apiKey"] == "test_key_binance"
        assert call_kwargs["secret"] == "test_secret_binance"

    def test_returns_none_for_unsupported_exchange(self, mock_settings):
        ex = _get_trade_exchange("coinbase")
        assert ex is None

    def test_caches_instance(self, mock_settings):
        with patch("ccxt.binance") as mock_class:
            mock_instance = MagicMock()
            mock_instance.markets = {"BTC/USDT": {}}
            mock_class.return_value = mock_instance
            ex1 = _get_trade_exchange("binance")
            ex2 = _get_trade_exchange("binance")
            assert ex1 is ex2
            mock_class.assert_called_once()

    def test_returns_none_without_keys(self):
        with patch("ccxt.binance") as mock_class:
            ex = _get_trade_exchange("binance")
            assert ex is None
            mock_class.assert_not_called()


# =============================================================================
# CEX-CEX arbitrage execution
# =============================================================================


class TestExecuteCexArbitrage:
    """execute_cex_arbitrage full execution flow."""

    def _make_opportunity(self, **overrides):
        return {
            "buy_at": "binance",
            "sell_at": "kraken",
            "pair": "BTC/USDT",
            "buy_price": 60000.0,
            "sell_price": 60200.0,
            "gap_pct": 0.33,
            "net_profit_pct": 0.15,
            "estimated_profit_eur": 0.75,
            "confidence": 0.8,
            **overrides,
        }

    @patch("app.modules.defi._get_trade_exchange")
    def test_skips_coingecko_reference(self, mock_get_ex, db_session):
        opp = self._make_opportunity(buy_at="CoinGecko (ref)")
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "CoinGecko" in result["error"]
        mock_get_ex.assert_not_called()

    @patch("app.modules.defi._get_trade_exchange")
    def test_skips_when_no_trade_keys(self, mock_get_ex, db_session):
        mock_get_ex.return_value = None
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "trade keys" in result["error"]

    @patch("app.modules.defi._get_trade_exchange")
    def test_budget_cap_prevents_execution(
        self, mock_get_ex, db_session, mock_settings
    ):
        from app.utils.budget import record_spend

        record_spend(db_session, 600.0, "defi", "exhaust")
        mock_buy = MagicMock()
        mock_sell = MagicMock()
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "Budget" in result["error"]

    @patch("app.modules.defi._get_trade_exchange")
    def test_insufficient_balance(self, mock_get_ex, db_session, mock_settings):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 5.0}}
        mock_sell = MagicMock()
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "Insufficient" in result["error"]

    @patch("app.modules.defi._get_trade_exchange")
    def test_buy_order_not_filled(self, mock_get_ex, db_session, mock_settings):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
        mock_buy.create_market_buy_order.return_value = {"filled": 0, "cost": 0}
        mock_sell = MagicMock()
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "not filled" in result["error"]

    @patch("app.modules.defi._get_trade_exchange")
    def test_sell_order_not_filled(self, mock_get_ex, db_session, mock_settings):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
        mock_buy.create_market_buy_order.return_value = {"filled": 0.1, "cost": 6000.0}
        mock_sell = MagicMock()
        mock_sell.create_market_sell_order.return_value = {"filled": 0, "cost": 0}
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is False
        assert "not filled" in result["error"]

    @patch("app.modules.defi._get_trade_exchange")
    def test_unprofitable_trade_returns_warning(
        self, mock_get_ex, db_session, mock_settings
    ):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
        mock_buy.create_market_buy_order.return_value = {
            "filled": 0.01666,
            "cost": 1000.0,
        }
        opp_price = 60000.0
        mock_sell = MagicMock()
        mock_sell.create_market_sell_order.return_value = {
            "filled": 0.01665,
            "cost": 999.0,
        }
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity(buy_price=opp_price, sell_price=opp_price)
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result.get("success") is True
        assert "not profitable" in result.get("warning", "")

    @patch("app.modules.defi._get_trade_exchange")
    def test_profitable_trade_records_revenue(
        self, mock_get_ex, db_session, mock_settings
    ):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
        mock_buy.create_market_buy_order.return_value = {
            "filled": 0.01666,
            "cost": 1000.0,
        }
        mock_sell = MagicMock()
        mock_sell.create_market_sell_order.return_value = {
            "filled": 0.01665,
            "cost": 1005.0,
        }
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["success"] is True
        assert result["net_profit_eur"] > 0
        from app.utils.pnl import module_revenue

        rev = module_revenue(db_session, module="arbitrage")
        assert rev > 0

    @patch("app.modules.defi._get_trade_exchange")
    def test_profitable_trade_returns_metadata(
        self, mock_get_ex, db_session, mock_settings
    ):
        mock_buy = MagicMock()
        mock_buy.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
        mock_buy.create_market_buy_order.return_value = {
            "filled": 0.01666,
            "cost": 1000.0,
        }
        mock_sell = MagicMock()
        mock_sell.create_market_sell_order.return_value = {
            "filled": 0.01665,
            "cost": 1005.0,
        }
        mock_get_ex.side_effect = [mock_buy, mock_sell]
        opp = self._make_opportunity()
        result = _run(execute_cex_arbitrage(db_session, opp))
        assert result["buy_exchange"] == "binance"
        assert result["sell_exchange"] == "kraken"
        assert result["pair"] == "BTC/USDT"
        assert result["buy_filled"] > 0
        assert result["sell_filled"] > 0


# =============================================================================
# Arbitrage dispatcher
# =============================================================================


class TestExecuteArbitrage:
    """execute_arbitrage routes to the correct executor."""

    @patch("app.modules.defi.execute_cex_arbitrage")
    def test_routes_cex_cex_to_cex_executor(self, mock_cex, db_session):
        mock_cex.return_value = {"success": True}
        opportunity = {
            "buy_at": "binance",
            "sell_at": "kraken",
            "pair": "BTC/USDT",
            "buy_price": 100,
            "sell_price": 101,
        }
        result = _run(execute_arbitrage(db_session, "ethereum", opportunity))
        mock_cex.assert_called_once_with(db_session, opportunity, 500.0)
        assert result["success"] is True

    def test_cex_names_are_reused(self, db_session):
        from app.modules.prices import EXCHANGE_FEES

        cex_names = {
            "binance",
            "coinbase",
            "kraken",
            "bybit",
            "kucoin",
            "okx",
            "gate",
            "mexc",
        }
        for ex in EXCHANGE_FEES:
            assert ex in cex_names, (
                f"Exchange '{ex}' has a fee config but is not in cex_names"
            )
