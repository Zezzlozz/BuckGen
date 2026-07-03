"""
FastAPI application entry point.
Initialises database, scheduler, and exposes health/liveness endpoints.
"""

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.models import Bounty, BountyStatus, Wallet, get_session, init_db
from app.modules.rpc import summary as rpc_summary
from app.modules.system import monitor
from app.modules.wallet import sync_wallet_to_db, zero_keyring
from app.scheduler.jobs import (
    check_gas_balances,
    check_prices,
    farm_airdrops,
    scan_bounties,
    self_heal,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("buckgen")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redact_db_url(url: str) -> str:
    """Remove credentials from a database URL for safe logging."""
    if url.startswith("sqlite"):
        return url
    # postgresql://user:pass@host:port/db -> postgresql://***@host:port/db
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            # Replace password with ***
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            parsed = parsed._replace(netloc=netloc)
        return urlunparse(parsed)
    except Exception:
        return url


def _require_api_key(request: Request) -> None:
    """Dependency: require X-API-Key header matching settings.API_KEY.

    Fails CLOSED: if no API_KEY is configured, requests are refused in
    production. Only DEBUG mode allows unauthenticated access, and even
    then it is logged loudly.
    """
    if not settings.API_KEY:
        if settings.DEBUG:
            logger.warning(
                "API_KEY unset — allowing unauthenticated request (DEBUG only)"
            )
            return
        raise HTTPException(
            status_code=503,
            detail="Server misconfigured: API_KEY not set. Refusing requests.",
        )
    api_key = request.headers.get("X-API-Key", "")
    # Constant-time comparison to avoid timing side channels on the secret.
    if not hmac.compare_digest(api_key, settings.API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _safe_error(msg: str = "Internal server error") -> dict:
    """Return a sanitized error response."""
    return {"success": False, "error": msg}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    """Register jobs and start the scheduler."""

    # Use same DB as the app for persistent job store
    scheduler.add_jobstore(SQLAlchemyJobStore(url=settings.DATABASE_URL))

    scheduler.add_job(
        scan_bounties,
        CronTrigger.from_crontab(settings.CRON_BOUNTY_SCAN),
        id="scan_bounties",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        check_prices,
        CronTrigger.from_crontab(settings.CRON_PRICE_CHECK),
        id="check_prices",
        replace_existing=True,
        misfire_grace_time=120,
    )
    # Stagger farm_airdrops to avoid overlapping bounty scan at :00
    scheduler.add_job(
        farm_airdrops,
        CronTrigger.from_crontab(settings.CRON_AIRDROP),
        id="farm_airdrops",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Stagger gas check 30 min after bounty scan to avoid :00 contention
    scheduler.add_job(
        check_gas_balances,
        CronTrigger.from_crontab("30 */2 * * *"),  # every 2 hours at :30
        id="check_gas_balances",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Stagger self_heal away from price check at :00/:30
    scheduler.add_job(
        self_heal,
        CronTrigger.from_crontab("5,35 * * * *"),  # every 30 min at :05/:35
        id="self_heal",
        replace_existing=True,
        misfire_grace_time=120,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — bounty=%s price=%s airdrop=%s",
        settings.CRON_BOUNTY_SCAN,
        settings.CRON_PRICE_CHECK,
        settings.CRON_AIRDROP,
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""
    # ---- Startup ---------------------------------------------------------
    # Refuse to boot a wallet-controlling service with no auth in production.
    if not settings.API_KEY and not settings.DEBUG:
        raise RuntimeError(
            "Refusing to start without API_KEY set. This service can move "
            "funds; set API_KEY, or run with DEBUG=true for local dev only."
        )

    missing = settings.validate()
    if missing:
        logger.warning("Missing config values: %s", ", ".join(missing))
    else:
        logger.info("All critical config values present")

    init_db(settings.DATABASE_URL)
    logger.info("Database initialised: %s", _redact_db_url(settings.DATABASE_URL))

    # ---- Seed initial wallets (if SEED_PHRASE is configured) -------------
    try:
        db = next(get_session())
        existing = db.query(Wallet).count()
        if existing == 0 and settings.SEED_PHRASE:
            logger.info("Seeding initial wallets...")
            # Create 1 hot wallet per chain + 1 disposable for airdrops
            for chain in ["ethereum", "base", "arbitrum", "polygon", "bsc"]:
                sync_wallet_to_db(db, index=0, chain=chain)
            sync_wallet_to_db(db, index=1, chain="ethereum", wallet_type="disposable")
            logger.info("Seeded %d wallets from seed phrase", db.query(Wallet).count())
        db.close()
    except ValueError:
        logger.info("No SEED_PHRASE configured — skipping wallet seeding")
    except Exception as exc:
        logger.warning("Wallet seeding skipped: %s", exc)

    start_scheduler()
    yield

    # ---- Shutdown --------------------------------------------------------
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
    zero_keyring()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BuckGen",
    version="0.1.0",
    description="Minimal Environment for Resource-Acquiring Agent",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — explicitly list allowed origins
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        # Add your Render deployment URL here, e.g.:
        # "https://buckgen.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Global exception handler — sanitize error responses
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Return sanitized JSON for unhandled exceptions instead of raw tracebacks."""
    logger.error(
        "Unhandled exception on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """Liveness check."""
    return {"status": "ok", "scheduler_running": scheduler.running}


@app.get("/config")
async def config_dump() -> dict:
    """Expose non-sensitive config for debugging."""
    return {
        "db_url": _redact_db_url(settings.DATABASE_URL),
        "cron_bounty": settings.CRON_BOUNTY_SCAN,
        "cron_price": settings.CRON_PRICE_CHECK,
        "cron_airdrop": settings.CRON_AIRDROP,
        "daily_gas_cap_eur": settings.DAILY_GAS_CAP_EUR,
        "stop_loss_eur": settings.STOP_LOSS_EUR,
    }


# ---------------------------------------------------------------------------
# Wallet endpoints
# ---------------------------------------------------------------------------
@app.get("/wallets")
async def list_wallets() -> dict:
    """List all active wallets (public addresses only)."""
    db = next(get_session())
    try:
        wallets = db.query(Wallet).filter(Wallet.is_active).all()
        return {
            "count": len(wallets),
            "wallets": [
                {
                    "address": w.address,
                    "chain": w.chain,
                    "wallet_type": w.wallet_type.value,
                    "derivation_path": w.derivation_path,
                    "balance_wei": w.balance_wei,
                    "is_active": w.is_active,
                    "last_used": w.last_used_at.isoformat() if w.last_used_at else None,
                }
                for w in wallets
            ],
        }
    finally:
        db.close()


@app.get("/wallets/{address}/balance")
async def wallet_balance(
    address: str = Path(min_length=40, max_length=44),
    chain: str = Query(default="ethereum", min_length=2, max_length=20),
) -> dict:
    """Check live balance for a wallet address on a given chain."""
    from app.modules.rpc import get_balance as rpc_balance

    bal = rpc_balance(address, chain)
    return {
        "address": address,
        "chain": chain,
        "balance_wei": bal.balance_wei,
        "balance": bal.balance_eth,
        "symbol": bal.symbol,
        "has_gas": bal.has_gas,
        "error": bal.error,
    }


@app.get("/wallets/{address}/balances")
async def wallet_balances_multi(
    address: str = Path(min_length=40, max_length=44),
) -> dict:
    """Check balance across ALL chains for one address."""
    from app.modules.rpc import get_balances_multi

    return get_balances_multi(address)


@app.get("/chains")
async def chains_status() -> dict:
    """Health + gas info for all configured chains."""
    return rpc_summary()


# ---------------------------------------------------------------------------
# Airdrop endpoints
# ---------------------------------------------------------------------------
@app.get("/airdrops/discover")
async def airdrop_discover() -> dict:
    """Scan GitHub for airdrop/testnet opportunities."""
    from app.modules.airdrop import discover_airdrops

    opportunities = await discover_airdrops(max_results=10)
    return {
        "count": len(opportunities),
        "opportunities": [
            {
                "title": o.title,
                "url": o.url,
                "source": o.source,
                "chains": o.chains,
                "score": o.score,
                "reward_tokens": o.reward_tokens,
                "requires_tasks": o.requires_tasks,
            }
            for o in opportunities
        ],
    }


@app.post("/airdrops/farm")
async def airdrop_farm(_: None = Depends(_require_api_key)) -> dict:
    """Run the full airdrop farming cycle manually."""
    from app.modules.airdrop import farm_opportunities

    db = next(get_session())
    try:
        result = await farm_opportunities(db)
        return {
            "status": "ok",
            "summary": {
                "airdrops_discovered": result["airdrops_discovered"],
                "wallets_created": result["wallets_created"],
                "faucet_claims_attempted": result["faucet_claims_attempted"],
                "faucet_claims_succeeded": result["faucet_claims_succeeded"],
                "registrations": result["registrations"],
            },
            "errors": result["errors"],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Price & Arbitrage endpoints
# ---------------------------------------------------------------------------
@app.get("/prices/tickers")
async def prices_tickers() -> dict:
    """Fetch live tickers from all exchanges for default pairs."""
    from app.modules.prices import fetch_all_tickers

    tickers = fetch_all_tickers()
    return {
        "count": sum(len(ts) for ts in tickers.values()),
        "pairs": {
            pair: [
                {
                    "exchange": t.exchange,
                    "bid": t.bid,
                    "ask": t.ask,
                    "last": t.last,
                    "volume": t.volume,
                    "error": t.error,
                }
                for t in tickers_list
            ]
            for pair, tickers_list in tickers.items()
        },
    }


@app.get("/prices/coingecko")
async def prices_coingecko() -> dict:
    """Fetch reference prices from CoinGecko."""
    from app.modules.prices import fetch_all_coingecko

    prices = await fetch_all_coingecko()
    return {
        "count": len(prices),
        "prices": {
            sym: {
                "usd": p.usd,
                "usd_24h_change": p.usd_24h_change,
                "usd_market_cap": p.usd_market_cap,
                "error": p.error,
            }
            for sym, p in prices.items()
        },
    }


@app.get("/prices/arbitrage")
async def prices_arbitrage(
    capital: float = Query(default=100.0, ge=0.0, le=100000.0),
) -> dict:
    """Detect arbitrage opportunities across all tracked pairs/exchanges."""
    from app.modules.prices import check_all_prices

    result = await check_all_prices(capital_eur=capital)
    return {
        "summary": {
            "pairs_checked": result["pairs_checked"],
            "exchanges_checked": result["exchanges_checked"],
            "tickers_fetched": result["tickers_fetched"],
            "opportunities_found": result["arbitrage_opportunities"],
        },
        "top_opportunities": result["top_opportunities"],
        "errors": result["errors"],
    }


# ---------------------------------------------------------------------------
# Trade execution (DeFi)
# ---------------------------------------------------------------------------
@app.post("/trade/swap")
async def trade_swap(
    _: None = Depends(_require_api_key),
    chain: str = Query(default="ethereum", min_length=2, max_length=20),
    wallet_index: int = Query(default=0, ge=0, le=1000),
    from_token: str = Query(default="", max_length=100),
    to_token: str = Query(default="", max_length=100),
    amount_wei: str = Query(default="", max_length=100),
    amount_eur: float = Query(default=0.0, ge=0.0),
    confirm: bool = Query(default=False),
) -> dict:
    """
    Execute a token swap via 1inch using the agent's HD wallet.

    For safety, defaults to dry-run mode (confirm=false): returns a preview
    without signing or broadcasting. Set confirm=true to actually submit.
    """
    from app.modules.defi import execute_swap

    db = next(get_session())
    try:
        result = await execute_swap(
            db=db,
            chain=chain,
            wallet_index=wallet_index,
            from_token=from_token,
            to_token=to_token,
            amount_wei=amount_wei,
            amount_eur_estimate=amount_eur,
            confirm=confirm,
        )
        return {
            "success": result.success,
            "tx_hash": result.tx_hash,
            "chain": chain,
            "from_amount": result.from_amount,
            "to_amount": result.to_amount,
            "gas_used_wei": result.gas_used_wei,
            "error": result.error,
        }
    finally:
        db.close()


@app.post("/trade/arbitrage")
async def trade_arbitrage(
    _: None = Depends(_require_api_key),
    chain: str = Query(default="ethereum", min_length=2, max_length=20),
    capital_eur: float = Query(default=500.0, ge=0.0, le=100000.0),
    confirm: bool = Query(default=False),
) -> dict:
    """
    Execute the best detected arbitrage opportunity on a given chain.

    Defaults to dry-run (confirm=false). Set confirm=true to place orders.
    """
    from app.modules.defi import execute_arbitrage
    from app.modules.prices import check_all_prices

    # First check for opportunities
    price_result = await check_all_prices(capital_eur=capital_eur)
    opportunities = price_result.get("top_opportunities", [])

    if not opportunities:
        return {"success": False, "error": "No arbitrage opportunities detected"}

    db = next(get_session())
    try:
        result = await execute_arbitrage(
            db=db,
            chain=chain,
            opportunity=opportunities[0],
            capital_eur=capital_eur,
            confirm=confirm,
        )
        return result
    finally:
        db.close()


@app.get("/trade/quote")
async def trade_quote(
    chain: str = Query(default="ethereum", min_length=2, max_length=20),
    from_token: str = Query(default="", max_length=100),
    to_token: str = Query(default="", max_length=100),
    amount_wei: str = Query(default="", max_length=100),
) -> dict:
    """Get a quote from 1inch without executing."""
    from app.modules.defi import get_swap_quote

    quote = await get_swap_quote(chain, from_token, to_token, amount_wei)
    if not quote:
        return {"success": False, "error": "Failed to get quote"}
    return {
        "success": True,
        "from_amount": quote.from_amount,
        "to_amount": quote.to_amount,
        "estimated_gas": quote.estimated_gas,
        "price_impact": quote.price_impact,
    }


# ---------------------------------------------------------------------------
# Bounty submission (auto-submit via LLM + GitHub API)
# ---------------------------------------------------------------------------
@app.get("/bounties/top")
async def bounties_top(
    min_score: float = Query(default=0.7, ge=0.0, le=1.0),
    limit: int = Query(default=5, ge=1, le=100),
) -> dict:
    """View top-scoring open bounties available for submission."""
    db = next(get_session())
    try:
        top = (
            db.query(Bounty)
            .filter(Bounty.status == BountyStatus.OPEN, Bounty.score >= min_score)
            .order_by(Bounty.score.desc())
            .limit(limit)
            .all()
        )
        return {
            "count": len(top),
            "bounties": [
                {
                    "id": b.id,
                    "title": b.title[:80],
                    "reward": b.reward_amount,
                    "currency": b.reward_currency,
                    "score": b.score,
                    "url": b.url,
                }
                for b in top
            ],
        }
    finally:
        db.close()


@app.post("/bounties/submit/{bounty_id}")
async def bounties_submit(
    _: None = Depends(_require_api_key), bounty_id: int = Path(ge=1)
) -> dict:
    """Deprecated: auto-posting is disabled. Use the human-in-the-loop flow:
    POST /bounties/{id}/research -> /draft -> /approve -> /post?confirm=true.
    """
    return {
        "success": False,
        "error": "auto-submit disabled",
        "use_instead": [
            f"POST /bounties/{bounty_id}/research",
            f"POST /bounties/{bounty_id}/draft",
            f"POST /bounties/{bounty_id}/approve",
            f"POST /bounties/{bounty_id}/post?confirm=true",
        ],
    }


@app.post("/bounties/submit-top")
async def bounties_submit_top(
    _: None = Depends(_require_api_key),
    max_subs: int = Query(default=3, ge=1, le=20),
    min_score: float = Query(default=0.7, ge=0.0, le=1.0),
) -> dict:
    """Deprecated: bulk auto-posting is disabled. This now researches the
    top bounties into your review queue instead of posting anything.
    """
    from app.modules.bounty_review import research_unassessed

    db = next(get_session())
    try:
        results = await research_unassessed(db, limit=max_subs)
        return {
            "success": True,
            "note": "Auto-post disabled. Researched into your review queue.",
            "researched": len(results),
            "results": results,
            "next": "GET /bounties/review-queue",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Bounty review (human-in-the-loop, ROI-ranked)
# ---------------------------------------------------------------------------
@app.get("/bounties/review-queue")
async def bounties_review_queue(
    limit: int = Query(default=20, ge=1, le=100),
    min_roi: float = Query(default=0.0, ge=0.0),
) -> dict:
    """Your bounties, ranked by expected $ / hour."""
    from app.modules.bounty_review import rank_by_roi

    db = next(get_session())
    try:
        return {"queue": rank_by_roi(db, limit=limit, min_roi=min_roi)}
    finally:
        db.close()


@app.get("/bounties/digest")
async def bounties_digest(limit: int = Query(default=15, ge=1, le=50)) -> dict:
    """Markdown digest of the ROI-ranked queue (easy to export/paste)."""
    from app.modules.bounty_review import build_digest

    db = next(get_session())
    try:
        return {"markdown": build_digest(db, limit=limit)}
    finally:
        db.close()


@app.post("/bounties/{bounty_id}/research")
async def bounties_research(
    _: None = Depends(_require_api_key), bounty_id: int = Path(ge=1)
) -> dict:
    """Generate a private ROI briefing. Posts nothing."""
    from app.modules.bounty_review import research_bounty

    db = next(get_session())
    try:
        return await research_bounty(db, bounty_id)
    finally:
        db.close()


@app.post("/bounties/{bounty_id}/draft")
async def bounties_draft(
    _: None = Depends(_require_api_key), bounty_id: int = Path(ge=1)
) -> dict:
    """Generate a draft solution for YOUR review. Posts nothing."""
    from app.modules.bounty_review import prepare_draft

    db = next(get_session())
    try:
        return await prepare_draft(db, bounty_id)
    finally:
        db.close()


@app.post("/bounties/{bounty_id}/approve")
async def bounties_approve(
    _: None = Depends(_require_api_key), bounty_id: int = Path(ge=1)
) -> dict:
    """Approve a reviewed draft for posting. Deliberate, per-item. Posts nothing."""
    from app.modules.bounty_review import approve

    db = next(get_session())
    try:
        return approve(db, bounty_id)
    finally:
        db.close()


@app.post("/bounties/{bounty_id}/post")
async def bounties_post(
    _: None = Depends(_require_api_key),
    bounty_id: int = Path(ge=1),
    confirm: bool = Query(default=False),
) -> dict:
    """Post an APPROVED draft. Requires approval AND confirm=true."""
    from app.modules.bounty_review import post_approved

    db = next(get_session())
    try:
        return await post_approved(db, bounty_id, confirm=confirm)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# On-chain task automation (testnet airdrop farming)
# ---------------------------------------------------------------------------
@app.post("/tasks/self-transfer")
async def tasks_self_transfer(
    _: None = Depends(_require_api_key),
    chain: str = Query(default="sepolia", min_length=2, max_length=20),
    wallet_index: int = Query(default=10, ge=0, le=1000),
) -> dict:
    """Send a small self-transfer on testnet for airdrop eligibility."""
    from app.modules.zksync_era import send_self_transfer

    db = next(get_session())
    try:
        result = await send_self_transfer(db, chain, wallet_index)
        return result
    finally:
        db.close()


@app.post("/tasks/deploy")
async def tasks_deploy(
    _: None = Depends(_require_api_key),
    chain: str = Query(default="sepolia", min_length=2, max_length=20),
    wallet_index: int = Query(default=10, ge=0, le=1000),
) -> dict:
    """Deploy a test contract on testnet for airdrop eligibility."""
    from app.modules.zksync_era import deploy_test_contract

    db = next(get_session())
    try:
        result = await deploy_test_contract(db, chain, wallet_index)
        return result
    finally:
        db.close()


@app.post("/tasks/run-all")
async def tasks_run_all(
    _: None = Depends(_require_api_key),
    wallet_index: int = Query(default=10, ge=0, le=1000),
) -> dict:
    """Run all on-chain tasks on all testnets."""
    from app.modules.zksync_era import run_all_testnets

    db = next(get_session())
    try:
        results = await run_all_testnets(db, wallet_index)
        return {"chains": len(results), "results": results}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Revenue / P&L tracking
# ---------------------------------------------------------------------------
@app.post("/bounties/{bounty_id}/mark-paid")
async def bounties_mark_paid(
    _: None = Depends(_require_api_key),
    bounty_id: int = Path(ge=1),
    actual_reward: float | None = None,
) -> dict:
    """Mark a bounty as PAID and record the revenue."""
    from app.utils.pnl import record_revenue

    db = next(get_session())
    try:
        bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
        if not bounty:
            return {"success": False, "error": "Bounty not found"}

        reward = actual_reward or bounty.reward_amount or 0
        bounty.status = BountyStatus.PAID
        bounty.updated_at = datetime.now(UTC)
        db.commit()

        if reward > 0:
            record_revenue(
                db,
                "bounties",
                reward,
                currency=bounty.reward_currency,
                source=bounty.url,
                memo=f"Bounty #{bounty.id}: {bounty.title[:60]}",
            )

        return {
            "success": True,
            "bounty_id": bounty.id,
            "reward_eur": reward,
            "revenue_recorded": reward > 0,
        }
    finally:
        db.close()


@app.get("/revenue/summary")
async def revenue_summary(hours: int = Query(default=0, ge=0, le=8760)) -> dict:
    """P&L summary across all modules."""
    from app.utils.pnl import pnl_summary

    db = next(get_session())
    try:
        summary = pnl_summary(db, hours=hours if hours > 0 else None)
        return summary
    finally:
        db.close()


@app.get("/revenue/module/{module}")
async def revenue_module(
    module: str = Path(
        min_length=1,
        max_length=20,
        pattern="^(bounties|arbitrage|airdrops|tasks|defi)$",
    ),
    hours: int = Query(default=0, ge=0, le=8760),
) -> dict:
    """P&L for a specific module."""
    from app.utils.pnl import module_pnl

    valid = {"bounties", "arbitrage", "airdrops", "tasks", "defi"}
    if module not in valid:
        return {"error": f"Invalid module. Choose from: {', '.join(sorted(valid))}"}

    db = next(get_session())
    try:
        pnl = module_pnl(db, module, hours=hours if hours > 0 else None)
        return pnl
    finally:
        db.close()


@app.get("/prices/history/{pair:path}")
async def prices_history(
    pair: str = Path(min_length=1, max_length=50),
    exchange: str = Query(default="", max_length=30),
    hours: int = Query(default=24, ge=1, le=720),
) -> dict:
    """Query price history for a trading pair (trend/volatility analysis)."""
    from app.modules.prices import get_price_history

    db = next(get_session())
    try:
        history = get_price_history(
            db,
            pair=pair.upper(),
            exchange=exchange if exchange else None,
            hours=hours,
        )
        return {
            "pair": pair.upper(),
            "exchange": exchange or "all",
            "hours": hours,
            "count": len(history),
            "snapshots": history,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard", include_in_schema=False)
async def web_dashboard():
    """
    Serves a browser-based monitoring dashboard for the BuckGen agent.
    All data is fetched client-side from the JSON API endpoints.
    """
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BuckGen — Agent Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 20px; }
  .container { max-width: 1400px; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 1.5rem; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; }
  h1 small { font-size: 0.8rem; color: #8b949e; font-weight: normal; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card h3 { color: #8b949e; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .card .value { font-size: 1.8rem; font-weight: 600; color: #f0f6fc; }
  .card .sub { font-size: 0.8rem; color: #8b949e; margin-top: 4px; }
  .status-ok { color: #3fb950; }
  .status-degraded { color: #d29922; }
  .status-down, .status-circuit_open { color: #f85149; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.85rem; }
  th { color: #8b949e; font-weight: 500; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 500; }
  .badge-ok { background: #1b3820; color: #3fb950; }
  .badge-degraded { background: #3d2e00; color: #d29922; }
  .badge-down { background: #3d1111; color: #f85149; }
  .refresh-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
  .refresh-bar button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }
  .refresh-bar button:hover { background: #30363d; }
  .refresh-bar span { color: #8b949e; font-size: 0.8rem; }
  .flex-row { display: flex; gap: 20px; flex-wrap: wrap; }
  .flex-row > * { flex: 1; min-width: 300px; }
  .error-msg { color: #f85149; font-size: 0.8rem; padding: 4px 0; }
  .chart-container { height: 200px; position: relative; }
  .bar { display: inline-block; background: #58a6ff; border-radius: 2px; margin-right: 2px; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
  <h1>BuckGen <small>Resource-Acquiring Agent</small></h1>
  <div class="refresh-bar">
    <button onclick="fetchAll()">Refresh Now</button>
    <span id="lastUpdate">Loading...</span>
  </div>
  <div class="grid" id="summaryCards"></div>
    <div class="flex-row">
      <div class="card" style="flex:2">
        <h3>Modules</h3>
        <table><thead><tr><th>Module</th><th>Status</th><th>Errors</th><th>Last OK</th><th>Last Error</th></tr></thead>
        <tbody id="moduleRows"></tbody></table>
      </div>
      <div class="card" style="flex:1">
        <h3>Chains</h3>
        <table><thead><tr><th>Chain</th><th>Block</th><th>Gas</th></tr></thead>
        <tbody id="chainRows"></tbody></table>
      </div>
    </div>
    <div class="flex-row">
      <div class="card" style="flex:1">
        <h3>P&amp;L by Module</h3>
        <table><thead><tr><th>Module</th><th>Revenue</th><th>Spend</th><th>Profit</th><th>ROI</th></tr></thead>
        <tbody id="pnlRows"></tbody></table>
      </div>
      <div class="card" style="flex:1">
        <h3>Top Revenue Sources</h3>
        <table><thead><tr><th>Source</th><th>Amount</th><th>Module</th></tr></thead>
        <tbody id="topRevenueRows"></tbody></table>
      </div>
    </div>
    <div class="flex-row">
      <div class="card" style="flex:1">
        <h3>Prices</h3>
        <table><thead><tr><th>Pair</th><th>Exchange</th><th>Bid</th><th>Ask</th><th>Last</th></tr></thead>
        <tbody id="priceRows"></tbody></table>
      </div>
      <div class="card" style="flex:1">
        <h3>Arbitrage Opportunities</h3>
        <table><thead><tr><th>Pair</th><th>Buy</th><th>Sell</th><th>Net %</th><th>EUR</th></tr></thead>
        <tbody id="arbRows"></tbody></table>
      </div>
    </div>
</div>
<script>
let autoRefresh = null;
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
function statusBadge(s) {
  const cls = s === 'ok' ? 'ok' : s === 'degraded' ? 'degraded' : 'down';
  return `<span class="badge badge-${cls}">${s}</span>`;
}
function ago(ts) {
  if (!ts || ts === 'never') return 'never';
  const m = ts.match(/(\d+)([smh])/);
  if (!m) return ts;
  return m[1] + m[2] + ' ago';
}
async function fetchAll() {
  const now = new Date().toLocaleTimeString();
  document.getElementById('lastUpdate').textContent = 'Last: ' + now;
  try {
    // Summary cards
    const status = await fetchJSON('/system/status');
    document.getElementById('summaryCards').innerHTML = [
      { label: 'Uptime', value: status.uptime_human, sub: 'since ' + new Date(status.started_at).toLocaleString() },
      { label: 'Modules OK', value: status.modules_ok + '/' + status.modules_total,
        sub: status.modules_degraded + ' degraded, ' + status.modules_down + ' down',
        cls: status.modules_down > 0 ? 'status-down' : status.modules_degraded > 0 ? 'status-degraded' : 'status-ok' },
      { label: 'Errors (1h)', value: status.errors_last_hour,
        sub: 'last 60 minutes', cls: status.errors_last_hour > 0 ? 'status-down' : 'status-ok' },
      { label: 'Chains', value: status.chains_connected + '/' + status.chains_total,
        sub: 'RPC endpoints connected', cls: status.chains_connected === status.chains_total ? 'status-ok' : 'status-degraded' },
      { label: 'Scheduler', value: status.scheduler_running ? 'Running' : 'Stopped',
        sub: status.scheduler_running ? '5 jobs registered' : 'Check logs',
        cls: status.scheduler_running ? 'status-ok' : 'status-down' },
    ].map(c => `<div class="card"><h3>${c.label}</h3><div class="value ${c.cls || ''}">${c.value}</div><div class="sub">${c.sub}</div></div>`).join('');

    // Module rows
    const health = await fetchJSON('/system/health');
    document.getElementById('moduleRows').innerHTML = health.modules.map(m =>
      `<tr><td>${m.name}</td><td>${statusBadge(m.status)}</td><td>${m.total_errors}</td><td>${ago(m.last_success_ago)}</td><td class="error-msg">${m.last_error_msg || ago(m.last_error_ago)}</td></tr>`
    ).join('');

    // Chain rows
    const chains = await fetchJSON('/chains');
    document.getElementById('chainRows').innerHTML = Object.entries(chains).map(([name, c]) =>
      `<tr><td>${name}</td><td>${c.connected ? c.block.toLocaleString() : '-'}</td><td>${c.connected ? c.gas_price_gwei + ' gwei' : 'down'}</td></tr>`
    ).join('');

    // Price rows
    const prices = await fetchJSON('/prices/tickers');
    document.getElementById('priceRows').innerHTML = Object.entries(prices.pairs).flatMap(([pair, ex]) =>
      ex.map(t => `<tr><td>${pair}</td><td>${t.exchange}</td><td>$${t.bid.toFixed(2)}</td><td>$${t.ask.toFixed(2)}</td><td>$${t.last.toFixed(2)}</td></tr>`)
    ).join('');

    // Arb rows (if any)
    const arb = await fetchJSON('/prices/arbitrage?capital=500');
    document.getElementById('arbRows').innerHTML = arb.top_opportunities.length
      ? arb.top_opportunities.map(o =>
          `<tr><td>${o.pair}</td><td>${o.buy_at} @ $${o.buy_price}</td><td>${o.sell_at} @ $${o.sell_price}</td><td>${o.net_profit_pct}%</td><td>EUR ${o.estimated_profit_eur}</td></tr>`
        ).join('')
      : '<tr><td colspan="5" style="text-align:center;color:#8b949e;">No arbitrage opportunities detected (markets are efficient)</td></tr>';

    // P&L rows
    const pnl = await fetchJSON('/revenue/summary');
    document.getElementById('pnlRows').innerHTML = pnl.per_module.map(m =>
      `<tr>
        <td>${m.module}</td>
        <td style="color:#3fb950;">EUR ${m.revenue_eur.toFixed(2)}</td>
        <td style="color:#f85149;">EUR ${m.spend_eur.toFixed(2)}</td>
        <td style="color:${m.profit_eur >= 0 ? '#3fb950' : '#f85149'};">EUR ${m.profit_eur.toFixed(2)}</td>
        <td style="color:${m.roi_pct >= 0 ? '#3fb950' : '#f85149'};">${m.roi_pct}%</td>
      </tr>`
    ).join('') + `
      <tr style="font-weight:bold; border-top: 2px solid #30363d;">
        <td>TOTAL</td>
        <td style="color:#3fb950;">EUR ${pnl.total_revenue_eur.toFixed(2)}</td>
        <td style="color:#f85149;">EUR ${pnl.total_spend_eur.toFixed(2)}</td>
        <td style="color:${pnl.total_profit_eur >= 0 ? '#3fb950' : '#f85149'};">EUR ${pnl.total_profit_eur.toFixed(2)}</td>
        <td style="color:${pnl.total_roi_pct >= 0 ? '#3fb950' : '#f85149'};">${pnl.total_roi_pct}%</td>
      </tr>`;

    // Top revenue sources
    document.getElementById('topRevenueRows').innerHTML = pnl.top_sources.length
      ? pnl.top_sources.map(s =>
          `<tr><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;">${s.source}</td><td style="color:#3fb950;">EUR ${s.amount_eur.toFixed(2)}</td><td>${s.module}</td></tr>`
        ).join('')
      : '<tr><td colspan="3" style="text-align:center;color:#8b949e;">No revenue recorded yet</td></tr>';
  } catch (e) {
    document.getElementById('lastUpdate').textContent = 'Error: ' + e.message;
  }
}
// Auto-refresh every 30s
fetchAll();
setInterval(fetchAll, 30000);
</script>
</body>
</html>"""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# System monitoring endpoints
# ---------------------------------------------------------------------------
@app.get("/system/health")
async def system_health() -> dict:
    """Detailed system health — per-module status, uptime, error counts."""
    return monitor.get_detailed()


@app.get("/system/status")
async def system_status() -> dict:
    """High-level system status summary."""
    summary = monitor.get_summary()
    summary["scheduler_running"] = scheduler.running

    # Add RPC status
    from app.modules.rpc import check_all_chains

    chains = check_all_chains()
    summary["chains_connected"] = sum(1 for c in chains.values() if c.connected)
    summary["chains_total"] = len(chains)
    return summary


@app.post("/system/reset/{module_name}")
async def system_reset(
    _: None = Depends(_require_api_key),
    module_name: str = Path(min_length=1, max_length=50),
) -> dict:
    """Manually reset a module's circuit breaker."""
    ok = monitor.reset_module(module_name)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown module: {module_name}")
    return {"status": "reset", "module": module_name}


@app.post("/system/recover")
async def system_recover(_: None = Depends(_require_api_key)) -> dict:
    """Attempt to recover all degraded modules."""
    results = await monitor.recover_all()
    return {"status": "ok", "recovery_results": results}
