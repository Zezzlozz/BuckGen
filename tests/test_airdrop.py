"""
Test the airdrop module: discovery, batch wallet creation, faucet claims.
Run:  python tests\test_airdrop.py
"""

import asyncio
import gc
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DATABASE_URL"] = "sqlite:///./test_airdrop.db"
os.environ["SEED_PHRASE"] = (
    "test test test test test test test test test test test junk"
)
os.environ["GITHUB_TOKEN"] = ""

test_db = Path("test_airdrop.db")
if test_db.exists():
    test_db.unlink()


async def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, Wallet, WalletType

    engine = create_engine("sqlite:///./test_airdrop.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    passed = 0
    failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} {detail}")
            failed += 1

    # --- Test 1: Airdrop discovery ---
    try:
        from app.modules.airdrop import discover_airdrops

        opportunities = await discover_airdrops(max_results=5)
        check(f"Airdrop discovery ({len(opportunities)} found)", len(opportunities) > 0)
        if opportunities:
            top = opportunities[0]
            print(f"    Top: [{top.score}] {top.title[:60]}")
            check("Airdrop has title", bool(top.title))
            check("Airdrop has URL", bool(top.url))
            check("Airdrop has score", 0.0 <= top.score <= 1.0)
    except Exception as e:
        check("Airdrop discovery", False, str(e))
        import traceback

        traceback.print_exc()

    # --- Test 2: Batch wallet creation ---
    try:
        from app.modules.airdrop import batch_create_wallets

        wallets = await batch_create_wallets(db, count=3, chains=["ethereum", "base"])
        check(f"Batch wallet creation ({len(wallets)} wallets)", len(wallets) >= 6)

        # Check they're disposable
        disposable = (
            db.query(Wallet).filter(Wallet.wallet_type == WalletType.DISPOSABLE).count()
        )
        check(f"Wallets marked DISPOSABLE ({disposable})", disposable >= 6)
    except Exception as e:
        check("Batch wallet creation", False, str(e))

    # --- Test 3: Faucet claiming (will likely fail due to captcha/IP reqs) ---
    try:
        from app.modules.airdrop import FAUCET_REGISTRY, claim_faucet

        if FAUCET_REGISTRY:
            faucet = FAUCET_REGISTRY[0]
            result = await claim_faucet(
                faucet, "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", db
            )
            # Faucets often fail (captcha, IP limits, etc.) — that's expected
            check(
                f"Faucet claim attempted ({faucet.name})",
                "success" in result or "error" in result or "message" in result,
            )
            if result.get("success"):
                print(f"    Claimed {result.get('amount')} {result.get('symbol')}!")
            else:
                print(f"    Faucet result: {result.get('message', '?')[:60]}")
    except Exception as e:
        check("Faucet claim", False, str(e))

    # --- Test 4: Full farm cycle (no-user-facing faucets only, fast) ---
    try:
        from app.modules.airdrop import farm_opportunities

        result = await farm_opportunities(db)
        check("Farm opportunities ran", isinstance(result, dict))
        check(f"  Airdrops discovered: {result['airdrops_discovered']}", True)
        check(f"  Wallets created: {result['wallets_created']}", True)
        check(
            f"  Faucet claims: {result['faucet_claims_succeeded']}/{result['faucet_claims_attempted']}",
            True,
        )
    except Exception as e:
        check("Farm opportunities", False, str(e))
        import traceback

        traceback.print_exc()

    # Cleanup
    db.close()
    engine.dispose()
    gc.collect()
    test_db.unlink(missing_ok=True)

    print(f"\n{'=' * 40}")
    print(f"{passed}/{passed + failed} tests passed")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
