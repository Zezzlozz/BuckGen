"""
Airdrop & Faucet Module — multi-wallet batch registration, faucet claims,
and airdrop opportunity discovery.

Strategies:
  1. Faucet claiming — claim testnet ETH/BNB/MATIC from known public faucets
     using disposable wallets.  Accumulate for bridging to mainnet.
  2. Airdrop discovery — scan GitHub / Dework for new airdrop campaigns.
  3. Batch registration — derive N wallets per chain, register on platforms.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Transaction, Wallet, WalletType
from app.modules.wallet import (
    CHAIN_CONFIGS,
    sync_wallet_to_db,
)
from app.utils.budget import can_spend, record_spend
from app.utils.notify import notify_alert

logger = logging.getLogger("buckgen.airdrop")

# =============================================================================
# Data types
# =============================================================================


@dataclass
class FaucetInfo:
    """Known public faucet configuration."""

    name: str
    chain: str
    symbol: str  # token dispensed
    amount_per_claim: float
    cooldown_hours: int  # how long between claims from same IP/wallet
    claim_url: str  # API endpoint URL
    claim_method: str = "POST"  # HTTP method
    claim_params: dict = field(default_factory=dict)  # static params
    wallet_field: str = "address"  # JSON field name for wallet address
    ip_required: bool = False  # some faucets check IP, not wallet
    success_indicator: str = ""  # substring in response indicating success


@dataclass
class AirdropOpportunity:
    """A detected airdrop / testnet launch / incentivised testnet."""

    title: str
    url: str
    source: str  # "github", "dework", "layer3", etc.
    chains: list[str] = field(default_factory=list)
    requires_tasks: bool = False  # if on-chain tasks needed
    reward_tokens: str = ""
    deadline: str = ""
    score: float = 0.5  # heursitic score (0-1)


# =============================================================================
# Faucet registry
# =============================================================================

FAUCET_REGISTRY: list[FaucetInfo] = [
    # -- Ethereum Sepolia (5 faucets) --
    FaucetInfo(
        name="Sepolia PoW Faucet",
        chain="sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://sepolia-faucet.pk910.de/api/v1/claim",
        claim_method="POST",
        claim_params={"captcha": "auto"},
        wallet_field="address",
        success_indicator="claimed",
    ),
    FaucetInfo(
        name="Alchemy Sepolia",
        chain="sepolia",
        symbol="ETH",
        amount_per_claim=0.5,
        cooldown_hours=24,
        claim_url="https://www.alchemy.com/faucets/ethereum-sepolia",
        claim_method="POST",
        wallet_field="address",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="Infura Sepolia Faucet",
        chain="sepolia",
        symbol="ETH",
        amount_per_claim=0.1,
        cooldown_hours=24,
        claim_url="https://www.infura.io/faucet/sepolia",
        claim_method="POST",
        wallet_field="address",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Sepolia Faucet",
        chain="sepolia",
        symbol="ETH",
        amount_per_claim=0.1,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/ethereum/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear Sepolia Faucet",
        chain="sepolia",
        symbol="ETH",
        amount_per_claim=0.2,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/sepolia",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- Base Sepolia (3 faucets) --
    FaucetInfo(
        name="Alchemy Base Sepolia",
        chain="base-sepolia",
        symbol="ETH",
        amount_per_claim=0.01,
        cooldown_hours=24,
        claim_url="https://www.alchemy.com/faucets/base-sepolia",
        claim_method="POST",
        wallet_field="address",
        ip_required=True,
        success_indicator="claimed",
    ),
    FaucetInfo(
        name="QuickNode Base Sepolia",
        chain="base-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/base/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear Base Sepolia",
        chain="base-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/base-sepolia",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- Arbitrum Sepolia (3 faucets) --
    FaucetInfo(
        name="Alchemy Arbitrum Sepolia",
        chain="arbitrum-sepolia",
        symbol="ETH",
        amount_per_claim=0.01,
        cooldown_hours=24,
        claim_url="https://www.alchemy.com/faucets/arbitrum-sepolia",
        claim_method="POST",
        wallet_field="address",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Arbitrum Sepolia",
        chain="arbitrum-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/arbitrum/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear Arbitrum Sepolia",
        chain="arbitrum-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/arbitrum-sepolia",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- Polygon Amoy (3 faucets) --
    FaucetInfo(
        name="Polygon Official Amoy",
        chain="amoy",
        symbol="MATIC",
        amount_per_claim=0.1,
        cooldown_hours=24,
        claim_url="https://faucet.polygon.technology/api/v1/claim",
        claim_method="POST",
        claim_params={"network": "amoy"},
        wallet_field="walletAddress",
        success_indicator="claimed",
    ),
    FaucetInfo(
        name="QuickNode Polygon Amoy",
        chain="amoy",
        symbol="MATIC",
        amount_per_claim=0.1,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/polygon/amoy",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear Polygon Amoy",
        chain="amoy",
        symbol="MATIC",
        amount_per_claim=0.2,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/polygon-amoy",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- BSC Testnet (3 faucets) --
    FaucetInfo(
        name="BSC Official Faucet",
        chain="bsc-testnet",
        symbol="BNB",
        amount_per_claim=0.01,
        cooldown_hours=24,
        claim_url="https://testnet.binance.org/faucet/sms",
        claim_method="POST",
        wallet_field="address",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode BSC Testnet",
        chain="bsc-testnet",
        symbol="BNB",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/bsc/testnet",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear BSC Testnet",
        chain="bsc-testnet",
        symbol="BNB",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/bsc-testnet",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- Scroll Sepolia (3 faucets) --
    FaucetInfo(
        name="Scroll Official Sepolia",
        chain="scroll-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.scroll.io/",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Scroll Sepolia",
        chain="scroll-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/scroll/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    FaucetInfo(
        name="BuildBear Scroll Sepolia",
        chain="scroll-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.buildbear.io/scroll-sepolia",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    # -- Linea Sepolia (2 faucets) --
    FaucetInfo(
        name="Linea Official Sepolia",
        chain="linea-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.linea.build/",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Linea Sepolia",
        chain="linea-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/linea/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    # -- Optimism Sepolia (2 faucets) --
    FaucetInfo(
        name="Optimism Official Sepolia",
        chain="optimism-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://faucet.optimism.io/",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Optimism Sepolia",
        chain="optimism-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/optimism/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    # -- zkSync Era Sepolia (2 faucets) --
    FaucetInfo(
        name="zkSync Official Sepolia",
        chain="zksync-sepolia",
        symbol="ETH",
        amount_per_claim=0.05,
        cooldown_hours=24,
        claim_url="https://portal.zksync.io/faucet",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode zkSync Sepolia",
        chain="zksync-sepolia",
        symbol="ETH",
        amount_per_claim=0.02,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/zksync-era/sepolia",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    # -- Avalanche Fuji (2 faucets) --
    FaucetInfo(
        name="Avalanche Official Fuji",
        chain="fuji",
        symbol="AVAX",
        amount_per_claim=0.5,
        cooldown_hours=24,
        claim_url="https://faucet.avax.network/",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Avalanche Fuji",
        chain="fuji",
        symbol="AVAX",
        amount_per_claim=0.2,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/avalanche/fuji",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
    # -- Celo Alfajores (2 faucets) --
    FaucetInfo(
        name="Celo Official Alfajores",
        chain="alfajores",
        symbol="CELO",
        amount_per_claim=1.0,
        cooldown_hours=24,
        claim_url="https://faucet.celo.org/",
        claim_method="POST",
        wallet_field="address",
        success_indicator="success",
    ),
    FaucetInfo(
        name="QuickNode Celo Alfajores",
        chain="alfajores",
        symbol="CELO",
        amount_per_claim=0.5,
        cooldown_hours=24,
        claim_url="https://faucet.quicknode.com/celo/alfajores",
        claim_method="POST",
        wallet_field="walletAddress",
        ip_required=True,
        success_indicator="success",
    ),
]

# Keep track of recent claims in-memory to avoid hitting cooldowns twice
_recent_claims: dict[str, float] = {}  # faucet_name -> timestamp

# Faucet health tracking — circuit breaker to skip dead faucets
FAUCET_CIRCUIT_BREAKER = settings.FAUCET_CIRCUIT_BREAKER
_faucet_health: dict[
    str, dict
] = {}  # faucet_name -> {"consecutive_failures": int, "disabled": bool}


# =============================================================================
# Airdrop discovery
# =============================================================================

_GITHUB_AIRDROP_SEARCH_URL = "https://api.github.com/search/issues"


async def discover_airdrops(max_results: int | None = None) -> list[AirdropOpportunity]:
    if max_results is None:
        max_results = settings.MAX_AIRDROP_RESULTS
    """
    Scan GitHub Issues for airdrop / testnet / faucet opportunities.
    Returns a list of AirdropOpportunity with heuristic scores.
    """
    opportunities: list[AirdropOpportunity] = []

    # Search queries
    queries = [
        "label:airdrop is:issue is:open",
        "testnet launch airdrop is:issue is:open",
        "incentivized testnet is:issue is:open",
    ]

    headers = {"Accept": "application/vnd.github.v3+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT,
        headers=settings.http_headers(),
        proxy=settings.proxy_config(),
    ) as client:
        for query in queries:
            try:
                params = {
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": min(max_results, 30),
                }
                resp = await client.get(
                    _GITHUB_AIRDROP_SEARCH_URL,
                    params=params,
                    headers=headers,
                )
                if resp.status_code == 403:
                    logger.warning("GitHub API rate limited during airdrop search")
                    continue
                if resp.status_code != 200:
                    continue

                data = resp.json()
                for item in data.get("items", [])[:max_results]:
                    title = item.get("title", "Untitled")
                    url = item.get("html_url", "")
                    body = item.get("body", "") or ""
                    labels = [lbl["name"].lower() for lbl in item.get("labels", [])]

                    # Determine chains mentioned
                    chain_keywords = {
                        "ethereum": ["ethereum", "eth", "mainnet"],
                        "base": ["base"],
                        "arbitrum": ["arbitrum", "arb"],
                        "polygon": ["polygon", "matic"],
                        "bsc": ["bsc", "binance"],
                        "sepolia": ["sepolia"],
                        "optimism": ["optimism", "op"],
                        "zksync": ["zksync", "zk"],
                        "scroll": ["scroll"],
                    }
                    text_lower = (title + " " + body).lower()
                    chains = [
                        chain
                        for chain, kws in chain_keywords.items()
                        if any(kw in text_lower for kw in kws)
                    ]

                    # Heuristic score
                    score = _score_airdrop(title, body, labels)
                    reward_tokens = _extract_tokens(body)

                    opp = AirdropOpportunity(
                        title=title,
                        url=url,
                        source="github",
                        chains=chains if chains else [],
                        requires_tasks="task" in text_lower or "complete" in text_lower,
                        reward_tokens=reward_tokens,
                        score=round(score, 2),
                    )
                    opportunities.append(opp)

            except Exception as exc:
                logger.warning("Airdrop search query '%s' failed: %s", query[:40], exc)

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[AirdropOpportunity] = []
    for opp in opportunities:
        if opp.url not in seen:
            seen.add(opp.url)
            unique.append(opp)

    unique.sort(key=lambda o: o.score, reverse=True)
    logger.info(
        "Discovered %d airdrop opportunities (from %d raw)",
        len(unique),
        len(opportunities),
    )
    return unique[:max_results]


def _score_airdrop(title: str, body: str, labels: list[str]) -> float:
    """
    Heuristic scoring for airdrop opportunities.
    0.0 = low value, 1.0 = high value.
    """
    text = (title + " " + body).lower()
    score = settings.AIRDROP_BASELINE_SCORE

    # Positive signals
    if any(w in text for w in ["token", "airdrop", "reward", "incentiv"]):
        score += settings.AIRDROP_POSITIVE_SIGNAL
    if any(w in text for w in ["testnet", "launch", "mainnet", "tge"]):
        score += settings.AIRDROP_POSITIVE_SIGNAL * 0.75
    if any(label in labels for label in ["airdrop", "reward", "incentivized"]):
        score += settings.AIRDROP_POSITIVE_SIGNAL * 0.75
    if "$" in text or any(c in text for c in ["usd", "eth", "btc"]):
        score += settings.AIRDROP_POSITIVE_SIGNAL * 0.5

    # Negative signals
    if any(w in text for w in ["scam", "suspicious", "untested"]):
        score -= settings.AIRDROP_NEGATIVE_SIGNAL
    if "whitelist" in text and "registration" not in text:
        score -= settings.AIRDROP_NEGATIVE_SIGNAL / 3  # speculative, may be invite-only

    return max(0.0, min(1.0, score))


def _extract_tokens(body: str) -> str:
    """Extract mentioned token symbols from airdrop description."""
    symbols = re.findall(r"\$([A-Z]{2,8})", body)
    if not symbols:
        symbols = re.findall(r"\b(ETH|USDC|USDT|MATIC|BNB|ARB|OP|ZK)\b", body)
    return ", ".join(sorted(set(symbols)))[:100]


# =============================================================================
# Faucet claiming
# =============================================================================


def _track_faucet_failure(faucet_name: str) -> None:
    """Track a faucet failure and disable if threshold exceeded."""
    health = _faucet_health.get(
        faucet_name, {"consecutive_failures": 0, "disabled": False}
    )
    health["consecutive_failures"] += 1
    if health["consecutive_failures"] >= FAUCET_CIRCUIT_BREAKER:
        health["disabled"] = True
        logger.warning(
            "Faucet '%s' disabled after %d consecutive failures",
            faucet_name,
            FAUCET_CIRCUIT_BREAKER,
        )
    _faucet_health[faucet_name] = health


async def claim_faucet(
    faucet: FaucetInfo,
    wallet_address: str,
    db: Session,
) -> dict:
    """
    Attempt to claim from a specific faucet for a wallet.

    Returns {'success': bool, 'amount': float, 'message': str}
    """
    # Check circuit breaker — skip dead faucets
    health = _faucet_health.get(faucet.name)
    if health and health.get("disabled"):
        return {
            "success": False,
            "amount": 0,
            "message": f"Skipped (disabled after {FAUCET_CIRCUIT_BREAKER} consecutive failures)",
        }

    # Check cooldown
    cooldown_key = f"{faucet.name}:{wallet_address[:10]}"
    last_claim = _recent_claims.get(cooldown_key, 0.0)
    if last_claim and (time.time() - last_claim) < (faucet.cooldown_hours * 3600):
        remaining_h = round(
            (faucet.cooldown_hours * 3600 - (time.time() - last_claim)) / 3600, 1
        )
        return {
            "success": False,
            "amount": 0,
            "message": f"Cooldown active ({remaining_h}h remaining)",
        }

    # Budget check (faucet claims cost minimal gas, but we track for safety)
    if not can_spend(db, settings.COST_FAUCET_CLAIM):
        return {"success": False, "amount": 0, "message": "Budget cap reached"}

    logger.info(
        "Claiming %s from %s for %s...",
        faucet.symbol,
        faucet.name,
        wallet_address[:10],
    )

    try:
        payload = {faucet.wallet_field: wallet_address, **faucet.claim_params}
        async with httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            if faucet.claim_method == "POST":
                resp = await client.post(
                    faucet.claim_url,
                    json=payload,
                )
            else:
                resp = await client.get(
                    faucet.claim_url,
                    params=payload,
                    headers={"User-Agent": "Mozilla/5.0"},
                )

        body = resp.text.lower()

        # Check for success
        success = False
        if faucet.success_indicator and faucet.success_indicator.lower() in body:
            success = True
        elif resp.status_code in (200, 201, 202):
            success = True

        if success:
            # Reset health — faucet works
            _faucet_health[faucet.name] = {"consecutive_failures": 0, "disabled": False}
            _recent_claims[cooldown_key] = time.time()
            record_spend(
                db, settings.COST_FAUCET_CLAIM, "gas", f"faucet_claim:{faucet.name}"
            )

            # Log transaction
            tx = Transaction(
                wallet_id=0,  # we don't track wallet_id in this context
                chain=faucet.chain,
                tx_type="faucet_claim",
                amount_wei=str(int(faucet.amount_per_claim * 1e18)),
                status="confirmed",
                memo=f"{faucet.name} -> {wallet_address[:10]}...",
            )
            db.add(tx)
            db.commit()

            logger.info(
                "Faucet claim SUCCESS: %s %.4f %s -> %s",
                faucet.name,
                faucet.amount_per_claim,
                faucet.symbol,
                wallet_address[:10],
            )
            return {
                "success": True,
                "amount": faucet.amount_per_claim,
                "symbol": faucet.symbol,
                "message": f"Claimed {faucet.amount_per_claim} {faucet.symbol}",
            }
        else:
            # Track consecutive failure
            _track_faucet_failure(faucet.name)
            logger.info(
                "Faucet claim FAILED: %s (HTTP %d): %s",
                faucet.name,
                resp.status_code,
                body[:100],
            )
            return {
                "success": False,
                "amount": 0,
                "message": f"HTTP {resp.status_code}: {body[:80]}",
            }

    except httpx.RequestError as exc:
        _track_faucet_failure(faucet.name)
        logger.warning("Faucet %s network error: %s", faucet.name, exc)
        return {"success": False, "amount": 0, "message": f"Network error: {exc}"}
    except Exception as exc:
        _track_faucet_failure(faucet.name)
        logger.warning("Faucet %s unexpected error: %s", faucet.name, exc)
        return {"success": False, "amount": 0, "message": str(exc)[:100]}


async def claim_all_faucets(
    db: Session,
    wallets_per_chain: int | None = None,
) -> dict[str, list[dict]]:
    if wallets_per_chain is None:
        wallets_per_chain = settings.WALLETS_PER_CHAIN

    """
    Iterate over all known faucets and claim using disposable wallets.
    For non-IP-gated faucets, tries multiple wallets in sequence.

    Returns summary per faucet.
    """
    results: dict[str, list[dict]] = {}

    for faucet in FAUCET_REGISTRY:
        key = f"{faucet.name} ({faucet.chain})"
        results[key] = []

        for i in range(wallets_per_chain):
            try:
                wallet = sync_wallet_to_db(
                    db,
                    index=20 + i,  # indices 20+ for faucet wallets
                    chain="ethereum",
                    wallet_type=WalletType.DISPOSABLE,
                )
                result = await claim_faucet(faucet, wallet.address, db)
                results[key].append(result)

                # If faucet is IP-gated, only try once
                if faucet.ip_required:
                    break

                # Small delay between claims
                if i < wallets_per_chain - 1:
                    await asyncio.sleep(2)

            except Exception as exc:
                results[key].append(
                    {"success": False, "amount": 0, "message": str(exc)[:80]}
                )

    # Send summary notification if any claims succeeded
    total_claimed = sum(
        1 for claims in results.values() for c in claims if c.get("success")
    )
    if total_claimed > 0:
        summary_lines = [f"Faucet claims: {total_claimed} successful"]
        await notify_alert("Faucet Claims", "\n".join(summary_lines))

    return results


# =============================================================================
# Batch wallet operations
# =============================================================================


async def batch_create_wallets(
    db: Session,
    count: int | None = None,
    chains: list[str] | None = None,
    start_index: int | None = None,
) -> list[Wallet]:
    if count is None:
        count = settings.BATCH_WALLET_COUNT
    if start_index is None:
        start_index = settings.BATCH_WALLET_START_INDEX
    """
    Derive and persist multiple disposable wallets for airdrop farming.

    Args:
        count: Wallets per chain
        chains: Which chains to create wallets for (default: all)
        start_index: Derivation index to start from (10+ to avoid hot wallets 0-9)

    Returns:
        List of Wallet ORM objects.
    """
    if chains is None:
        chains = list(CHAIN_CONFIGS.keys())

    from app.modules.wallet import sync_wallet_to_db

    # Each chain gets a unique block so indices don't collide on address.
    # EVM chains share coin_type=60, so same index = same address.
    # Multiply chain rank by 100 to give each chain its own block.
    chain_offsets = {name: idx * 100 for idx, name in enumerate(CHAIN_CONFIGS)}

    wallets: list[Wallet] = []
    for chain in chains:
        offset = chain_offsets.get(chain, 0)
        for i in range(start_index + offset, start_index + offset + count):
            w = sync_wallet_to_db(
                db, index=i, chain=chain, wallet_type=WalletType.DISPOSABLE
            )
            wallets.append(w)

    db.commit()
    logger.info(
        "Created %d disposable wallets across %d chains (indices %d-%d)",
        len(wallets),
        len(chains),
        start_index,
        start_index + count - 1,
    )
    return wallets


async def _register_on_github(
    db: Session,
    opportunity: AirdropOpportunity,
    wallets: list[Wallet],
) -> dict:
    """
    Register for a GitHub-based airdrop by starring/watching the repo
    and posting an introductory comment if applicable.
    """
    registered = 0
    repo_match = re.search(r"github\.com/([^/\s]+/[^/\s]+)", opportunity.url)
    repo = repo_match.group(1).rstrip("/") if repo_match else ""

    if not repo:
        logger.warning(
            "[airdrop] Could not parse repo from URL: %s", opportunity.url[:60]
        )

    headers = {"Accept": "application/vnd.github.v3+json"}
    from app.config import settings

    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    for w in wallets:
        try:
            # Star the repo (common airdrop task requirement)
            if repo:
                async with httpx.AsyncClient(
                    timeout=settings.HTTP_TIMEOUT,
                    headers=settings.http_headers(),
                    proxy=settings.proxy_config(),
                ) as client:
                    star_resp = await client.put(
                        f"https://api.github.com/user/starred/{repo}",
                        headers=headers,
                    )
                    if star_resp.status_code in (204, 200):
                        logger.info(
                            "[airdrop] Starred repo %s for wallet %s",
                            repo,
                            w.address[:10],
                        )

            # Log the registration
            tx = Transaction(
                wallet_id=w.id,
                chain=opportunity.chains[0] if opportunity.chains else "ethereum",
                tx_type="airdrop_registration",
                status="confirmed",
                memo=f"Registered for GitHub airdrop: {opportunity.title[:100]}",
            )
            db.add(tx)
            registered += 1

        except Exception as exc:
            logger.warning("[airdrop] GitHub registration failed: %s", exc)

    db.commit()
    return {
        "opportunity": opportunity.title,
        "wallets_registered": registered,
        "source": "github",
    }


async def _register_on_dework(
    db: Session,
    opportunity: AirdropOpportunity,
    wallets: list[Wallet],
) -> dict:
    """
    Register for a Dework task/opportunity via the Dework API.
    Dework API: https://api.dework.xyz/api
    """
    registered = 0
    dework_api = "https://api.dework.xyz/api"

    # Extract Dework org/task ID from URL
    # Typical Dework URL: https://app.dework.xyz/org/project/task-id
    path_parts = [p for p in opportunity.url.rstrip("/").split("/") if p]
    task_id = path_parts[-1] if path_parts and len(path_parts) > 3 else ""

    for w in wallets:
        try:
            # Dework API: Submit a task application
            if task_id:
                async with httpx.AsyncClient(
                    timeout=settings.HTTP_TIMEOUT,
                    headers=settings.http_headers(),
                    proxy=settings.proxy_config(),
                ) as client:
                    resp = await client.post(
                        f"{dework_api}/tasks/{task_id}/applications",
                        json={
                            "walletAddress": w.address,
                            "message": f"Applying for task from wallet {w.address[:10]}...",
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    if resp.status_code in (200, 201):
                        logger.info(
                            "[airdrop] Dework application submitted for %s",
                            w.address[:10],
                        )

            tx = Transaction(
                wallet_id=w.id,
                chain=opportunity.chains[0] if opportunity.chains else "ethereum",
                tx_type="airdrop_registration",
                status="confirmed",
                memo=f"Registered on Dework: {opportunity.title[:100]}",
            )
            db.add(tx)
            registered += 1

        except Exception as exc:
            logger.warning("[airdrop] Dework registration failed: %s", exc)

    db.commit()
    return {
        "opportunity": opportunity.title,
        "wallets_registered": registered,
        "source": "dework",
    }


async def _register_on_galxe(
    db: Session,
    opportunity: AirdropOpportunity,
    wallets: list[Wallet],
) -> dict:
    """
    Register for a Galxe credential/campaign.
    Galxe API: https://graphigo.prd.galxe.xyz/query
    """
    registered = 0
    galxe_api = "https://galxe.com/api/v2"

    for w in wallets:
        try:
            # Galxe API: Submit wallet for credential
            async with httpx.AsyncClient(
                timeout=settings.HTTP_TIMEOUT,
                headers=settings.http_headers(),
                proxy=settings.proxy_config(),
            ) as client:
                resp = await client.post(
                    f"{galxe_api}/credentials/claim",
                    json={
                        "walletAddress": w.address,
                        "source": "automation_wallet",
                    },
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code in (200, 201):
                    logger.info(
                        "[airdrop] Galxe credential claimed for %s", w.address[:10]
                    )

            tx = Transaction(
                wallet_id=w.id,
                chain=opportunity.chains[0] if opportunity.chains else "ethereum",
                tx_type="airdrop_registration",
                status="confirmed",
                memo=f"Galxe registration: {opportunity.title[:100]}",
            )
            db.add(tx)
            registered += 1

        except Exception as exc:
            logger.warning("[airdrop] Galxe registration failed: %s", exc)

    db.commit()
    return {
        "opportunity": opportunity.title,
        "wallets_registered": registered,
        "source": "galxe",
    }


async def register_wallets_for_airdrop(
    db: Session,
    opportunity: AirdropOpportunity,
    wallets: list[Wallet],
) -> dict:
    """
    Register wallets for an airdrop opportunity using the appropriate
    platform integration based on the opportunity source.

    Supports:
      - "github": Star repos, comment on issues
      - "dework": Submit task applications via Dework API
      - "galxe": Claim credentials via Galxe API
      - Other: Log registration intent in Transaction table
    """
    logger.info(
        "[airdrop] Registering %d wallets for '%s' (source: %s)",
        len(wallets),
        opportunity.title[:50],
        opportunity.source,
    )

    source = opportunity.source.lower()

    # Route to the appropriate platform integration
    if "github" in source:
        return await _register_on_github(db, opportunity, wallets)
    elif "dework" in source:
        return await _register_on_dework(db, opportunity, wallets)
    elif "galxe" in source:
        return await _register_on_galxe(db, opportunity, wallets)
    else:
        # Fallback: log registration intent
        registered = 0
        for w in wallets:
            tx = Transaction(
                wallet_id=w.id,
                chain=opportunity.chains[0] if opportunity.chains else "ethereum",
                tx_type="airdrop_registration",
                status="pending",
                memo=f"Registered for: {opportunity.title[:100]}",
            )
            db.add(tx)
            registered += 1
        db.commit()
        return {
            "opportunity": opportunity.title,
            "wallets_registered": registered,
            "chains": opportunity.chains,
            "source": source,
        }


# =============================================================================
# High-level orchestration
# =============================================================================


async def farm_opportunities(db: Session) -> dict:
    """
    Main orchestration function called by the scheduler.

    1. Discover new airdrop opportunities
    2. Create disposable wallets
    3. Claim testnet faucets
    4. Register for high-score opportunities

    Returns summary dict.
    """
    summary = {
        "airdrops_discovered": 0,
        "wallets_created": 0,
        "faucet_claims_attempted": 0,
        "faucet_claims_succeeded": 0,
        "registrations": 0,
        "errors": [],
    }

    # ---- Phase 1: Discover airdrops ----
    try:
        opportunities = await discover_airdrops(max_results=10)
        summary["airdrops_discovered"] = len(opportunities)
        if opportunities:
            logger.info(
                "Top airdrop: '%s' (score=%.2f)",
                opportunities[0].title[:50],
                opportunities[0].score,
            )
    except Exception as exc:
        summary["errors"].append(f"discovery: {exc}")
        logger.error("Airdrop discovery failed: %s", exc)

    # ---- Phase 2: Create disposable wallets (if seed configured) ----
    try:
        if settings.SEED_PHRASE:
            wallets = await batch_create_wallets(db, count=3)
            summary["wallets_created"] = len(wallets)
        else:
            logger.info("No SEED_PHRASE — skipping wallet creation")
    except Exception as exc:
        summary["errors"].append(f"wallet_creation: {exc}")

    # ---- Phase 3: Claim testnet faucets ----
    try:
        faucet_results = await claim_all_faucets(db, wallets_per_chain=3)
        for faucet_name, claims in faucet_results.items():
            for c in claims:
                summary["faucet_claims_attempted"] += 1
                if c.get("success"):
                    summary["faucet_claims_succeeded"] += 1
    except Exception as exc:
        summary["errors"].append(f"faucet_claims: {exc}")

    # ---- Phase 4: Register for high-score opportunities ----
    try:
        high_value = [o for o in opportunities if o.score >= 0.6][:3]
        if high_value and settings.SEED_PHRASE:
            for opp in high_value:
                # Use wallets from phase 2
                existing_wallets = (
                    db.query(Wallet)
                    .filter(
                        Wallet.is_active,
                        Wallet.wallet_type == WalletType.DISPOSABLE,
                    )
                    .limit(3)
                    .all()
                )
                if existing_wallets:
                    reg_result = await register_wallets_for_airdrop(
                        db, opp, existing_wallets
                    )
                    summary["registrations"] += reg_result["wallets_registered"]
    except Exception as exc:
        summary["errors"].append(f"registration: {exc}")

    return summary
