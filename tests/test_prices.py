"""
Test the price/arbitrage module: exchange tickers, CoinGecko, arb detection.
Run:  python tests\test_prices.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
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

    # --- Test 1: Exchange tickers ---
    try:
        from app.modules.prices import fetch_all_tickers, DEFAULT_TRADING_PAIRS

        tickers = fetch_all_tickers()
        total = sum(len(ts) for ts in tickers.values())
        check(f"Tickers fetched ({total} total)", total > 0)

        # Check each pair has valid data
        for pair in DEFAULT_TRADING_PAIRS:
            valid = [t for t in tickers.get(pair, []) if not t.error and t.last > 0]
            if valid:
                print(
                    f"    {pair}: {len(valid)} exchanges, "
                    f"prices ${min(t.last for t in valid):.2f} - "
                    f"${max(t.last for t in valid):.2f}"
                )
    except Exception as e:
        check("Exchange tickers", False, str(e))
        import traceback

        traceback.print_exc()

    # --- Test 2: CoinGecko prices ---
    try:
        from app.modules.prices import fetch_all_coingecko

        cg_prices = await fetch_all_coingecko()
        check(f"CoinGecko prices ({len(cg_prices)} coins)", len(cg_prices) > 0)
        for sym, p in cg_prices.items():
            if p.usd > 0:
                print(f"    {sym}: ${p.usd:.2f} (24h: {p.usd_24h_change:+.2f}%)")
                break
    except Exception as e:
        check("CoinGecko prices", False, str(e))

    # --- Test 3: Arbitrage detection ---
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
        if opps:
            top = opps[0]
            print(
                f"    Top: {top.pair} Buy {top.buy_at} @ ${top.buy_price:.2f} "
                f"-> Sell {top.sell_at} @ ${top.sell_price:.2f}"
            )
            print(
                f"    Gap: {top.gap_pct}%, Net: {top.net_profit_pct}%, "
                f"Est profit: EUR {top.estimated_profit_eur}"
            )
            check("Top opp has valid gap", top.gap_pct > 0)
            check("Top opp has profit estimate", top.estimated_profit_eur >= 0)
        else:
            print("    No arb opportunities found (markets may be efficient)")
            check("No arb opportunities", True)
    except Exception as e:
        check("Arbitrage detection", False, str(e))
        import traceback

        traceback.print_exc()

    # --- Test 4: Full check_all_prices ---
    try:
        from app.modules.prices import check_all_prices

        result = await check_all_prices(capital_eur=100.0)
        check(f"Full price check ran", isinstance(result, dict))
        check(f"  Pairs: {result['pairs_checked']}", result["pairs_checked"] > 0)
        check(
            f"  Exchanges: {result['exchanges_checked']}",
            result["exchanges_checked"] > 0,
        )
        check(f"  Tickers: {result['tickers_fetched']}", result["tickers_fetched"] > 0)
        check(
            f"  CG prices: {result['coingecko_prices']}", result["coingecko_prices"] > 0
        )
    except Exception as e:
        check("Full price check", False, str(e))
        import traceback

        traceback.print_exc()

    print(f"\n{'=' * 40}")
    print(f"{passed}/{passed + failed} tests passed")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
