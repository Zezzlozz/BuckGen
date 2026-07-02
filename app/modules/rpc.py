"""
Multi-chain RPC client — balance checks, gas estimation, transaction building.

Supported chains: ethereum, base, arbitrum, polygon, bsc
All use publicnode.com free endpoints by default (configurable in .env).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
from web3 import Web3
from web3.types import Wei
from web3.exceptions import Web3RPCError, TimeExhausted

from app.config import settings
from app.modules.wallet import CHAIN_CONFIGS

logger = logging.getLogger("buckgen.rpc")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class ChainStatus:
    """Health + gas info for a single chain."""

    chain: str
    connected: bool
    block_number: int = 0
    gas_price_gwei: float = 0.0
    error: str = ""


@dataclass
class WalletBalance:
    """Balance info for a wallet on a specific chain."""

    address: str
    chain: str
    balance_wei: int
    balance_eth: float  # or MATIC/BNB for Polygon/BSC
    symbol: str
    has_gas: bool  # true if balance > 0.0005 ETH equivalent
    error: str = ""


# ---------------------------------------------------------------------------
# RPC connection cache
# ---------------------------------------------------------------------------
_w3_cache: dict[str, Web3] = {}

_RPC_URLS: dict[str, str] = {
    "ethereum": settings.ETH_RPC_URL,
    "base": settings.BASE_RPC_URL,
    "arbitrum": settings.ARBITRUM_RPC_URL,
    "polygon": settings.POLYGON_RPC_URL,
    "bsc": settings.BSC_RPC_URL,
}


def get_web3(chain: str) -> Optional[Web3]:
    """Get (or create) a cached Web3 connection for the given chain."""
    if chain in _w3_cache:
        return _w3_cache[chain]

    url = _RPC_URLS.get(chain)
    if not url:
        logger.warning("No RPC URL configured for chain '%s'", chain)
        return None

    try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
        if w3.is_connected():
            _w3_cache[chain] = w3
            return w3
        else:
            logger.warning("RPC %s (%s): not connected", chain, url)
            return None
    except Exception as exc:
        logger.warning("RPC %s (%s): connection error: %s", chain, url, exc)
        return None


def _symbol(chain: str) -> str:
    return CHAIN_CONFIGS.get(chain, CHAIN_CONFIGS["ethereum"])["symbol"]


# ---------------------------------------------------------------------------
# Chain health
# ---------------------------------------------------------------------------
def check_chain(chain: str) -> ChainStatus:
    """Check if an RPC endpoint is alive and return gas price."""
    w3 = get_web3(chain)
    if not w3:
        return ChainStatus(chain=chain, connected=False, error="no connection")

    try:
        block = w3.eth.block_number
        gas_wei = w3.eth.gas_price
        gas_gwei = float(w3.from_wei(gas_wei, "gwei"))
        return ChainStatus(
            chain=chain,
            connected=True,
            block_number=block,
            gas_price_gwei=round(gas_gwei, 1),
        )
    except Exception as exc:
        return ChainStatus(chain=chain, connected=False, error=str(exc)[:120])


def check_all_chains() -> dict[str, ChainStatus]:
    """Return health status for all configured chains (parallel execution)."""
    chains = list(_RPC_URLS.keys())
    result: dict[str, ChainStatus] = {}

    with ThreadPoolExecutor(max_workers=len(chains)) as pool:
        fut = {pool.submit(check_chain, c): c for c in chains}
        for f in as_completed(fut):
            c = fut[f]
            try:
                result[c] = f.result()
            except Exception as exc:
                result[c] = ChainStatus(chain=c, connected=False, error=str(exc)[:120])

    return result


# ---------------------------------------------------------------------------
# Balance queries
# ---------------------------------------------------------------------------
_MIN_GAS_THRESHOLD = 0.0005  # ETH (or equivalent) — enough for ~1-2 txs


def get_balance(address: str, chain: str = "ethereum") -> WalletBalance:
    """
    Check native token balance for an address on the given chain.

    Returns WalletBalance with:
      - balance_wei: raw wei value
      - balance_eth: converted to human-readable units
      - has_gas: whether balance exceeds minimum gas threshold
    """
    w3 = get_web3(chain)
    if not w3:
        return WalletBalance(
            address=address,
            chain=chain,
            balance_wei=0,
            balance_eth=0.0,
            symbol=_symbol(chain),
            has_gas=False,
            error="no rpc connection",
        )

    try:
        checksum = w3.to_checksum_address(address)
        wei_balance = w3.eth.get_balance(checksum)
        eth_balance = float(w3.from_wei(wei_balance, "ether"))
        has_gas = eth_balance >= _MIN_GAS_THRESHOLD

        return WalletBalance(
            address=address,
            chain=chain,
            balance_wei=wei_balance,
            balance_eth=round(eth_balance, 6),
            symbol=_symbol(chain),
            has_gas=has_gas,
        )
    except Exception as exc:
        return WalletBalance(
            address=address,
            chain=chain,
            balance_wei=0,
            balance_eth=0.0,
            symbol=_symbol(chain),
            has_gas=False,
            error=str(exc)[:120],
        )


def get_balances_multi(
    address: str, chains: list[str] | None = None
) -> dict[str, WalletBalance]:
    """
    Check native balance across multiple chains for one address (parallel).
    If chains is None, checks all configured chains.
    """
    if chains is None:
        chains = list(_RPC_URLS.keys())
    result: dict[str, WalletBalance] = {}

    with ThreadPoolExecutor(max_workers=len(chains)) as pool:
        fut = {pool.submit(get_balance, address, c): c for c in chains}
        for f in as_completed(fut):
            c = fut[f]
            try:
                result[c] = f.result()
            except Exception as exc:
                result[c] = WalletBalance(
                    address=address,
                    chain=c,
                    balance_wei=0,
                    balance_eth=0.0,
                    symbol=_symbol(c),
                    has_gas=False,
                    error=str(exc)[:120],
                )

    return result


# ---------------------------------------------------------------------------
# Gas estimation
# ---------------------------------------------------------------------------
def estimate_gas(
    chain: str = "ethereum",
    tx_type: str = "transfer",
) -> dict:
    """
    Estimate gas cost for a simple ETH transfer.

    Returns:
      {
        "gas_price_gwei": float,
        "gas_limit": int (21000 for simple transfer),
        "estimated_cost_eth": float,
        "estimated_cost_usd": float,  # rough estimate, 0 if unavailable
      }
    """
    w3 = get_web3(chain)
    if not w3:
        return {"error": "no rpc connection"}

    try:
        gas_price_wei = w3.eth.gas_price
        gas_price_gwei = float(w3.from_wei(gas_price_wei, "gwei"))
        gas_limit = 21000 if tx_type == "transfer" else 100000
        cost_wei = gas_price_wei * gas_limit
        cost_eth = float(w3.from_wei(cost_wei, "ether"))

        return {
            "gas_price_gwei": round(gas_price_gwei, 1),
            "gas_limit": gas_limit,
            "estimated_cost_eth": round(cost_eth, 8),
            "chain": chain,
        }
    except Exception as exc:
        return {"error": str(exc)[:120]}


# ---------------------------------------------------------------------------
# RPC health check endpoint handler
# ---------------------------------------------------------------------------
def summary() -> dict:
    """Return a human-readable summary of all chain statuses + gas costs (parallel)."""
    chains = list(_RPC_URLS.keys())
    result: dict = {}

    def _chain_summary(chain: str) -> tuple[str, dict]:
        status = check_chain(chain)
        gas = estimate_gas(chain)
        return chain, {
            "connected": status.connected,
            "block": status.block_number,
            "gas_price_gwei": status.gas_price_gwei,
            "estimated_tx_cost_eth": gas.get("estimated_cost_eth", 0),
        }

    with ThreadPoolExecutor(max_workers=len(chains)) as pool:
        fut = {pool.submit(_chain_summary, c): c for c in chains}
        for f in as_completed(fut):
            try:
                k, v = f.result()
                result[k] = v
            except Exception as exc:
                pass

    return result
