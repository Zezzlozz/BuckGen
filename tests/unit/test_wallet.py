"""
Unit tests for HD Wallet Manager (app/modules/wallet.py).

Tests cover:
  - BIP44 derivation path generation
  - Wallet derivation with known seed
  - Keyring caching behaviour
  - DB sync operations
  - Private key access
  - Keyring zeroing on shutdown
  - Batch wallet creation
"""

import pytest
from eth_account.signers.local import LocalAccount

from app.modules.wallet import (
    _bip44_path,
    derive_wallet,
    get_wallet,
    get_all_wallets,
    sync_wallet_to_db,
    derive_and_sync_batch,
    get_private_key,
    zero_keyring,
    CHAIN_CONFIGS,
)
from app.db.models import Wallet, WalletType


# =============================================================================
# Known addresses for the Hardhat test mnemonic:
#   "test test test test test test test test test test test junk"
# =============================================================================
HARDHAT_ACCOUNTS = [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
]


@pytest.fixture(autouse=True)
def _clear_keyring():
    """Ensure a clean keyring before and after each test."""
    zero_keyring()
    yield
    zero_keyring()


class TestBip44Path:
    """_bip44_path generates correct derivation paths."""

    def test_default_coin_type(self):
        assert _bip44_path(0) == "m/44'/60'/0'/0/0"

    def test_index_1(self):
        assert _bip44_path(1) == "m/44'/60'/0'/0/1"

    def test_index_5(self):
        assert _bip44_path(5) == "m/44'/60'/0'/0/5"

    def test_custom_coin_type(self):
        assert _bip44_path(0, coin_type=501) == "m/44'/501'/0'/0/0"


class TestDeriveWallet:
    """derive_wallet returns correct accounts."""

    def test_derives_hardhat_0(self):
        acct = derive_wallet(index=0)
        assert acct.address == HARDHAT_ACCOUNTS[0]

    def test_derives_hardhat_1(self):
        acct = derive_wallet(index=1)
        assert acct.address == HARDHAT_ACCOUNTS[1]

    def test_derives_hardhat_4(self):
        """Index 4 should be the 5th Hardhat account (0-indexed)."""
        acct = derive_wallet(index=4)
        assert acct.address == HARDHAT_ACCOUNTS[4], (
            f"Index 4 expected {HARDHAT_ACCOUNTS[4]}, got {acct.address}"
        )

    def test_returns_local_account(self):
        acct = derive_wallet(index=0)
        assert isinstance(acct, LocalAccount)
        assert len(acct.key) == 32  # private key is 32 bytes

    def test_different_indices_different_addresses(self):
        a0 = derive_wallet(0).address
        a1 = derive_wallet(1).address
        assert a0 != a1

    def test_caches_across_calls(self):
        acct1 = derive_wallet(0)
        acct2 = derive_wallet(0)
        assert acct1 is acct2  # same object from cache


class TestKeyringCache:
    """Keyring caches derived wallets."""

    def test_enters_keyring(self):
        derive_wallet(0)
        assert len(get_all_wallets()) == 1

    def test_get_wallet_by_path(self, _clear_keyring):
        derive_wallet(0)
        path = "m/44'/60'/0'/0/0"
        acct = get_wallet(path)
        assert acct is not None
        assert acct.address == HARDHAT_ACCOUNTS[0]

    def test_get_wallet_missing_path(self):
        assert get_wallet("m/44'/60'/0'/0/99") is None

    def test_all_wallets_list(self):
        derive_wallet(0)
        derive_wallet(1)
        assert len(get_all_wallets()) == 2


class TestSyncWalletToDb:
    """sync_wallet_to_db persists wallets and avoids duplicates."""

    def test_sync_creates_wallet(self, db_session):
        w = sync_wallet_to_db(db_session, index=0)
        assert isinstance(w, Wallet)
        assert w.address == HARDHAT_ACCOUNTS[0]
        assert w.wallet_type == WalletType.HOT
        assert w.is_active is True
        assert w.chain == "ethereum"

    def test_sync_idempotent(self, db_session):
        w1 = sync_wallet_to_db(db_session, index=0)
        w2 = sync_wallet_to_db(db_session, index=0)
        assert w1.id == w2.id  # same row, not duplicated

    def test_sync_with_chain_and_type(self, db_session):
        w = sync_wallet_to_db(
            db_session, index=1, chain="base", wallet_type=WalletType.DISPOSABLE
        )
        assert w.chain == "base"
        assert w.wallet_type == WalletType.DISPOSABLE

    def test_sync_derivation_path(self, db_session):
        w = sync_wallet_to_db(db_session, index=2)
        assert w.derivation_path == "m/44'/60'/0'/0/2"


class TestGetPrivateKey:
    """get_private_key retrieves the correct key."""

    def test_returns_key_for_derived_address(self):
        acct = derive_wallet(0)
        pk = get_private_key(acct.address)
        assert pk == acct.key
        assert len(pk) == 32

    def test_case_insensitive(self):
        acct = derive_wallet(0)
        pk = get_private_key(acct.address.upper())
        assert pk is not None

    def test_none_for_unknown_address(self):
        assert get_private_key("0x0000000000000000000000000000000000000000") is None

    def test_none_after_zeroing(self):
        acct = derive_wallet(0)
        zero_keyring()
        assert get_private_key(acct.address) is None


class TestZeroKeyring:
    """zero_keyring clears all private keys from memory."""

    def test_clears_all_wallets(self):
        derive_wallet(0)
        derive_wallet(1)
        zero_keyring()
        assert len(get_all_wallets()) == 0

    def test_wallet_unavailable_after_zero(self):
        derive_wallet(0)
        zero_keyring()
        assert get_wallet("m/44'/60'/0'/0/0") is None


class TestDeriveAndSyncBatch:
    """derive_and_sync_batch creates N wallets and syncs across chains.

    Note: All EVM chains share coin_type=60, so the same wallet index
    produces the same address. sync_wallet_to_db deduplicates by address,
    returning the existing Wallet record. The returned list contains all
    sync_wallet_to_db results (one per (chain, index) combination).
    """

    def test_default_returns_all_combinations(self, db_session):
        """count=2 * 5 chains = 10 items in the returned list."""
        wallets = derive_and_sync_batch(db_session, count=2)
        assert len(wallets) == 10  # 2 indices x 5 chains

    def test_specific_chains(self, db_session):
        """count=3 * 2 chains = 6 items."""
        wallets = derive_and_sync_batch(
            db_session, count=3, chains=["ethereum", "base"]
        )
        assert len(wallets) == 6

    def test_unique_wallets_in_db(self, db_session):
        """Only unique addresses are stored in DB, not chain variants."""
        derive_and_sync_batch(db_session, count=1)
        total = db_session.query(Wallet).count()
        assert total == 1  # 1 unique wallet (index 0)

    def test_duplicates_not_recreated(self, db_session):
        """Running twice produces no new DB rows."""
        derive_and_sync_batch(db_session, count=1)
        derive_and_sync_batch(db_session, count=1)
        total = db_session.query(Wallet).count()
        assert total == 1
