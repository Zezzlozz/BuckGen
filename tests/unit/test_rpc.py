"""
Unit tests for Multi-chain RPC Client (app/modules/rpc.py).

Tests cover:
  - get_web3: caching, missing chain, connection error
  - check_chain: success, no connection, exception
  - check_all_chains: parallel execution across chains
  - get_balance: success, no RPC, API error
  - get_balances_multi: parallel multi-chain balance
  - estimate_gas: success, no RPC, API error
  - summary: combined chain status + gas
"""

from unittest.mock import MagicMock, patch

import pytest

from app.modules.rpc import (
    _w3_cache,
    check_all_chains,
    check_chain,
    estimate_gas,
    get_balance,
    get_balances_multi,
    get_web3,
    summary,
)


def _make_eth_mock(block_number=100, gas_price_wei=1, balance_wei=0, connected=True):
    """Create a mock Web3 instance with a configured ``eth`` namespace.

    Using a single ``eth`` MagicMock avoids the problem of MagicMock
    creating fresh children on every attribute access.
    """
    eth = MagicMock()
    eth.block_number = block_number
    eth.gas_price = gas_price_wei
    eth.get_balance.return_value = balance_wei

    w3 = MagicMock()
    w3.eth = eth
    w3.is_connected.return_value = connected
    # from_wei(wei, "ether") -> return eth value; from_wei(wei, "gwei") -> return gwei
    w3.from_wei.side_effect = lambda w, unit: (
        float(w) / 1e18 if unit == "ether" else float(w) / 1e9
    )
    w3.to_checksum_address.return_value = "0xCheckSum"
    return w3


@pytest.fixture(autouse=True)
def _clear_w3_cache():
    """Clear the RPC connection cache before and after each test."""
    _w3_cache.clear()
    yield
    _w3_cache.clear()


# =============================================================================
# get_web3
# =============================================================================


class TestGetWeb3:
    """get_web3 — get or create a cached Web3 connection."""

    def test_returns_cached_instance(self):
        w3 = _make_eth_mock()
        _w3_cache["ethereum"] = w3
        assert get_web3("ethereum") is w3

    def test_returns_none_for_unknown_chain(self):
        assert get_web3("nonexistent") is None


# =============================================================================
# check_chain
# =============================================================================


class TestCheckChain:
    """check_chain — health + gas info for a single chain."""

    def test_connected(self):
        w3 = _make_eth_mock(block_number=12345, gas_price_wei=1000000000)
        _w3_cache["ethereum"] = w3

        status = check_chain("ethereum")
        assert status.connected is True
        assert status.block_number == 12345
        assert status.gas_price_gwei == 1.0

    def test_no_connection_returns_error(self):
        with patch("app.modules.rpc.get_web3", return_value=None):
            status = check_chain("ethereum")
            assert status.connected is False
            assert status.error != ""

    def test_exception_during_check(self):
        class _RaisingEth:
            block_number = 12345

            @property
            def gas_price(self):
                raise ValueError("bad RPC")

        w3 = MagicMock()
        w3.eth = _RaisingEth()
        _w3_cache["ethereum"] = w3

        status = check_chain("ethereum")
        assert status.connected is False
        assert status.error != ""


# =============================================================================
# check_all_chains
# =============================================================================


class TestCheckAllChains:
    """check_all_chains — parallel health check for all chains."""

    def test_returns_all_chains(self):
        w3 = _make_eth_mock(block_number=1, gas_price_wei=1)
        _w3_cache["ethereum"] = w3

        results = check_all_chains()
        assert isinstance(results, dict)
        assert len(results) >= 5

    def test_chain_without_connection_reported(self):
        results = check_all_chains()
        for chain, status in results.items():
            assert isinstance(status.connected, bool)


# =============================================================================
# get_balance
# =============================================================================


class TestGetBalance:
    """get_balance — native token balance for an address."""

    def test_successful_balance_check(self):
        w3 = _make_eth_mock(balance_wei=1000000000000000000)
        _w3_cache["ethereum"] = w3

        bal = get_balance("0xabc", "ethereum")
        assert bal.balance_wei == 1000000000000000000
        assert bal.balance_eth == 1.0
        assert bal.has_gas is True
        assert bal.error == ""

    def test_no_rpc_connection(self):
        with patch("app.modules.rpc.get_web3", return_value=None):
            bal = get_balance("0xabc", "ethereum")
            assert bal.balance_wei == 0
            assert bal.has_gas is False
            assert bal.error == "no rpc connection"

    def test_exception_during_balance_check(self):
        w3 = _make_eth_mock()
        w3.to_checksum_address.side_effect = ValueError("invalid address")
        _w3_cache["ethereum"] = w3

        bal = get_balance("invalid", "ethereum")
        assert bal.balance_wei == 0
        assert bal.has_gas is False
        assert bal.error != ""

    def test_low_balance_has_gas_false(self):
        w3 = _make_eth_mock(balance_wei=100000)
        _w3_cache["ethereum"] = w3

        bal = get_balance("0xabc", "ethereum")
        assert bal.has_gas is False


# =============================================================================
# get_balances_multi
# =============================================================================


class TestGetBalancesMulti:
    """get_balances_multi — parallel multi-chain balance check."""

    def test_checks_all_chains_by_default(self):
        w3 = _make_eth_mock(balance_wei=0)
        _w3_cache["ethereum"] = w3

        results = get_balances_multi("0xabc")
        assert isinstance(results, dict)
        assert "ethereum" in results

    def test_checks_specified_chains(self):
        w3 = _make_eth_mock(balance_wei=0)
        _w3_cache["ethereum"] = w3

        results = get_balances_multi("0xabc", chains=["ethereum"])
        assert len(results) == 1
        assert "ethereum" in results


# =============================================================================
# estimate_gas
# =============================================================================


class TestEstimateGas:
    """estimate_gas — estimate native token transfer cost."""

    def test_successful_estimate(self):
        w3 = _make_eth_mock(gas_price_wei=20000000000)
        _w3_cache["ethereum"] = w3

        gas = estimate_gas("ethereum")
        assert "error" not in gas
        assert gas["gas_price_gwei"] == 20.0
        assert gas["estimated_cost_eth"] > 0
        assert gas["chain"] == "ethereum"

    def test_no_rpc_connection(self):
        with patch("app.modules.rpc.get_web3", return_value=None):
            gas = estimate_gas("ethereum")
            assert "error" in gas
            assert gas["error"] == "no rpc connection"

    def test_exception_during_estimate(self):
        class _RaisingEth:
            @property
            def gas_price(self):
                raise ValueError("bad RPC")

        w3 = MagicMock()
        w3.eth = _RaisingEth()
        _w3_cache["ethereum"] = w3

        gas = estimate_gas("ethereum")
        assert "error" in gas


# =============================================================================
# summary
# =============================================================================


class TestSummary:
    """summary — combined chain status + gas costs."""

    def test_returns_results(self):
        w3 = _make_eth_mock(block_number=1, gas_price_wei=1)
        _w3_cache["ethereum"] = w3

        s = summary()
        assert isinstance(s, dict)
        assert len(s) >= 1
        for chain, info in s.items():
            assert "connected" in info
            assert "gas_price_gwei" in info
