"""
DeFi Execution Module — swap tokens via 1inch Aggregator API
and execute CEX-CEX arbitrage via ccxt.

This module enables the agent to:
  1. Swap tokens on-chain via 1inch Aggregator API
  2. Execute CEX-CEX arbitrage by buying on the cheap exchange
     and selling on the expensive exchange using ccxt trade API keys

Supported chains: ethereum, base, arbitrum, polygon, bsc
Supported CEXes: Binance, Kraken, Bybit (via trade API keys)
Capital: EUR 100+ available

Security:
  - All swaps go through budget guard (daily cap + stop-loss)
  - Private keys never leave memory
  - Transactions are logged in the Transaction table
  - Exchange trade keys are separate from read-only keys
  - Market orders only (no limit order risk)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import ccxt
import httpx
from sqlalchemy.orm import Session
from web3 import Web3

from app.config import settings
from app.db.models import Transaction
from app.modules.rpc import get_web3
from app.modules.wallet import CHAIN_CONFIGS, derive_wallet
from app.utils.budget import can_spend, record_spend
from app.utils.notify import notify_alert
from app.utils.pnl import record_revenue

logger = logging.getLogger("buckgen.defi")

# =============================================================================
# Constants
# =============================================================================

# 1inch API v5 endpoints per chain
INCH_API_V5 = {
    "ethereum": "https://api.1inch.dev/swap/v5.2/1",
    "base": "https://api.1inch.dev/swap/v5.2/8453",
    "arbitrum": "https://api.1inch.dev/swap/v5.2/42161",
    "polygon": "https://api.1inch.dev/swap/v5.2/137",
    "bsc": "https://api.1inch.dev/swap/v5.2/56",
}

# Common token addresses (USDC on each chain)
USDC_ADDRESSES: dict[str, str] = {
    "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "polygon": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "bsc": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
}

# Native token wrapped address (WETH, WBNB, etc.)
WNATIVE: dict[str, str] = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "polygon": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
}

SLIPPAGE = 0.5  # 0.5% default slippage


# =============================================================================
# Data types
# =============================================================================


@dataclass
class SwapQuote:
    """Quote from 1inch for a token swap."""

    from_token: str
    to_token: str
    from_amount: str  # raw wei
    to_amount: str  # raw wei
    estimated_gas: int
    tx_data: dict  # raw transaction data from 1inch
    price_impact: float = 0.0


@dataclass
class SwapResult:
    """Result of an executed swap."""

    success: bool
    tx_hash: str
    from_token: str
    to_token: str
    from_amount: float
    to_amount: float
    gas_used_wei: int = 0
    error: str = ""


# =============================================================================
# 1inch API client
# =============================================================================


async def get_swap_quote(
    chain: str,
    from_token: str,  # "0x..." or "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE" for native
    to_token: str,
    amount_wei: str,  # amount in wei as string
    slippage: float = SLIPPAGE,
) -> SwapQuote | None:
    """
    Get a swap quote from the 1inch API.

    Args:
        chain: Chain name
        from_token: Token address to swap from
        to_token: Token address to swap to
        amount_wei: Amount of from_token in wei
        slippage: Max slippage percentage

    Returns:
        SwapQuote with tx_data, or None on failure.
    """
    base_url = INCH_API_V5.get(chain)
    if not base_url:
        logger.warning("[defi] Unsupported chain: %s", chain)
        return None

    # Native ETH address for 1inch
    native = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

    params = {
        "src": from_token,
        "dst": to_token,
        "amount": amount_wei,
        "slippage": str(slippage),
        "from": native,  # placeholder — 1inch needs a from address for quote
        "disableEstimate": "false",
    }

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            resp = await client.get(
                f"{base_url}/quote",
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(
                    "[defi] 1inch quote error (HTTP %d): %s",
                    resp.status_code,
                    resp.text[:120],
                )
                return None

            data = resp.json()
            return SwapQuote(
                from_token=from_token,
                to_token=to_token,
                from_amount=amount_wei,
                to_amount=str(data.get("toAmount", "0")),
                estimated_gas=data.get("estimatedGas", 0),
                tx_data=data,
                price_impact=float(data.get("priceImpact", 0)),
            )

    except Exception as exc:
        logger.warning("[defi] 1inch quote exception: %s", exc)
        return None


async def build_swap_transaction(
    chain: str,
    from_token: str,
    to_token: str,
    amount_wei: str,
    wallet_address: str,
    slippage: float = SLIPPAGE,
) -> dict | None:
    """
    Build a swap transaction using the 1inch API.

    Returns the raw transaction dict ready for signing:
        {to, data, value, gasPrice, gas, chainId}
    """
    base_url = INCH_API_V5.get(chain)
    if not base_url:
        return None

    params = {
        "src": from_token,
        "dst": to_token,
        "amount": amount_wei,
        "from": wallet_address,
        "slippage": str(slippage),
    }

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            resp = await client.get(
                f"{base_url}/swap",
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(
                    "[defi] 1inch swap build error (HTTP %d): %s",
                    resp.status_code,
                    resp.text[:120],
                )
                return None

            tx_data = resp.json().get("tx", {})
            return tx_data

    except Exception as exc:
        logger.warning("[defi] 1inch swap build exception: %s", exc)
        return None


# =============================================================================
# Execution
# =============================================================================


async def execute_swap(
    db: Session,
    chain: str,
    wallet_index: int = 0,
    from_token: str = "",
    to_token: str = "",
    amount_wei: str = "",
    amount_eur_estimate: float = 0.0,
    confirm: bool = False,
) -> SwapResult:
    """
    Execute a token swap via 1inch using the agent's HD wallet.

    Checks budget, builds the tx, and — ONLY if ``confirm=True`` — signs
    and broadcasts it. With ``confirm=False`` (the default) it returns a
    non-submitted preview and never touches the chain.

    Args:
        db: Database session
        chain: Chain name
        wallet_index: HD wallet index to use
        from_token: Token address to sell
        to_token: Token address to buy
        amount_wei: Amount to swap (in wei of from_token)
        amount_eur_estimate: Estimated EUR cost for budget check

    Returns:
        SwapResult with tx_hash or error.
    """
    # --- Budget check ---
    if amount_eur_estimate > 0 and not can_spend(db, amount_eur_estimate):
        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=0,
            to_amount=0,
            error="Budget cap reached",
        )

    # --- Get wallet ---
    chain_config = CHAIN_CONFIGS.get(chain, CHAIN_CONFIGS["ethereum"])
    chain_id = chain_config["chain_id"]

    try:
        account = derive_wallet(wallet_index, chain)
    except ValueError as exc:
        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=0,
            to_amount=0,
            error=str(exc),
        )

    wallet_address = account.address
    private_key = account.key

    # --- Build swap tx ---
    tx_data = await build_swap_transaction(
        chain=chain,
        from_token=from_token,
        to_token=to_token,
        amount_wei=amount_wei,
        wallet_address=wallet_address,
    )

    if not tx_data:
        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=0,
            to_amount=0,
            error="Failed to build swap transaction",
        )

    # --- Get Web3 connection ---
    w3 = get_web3(chain)
    if not w3:
        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=0,
            to_amount=0,
            error="No RPC connection",
        )

    # --- Dry-run gate: never broadcast without explicit confirm ---
    if not confirm:
        logger.info(
            "DRY-RUN swap on %s: %s -> %s amount=%s (set confirm=true to submit)",
            chain,
            from_token,
            to_token,
            amount_wei,
        )
        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=int(amount_wei or 0),
            to_amount=0,
            error="dry_run: not submitted (confirm=false)",
        )

    # --- Prepare and sign transaction ---
    try:
        tx = {
            "to": Web3.to_checksum_address(tx_data["to"]),
            "data": tx_data["data"],
            "value": int(tx_data.get("value", "0")),
            "gas": int(tx_data.get("gas", 200000)),
            "gasPrice": int(tx_data.get("gasPrice", w3.eth.gas_price)),
            "chainId": chain_id,
            "nonce": w3.eth.get_transaction_count(wallet_address),
        }

        # Estimate gas if possible
        try:
            estimated_gas = w3.eth.estimate_gas(tx)
            tx["gas"] = estimated_gas
        except Exception:
            tx["gas"] = int(tx["gas"] * 1.2)  # 20% buffer

        # --- SIGN ---
        signed = w3.eth.account.sign_transaction(tx, private_key)

        # --- SUBMIT ---
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        # --- Log transaction ---
        tx_record = Transaction(
            wallet_id=wallet_index,
            chain=chain,
            tx_hash=tx_hash_hex,
            tx_type="swap",
            amount_wei=amount_wei,
            gas_used_wei=str(tx["gas"]),
            status="pending",
            memo=f"1inch swap on {chain}",
        )
        db.add(tx_record)

        # Record spend
        if amount_eur_estimate > 0:
            record_spend(db, amount_eur_estimate, "defi", f"swap on {chain}")

        db.commit()

        logger.info(
            "[defi] Swap submitted: %s | tx=%s",
            chain,
            tx_hash_hex[:20],
        )

        # Calculate approximate output
        to_amount_eth = 0
        try:
            to_amount_raw = int(tx_data.get("toAmount", "0"))
            to_amount_eth = float(w3.from_wei(to_amount_raw, "ether"))
        except Exception:
            pass

        return SwapResult(
            success=True,
            tx_hash=tx_hash_hex,
            from_token=from_token,
            to_token=to_token,
            from_amount=float(w3.from_wei(int(amount_wei), "ether")),
            to_amount=to_amount_eth,
            gas_used_wei=tx["gas"],
        )

    except Exception as exc:
        error_msg = str(exc)[:200]
        logger.error("[defi] Swap execution failed: %s", error_msg)

        # Log failed transaction
        tx_record = Transaction(
            wallet_id=wallet_index,
            chain=chain,
            tx_type="swap",
            amount_wei=amount_wei,
            status="failed",
            memo=f"Failed: {error_msg[:100]}",
        )
        db.add(tx_record)
        db.commit()

        return SwapResult(
            success=False,
            tx_hash="",
            from_token=from_token,
            to_token=to_token,
            from_amount=0,
            to_amount=0,
            error=error_msg,
        )


# =============================================================================
# CEX-CEX Arbitrage execution via ccxt
# =============================================================================

# Cached trade exchange instances
_trade_exchanges: dict[str, ccxt.Exchange] = {}


def _get_trade_exchange(name: str) -> ccxt.Exchange | None:
    """Get (or create) a cached ccxt exchange instance with trade API keys."""
    if name in _trade_exchanges:
        return _trade_exchanges[name]

    try:
        exchange_class = getattr(ccxt, name.lower(), None)
        if exchange_class is None:
            logger.warning("[defi] Unknown exchange: %s", name)
            return None

        # Map exchange names to trade key config settings
        trade_key_map = {
            "binance": ("BINANCE_TRADE_KEY", "BINANCE_TRADE_SECRET"),
            "kraken": ("KRAKEN_TRADE_KEY", "KRAKEN_TRADE_SECRET"),
            "bybit": ("BYBIT_TRADE_KEY", "BYBIT_TRADE_SECRET"),
        }

        key_name, secret_name = trade_key_map.get(name, (None, None))
        api_key = getattr(settings, key_name, "") if key_name else ""
        api_secret = getattr(settings, secret_name, "") if secret_name else ""

        if not api_key or not api_secret:
            logger.warning(
                "[defi] No trade API keys configured for %s (set %s and %s env vars)",
                name,
                key_name,
                secret_name,
            )
            return None

        config = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "spot"},
        }

        exchange = exchange_class(config)
        exchange.load_markets()
        _trade_exchanges[name] = exchange
        logger.info(
            "[defi] Trade exchange %s connected (%d markets)",
            name,
            len(exchange.markets),
        )
        return exchange

    except Exception as exc:
        logger.warning("[defi] Failed to init trade exchange %s: %s", name, exc)
        return None


async def execute_cex_arbitrage(
    db: Session,
    opportunity: dict,
    capital_eur: float = 500.0,
    confirm: bool = False,
) -> dict:
    """
    Execute a CEX-CEX arbitrage opportunity by placing market orders
    on both exchanges via ccxt trade API keys.

    Flow:
      1. Get trade-capable exchange instances for both sides
      2. Check USDT balance on the buy exchange
      3. Place market BUY order on cheap exchange
      4. Place market SELL order on expensive exchange
      5. Record trade + revenue in P&L

    Args:
        db: Database session
        opportunity: ArbitrageOpportunity dict from prices module
        capital_eur: Maximum capital to deploy (default: EUR 500)

    Returns:
        Execution result dict.
    """
    buy_exchange_name = opportunity.get("buy_at", "")
    sell_exchange_name = opportunity.get("sell_at", "")
    pair = opportunity.get("pair", "")
    buy_price = opportunity.get("buy_price", 0)
    sell_price = opportunity.get("sell_price", 0)
    net_profit_pct = opportunity.get("net_profit_pct", 0)

    logger.info(
        "[defi] CEX arb: Buy %s on %s @ %.4f -> Sell on %s @ %.4f (net %.2f%%)",
        pair,
        buy_exchange_name,
        buy_price,
        sell_exchange_name,
        sell_price,
        net_profit_pct,
    )

    # Skip CoinGecko (reference) and DEX-only opportunities
    if "CoinGecko" in buy_exchange_name or "CoinGecko" in sell_exchange_name:
        return {
            "success": False,
            "error": "Cannot execute against CoinGecko reference price",
        }

    # Get trade exchange instances
    buy_exchange = _get_trade_exchange(buy_exchange_name)
    sell_exchange = _get_trade_exchange(sell_exchange_name)

    if not buy_exchange:
        return {"success": False, "error": f"No trade keys for {buy_exchange_name}"}
    if not sell_exchange:
        return {"success": False, "error": f"No trade keys for {sell_exchange_name}"}

    # Budget check: estimate max loss at 2% of capital
    budget_needed = capital_eur * 0.02
    if not can_spend(db, budget_needed):
        return {"success": False, "error": "Budget cap reached"}

    try:
        # --- Step 1: Check balances on buy exchange ---
        # We need the quote currency (e.g., USDT) to buy
        quote = pair.split("/")[1]  # e.g., "USDT"
        balance = buy_exchange.fetch_balance()
        quote_free = balance.get(quote, {}).get("free", 0)

        # Calculate trade size: use min(capital / buy_price, available balance * 0.95)
        max_quote_capital = capital_eur  # assume ~1:1 EUR/USD for quote
        trade_size_quote = min(max_quote_capital, quote_free * 0.95)

        if trade_size_quote < 10:  # minimum $10 trade
            return {
                "success": False,
                "error": f"Insufficient {quote} balance on {buy_exchange_name}: "
                f"{quote_free:.2f} (need >= $10)",
            }

        # Calculate base amount (what we're buying)
        trade_size_base = trade_size_quote / buy_price

        logger.info(
            "[defi] Trading %.4f %s (%.2f %s) on %s",
            trade_size_base,
            pair,
            trade_size_quote,
            quote,
            buy_exchange_name,
        )

        # --- Dry-run gate: never place live orders without explicit confirm ---
        if not confirm:
            logger.info(
                "DRY-RUN CEX arb: would BUY %.4f %s on %s / SELL on %s "
                "(set confirm=true to place orders)",
                trade_size_base,
                pair,
                buy_exchange_name,
                sell_exchange_name,
            )
            return {
                "success": False,
                "error": "dry_run: not submitted (confirm=false)",
                "would_trade_base": trade_size_base,
                "would_trade_quote": trade_size_quote,
                "pair": pair,
            }

        # Record the spend for budget tracking
        record_spend(
            db,
            budget_needed,
            "defi",
            f"CEX arb {pair}: {buy_exchange_name}->{sell_exchange_name}",
        )

        # --- Step 2: Place market BUY order on cheap exchange ---
        buy_order = buy_exchange.create_market_buy_order(pair, trade_size_base)
        buy_filled = float(buy_order.get("filled", 0))
        buy_cost = float(buy_order.get("cost", 0))  # how much quote was spent
        logger.info(
            "[defi] BUY order filled: %f %s for %f %s",
            buy_filled,
            pair,
            buy_cost,
            quote,
        )

        if buy_filled <= 0:
            return {"success": False, "error": "Buy order not filled"}

        # --- Step 3: Place market SELL order on expensive exchange ---
        sell_order = sell_exchange.create_market_sell_order(
            pair, buy_filled * 0.999
        )  # 0.1% buffer for rounding
        sell_filled = float(sell_order.get("filled", 0))
        sell_cost = float(sell_order.get("cost", 0))  # how much quote received

        logger.info(
            "[defi] SELL order filled: %f %s for %f %s",
            sell_filled,
            pair,
            sell_cost,
            quote,
        )

        if sell_filled <= 0:
            return {"success": False, "error": "Sell order not filled"}

        # --- Step 4: Calculate profit ---
        gross_profit_quote = sell_cost - buy_cost
        # Account for taker fees (0.1% per leg typical)
        fee_pct = 0.001  # 0.1% taker fee
        fee_cost = buy_cost * fee_pct + sell_cost * fee_pct
        net_profit_quote = gross_profit_quote - fee_cost
        net_profit_eur = net_profit_quote  # ~1:1 USD/EUR

        # Skip if not profitable (slippage ate the spread)
        if net_profit_eur <= 0:
            logger.warning(
                "[defi] CEX arb was not profitable after execution: "
                "gross=%.4f fees=%.4f net=%.4f",
                gross_profit_quote,
                fee_cost,
                net_profit_quote,
            )
            return {
                "success": True,
                "warning": "Trade executed but not profitable",
                "buy_exchange": buy_exchange_name,
                "sell_exchange": sell_exchange_name,
                "pair": pair,
                "buy_filled": buy_filled,
                "sell_filled": sell_filled,
                "gross_profit_eur": round(gross_profit_quote, 2),
                "net_profit_eur": round(net_profit_quote, 2),
            }

        # --- Step 5: Record revenue ---
        record_revenue(
            db,
            "arbitrage",
            net_profit_eur,
            source=f"{pair} {buy_exchange_name}->{sell_exchange_name}",
            memo=f"CEX arb: bought {buy_filled:.4f} on {buy_exchange_name} @ {buy_price:.2f}, "
            f"sold on {sell_exchange_name} @ {sell_price:.2f}",
        )

        # Log transaction
        tx_record = Transaction(
            wallet_id=0,
            chain="cex",
            tx_type="cex_arb",
            amount_wei=str(int(trade_size_base * 1e8)),
            status="confirmed",
            memo=f"CEX arb {pair}: {buy_exchange_name}->{sell_exchange_name}",
        )
        db.add(tx_record)
        db.commit()

        await notify_alert(
            f"CEX Arbitrage Executed: EUR {net_profit_eur:.2f}",
            f"Pair: {pair}\n"
            f"Buy: {buy_exchange_name} @ {buy_price:.2f}\n"
            f"Sell: {sell_exchange_name} @ {sell_price:.2f}\n"
            f"Volume: {buy_filled:.4f}\n"
            f"Net Profit: EUR {net_profit_eur:.2f}",
        )

        return {
            "success": True,
            "buy_exchange": buy_exchange_name,
            "sell_exchange": sell_exchange_name,
            "pair": pair,
            "buy_filled": buy_filled,
            "sell_filled": sell_filled,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "gross_profit_eur": round(gross_profit_quote, 2),
            "net_profit_eur": round(net_profit_eur, 2),
        }

    except ccxt.InsufficientFunds as exc:
        logger.warning("[defi] Insufficient funds for CEX arb: %s", exc)
        return {"success": False, "error": f"Insufficient funds: {exc}"[:120]}
    except ccxt.NetworkError as exc:
        logger.warning("[defi] CEX arb network error: %s", exc)
        return {"success": False, "error": f"Network error: {exc}"[:120]}
    except Exception as exc:
        logger.error("[defi] CEX arb execution failed: %s", exc)
        return {"success": False, "error": str(exc)[:200]}


# =============================================================================
# Arbitrage execution helper — routes to CEX or on-chain
# =============================================================================


async def execute_arbitrage(
    db: Session,
    chain: str,
    opportunity: dict,
    capital_eur: float = 500.0,
    confirm: bool = False,
) -> dict:
    """
    Execute an arbitrage opportunity — routes to the appropriate executor.

    Broadcasts only when ``confirm=True``; otherwise returns a preview.

    For CEX-CEX opportunities (buy_at/sell_at are exchange names like "binance"),
    uses ccxt trade API keys to place market orders.

    For on-chain opportunities (chain specified), uses 1inch swap.

    Args:
        db: Database session
        chain: Chain to execute on (for on-chain arb)
        opportunity: ArbitrageOpportunity dict from prices module
        capital_eur: Capital to deploy

    Returns:
        Execution result dict.
    """
    buy_at = opportunity.get("buy_at", "").lower()
    sell_at = opportunity.get("sell_at", "").lower()

    # Known CEX exchange names
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

    is_cex_cex = buy_at in cex_names and sell_at in cex_names

    if is_cex_cex:
        return await execute_cex_arbitrage(db, opportunity, capital_eur, confirm=confirm)

    # Fallback: on-chain swap via 1inch
    logger.info("[defi] On-chain arb: swapping on %s", chain)
    chain_config = CHAIN_CONFIGS.get(chain, CHAIN_CONFIGS["ethereum"])
    symbol = chain_config["symbol"]
    native_address = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

    # Calculate trade amount
    trade_amount_eth = (capital_eur / 2) / 2000
    w3 = get_web3(chain)
    if w3:
        try:
            balance = w3.eth.get_balance(derive_wallet(0, chain).address)
            max_trade = min(
                int(Decimal(str(trade_amount_eth)) * Decimal(10**18)),
                balance // 2,
            )
        except Exception:
            max_trade = int(Decimal(str(trade_amount_eth)) * Decimal(10**18))
    else:
        max_trade = int(Decimal(str(trade_amount_eth)) * Decimal(10**18))

    if max_trade <= 0:
        return {"success": False, "error": "Insufficient balance"}

    usdc = USDC_ADDRESSES.get(chain, "")
    if not usdc:
        return {"success": False, "error": f"No USDC address for {chain}"}

    result = await execute_swap(
        db=db,
        chain=chain,
        wallet_index=0,
        from_token=native_address,
        to_token=usdc,
        amount_wei=str(max_trade),
        amount_eur_estimate=capital_eur * 0.02,
        confirm=confirm,
    )

    if result.success:
        await notify_alert(
            f"On-chain Arb Executed on {chain}",
            f"Swapped {result.from_amount:.6f} {symbol} -> USDC\n"
            f"Tx: {result.tx_hash[:20]}...",
        )

    return {
        "success": result.success,
        "tx_hash": result.tx_hash,
        "chain": chain,
        "from_amount": result.from_amount,
        "error": result.error,
    }
