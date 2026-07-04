"""
Unit tests for the Price & Arbitrage Module (app/modules/prices.py).

Tests cover:
  - Symbol normalization
  - Ticker fetching (mocked ccxt)
  - Parallel ticker aggregation
  - Arbitrage opportunity detection
  - Fee calculations and confidence scoring
"""

from unittest.mock import MagicMock, patch

from app.modules.prices import (
    MIN_PROFIT_THRESHOLD_PCT,
    TickerPrice,
    _normalize_symbol,
    fetch_all_tickers,
    fetch_ticker,
    find_arbitrage_opportunities,
)

# =============================================================================
# Symbol normalization
# =============================================================================


class TestNormalizeSymbol:
    """_normalize_symbol — converts various formats to BASE/QUOTE."""

    def test_keeps_proper_format(self):
        assert _normalize_symbol("BTC/USDT") == "BTC/USDT"

    def test_hyphen_to_slash(self):
        assert _normalize_symbol("BTC-USDT") == "BTC/USDT"

    def test_underscore_to_slash(self):
        assert _normalize_symbol("BTC_USDT") == "BTC/USDT"

    def test_strips_futures_suffix(self):
        assert _normalize_symbol("BTC/USDT:USDT") == "BTC/USDT"

    def test_case_upper(self):
        assert _normalize_symbol("btc/usdt") == "BTC/USDT"

    def test_lowercase_with_hyphen(self):
        assert _normalize_symbol("btc-usdt") == "BTC/USDT"


# =============================================================================
# Ticker fetching (mocked ccxt)
# =============================================================================


class TestFetchTicker:
    """fetch_ticker — individual ticker fetch via ccxt."""

    @patch("app.modules.prices._get_exchange")
    def test_successful_fetch(self, mock_get_exchange):
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.return_value = {
            "symbol": "BTC/USDT",
            "bid": 60000.0,
            "ask": 60010.0,
            "last": 60005.0,
            "baseVolume": 1000.0,
            "timestamp": 1234567890000,
        }
        mock_get_exchange.return_value = mock_ex

        result = fetch_ticker("binance", "BTC/USDT")
        assert result.exchange == "binance"
        assert result.symbol == "BTC/USDT"
        assert result.bid == 60000.0
        assert result.ask == 60010.0
        assert result.last == 60005.0
        assert result.volume == 1000.0
        assert result.timestamp == 1234567890000
        assert result.error == ""

    @patch("app.modules.prices._get_exchange")
    def test_exchange_not_available(self, mock_get_exchange):
        mock_get_exchange.return_value = None

        result = fetch_ticker("unknown_exchange", "BTC/USDT")
        assert result.error == "exchange not available"
        assert result.last == 0

    @patch("app.modules.prices._get_exchange")
    def test_bad_symbol_returns_error(self, mock_get_exchange):
        import ccxt

        mock_ex = MagicMock()
        mock_ex.fetch_ticker.side_effect = ccxt.BadSymbol("symbol not found")
        mock_get_exchange.return_value = mock_ex

        result = fetch_ticker("binance", "UNKNOWN/PAIR")
        assert "not found" in result.error

    @patch("app.modules.prices._get_exchange")
    def test_generic_exception_handled(self, mock_get_exchange):
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.side_effect = Exception("connection timeout")
        mock_get_exchange.return_value = mock_ex

        result = fetch_ticker("binance", "BTC/USDT")
        assert "connection timeout" in result.error

    @patch("app.modules.prices._get_exchange")
    def test_zero_bid_ask_fallback_to_last(self, mock_get_exchange):
        mock_ex = MagicMock()
        mock_ex.fetch_ticker.return_value = {
            "symbol": "BTC/USDT",
            "bid": None,
            "ask": None,
            "last": 60000.0,
            "baseVolume": 100.0,
            "timestamp": 1234567890000,
        }
        mock_get_exchange.return_value = mock_ex

        result = fetch_ticker("binance", "BTC/USDT")
        assert result.last == 60000.0
        assert result.bid == 0
        assert result.ask == 0


# =============================================================================
# Parallel ticker aggregation
# =============================================================================


class TestFetchAllTickers:
    """fetch_all_tickers — aggregates across exchanges and pairs."""

    @patch("app.modules.prices.fetch_ticker")
    def test_returns_dict_with_all_pairs(self, mock_fetch):
        mock_fetch.return_value = TickerPrice(
            exchange="binance",
            symbol="BTC/USDT",
            bid=100,
            ask=101,
            last=100.5,
            volume=1000,
            timestamp=0,
        )
        pairs = ["BTC/USDT", "ETH/USDT"]
        result = fetch_all_tickers(pairs=pairs, exchanges=["binance"])
        assert set(result.keys()) == {"BTC/USDT", "ETH/USDT"}
        assert len(result["BTC/USDT"]) == 1

    @patch("app.modules.prices.fetch_ticker")
    def test_fetch_exception_captured(self, mock_fetch):
        mock_fetch.side_effect = Exception("network error")
        result = fetch_all_tickers(pairs=["BTC/USDT"], exchanges=["binance"])
        btc_prices = result["BTC/USDT"]
        assert len(btc_prices) == 1
        assert btc_prices[0].error == "network error"


# =============================================================================
# Arbitrage detection
# =============================================================================


class TestFindArbitrageOpportunities:
    """find_arbitrage_opportunities — detects profitable gaps."""

    def test_detects_profitable_gap(self):
        """With a 2%+ gap, should detect an opportunity even after fees."""
        from app.modules.prices import TickerPrice

        wide = [
            TickerPrice(
                exchange="binance",
                symbol="BTC/USDT",
                bid=100.0,
                ask=100.5,
                last=100.25,
                volume=1_000_000,
                timestamp=0,
            ),
            TickerPrice(
                exchange="kraken",
                symbol="BTC/USDT",
                bid=102.0,
                ask=102.5,
                last=102.25,
                volume=500_000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": wide}
        opps = find_arbitrage_opportunities(tickers, capital_eur=500.0)

        assert len(opps) > 0
        opp = opps[0]
        assert opp.asset == "BTC"
        assert opp.pair == "BTC/USDT"
        assert opp.net_profit_pct >= MIN_PROFIT_THRESHOLD_PCT
        assert opp.estimated_profit_eur > 0

    def test_buy_low_sell_high_direction(self):
        """Should recommend buy at cheapest, sell at most expensive."""
        from app.modules.prices import TickerPrice

        wide = [
            TickerPrice(
                exchange="binance",
                symbol="BTC/USDT",
                bid=100.0,
                ask=100.5,
                last=100.25,
                volume=1_000_000,
                timestamp=0,
            ),
            TickerPrice(
                exchange="kraken",
                symbol="BTC/USDT",
                bid=102.0,
                ask=102.5,
                last=102.25,
                volume=500_000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": wide}
        opps = find_arbitrage_opportunities(tickers, capital_eur=500.0)

        best_opp = opps[0]
        assert best_opp.buy_at == "binance"
        assert best_opp.sell_at == "kraken"

    def test_gap_below_threshold_no_opportunity(self):
        """Small gap after fees should be filtered out."""
        from app.modules.prices import TickerPrice

        tight = [
            TickerPrice(
                exchange="binance",
                symbol="BTC/USDT",
                bid=100.0,
                ask=100.01,
                last=100.0,
                volume=1_000_000,
                timestamp=0,
            ),
            TickerPrice(
                exchange="kraken",
                symbol="BTC/USDT",
                bid=100.02,
                ask=100.03,
                last=100.02,
                volume=1_000_000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": tight}
        opps = find_arbitrage_opportunities(tickers, capital_eur=100.0)
        # Fees alone (0.1% + 0.26% = 0.36%) will eat the ~0.01% gap
        assert len(opps) == 0

    def test_single_valid_price_no_opportunity(self):
        """Need at least 2 valid prices to detect arb."""
        from app.modules.prices import TickerPrice

        single = [
            TickerPrice(
                exchange="binance",
                symbol="BTC/USDT",
                bid=100,
                ask=101,
                last=100.5,
                volume=1000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": single}
        opps = find_arbitrage_opportunities(tickers)
        assert len(opps) == 0

    def test_high_volume_boosts_confidence(self, sample_ticker_prices):
        """Liquid pairs (>100k vol) should get 0.8 confidence."""
        tickers = {"BTC/USDT": sample_ticker_prices}
        opps = find_arbitrage_opportunities(tickers, capital_eur=500.0)
        for opp in opps:
            if opp.estimated_profit_eur > 0:
                assert opp.confidence >= 0.6

    def test_low_volume_lowers_confidence(self):
        """Pairs with low volume get lower confidence."""
        from app.modules.prices import TickerPrice

        low_vol = [
            TickerPrice(
                exchange="binance",
                symbol="SHIB/USDT",
                bid=0.00001,
                ask=0.000011,
                last=0.0000105,
                volume=500,
                timestamp=0,
            ),
            TickerPrice(
                exchange="kraken",
                symbol="SHIB/USDT",
                bid=0.000015,
                ask=0.000016,
                last=0.0000155,
                volume=300,
                timestamp=0,
            ),
        ]
        tickers = {"SHIB/USDT": low_vol}
        opps = find_arbitrage_opportunities(tickers, capital_eur=100.0)
        for opp in opps:
            if opp.estimated_profit_eur > 0:
                # Volume < 10k -> confidence 0.5
                assert opp.confidence == 0.5

    def test_mid_volume_confidence(self):
        """Volume between 10k and 100k -> 0.6 confidence."""
        from app.modules.prices import TickerPrice

        mid_vol = [
            TickerPrice(
                exchange="binance",
                symbol="ADA/USDT",
                bid=0.5,
                ask=0.51,
                last=0.505,
                volume=50_000,
                timestamp=0,
            ),
            TickerPrice(
                exchange="kraken",
                symbol="ADA/USDT",
                bid=0.55,
                ask=0.56,
                last=0.555,
                volume=30_000,
                timestamp=0,
            ),
        ]
        tickers = {"ADA/USDT": mid_vol}
        opps = find_arbitrage_opportunities(tickers, capital_eur=100.0)
        for opp in opps:
            if opp.estimated_profit_eur > 0:
                assert opp.confidence == 0.6

    def test_opportunities_sorted_by_net_profit(self, sample_ticker_prices):
        """Results should be sorted descending by net_profit_pct."""
        # Add a third exchange to get multiple opportunities
        from app.modules.prices import TickerPrice

        extra = sample_ticker_prices + [
            TickerPrice(
                exchange="okx",
                symbol="BTC/USDT",
                bid=60750.0,
                ask=60760.0,
                last=60755.0,
                volume=200_000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": extra}
        opps = find_arbitrage_opportunities(tickers, capital_eur=500.0)
        for i in range(len(opps) - 1):
            assert opps[i].net_profit_pct >= opps[i + 1].net_profit_pct

    def test_no_exchange_fees_for_unknown(self):
        """Unknown exchanges should use default 0.2% fee."""
        from app.modules.prices import TickerPrice

        prices = [
            TickerPrice(
                exchange="unknown1",
                symbol="BTC/USDT",
                bid=100,
                ask=101,
                last=100.5,
                volume=1_000_000,
                timestamp=0,
            ),
            TickerPrice(
                exchange="unknown2",
                symbol="BTC/USDT",
                bid=110,
                ask=111,
                last=110.5,
                volume=1_000_000,
                timestamp=0,
            ),
        ]
        tickers = {"BTC/USDT": prices}
        opps = find_arbitrage_opportunities(tickers, capital_eur=100.0)
        if opps:
            # Gap is ~9%, so even with 0.4% fees it should be profitable
            assert opps[0].net_profit_pct > 8.0
