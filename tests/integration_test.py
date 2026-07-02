"""
Full integration test.
Run:  python tests\integration_test.py
"""

import asyncio
import sys
import os
import gc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DATABASE_URL"] = "sqlite:///./test_integration.db"
os.environ["SEED_PHRASE"] = (
    "test test test test test test test test test test test junk"
)

test_db = Path("test_integration.db")
if test_db.exists():
    test_db.unlink()


async def run_tests():
    import time

    passed = 0
    failed = 0
    errors = []

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} {detail}")
            failed += 1
            errors.append(f"{name}: {detail}")

    # --- 1. Config ---
    try:
        from app.config import settings

        assert hasattr(settings, "GITHUB_TOKEN")
        assert hasattr(settings, "SEED_PHRASE")
        assert (
            settings.SEED_PHRASE
            == "test test test test test test test test test test test junk"
        )
        check("Config loads and has SEED_PHRASE", True)
    except Exception as e:
        check("Config loads", False, str(e))

    # --- 2. DB init ---
    try:
        from app.db.models import (
            Base,
            Bounty,
            Wallet,
            WalletType,
            BountyStatus,
            BountyPlatform,
        )
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///./test_integration.db")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()
        check("DB tables created", True)
    except Exception as e:
        check("DB init", False, str(e))
        return

    # --- 3. Wallet derivation ---
    try:
        from app.modules.wallet import derive_wallet, sync_wallet_to_db

        acct0 = derive_wallet(index=0, chain="ethereum")
        assert acct0.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", (
            f"Expected Hardhat #0, got {acct0.address}"
        )
        check("HD wallet derivation (Hardhat #0)", True)

        acct1 = derive_wallet(index=1, chain="ethereum")
        assert acct1.address == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        check("HD wallet derivation (Hardhat #1)", True)

        # DB sync
        w = sync_wallet_to_db(db, index=0, chain="ethereum")
        assert w.address == acct0.address
        check("Wallet synced to DB", True)

        # Private key access
        from app.modules.wallet import get_private_key

        pk = get_private_key(acct0.address)
        assert pk is not None and len(pk) == 32
        check("Private key accessible in memory", True)

        # Keyring zeroing
        from app.modules.wallet import zero_keyring, get_all_wallets

        assert len(get_all_wallets()) >= 2
        zero_keyring()
        assert len(get_all_wallets()) == 0
        check("Keyring zeroed on shutdown", True)
    except Exception as e:
        check("Wallet derivation", False, str(e))
        import traceback

        traceback.print_exc()

    # --- 4. RPC connectivity ---
    try:
        from app.modules.rpc import check_all_chains, get_balance, estimate_gas

        statuses = check_all_chains()
        connected = sum(1 for s in statuses.values() if s.connected)
        check(
            f"RPC connected ({connected}/{len(statuses)} chains)",
            connected >= 1,
            f"connected chains: {connected}",
        )
        if connected >= 1:
            # Show which chains are live
            live = [c for c, s in statuses.items() if s.connected]
            check(f"Live chains: {', '.join(live)}", True)

        bal = get_balance("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "ethereum")
        assert bal.balance_eth > 0, (
            f"Vitalik should have ETH balance, got {bal.balance_eth}"
        )
        check(f"Balance query (Vitalik: {bal.balance_eth} ETH)", True)

        gas = estimate_gas("ethereum")
        assert "error" not in gas, f"Gas estimate error: {gas}"
        check(
            f"Gas estimation ({gas['gas_price_gwei']} gwei, {gas['estimated_cost_eth']} ETH)",
            True,
        )
    except Exception as e:
        check("RPC module", False, str(e))
        import traceback

        traceback.print_exc()

    # --- 5. Bounty scan ---
    try:
        from app.modules.gitcoin import fetch_open_bounties, normalize_bounty
        from app.llm.scorer import score_bounty

        bounties = await fetch_open_bounties(max_bounties=5)
        assert len(bounties) > 0, "No bounties returned"
        check(f"Fetched {len(bounties)} GitHub bounty issues", True)

        norm = normalize_bounty(bounties[0])
        required = {
            "external_id",
            "title",
            "description",
            "reward_amount",
            "reward_currency",
            "experience_level",
            "url",
            "repo",
        }
        assert required.issubset(norm.keys())
        check("normalize_bounty returns all keys", True)

        score = await score_bounty(norm)
        assert 0.0 <= score <= 1.0
        check(f"LLM score = {score:.2f}", True)

        # Save to DB
        bounty = Bounty(
            platform=BountyPlatform.GITHUB,
            external_id=norm["external_id"],
            title=norm["title"],
            description=norm["description"][:1024],
            reward_amount=norm["reward_amount"],
            reward_currency=norm["reward_currency"],
            experience_level=norm["experience_level"],
            url=norm["url"],
            score=score,
            status=BountyStatus.OPEN,
        )
        db.add(bounty)
        db.commit()
        saved = db.query(Bounty).filter_by(external_id=norm["external_id"]).first()
        assert saved is not None
        check("Bounty persisted to DB", True)
    except Exception as e:
        check("Bounty scan", False, str(e))

    # --- 6. Budget ---
    try:
        from app.utils.budget import within_daily_cap, within_stop_loss, can_spend

        assert within_daily_cap(db)
        assert within_stop_loss(db)
        assert can_spend(db, 1.0)
        check("Budget guards pass", True)
    except Exception as e:
        check("Budget", False, str(e))

    # --- 7. Blacklist ---
    try:
        from app.utils.blacklist import is_blacklisted

        assert not is_blacklisted(db, "platform", "github:test")
        check("Blacklist: new source not blacklisted", True)
    except Exception as e:
        check("Blacklist", False, str(e))

    # --- 8. Exchange tickers ---
    try:
        from app.modules.prices import fetch_all_tickers, DEFAULT_TRADING_PAIRS

        tickers = fetch_all_tickers()
        total = sum(len(ts) for ts in tickers.values())
        check(f"Exchange tickers ({total})", total > 0)

        btc = [t for t in tickers.get("BTC/USDT", []) if not t.error and t.last > 0]
        check(f"BTC/USDT from {len(btc)} exchanges", len(btc) >= 2)
        if btc:
            min_p = min(t.last for t in btc)
            max_p = max(t.last for t in btc)
            check(
                f"BTC spread: ${min_p:.2f}-${max_p:.2f} ({((max_p - min_p) / min_p * 100):.3f}%)",
                max_p > min_p,
            )
    except Exception as e:
        check("Exchange tickers", False, str(e))

    # --- 9. CoinGecko ---
    try:
        from app.modules.prices import fetch_all_coingecko

        cg = await fetch_all_coingecko()
        btc_cg = cg.get("BTC", {})
        if hasattr(btc_cg, "usd"):
            check(f"CG BTC: ${btc_cg.usd}", btc_cg.usd > 0)
        check(f"CG prices fetched ({len(cg)} coins)", len(cg) >= 3)
    except Exception as e:
        check("CoinGecko", False, str(e))

    # --- 10. Arbitrage scan ---
    try:
        from app.modules.prices import (
            fetch_all_tickers,
            fetch_all_coingecko,
            find_arbitrage_opportunities,
        )

        tickers = fetch_all_tickers()
        cg = await fetch_all_coingecko()
        opps = find_arbitrage_opportunities(tickers, cg, capital_eur=100.0)
        check(f"Arbitrage scan ({len(opps)} opportunities)", isinstance(opps, list))
    except Exception as e:
        check("Arbitrage scan", False, str(e))

    # --- 11. System monitor ---
    try:
        from app.modules.system import monitor

        summary = monitor.get_summary()
        check("System monitor has modules", summary["modules_total"] >= 7)
        check("All modules start OK", summary["modules_ok"] == summary["modules_total"])
        check("Zero errors at startup", summary["errors_last_hour"] == 0)
        check("Uptime tracking works", summary["uptime_seconds"] >= 0)

        # Test error recording
        monitor.record_error("test_module", "test error")
        monitor.record_error("test_module", "test error 2")
        state = monitor.get_state("test_module")
        check("Error tracking works", state["total_errors"] == 2)
        check("Status degraded after errors", state["status"] == "degraded")

        # Test reset
        monitor.reset_module("test_module")
        state = monitor.get_state("test_module")
        check("Reset clears errors", state["consecutive_failures"] == 0)
        check("Reset restores status", state["status"] == "ok")

        # Test detailed output
        detailed = monitor.get_detailed()
        check("Detailed report has module list", len(detailed["modules"]) >= 7)
    except Exception as e:
        check("System monitor", False, str(e))

    # --- 12. Airdrop discovery ---
    try:
        from app.modules.airdrop import discover_airdrops, batch_create_wallets

        opps = await discover_airdrops(max_results=5)
        check(f"Airdrop discovery ({len(opps)} found)", len(opps) > 0)
        if opps:
            check(f"Top airdrop score: {opps[0].score:.2f}", opps[0].score > 0)

        wallets = await batch_create_wallets(
            db, count=2, chains=["ethereum", "arbitrum"]
        )
        check(f"Batch wallets ({len(wallets)} created)", len(wallets) >= 4)
        disposable = (
            db.query(Wallet).filter(Wallet.wallet_type == WalletType.DISPOSABLE).count()
        )
        check(f"DISPOSABLE wallets in DB ({disposable})", disposable >= 4)
    except Exception as e:
        check("Airdrop module", False, str(e))

    # Cleanup
    db.close()
    engine.dispose()
    gc.collect()
    test_db.unlink(missing_ok=True)

    print(f"\n{'=' * 40}")
    print(f"{passed}/{passed + failed} tests passed")
    if failed == 0:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        for e in errors:
            print(f"  - {e}")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
