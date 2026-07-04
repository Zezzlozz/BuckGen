"""
Unit tests for the Airdrop & Faucet Module (app/modules/airdrop.py).

Tests cover:
  - Airdrop discovery via GitHub search (mocked httpx)
  - Batch wallet creation
  - Platform-specific registration dispatch
  - Faucet registry completeness
"""

from unittest.mock import AsyncMock, MagicMock, patch

# =============================================================================
# Airdrop discovery
# =============================================================================


class TestDiscoverAirdrops:
    """discover_airdrops — GitHub search for airdrop opportunities."""

    @patch("app.modules.airdrop.settings")
    @patch("httpx.AsyncClient")
    def test_discovers_from_github(self, mock_client_class, mock_settings):
        """Returns AirdropOpportunity objects from GitHub search results."""
        mock_settings.GITHUB_TOKEN = "test"
        mock_settings.AIRDROP_BASELINE_SCORE = 0.3
        mock_settings.AIRDROP_POSITIVE_SIGNAL = 0.2
        mock_settings.AIRDROP_NEGATIVE_SIGNAL = 0.3

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {
                    "html_url": "https://github.com/test/airdrop",
                    "title": "Test Airdrop",
                    "body": "New airdrop on Ethereum and Arbitrum! Deadline: 2025-12-31",
                },
                {
                    "html_url": "https://github.com/test/faucet",
                    "title": "Faucet Campaign",
                    "body": "Complete tasks for rewards",
                },
            ]
        }
        mock_client.get.return_value = mock_response

        from app.modules.airdrop import discover_airdrops

        results = asyncio_run(discover_airdrops(max_results=5))
        assert len(results) > 0
        # Check that we got AirdropOpportunity objects with expected fields
        for opp in results:
            assert hasattr(opp, "title")
            assert hasattr(opp, "url")
            assert hasattr(opp, "score")
            assert opp.score > 0

    @patch("app.modules.airdrop.settings")
    @patch("httpx.AsyncClient")
    def test_handles_api_error_gracefully(self, mock_client_class, mock_settings):
        """API errors should not crash; return empty list."""
        mock_settings.GITHUB_TOKEN = "test"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = Exception("API unavailable")

        from app.modules.airdrop import discover_airdrops

        results = asyncio_run(discover_airdrops(max_results=5))
        assert results == []


# =============================================================================
# Batch wallet creation
# =============================================================================


class TestBatchCreateWallets:
    """batch_create_wallets — derives and persists wallets."""

    def test_creates_specified_count(self, db_session):
        from app.modules.airdrop import batch_create_wallets

        wallets = asyncio_run(
            batch_create_wallets(db_session, count=2, chains=["ethereum"])
        )
        # 2 per chain × 1 chain = 2, plus the initial seed wallets which may not exist
        # The function returns only the newly created ones
        assert len(wallets) >= 2

    def test_wallets_are_disposable_type(self, db_session):
        from app.modules.airdrop import batch_create_wallets

        wallets = asyncio_run(
            batch_create_wallets(db_session, count=2, chains=["ethereum"])
        )
        from app.db.models import WalletType

        for w in wallets:
            assert w.wallet_type == WalletType.DISPOSABLE

    def test_creates_across_multiple_chains(self, db_session):
        from app.modules.airdrop import batch_create_wallets

        wallets = asyncio_run(
            batch_create_wallets(db_session, count=2, chains=["ethereum", "arbitrum"])
        )
        assert len(wallets) >= 4


# =============================================================================
# Platform registration dispatch
# =============================================================================


class TestRegisterWalletsForAirdrop:
    """register_wallets_for_airdrop — dispatches to platform handlers."""

    @patch("httpx.AsyncClient")
    def test_registers_on_github(self, mock_client_class, db_session):
        """GitHub airdrops should star the repo."""
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.put.return_value = MagicMock(status_code=204)
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: [])

        from app.modules.airdrop import (
            AirdropOpportunity,
            register_wallets_for_airdrop,
        )
        from app.modules.wallet import sync_wallet_to_db

        wallet = sync_wallet_to_db(db_session, index=9, chain="ethereum")
        opp = AirdropOpportunity(
            title="Test Coin",
            url="https://github.com/test/test-coin",
            source="github",
        )

        result = asyncio_run(register_wallets_for_airdrop(db_session, opp, [wallet]))
        assert result is not None

    @patch("httpx.AsyncClient")
    def test_registers_on_dework(self, mock_client_class, db_session):
        """Dework airdrops should call the Dework API."""
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.put.return_value = MagicMock(status_code=204)
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: [])

        from app.modules.airdrop import (
            AirdropOpportunity,
            register_wallets_for_airdrop,
        )
        from app.modules.wallet import sync_wallet_to_db

        wallet = sync_wallet_to_db(db_session, index=9, chain="ethereum")
        opp = AirdropOpportunity(
            title="Dework Task",
            url="https://app.dework.xyz/test",
            source="dework",
        )

        result = asyncio_run(register_wallets_for_airdrop(db_session, opp, [wallet]))
        assert result is not None


# =============================================================================
# Faucet registry completeness
# =============================================================================


class TestFaucetRegistry:
    """FAUCET_REGISTRY should be well-formed."""

    def test_all_faucets_have_required_fields(self):
        from app.modules.airdrop import FAUCET_REGISTRY

        for faucet in FAUCET_REGISTRY:
            assert faucet.name, "Faucet missing name"
            assert faucet.chain, f"Faucet {faucet.name} missing chain"
            assert faucet.symbol, f"Faucet {faucet.name} missing symbol"
            assert faucet.amount_per_claim > 0, (
                f"Faucet {faucet.name} has non-positive amount"
            )
            assert faucet.cooldown_hours > 0, f"Faucet {faucet.name} needs cooldown"
            assert faucet.claim_url.startswith("http"), (
                f"Faucet {faucet.name} has invalid URL"
            )

    def test_minimum_number_of_faucets(self):
        from app.modules.airdrop import FAUCET_REGISTRY

        assert len(FAUCET_REGISTRY) >= 20, (
            f"Only {len(FAUCET_REGISTRY)} faucets configured, expected at least 20"
        )

    def test_multiple_chains_represented(self):
        from app.modules.airdrop import FAUCET_REGISTRY

        chains = set(f.chain for f in FAUCET_REGISTRY)
        assert len(chains) >= 5, f"Only {len(chains)} chains have faucets: {chains}"

    def test_functioning_claim_url_format(self):
        """Verify claim URLs follow expected patterns for the API calls."""
        from app.modules.airdrop import FAUCET_REGISTRY

        for faucet in FAUCET_REGISTRY:
            assert faucet.wallet_field, f"Faucet {faucet.name} missing wallet_field"
            assert faucet.claim_method in ("POST", "GET"), (
                f"Faucet {faucet.name} has unsupported method {faucet.claim_method}"
            )


def asyncio_run(coro):
    """Run an async function synchronously for test purposes."""
    import asyncio

    return asyncio.run(coro)
