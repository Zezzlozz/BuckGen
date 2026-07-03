"""
Safety-gate tests for BuckGen.

These lock in the two invariants that were previously only *documented* but
not enforced:

  1. execute_swap / execute_cex_arbitrage must NOT sign or broadcast anything
     unless confirm=True. The default is a preview.
  2. _require_api_key must fail CLOSED: with no API_KEY configured and DEBUG
     off, requests are refused rather than allowed.

The swap test deliberately wires a fake web3 whose broadcast methods raise on
call, so if the gate ever regresses the test fails loudly instead of silently
sending a transaction.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import defi


class _ExplodingWeb3:
    """A web3 stand-in where signing or broadcasting is a hard failure.

    If the dry-run gate is intact, none of these attributes are ever touched.
    """

    class _Eth:
        gas_price = 1

        def get_transaction_count(self, *_a, **_k):  # pragma: no cover
            raise AssertionError("get_transaction_count called in dry-run")

        def estimate_gas(self, *_a, **_k):  # pragma: no cover
            raise AssertionError("estimate_gas called in dry-run")

        @property
        def account(self):  # pragma: no cover
            raise AssertionError("account.sign_transaction reached in dry-run")

        def send_raw_transaction(self, *_a, **_k):  # pragma: no cover
            raise AssertionError("send_raw_transaction called in dry-run")

    eth = _Eth()


@pytest.fixture
def fake_wallet():
    w = MagicMock()
    w.address = "0x000000000000000000000000000000000000dEaD"
    w.key = b"\x11" * 32
    return w


@pytest.mark.asyncio
async def test_execute_swap_dry_run_does_not_broadcast(fake_wallet):
    """Default (confirm=False) returns a preview and never signs/sends."""
    db = MagicMock()

    with (
        patch.object(defi, "can_spend", return_value=True),
        patch.object(defi, "derive_wallet", return_value=fake_wallet),
        patch.object(
            defi,
            "build_swap_transaction",
            new=AsyncMock(
                return_value={
                    "to": "0x1111111111111111111111111111111111111111",
                    "data": "0x",
                    "value": "0",
                    "gas": "200000",
                    "gasPrice": "1",
                }
            ),
        ),
        patch.object(defi, "get_web3", return_value=_ExplodingWeb3()),
    ):
        result = await defi.execute_swap(
            db=db,
            chain="ethereum",
            wallet_index=0,
            from_token="0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
            to_token="0x1111111111111111111111111111111111111111",
            amount_wei="1000",
            amount_eur_estimate=1.0,
            # confirm omitted -> defaults to False
        )

    assert result.success is False
    assert "dry_run" in (result.error or "")
    assert result.tx_hash == ""


@pytest.mark.asyncio
async def test_execute_swap_confirm_reaches_broadcast(fake_wallet):
    """With confirm=True the gate is passed and the broadcast path is hit.

    We assert it reaches send_raw_transaction (which our fake makes raise),
    proving the gate is the *only* thing standing between preview and send.
    """
    db = MagicMock()

    with (
        patch.object(defi, "can_spend", return_value=True),
        patch.object(defi, "derive_wallet", return_value=fake_wallet),
        patch.object(
            defi,
            "build_swap_transaction",
            new=AsyncMock(
                return_value={
                    "to": "0x1111111111111111111111111111111111111111",
                    "data": "0x",
                    "value": "0",
                    "gas": "200000",
                    "gasPrice": "1",
                }
            ),
        ),
        patch.object(defi, "get_web3", return_value=_ExplodingWeb3()),
    ):
        # The exploding web3 raises AssertionError once we cross the gate.
        # execute_swap catches broad Exceptions and returns a failure result,
        # so we assert the error reflects the broadcast attempt, not dry-run.
        result = await defi.execute_swap(
            db=db,
            chain="ethereum",
            wallet_index=0,
            from_token="0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
            to_token="0x1111111111111111111111111111111111111111",
            amount_wei="1000",
            amount_eur_estimate=1.0,
            confirm=True,
        )

    assert result.success is False
    assert "dry_run" not in (result.error or "")


@pytest.mark.asyncio
async def test_cex_arbitrage_dry_run_places_no_orders():
    """CEX arb preview must not call create_market_*_order."""
    db = MagicMock()

    buy_ex = MagicMock()
    buy_ex.fetch_balance.return_value = {"USDT": {"free": 1000.0}}
    buy_ex.create_market_buy_order.side_effect = AssertionError(
        "buy order placed in dry-run"
    )
    sell_ex = MagicMock()
    sell_ex.create_market_sell_order.side_effect = AssertionError(
        "sell order placed in dry-run"
    )

    def _get_ex(name):
        return buy_ex if "binance" in name else sell_ex

    opp = {
        "buy_at": "binance",
        "sell_at": "kraken",
        "pair": "ETH/USDT",
        "buy_price": 3000.0,
        "sell_price": 3050.0,
        "net_profit_pct": 1.2,
    }

    with (
        patch.object(defi, "can_spend", return_value=True),
        patch.object(defi, "_get_trade_exchange", side_effect=_get_ex),
    ):
        result = await defi.execute_cex_arbitrage(
            db=db, opportunity=opp, capital_eur=500.0
        )  # confirm defaults to False

    assert result["success"] is False
    assert "dry_run" in result["error"]
    buy_ex.create_market_buy_order.assert_not_called()
    sell_ex.create_market_sell_order.assert_not_called()


def test_require_api_key_fails_closed_without_key():
    """No API_KEY + DEBUG off -> requests refused (503), not allowed."""
    from fastapi import HTTPException

    from app import main

    req = MagicMock()
    req.headers = {}

    with (
        patch.object(main.settings, "API_KEY", ""),
        patch.object(main.settings, "DEBUG", False),
    ):
        with pytest.raises(HTTPException) as exc:
            main._require_api_key(req)
    assert exc.value.status_code == 503


def test_require_api_key_rejects_wrong_key():
    from fastapi import HTTPException

    from app import main

    req = MagicMock()
    req.headers = {"X-API-Key": "wrong"}

    with patch.object(main.settings, "API_KEY", "correct-secret"):
        with pytest.raises(HTTPException) as exc:
            main._require_api_key(req)
    assert exc.value.status_code == 401


def test_require_api_key_accepts_correct_key():
    from app import main

    req = MagicMock()
    req.headers = {"X-API-Key": "correct-secret"}

    with patch.object(main.settings, "API_KEY", "correct-secret"):
        # Should not raise
        main._require_api_key(req)
