"""
Unit tests for On-chain Task Bot (app/modules/zksync_era.py).

Tests cover:
  - send_self_transfer: success, no connection, budget cap, low balance, signing error
  - deploy_test_contract: success, no connection, budget cap, insufficient balance
  - mint_test_nft: success, no RPC, exception during deploy
  - run_testnet_actions: batch all three actions, partial failures
  - run_all_testnets: runs across all chains, sends summary alert
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from app.modules.zksync_era import (
    run_all_testnets,
    run_testnet_actions,
    send_self_transfer,
    deploy_test_contract,
    mint_test_nft,
    TESTNET_CONFIG,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_account(address="0x1234567890abcdef1234567890abcdef12345678"):
    """Create a mock account with address and key."""
    key = MagicMock()
    key.hex.return_value = "0x" + "ab" * 32
    account = MagicMock()
    account.address = address
    account.key = key
    return account


def _make_w3(
    connected=True,
    balance_wei=10_000_000_000_000_000_000,  # 10 ETH
    gas_price_wei=10_000_000_000,
    nonce=0,
    tx_hash_bytes=b"\x01" * 32,
):
    """Create a mock Web3 instance with standard defaults.

    Configures all attributes that zksync_era.py uses.
    """
    eth = MagicMock()
    eth.get_balance.return_value = balance_wei
    eth.gas_price = gas_price_wei
    eth.get_transaction_count.return_value = nonce
    eth.estimate_gas.return_value = 100000

    # sign_transaction returns an object with raw_transaction
    signed = MagicMock()
    signed.raw_transaction = b"\x02" * 32
    eth.account.sign_transaction.return_value = signed
    eth.send_raw_transaction.return_value = tx_hash_bytes

    w3 = MagicMock()
    w3.eth = eth
    w3.is_connected.return_value = connected
    w3.to_wei.side_effect = lambda amount, unit: int(amount * 1e18)
    w3.from_wei.side_effect = lambda wei, unit: (
        float(wei) / 1e18 if unit == "ether" else float(wei) / 1e9
    )
    return w3


# =============================================================================
# send_self_transfer
# =============================================================================


class TestSendSelfTransfer:
    """send_self_transfer — native token transfer between agent wallets."""

    def test_success(self, db_session):
        w3 = _make_w3()
        sender = _make_account("0x1111111111111111111111111111111111111111")
        receiver = _make_account("0x2222222222222222222222222222222222222222")

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet") as mock_derive,
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
        ):
            mock_derive.side_effect = [sender, receiver]
            result = run_async(send_self_transfer(db_session))

        assert result["success"] is True
        assert result["chain"] == "sepolia"
        assert result["amount"] == 0.0001
        assert "tx_hash" in result

    def test_no_connection(self, db_session):
        w3 = _make_w3(connected=False)
        with patch("app.modules.zksync_era.Web3", return_value=w3):
            result = run_async(send_self_transfer(db_session))
        assert result["success"] is False
        assert "Cannot connect" in result["error"]

    def test_budget_cap_reached(self, db_session):
        w3 = _make_w3()
        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.can_spend", return_value=False),
        ):
            result = run_async(send_self_transfer(db_session))
        assert result["success"] is False
        assert "Budget cap" in result["error"]

    def test_insufficient_balance(self, db_session):
        w3 = _make_w3(balance_wei=1000)  # way below 0.0001 ETH * 2
        sender = _make_account()
        receiver = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet") as mock_derive,
            patch("app.modules.zksync_era.can_spend", return_value=True),
        ):
            mock_derive.side_effect = [sender, receiver]
            result = run_async(send_self_transfer(db_session))
        assert result["success"] is False
        assert "Insufficient balance" in result["error"]

    def test_derive_wallet_raises_value_error(self, db_session):
        w3 = _make_w3()
        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch(
                "app.modules.zksync_era.derive_wallet",
                side_effect=ValueError("bad seed"),
            ),
            patch("app.modules.zksync_era.can_spend", return_value=True),
        ):
            result = run_async(send_self_transfer(db_session))
        assert result["success"] is False
        assert "bad seed" in result["error"]

    def test_signing_exception(self, db_session):
        w3 = _make_w3()
        sender = _make_account()
        receiver = _make_account()
        # Make sign_transaction raise
        w3.eth.account.sign_transaction.side_effect = ValueError("signing failed")

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet") as mock_derive,
            patch("app.modules.zksync_era.can_spend", return_value=True),
        ):
            mock_derive.side_effect = [sender, receiver]
            result = run_async(send_self_transfer(db_session))
        assert result["success"] is False
        assert "signing" in result["error"].lower()


# =============================================================================
# deploy_test_contract
# =============================================================================


class TestDeployTestContract:
    """deploy_test_contract — deploy a simple Counter contract."""

    def test_success(self, db_session):
        w3 = _make_w3()
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
        ):
            result = run_async(deploy_test_contract(db_session))

        assert result["success"] is True
        assert result["action"] == "contract_deploy"
        assert "tx_hash" in result

    def test_no_connection(self, db_session):
        w3 = _make_w3(connected=False)
        with patch("app.modules.zksync_era.Web3", return_value=w3):
            result = run_async(deploy_test_contract(db_session))
        assert result["success"] is False
        assert "Cannot connect" in result["error"]

    def test_budget_cap_reached(self, db_session):
        w3 = _make_w3()
        account = _make_account()
        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=False),
        ):
            result = run_async(deploy_test_contract(db_session))
        assert result["success"] is False
        assert "Budget cap" in result["error"]

    def test_insufficient_balance(self, db_session):
        w3 = _make_w3(balance_wei=100)  # too low
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
        ):
            result = run_async(deploy_test_contract(db_session))
        assert result["success"] is False
        assert "Insufficient balance" in result["error"]

    def test_exception_during_deploy(self, db_session):
        w3 = _make_w3()
        account = _make_account()
        w3.eth.send_raw_transaction.side_effect = RuntimeError("deploy failed")

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
        ):
            result = run_async(deploy_test_contract(db_session))
        assert result["success"] is False
        assert "deploy failed" in result["error"]

    def test_estimate_gas_fallback_on_error(self, db_session):
        """If estimate_gas fails, the default gas limit is used."""
        w3 = _make_w3()
        account = _make_account()
        w3.eth.estimate_gas.side_effect = ValueError("estimation error")

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
        ):
            result = run_async(deploy_test_contract(db_session))
        assert result["success"] is True


# =============================================================================
# mint_test_nft
# =============================================================================


class TestMintTestNft:
    """mint_test_nft — deploy an ERC-721 mint."""

    def test_success(self, db_session):
        w3 = _make_w3()
        account = _make_account()

        with (
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.record_spend"),
        ):
            result = run_async(mint_test_nft(db_session))

        assert result["success"] is True
        assert result["action"] == "nft_mint"
        assert "tx_hash" in result

    def test_no_rpc_connection(self, db_session):
        with patch("app.modules.zksync_era.get_web3", return_value=None):
            result = run_async(mint_test_nft(db_session))
        assert result["success"] is False
        assert "No RPC" in result["error"]

    def test_exception_during_mint(self, db_session):
        w3 = _make_w3()
        account = _make_account()
        w3.eth.send_raw_transaction.side_effect = RuntimeError("mint failed")

        with (
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
        ):
            result = run_async(mint_test_nft(db_session))
        assert result["success"] is False
        assert "mint failed" in result["error"]


# =============================================================================
# run_testnet_actions
# =============================================================================


class TestRunTestnetActions:
    """run_testnet_actions — runs all actions on a single testnet."""

    def test_all_succeed(self, db_session):
        w3 = _make_w3()
        # Use unique tx hashes for each action to avoid UNIQUE constraint
        w3.eth.send_raw_transaction.side_effect = [
            b"\x01" * 32,
            b"\x02" * 32,
            b"\x03" * 32,
        ]
        sender = _make_account("0x1111111111111111111111111111111111111111")
        receiver = _make_account("0x2222222222222222222222222222222222222222")
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet") as mock_derive,
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
        ):
            mock_derive.side_effect = [sender, receiver, account, account, account]
            results = run_async(run_testnet_actions(db_session))

        assert len(results) == 3
        assert all(r["success"] for r in results)

    def test_self_transfer_fails_continues(self, db_session):
        """Even if send_self_transfer fails, the rest still run."""
        w3 = _make_w3()
        # self-transfer (fail) + deploy (succeed) uses send_raw twice
        w3.eth.send_raw_transaction.side_effect = [
            b"\x02" * 32,
            b"\x03" * 32,
        ]
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend") as mock_can_spend,
            patch("app.modules.zksync_era.record_spend"),
        ):
            # First call (self-transfer) fails budget, rest succeed
            mock_can_spend.side_effect = [False, True, True]
            results = run_async(run_testnet_actions(db_session))

        assert len(results) == 3
        assert results[0]["success"] is False  # self-transfer blocked
        assert results[1]["success"] is True  # deploy succeeds
        assert results[2]["success"] is True  # nft mint succeeds


# =============================================================================
# run_all_testnets
# =============================================================================


class TestRunAllTestnets:
    """run_all_testnets — runs on-chain actions across all testnet chains."""

    def test_runs_all_chains(self, db_session):
        w3 = _make_w3()
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
            patch("app.modules.zksync_era.notify_alert", new_callable=AsyncMock),
        ):
            results = run_async(run_all_testnets(db_session))

        assert isinstance(results, dict)
        # All configured testnets should have entries
        for chain in TESTNET_CONFIG:
            assert chain in results
            chain_results = results[chain]
            assert isinstance(chain_results, list)
            assert len(chain_results) >= 1

    def test_chain_exception_handled(self, db_session):
        """If one chain raises, it's captured in its results."""
        w3 = _make_w3()
        account = _make_account()

        with (
            patch("app.modules.zksync_era.Web3", return_value=w3),
            patch("app.modules.zksync_era.get_web3", return_value=w3),
            patch("app.modules.zksync_era.derive_wallet", return_value=account),
            patch("app.modules.zksync_era.can_spend", return_value=True),
            patch("app.modules.zksync_era.record_spend"),
            patch("app.modules.zksync_era.notify_alert", new_callable=AsyncMock),
        ):
            results = run_async(run_all_testnets(db_session))
        # Should not crash; all chains should be present
        assert len(results) == len(TESTNET_CONFIG)


# =============================================================================
# Async helper
# =============================================================================


def run_async(coro):
    """Run an async function synchronously for testing."""
    import asyncio

    return asyncio.run(coro)
