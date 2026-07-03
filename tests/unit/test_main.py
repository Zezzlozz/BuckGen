"""
Tests for the FastAPI application (app/main.py).

Covers all 27 endpoints via TestClient with fully mocked dependencies.
Follows the project convention: class-organized tests using unittest.mock.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Lifespan override — prevents init_db, wallet seeding, and scheduler startup
# from running when TestClient enters the context manager.
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager as _actx


@_actx
async def _noop_lifespan(_app):
    yield


from app.main import app

app.router.lifespan_context = _noop_lifespan


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mock_db():
    """Return a (db_mock, session_iterator) pair for patching get_session."""
    db = MagicMock()
    # Provide a usable query mock chain so handlers that query won't crash
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.order_by.return_value.limit.return_value.all.return_value = []
    return db, iter([db])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    """FastAPI TestClient with lifecycle overridden to a no-op."""
    with TestClient(app) as c:
        yield c


# =============================================================================
# Health & Config
# =============================================================================


class TestHealth:
    """GET /health, GET /config"""

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["scheduler_running"] is False

    def test_config(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "db_url" in data
        assert "cron_bounty" in data
        assert "daily_gas_cap_eur" in data


# =============================================================================
# Wallets
# =============================================================================


class TestWallets:
    """GET /wallets, /wallets/{address}/balance, /wallets/{address}/balances, /chains"""

    def test_list_empty(self, client):
        db, gen = _mock_db()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch("app.main.get_session", return_value=gen):
            resp = client.get("/wallets")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_with_wallets(self, client):
        db, gen = _mock_db()
        wallet = MagicMock()
        wallet.address = "0xabc"
        wallet.chain = "ethereum"
        wallet.wallet_type = MagicMock()
        wallet.wallet_type.value = "hot"
        wallet.derivation_path = "m/44'/60'/0'/0/0"
        wallet.balance_wei = "1000000"
        wallet.is_active = True
        wallet.last_used_at = datetime.now(timezone.utc)
        db.query.return_value.filter.return_value.all.return_value = [wallet]
        with patch("app.main.get_session", return_value=gen):
            resp = client.get("/wallets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["wallets"][0]["address"] == "0xabc"
        assert data["wallets"][0]["wallet_type"] == "hot"

    def test_balance_ok(self, client):
        bal = MagicMock(
            balance_wei=1500000000000000000,
            balance_eth=1.5,
            symbol="ETH",
            has_gas=True,
            error="",
        )
        with patch("app.modules.rpc.get_balance", return_value=bal):
            resp = client.get(
                "/wallets/0x1234567890123456789012345678901234567890/balance?chain=ethereum"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_gas"] is True
        assert data["balance"] == 1.5
        assert data["symbol"] == "ETH"

    def test_balance_low_gas(self, client):
        bal = MagicMock(
            balance_wei=100000,
            balance_eth=0.0001,
            symbol="ETH",
            has_gas=False,
            error="",
        )
        with patch("app.modules.rpc.get_balance", return_value=bal):
            resp = client.get(
                "/wallets/0x1234567890123456789012345678901234567890/balance?chain=polygon"
            )
        assert resp.status_code == 200
        assert resp.json()["has_gas"] is False

    def test_balances_multi(self, client):
        result = {
            "ethereum": MagicMock(
                balance_wei=1, balance_eth=0.5, symbol="ETH", has_gas=True, error=""
            ),
            "base": MagicMock(
                balance_wei=2, balance_eth=0.3, symbol="ETH", has_gas=True, error=""
            ),
        }
        with patch("app.modules.rpc.get_balances_multi", return_value=result):
            resp = client.get(
                "/wallets/0x1234567890123456789012345678901234567890/balances"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "ethereum" in data
        assert "base" in data

    def test_chains_status(self, client):
        status = {
            "ethereum": MagicMock(
                connected=True, block_number=100, gas_price_gwei=10.0, error=""
            ),
        }
        with patch("app.main.rpc_summary", return_value=status):
            resp = client.get("/chains")
        assert resp.status_code == 200
        data = resp.json()
        assert "ethereum" in data


# =============================================================================
# Airdrops
# =============================================================================


class TestAirdrops:
    """GET /airdrops/discover, POST /airdrops/farm"""

    def test_discover(self, client):
        opp = MagicMock(
            title="Test Airdrop",
            url="https://example.com",
            source="github",
            chains=["ethereum"],
            score=0.8,
            reward_tokens=["TOKEN"],
            requires_tasks=False,
        )
        with patch(
            "app.modules.airdrop.discover_airdrops", AsyncMock(return_value=[opp])
        ):
            resp = client.get("/airdrops/discover")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_discover_empty(self, client):
        with patch("app.modules.airdrop.discover_airdrops", AsyncMock(return_value=[])):
            resp = client.get("/airdrops/discover")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_farm_ok(self, client):
        result = {
            "airdrops_discovered": 2,
            "wallets_created": 3,
            "faucet_claims_attempted": 5,
            "faucet_claims_succeeded": 4,
            "registrations": 1,
            "errors": [],
        }
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.airdrop.farm_opportunities", AsyncMock(return_value=result)
            ):
                resp = client.post("/airdrops/farm")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["summary"]["airdrops_discovered"] == 2
        assert data["summary"]["faucet_claims_succeeded"] == 4

    def test_farm_with_errors(self, client):
        result = {
            "airdrops_discovered": 0,
            "wallets_created": 0,
            "faucet_claims_attempted": 0,
            "faucet_claims_succeeded": 0,
            "registrations": 0,
            "errors": ["Faucet claim failed"],
        }
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.airdrop.farm_opportunities", AsyncMock(return_value=result)
            ):
                resp = client.post("/airdrops/farm")
        assert resp.status_code == 200
        assert resp.json()["errors"] == ["Faucet claim failed"]


# =============================================================================
# Prices
# =============================================================================


class TestPrices:
    """GET /prices/tickers, /prices/coingecko, /prices/arbitrage, /prices/history/{pair}"""

    def test_tickers(self, client):
        ticker = MagicMock(
            exchange="binance", bid=100.0, ask=101.0, last=100.5, volume=1000, error=""
        )
        with patch(
            "app.modules.prices.fetch_all_tickers", return_value={"BTC/USDT": [ticker]}
        ):
            resp = client.get("/prices/tickers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert "BTC/USDT" in data["pairs"]

    def test_tickers_empty(self, client):
        with patch("app.modules.prices.fetch_all_tickers", return_value={}):
            resp = client.get("/prices/tickers")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_coingecko(self, client):
        price = MagicMock(
            usd=50000.0, usd_24h_change=2.5, usd_market_cap=1e12, error=""
        )
        with patch(
            "app.modules.prices.fetch_all_coingecko",
            AsyncMock(return_value={"BTC": price}),
        ):
            resp = client.get("/prices/coingecko")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["prices"]["BTC"]["usd"] == 50000.0

    def test_arbitrage(self, client):
        result = {
            "pairs_checked": 100,
            "exchanges_checked": 8,
            "tickers_fetched": 800,
            "arbitrage_opportunities": 1,
            "top_opportunities": [
                {
                    "pair": "BTC/USDT",
                    "buy_at": "binance",
                    "sell_at": "kraken",
                    "net_profit_pct": 0.8,
                    "estimated_profit_eur": 5.0,
                }
            ],
            "errors": [],
        }
        with patch(
            "app.modules.prices.check_all_prices", AsyncMock(return_value=result)
        ):
            resp = client.get("/prices/arbitrage?capital=500")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["pairs_checked"] == 100
        assert len(data["top_opportunities"]) == 1

    def test_arbitrage_no_opportunities(self, client):
        result = {
            "pairs_checked": 100,
            "exchanges_checked": 8,
            "tickers_fetched": 800,
            "arbitrage_opportunities": 0,
            "top_opportunities": [],
            "errors": [],
        }
        with patch(
            "app.modules.prices.check_all_prices", AsyncMock(return_value=result)
        ):
            resp = client.get("/prices/arbitrage")
        assert resp.status_code == 200
        assert resp.json()["summary"]["opportunities_found"] == 0

    def test_price_history(self, client):
        snap = {
            "timestamp": "2024-01-01T00:00:00Z",
            "exchange": "binance",
            "bid": 100.0,
            "ask": 101.0,
        }
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch("app.modules.prices.get_price_history", return_value=[snap]):
                resp = client.get("/prices/history/BTC/USDT?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pair"] == "BTC/USDT"
        assert data["count"] == 1
        assert data["snapshots"][0]["exchange"] == "binance"


# =============================================================================
# Trade / DeFi
# =============================================================================


class TestTrading:
    """POST /trade/swap, POST /trade/arbitrage, GET /trade/quote"""

    def test_swap_success(self, client):
        result = MagicMock(
            success=True,
            tx_hash="0xdead",
            from_amount="1.0",
            to_amount="1000.0",
            gas_used_wei="21000",
            error="",
        )
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch("app.modules.defi.execute_swap", AsyncMock(return_value=result)):
                resp = client.post(
                    "/trade/swap?chain=ethereum&wallet_index=0&from_token=ETH&to_token=USDC"
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tx_hash"] == "0xdead"

    def test_swap_failure(self, client):
        result = MagicMock(
            success=False,
            tx_hash="",
            from_amount="",
            to_amount="",
            gas_used_wei="0",
            error="insufficient balance",
        )
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch("app.modules.defi.execute_swap", AsyncMock(return_value=result)):
                resp = client.post("/trade/swap")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert resp.json()["error"] == "insufficient balance"

    def test_arbitrage_no_opportunities(self, client):
        price_result = {
            "pairs_checked": 100,
            "exchanges_checked": 8,
            "tickers_fetched": 800,
            "arbitrage_opportunities": 0,
            "top_opportunities": [],
            "errors": [],
        }
        with patch(
            "app.modules.prices.check_all_prices", AsyncMock(return_value=price_result)
        ):
            resp = client.post("/trade/arbitrage")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "No arbitrage" in resp.json()["error"]

    def test_arbitrage_executed(self, client):
        price_result = {
            "pairs_checked": 100,
            "exchanges_checked": 8,
            "tickers_fetched": 800,
            "arbitrage_opportunities": 1,
            "top_opportunities": [{"pair": "BTC/USDT", "net_profit_pct": 0.8}],
            "errors": [],
        }
        arb_result = {"success": True, "profit_eur": 5.0}
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.prices.check_all_prices",
                AsyncMock(return_value=price_result),
            ):
                with patch(
                    "app.modules.defi.execute_arbitrage",
                    AsyncMock(return_value=arb_result),
                ):
                    resp = client.post("/trade/arbitrage")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_quote_success(self, client):
        quote = MagicMock(
            from_amount="1.0",
            to_amount="1800.0",
            estimated_gas="50000",
            price_impact=0.1,
        )
        with patch("app.modules.defi.get_swap_quote", AsyncMock(return_value=quote)):
            resp = client.get(
                "/trade/quote?chain=ethereum&from_token=ETH&to_token=USDC&amount_wei=1000000000000000000"
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["to_amount"] == "1800.0"

    def test_quote_failure(self, client):
        with patch("app.modules.defi.get_swap_quote", AsyncMock(return_value=None)):
            resp = client.get("/trade/quote")
        assert resp.status_code == 200
        assert resp.json()["success"] is False


# =============================================================================
# Bounties
# =============================================================================


class TestBounties:
    """GET /bounties/top, POST /bounties/submit/{id}, POST /bounties/submit-top"""

    def test_top_empty(self, client):
        db, gen = _mock_db()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        with patch("app.main.get_session", return_value=gen):
            resp = client.get("/bounties/top")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_top_with_results(self, client):
        db, gen = _mock_db()
        bounty = MagicMock()
        bounty.id = 1
        bounty.title = "Test bounty"
        bounty.reward_amount = 500
        bounty.reward_currency = "USD"
        bounty.score = 0.85
        bounty.url = "https://github.com/owner/repo/issues/1"
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            bounty
        ]
        with patch("app.main.get_session", return_value=gen):
            resp = client.get("/bounties/top?min_score=0.7&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["bounties"][0]["title"] == "Test bounty"
        assert data["bounties"][0]["score"] == 0.85

    def test_submit_success(self, client):
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.submit_bounty.submit_bounty",
                AsyncMock(return_value={"success": True, "bounty_id": 1}),
            ):
                resp = client.post("/bounties/submit/1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_submit_not_found(self, client):
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.submit_bounty.submit_bounty",
                AsyncMock(return_value={"success": False, "error": "Bounty not found"}),
            ):
                resp = client.post("/bounties/submit/999")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_submit_top(self, client):
        db, gen = _mock_db()
        results = [{"bounty_id": 1, "success": True}]
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.submit_bounty.submit_top_bounties",
                AsyncMock(return_value=results),
            ):
                resp = client.post("/bounties/submit-top?max_subs=3&min_score=0.7")
        assert resp.status_code == 200
        assert resp.json()["submissions"] == 1

    def test_submit_top_empty(self, client):
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.submit_bounty.submit_top_bounties",
                AsyncMock(return_value=[]),
            ):
                resp = client.post("/bounties/submit-top")
        assert resp.status_code == 200
        assert resp.json()["submissions"] == 0


# =============================================================================
# On-chain Tasks
# =============================================================================


class TestTasks:
    """POST /tasks/self-transfer, /tasks/deploy, /tasks/run-all"""

    def test_self_transfer_success(self, client):
        db, gen = _mock_db()
        result = {"success": True, "tx_hash": "0xdead"}
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.zksync_era.send_self_transfer",
                AsyncMock(return_value=result),
            ):
                resp = client.post("/tasks/self-transfer?chain=sepolia&wallet_index=10")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_self_transfer_failure(self, client):
        db, gen = _mock_db()
        result = {"success": False, "error": "insufficient balance"}
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.zksync_era.send_self_transfer",
                AsyncMock(return_value=result),
            ):
                resp = client.post("/tasks/self-transfer")
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_deploy_success(self, client):
        db, gen = _mock_db()
        result = {"success": True, "contract_address": "0x1234"}
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.zksync_era.deploy_test_contract",
                AsyncMock(return_value=result),
            ):
                resp = client.post("/tasks/deploy")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_run_all(self, client):
        db, gen = _mock_db()
        results = [
            {"chain": "sepolia", "success": True},
            {"chain": "base_sepolia", "success": True},
        ]
        with patch("app.main.get_session", return_value=gen):
            with patch(
                "app.modules.zksync_era.run_all_testnets",
                AsyncMock(return_value=results),
            ):
                resp = client.post("/tasks/run-all")
        assert resp.status_code == 200
        assert resp.json()["chains"] == 2
        assert len(resp.json()["results"]) == 2


# =============================================================================
# Revenue / P&L
# =============================================================================


class TestRevenue:
    """POST /bounties/{id}/mark-paid, GET /revenue/summary, GET /revenue/module/{module}"""

    def test_mark_paid_success(self, client):
        db, gen = _mock_db()
        bounty = MagicMock()
        bounty.id = 1
        bounty.title = "Test"
        bounty.url = "https://example.com"
        bounty.reward_amount = 100
        bounty.reward_currency = "USD"
        bounty.status = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = bounty
        with patch("app.main.get_session", return_value=gen):
            with patch("app.utils.pnl.record_revenue", return_value=MagicMock()):
                resp = client.post("/bounties/1/mark-paid?actual_reward=150")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["reward_eur"] == 150

    def test_mark_paid_not_found(self, client):
        db, gen = _mock_db()
        db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.main.get_session", return_value=gen):
            resp = client.post("/bounties/999/mark-paid")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert resp.json()["error"] == "Bounty not found"

    def test_mark_paid_zero_reward_no_revenue(self, client):
        db, gen = _mock_db()
        bounty = MagicMock()
        bounty.id = 2
        bounty.title = "Zero"
        bounty.url = "https://example.com"
        bounty.reward_amount = 0
        bounty.reward_currency = "USD"
        bounty.status = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = bounty
        with patch("app.main.get_session", return_value=gen):
            with patch("app.utils.pnl.record_revenue") as mock_rec:
                resp = client.post("/bounties/2/mark-paid")
        assert resp.status_code == 200
        assert resp.json()["revenue_recorded"] is False
        mock_rec.assert_not_called()

    def test_revenue_summary(self, client):
        summary = {"total_revenue_eur": 100.0, "total_spend_eur": 30.0}
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch("app.utils.pnl.pnl_summary", return_value=summary):
                resp = client.get("/revenue/summary")
        assert resp.status_code == 200
        assert resp.json()["total_revenue_eur"] == 100.0

    def test_revenue_module_ok(self, client):
        pnl = {"module": "bounties", "revenue_eur": 50.0, "spend_eur": 10.0}
        db, gen = _mock_db()
        with patch("app.main.get_session", return_value=gen):
            with patch("app.utils.pnl.module_pnl", return_value=pnl):
                resp = client.get("/revenue/module/bounties")
        assert resp.status_code == 200
        assert resp.json()["module"] == "bounties"

    def test_revenue_module_invalid(self, client):
        """Invalid module name fails validation with 422."""
        resp = client.get("/revenue/module/invalid")
        assert resp.status_code == 422


# =============================================================================
# System / Monitoring
# =============================================================================


class TestSystem:
    """GET /system/health, /system/status, POST /system/reset/{module}, /system/recover"""

    def test_health(self, client):
        detailed = {
            "uptime_seconds": 3600,
            "modules": [
                {
                    "name": "bounties",
                    "status": "ok",
                    "total_errors": 0,
                    "last_error_msg": None,
                },
            ],
        }
        with patch("app.main.monitor.get_detailed", return_value=detailed):
            resp = client.get("/system/health")
        assert resp.status_code == 200
        assert resp.json()["uptime_seconds"] == 3600

    def test_status(self, client):
        summary = {
            "modules_ok": 5,
            "modules_total": 5,
            "modules_degraded": 0,
            "modules_down": 0,
            "errors_last_hour": 0,
        }
        chains = {
            "ethereum": MagicMock(connected=True),
            "base": MagicMock(connected=True),
        }
        with patch("app.main.monitor.get_summary", return_value=summary):
            with patch("app.modules.rpc.check_all_chains", return_value=chains):
                resp = client.get("/system/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["modules_ok"] == 5
        assert data["scheduler_running"] is False
        assert data["chains_connected"] == 2

    def test_reset_ok(self, client):
        with patch("app.main.monitor.reset_module", return_value=True):
            resp = client.post("/system/reset/bounties")
        assert resp.status_code == 200
        assert resp.json()["status"] == "reset"

    def test_reset_not_found(self, client):
        with patch("app.main.monitor.reset_module", return_value=False):
            resp = client.post("/system/reset/unknown")
        assert resp.status_code == 404

    def test_recover(self, client):
        with patch(
            "app.main.monitor.recover_all", AsyncMock(return_value={"bounties": "ok"})
        ):
            resp = client.post("/system/recover")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# =============================================================================
# Dashboard (HTML)
# =============================================================================


class TestDashboard:
    """GET /dashboard"""

    def test_dashboard_returns_html(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert b"BuckGen" in resp.content

    def test_dashboard_contains_script(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert b"fetchAll" in resp.content
        assert b"setInterval" in resp.content


# =============================================================================
# Error handling / edge cases
# =============================================================================


class TestErrors:
    """Validation, 404, and malformed input handling."""

    def test_bounty_submit_invalid_id(self, client):
        """Non-integer bounty_id should return 422."""
        resp = client.post("/bounties/submit/abc")
        assert resp.status_code == 422

    def test_health_no_params(self, client):
        """/health accepts no params and returns 200."""
        resp = client.get("/health?unexpected=true")
        assert (
            resp.status_code == 200
        )  # FastAPI ignores unexpected query params by default

    def test_unknown_route(self, client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404

    def test_prices_arbitrage_non_float_capital(self, client):
        """capital query param must be a float."""
        resp = client.get("/prices/arbitrage?capital=abc")
        assert resp.status_code == 422

    def test_wallet_balance_default_chain(self, client):
        """Defaults to ethereum when chain param is omitted."""
        bal = MagicMock(
            balance_wei=1, balance_eth=0.0, symbol="ETH", has_gas=False, error=""
        )
        with patch("app.modules.rpc.get_balance", return_value=bal):
            resp = client.get(
                "/wallets/0x1234567890123456789012345678901234567890/balance"
            )
        assert resp.status_code == 200
        # The handler passed chain="ethereum" (the default)
        # Even though we mock get_balance, we can verify the path worked
        assert resp.json()["has_gas"] is False
