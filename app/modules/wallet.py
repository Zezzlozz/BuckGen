"""
HD Wallet Manager — derives EVM wallets from an encrypted seed phrase.

Supports:
  - BIP44 derivation (m/44'/60'/0'/0/i) for N sequential accounts
  - In-memory private key storage (never written to disk unencrypted)
  - DB persistence of derived addresses + derivation paths
  - Wallet recovery from seed phrase

Security:
  - Seed phrase is loaded from config (AES-256-GCM encrypted at rest)
  - Private keys live in a dict, zeroed on shutdown
  - Only public addresses are stored in the database
"""

import logging

from eth_account import Account
from eth_account.signers.local import LocalAccount
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Wallet, WalletType
from app.utils.crypto import zero_bytes

logger = logging.getLogger("buckgen.wallet")

# In-memory cache: derivation_path -> LocalAccount
_keyring: dict[str, LocalAccount] = {}

# Enable HD wallet features in eth_account
Account.enable_unaudited_hdwallet_features()


# ---------------------------------------------------------------------------
# Chain config
# ---------------------------------------------------------------------------
# BIP44 coin types for supported chains (all EVM uses 60')
# Format: (chain_id, name, symbol, explorer_url)
CHAIN_CONFIGS: dict[str, dict] = {
    "ethereum": {
        "chain_id": 1,
        "coin_type": 60,
        "symbol": "ETH",
        "explorer": "https://etherscan.io/address/{addr}",
    },
    "base": {
        "chain_id": 8453,
        "coin_type": 60,
        "symbol": "ETH",
        "explorer": "https://basescan.org/address/{addr}",
    },
    "arbitrum": {
        "chain_id": 42161,
        "coin_type": 60,
        "symbol": "ETH",
        "explorer": "https://arbiscan.io/address/{addr}",
    },
    "polygon": {
        "chain_id": 137,
        "coin_type": 60,
        "symbol": "MATIC",
        "explorer": "https://polygonscan.com/address/{addr}",
    },
    "bsc": {
        "chain_id": 56,
        "coin_type": 60,
        "symbol": "BNB",
        "explorer": "https://bscscan.com/address/{addr}",
    },
}


def _bip44_path(index: int, coin_type: int = 60) -> str:
    """Return BIP44 derivation path for account index."""
    return f"m/44'/{coin_type}'/0'/0/{index}"


def get_seed_phrase() -> str:
    """Return the decrypted seed phrase from config.
    Raises ValueError if not configured.
    """
    seed = settings.SEED_PHRASE
    if not seed:
        raise ValueError(
            "SEED_PHRASE not configured. Set it in .env or use encrypt_seed()."
        )
    return seed


# ---------------------------------------------------------------------------
# Wallet derivation
# ---------------------------------------------------------------------------
def derive_wallet(
    index: int = 0,
    chain: str = "ethereum",
) -> LocalAccount:
    """
    Derive (or retrieve from cache) a wallet by index.

    Args:
        index: Sequential account index (0, 1, 2, ...)
        chain: Chain name from CHAIN_CONFIGS (determines coin_type)

    Returns:
        LocalAccount with .address and .key (private key bytes)

    The private key is cached in memory until shutdown.
    """
    coin_type = CHAIN_CONFIGS.get(chain, CHAIN_CONFIGS["ethereum"])["coin_type"]
    path = _bip44_path(index, coin_type)

    if path in _keyring:
        return _keyring[path]

    seed = get_seed_phrase()
    account = Account.from_mnemonic(seed, account_path=path)
    _keyring[path] = account
    logger.info("Derived wallet %s at path %s", account.address, path)
    return account


def get_wallet(path: str) -> LocalAccount | None:
    """Retrieve a previously derived wallet by its derivation path."""
    return _keyring.get(path)


def get_all_wallets() -> list[LocalAccount]:
    """Return all currently loaded wallets."""
    return list(_keyring.values())


# ---------------------------------------------------------------------------
# DB sync
# ---------------------------------------------------------------------------
def sync_wallet_to_db(
    db: Session,
    index: int = 0,
    chain: str = "ethereum",
    wallet_type: WalletType = WalletType.HOT,
) -> Wallet:
    """
    Derive a wallet and ensure it exists in the database.

    Returns the Wallet ORM object (without private key).
    """
    account = derive_wallet(index, chain)
    coin_type = CHAIN_CONFIGS.get(chain, CHAIN_CONFIGS["ethereum"])["coin_type"]
    path = _bip44_path(index, coin_type)

    existing = db.query(Wallet).filter(Wallet.address == account.address).first()
    if existing:
        return existing

    wallet = Wallet(
        address=account.address,
        wallet_type=wallet_type,
        derivation_path=path,
        chain=chain,
        is_active=True,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    logger.info("Wallet %s synced to DB (chain=%s)", account.address, chain)
    return wallet


def derive_and_sync_batch(
    db: Session,
    count: int = 3,
    chains: list[str] | None = None,
) -> list[Wallet]:
    """
    Derive N wallets per chain and sync all to DB.
    Used for airdrop farming (many disposable wallets).

    Args:
        count: Number of wallets per chain
        chains: List of chain names (default: all supported chains)

    Returns:
        List of Wallet ORM objects
    """
    if chains is None:
        chains = list(CHAIN_CONFIGS.keys())

    wallets: list[Wallet] = []
    for chain in chains:
        for i in range(count):
            wallet = sync_wallet_to_db(db, index=i, chain=chain)
            wallets.append(wallet)
    return wallets


# ---------------------------------------------------------------------------
# Private key access (cautious)
# ---------------------------------------------------------------------------
def get_private_key(address: str) -> bytes | None:
    """
    Get the private key bytes for a derived address.
    Returns None if the wallet hasn't been derived in this session.

    The agent uses this to sign transactions in memory only.
    Never log or persist the returned key.
    """
    for path, account in _keyring.items():
        if account.address.lower() == address.lower():
            return account.key
    return None


def get_private_key_for_path(path: str) -> bytes | None:
    """Get private key by derivation path."""
    account = _keyring.get(path)
    return account.key if account else None


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def zero_keyring() -> None:
    """Zero all private keys in memory. Call on application shutdown."""
    for path, account in _keyring.items():
        zero_bytes(account.key)
    _keyring.clear()
    logger.info("Keyring zeroed and cleared")
