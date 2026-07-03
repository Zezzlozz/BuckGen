"""
Test HD wallet derivation + RPC balance checking.
Run:  python tests\test_wallet_rpc.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.wallet import derive_wallet, CHAIN_CONFIGS
from app.modules.rpc import check_all_chains, get_balance, estimate_gas


def test_wallet_derivation():
    """Test deterministic HD wallet derivation."""
    # Use a well-known test mnemonic (Hardhat default)
    import os

    os.environ["SEED_PHRASE"] = (
        "test test test test test test test test test test test junk"
    )

    acct0 = derive_wallet(index=0, chain="ethereum")
    acct1 = derive_wallet(index=1, chain="ethereum")

    # Hardhat test mnemonic #0
    assert acct0.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", (
        f"Expected Hardhat #0, got {acct0.address}"
    )
    assert acct1.address == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", (
        f"Expected Hardhat #1, got {acct1.address}"
    )

    # Same seed + same path = same address (cached)
    acct0b = derive_wallet(index=0, chain="ethereum")
    assert acct0b is acct0, "Cached wallet should be the same object"

    print(f"  PASS: Wallet derivation (Hardhat #0: {acct0.address})")
    print(f"  PASS: Wallet derivation (Hardhat #1: {acct1.address})")
    print(f"  PASS: Deterministic caching works")


def test_rpc_connectivity():
    """Test that at least one RPC endpoint is reachable."""
    statuses = check_all_chains()
    connected = [chain for chain, s in statuses.items() if s.connected]

    print(f"  {len(connected)}/{len(statuses)} chains connected: {connected}")
    for chain, s in statuses.items():
        if s.connected:
            print(f"    {chain}: block={s.block_number}, gas={s.gas_price_gwei} gwei")

    assert len(connected) >= 1, "At least one chain must be reachable"
    print("  PASS: RPC connectivity")


def test_balance_check():
    """Test balance query for a known address."""
    if "SEED_PHRASE" not in os.environ:
        os.environ["SEED_PHRASE"] = (
            "test test test test test test test test test test test junk"
        )
    bal = get_balance("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", "ethereum")
    print(f"  Test wallet ETH balance: {bal.balance_eth} {bal.symbol}")
    print(f"  has_gas={bal.has_gas}, error='{bal.error}'")

    # Vitalik's address should have some balance
    vitalik = get_balance("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "ethereum")
    print(f"  Vitalik ETH balance: {vitalik.balance_eth} {vitalik.symbol}")
    assert not vitalik.error, f"Balance check should not error: {vitalik.error}"
    print("  PASS: Balance query")


def test_gas_estimate():
    """Test gas estimation."""
    gas = estimate_gas("ethereum")
    assert "error" not in gas, f"Gas estimate failed: {gas}"
    print(
        f"  ETH gas: {gas['gas_price_gwei']} gwei, "
        f"tx cost: {gas['estimated_cost_eth']} ETH"
    )
    print("  PASS: Gas estimation")


if __name__ == "__main__":
    import os

    os.environ["SEED_PHRASE"] = (
        "test test test test test test test test test test test junk"
    )

    tests = [
        ("Wallet derivation", test_wallet_derivation),
        ("RPC connectivity", test_rpc_connectivity),
        ("Balance query", test_balance_check),
        ("Gas estimate", test_gas_estimate),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"{passed}/{passed + failed} tests passed")
    if failed == 0:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
