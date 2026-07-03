"""
Scheduled job definitions.
Each job is a plain async function registered in APScheduler.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Bounty, BountyPlatform, BountyStatus, Wallet, get_session
from app.llm.scorer import score_bounty
from app.modules import gitcoin  # GitHub Issues bounty scanner
from app.modules.system import monitor
from app.utils.notify import notify_alert, notify_bounty_found, notify_error

logger = logging.getLogger("buckgen.jobs")


# ---------------------------------------------------------------------------
# Bounty scanner — every 4 hours
# ---------------------------------------------------------------------------
async def scan_bounties() -> None:
    """
    Search GitHub Issues for open bounty-labelled issues.
    Score each with LLM (parallel).  Store in DB, notify on high-value finds.
    """
    logger.info("[bounty] Scan started at %s", datetime.now(UTC).isoformat())

    raw_list = await gitcoin.fetch_open_bounties(max_bounties=200)
    if not raw_list:
        logger.info("[bounty] No bounties returned — skipping")
        return

    db: Session = next(get_session())
    try:
        # Normalize all bounties first
        norms = [gitcoin.normalize_bounty(raw) for raw in raw_list]

        # ---- Batch existence check (single query instead of N+1) ----
        all_ext_ids = [n["external_id"] for n in norms]
        existing_records = set(
            row[0]
            for row in db.query(Bounty.external_id)
            .filter(
                Bounty.platform == BountyPlatform.GITHUB,
                Bounty.external_id.in_(all_ext_ids),
            )
            .all()
        )

        # Filter to truly new bounties
        new_norms = [n for n in norms if n["external_id"] not in existing_records]

        if not new_norms:
            logger.info("[bounty] All %d bounties already in DB", len(norms))
            monitor.record_success("bounties")
            return

        logger.info(
            "[bounty] %d new bounties to score out of %d", len(new_norms), len(norms)
        )

        # ---- Parallel LLM scoring ----
        score_tasks = [score_bounty(n) for n in new_norms]
        scores = await asyncio.gather(*score_tasks, return_exceptions=True)

        # ---- Bulk insert ----
        new_count = 0
        high_score_count = 0

        for norm, llm_score in zip(new_norms, scores):
            if isinstance(llm_score, Exception):
                llm_score = 0.0
                logger.warning(
                    "[bounty] LLM score failed for %s: %s",
                    norm["title"][:40],
                    llm_score,
                )

            bounty = Bounty(
                platform=BountyPlatform.GITHUB,
                external_id=norm["external_id"],
                title=norm["title"],
                description=norm["description"][:4000],
                reward_amount=norm["reward_amount"],
                reward_currency=norm["reward_currency"],
                experience_level=norm["experience_level"],
                url=norm["url"],
                score=float(llm_score),
                status=BountyStatus.OPEN,
            )
            db.add(bounty)
            new_count += 1

            if float(llm_score) >= 0.6:
                high_score_count += 1
                await notify_bounty_found(
                    {
                        **norm,
                        "score": float(llm_score),
                    }
                )

        db.commit()
        logger.info(
            "[bounty] Scan complete: %d new, %d high-score (>=0.6)",
            new_count,
            high_score_count,
        )
        monitor.record_success("bounties")

    except Exception as exc:
        db.rollback()
        logger.error("[bounty] Scan failed: %s", exc)
        await notify_error("bounty_scan", str(exc))
        monitor.record_error("bounties", str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Gas balance check — every 2 hours
# ---------------------------------------------------------------------------
async def check_gas_balances() -> None:
    """
    Check native token balance for all active wallets across all chains.
    Reports wallets that are low on gas via Telegram.
    """
    logger.info(
        "[gas] Balance check started at %s",
        datetime.now(UTC).isoformat(),
    )

    db: Session = next(get_session())
    try:
        wallets = db.query(Wallet).filter(Wallet.is_active).all()
        if not wallets:
            logger.info("[gas] No active wallets in DB — skipping")
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        from app.modules.rpc import get_balance

        low_gas: list[str] = []
        total_balances: dict[str, float] = {}

        # Parallel wallet balance checks
        def _check_wallet(w: Wallet) -> tuple:
            bal = get_balance(w.address, w.chain)
            return w.id, bal

        with ThreadPoolExecutor(max_workers=min(len(wallets), 10)) as pool:
            fut_map = {pool.submit(_check_wallet, w): w for w in wallets}
            for future in as_completed(fut_map):
                w = fut_map[future]
                try:
                    _, bal = future.result()
                    chain_sym = bal.symbol
                    total_balances[w.chain] = (
                        total_balances.get(w.chain, 0.0) + bal.balance_eth
                    )

                    if not bal.has_gas and bal.error == "":
                        low_gas.append(
                            f"{w.address[:10]}...{w.address[-6:]} on {w.chain} "
                            f"({bal.balance_eth} {chain_sym})"
                        )

                    # Update cached balance in DB
                    w.balance_wei = str(bal.balance_wei)
                    w.last_used_at = datetime.now(UTC)
                except Exception as exc:
                    logger.warning(
                        "[gas] Balance check failed for %s: %s", w.address[:10], exc
                    )

        db.commit()

        # Build summary
        lines = ["*Wallet Balances*"]
        for chain, total in total_balances.items():
            lines.append(f"  {chain}: {total:.6f}")
        summary = "\n".join(lines)
        logger.info("[gas] %s", summary)

        if low_gas:
            alert_msg = "*Low Gas Wallets*\n" + "\n".join(low_gas[:5])
            await notify_alert("Low Gas", alert_msg)
            logger.warning("[gas] %d wallet(s) low on gas", len(low_gas))

        logger.info("[gas] Balance check complete — %d wallets checked", len(wallets))
        monitor.record_success("gas")

    except Exception as exc:
        db.rollback()
        logger.error("[gas] Balance check failed: %s", exc)
        await notify_error("gas_check", str(exc))
        monitor.record_error("gas", str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Price monitor — every hour
# ---------------------------------------------------------------------------
async def check_prices() -> None:
    """
    Monitor exchange rates for arbitrage opportunities.
    Uses ccxt to poll Binance / Coinbase / Kraken / Bybit + CoinGecko.
    Stores ticker snapshots for trend analysis.
    """
    logger.info("[price] Check started at %s", datetime.now(UTC).isoformat())

    db: Session = next(get_session())
    try:
        from app.modules.prices import (
            check_all_prices,
            fetch_all_tickers,
            store_ticker_snapshots,
        )

        result = await check_all_prices(capital_eur=500.0)

        # Store ticker snapshots for trend/volatility analysis
        try:
            # Reuse the pairs from check_all_prices for snapshots
            from app.modules.prices import DEFAULT_TRADING_PAIRS

            exchanges = [
                "binance",
                "coinbase",
                "kraken",
                "bybit",
                "kucoin",
                "okx",
                "gate",
                "mexc",
            ]
            tickers = fetch_all_tickers(DEFAULT_TRADING_PAIRS, exchanges)
            stored = store_ticker_snapshots(db, tickers)
            if stored:
                logger.info("[price] Stored %d price snapshots", stored)
        except Exception as snap_err:
            logger.warning("[price] Snapshot storage failed: %s", snap_err)

        logger.info(
            "[price] Complete: %d pairs, %d tickers, %d arb opportunities",
            result.get("pairs_checked", 0),
            result.get("tickers_fetched", 0),
            result.get("arbitrage_opportunities", 0),
        )

        if result.get("errors"):
            for err in result["errors"]:
                logger.warning("[price] Phase error: %s", err)

        # Notify on high-confidence opportunities
        top_opps = result.get("top_opportunities", [])
        high_conf = [o for o in top_opps if o.get("confidence", 0) >= 0.6]
        if high_conf:
            lines = ["*Arbitrage Opportunities*"]
            for opp in high_conf[:3]:
                lines.append(
                    f"{opp['pair']}: Buy {opp['buy_at']} @ ${opp['buy_price']:.2f} "
                    f"-> Sell {opp['sell_at']} @ ${opp['sell_price']:.2f} "
                    f"(net {opp['net_profit_pct']}%, ~EUR {opp['estimated_profit_eur']})"
                )
            await notify_alert(
                f"{len(high_conf)} Arb Opportunities",
                "\n".join(lines),
            )

        monitor.record_success("prices")

    except Exception as exc:
        db.rollback()
        logger.error("[price] Check failed: %s", exc)
        await notify_error("price_check", str(exc))
        monitor.record_error("prices", str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Airdrop / testnet farmer — every 6 hours
# ---------------------------------------------------------------------------
async def farm_airdrops() -> None:
    """
    Check for new testnet / L2 launch activity.
    Register disposable wallets, claim faucets, complete on-chain tasks.
    """
    logger.info("[airdrop] Farm started at %s", datetime.now(UTC).isoformat())

    db: Session = next(get_session())
    try:
        from app.modules.airdrop import farm_opportunities

        result = await farm_opportunities(db)

        logger.info(
            "[airdrop] Farm complete: %d airdrops, %d wallets, "
            "%d/%d faucet claims, %d registrations",
            result.get("airdrops_discovered", 0),
            result.get("wallets_created", 0),
            result.get("faucet_claims_succeeded", 0),
            result.get("faucet_claims_attempted", 0),
            result.get("registrations", 0),
        )

        if result.get("errors"):
            for err in result["errors"]:
                logger.warning("[airdrop] Error in phase: %s", err)

        if result.get("faucet_claims_succeeded", 0) > 0:
            await notify_alert(
                "Airdrop Farm Complete",
                f"Discovered {result['airdrops_discovered']} opportunities\n"
                f"Created {result['wallets_created']} wallets\n"
                f"Claimed {result['faucet_claims_succeeded']} faucets\n"
                f"Registered for {result['registrations']} airdrops",
            )

        monitor.record_success("airdrops")

    except Exception as exc:
        db.rollback()
        logger.error("[airdrop] Farm failed: %s", exc)
        await notify_error("airdrop_farm", str(exc))
        monitor.record_error("airdrops", str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Self-heal — every 30 minutes
# ---------------------------------------------------------------------------
async def self_heal() -> None:
    """
    Periodic system health check and recovery.
      - Resets circuit breakers for modules that timed out
      - Clears stale RPC/exchange connections
      - Attempts to recover degraded modules
      - Reports system status
    """
    logger.info("[heal] Self-heal cycle started")

    try:
        summary = monitor.get_summary()
        logger.info(
            "[heal] Status: %d/%d modules OK, %d degraded, %d down, "
            "%d errors last hour",
            summary["modules_ok"],
            summary["modules_total"],
            summary["modules_degraded"],
            summary["modules_down"],
            summary["errors_last_hour"],
        )

        # Recover degraded/down modules
        if summary["modules_degraded"] > 0 or summary["modules_down"] > 0:
            logger.info("[heal] Attempting recovery of degraded modules...")
            results = await monitor.recover_all()
            for mod, result in results.items():
                logger.info("[heal] Recovery '%s': %s", mod, result)

        # Notify if things are bad
        if summary["modules_down"] > 2:
            await notify_alert(
                f"System Heal: {summary['modules_down']} modules down",
                f"Degraded: {summary['modules_degraded']}, "
                f"Errors last hour: {summary['errors_last_hour']}",
            )

        monitor.record_success("scheduler")

    except Exception as exc:
        logger.error("[heal] Self-heal failed: %s", exc)
        monitor.record_error("scheduler", str(exc))
