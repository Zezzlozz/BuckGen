"""
SQLAlchemy database models for state persistence.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Text,
    Enum,
    DateTime,
    Boolean,
    create_engine,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker


# ---------------------------------------------------------------------------
# Engine & session
# ---------------------------------------------------------------------------
_engine = None
_SessionLocal = None


def init_db(db_url: str) -> None:
    global _engine, _SessionLocal
    import sqlalchemy

    # Ensure data directory exists for SQLite file
    if db_url.startswith("sqlite"):
        db_path = db_url.replace("sqlite:///", "")
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        db_url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    Base.metadata.create_all(bind=_engine)


def get_session():
    """Yield a session context for FastAPI dependency injection."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class BountyPlatform(str, enum.Enum):
    GITHUB = "github"  # GitHub Issues with bounty labels (primary)
    DEWORK = "dework"
    LAYER3 = "layer3"
    HACKQUEST = "hackquest"


class BountyStatus(str, enum.Enum):
    OPEN = "open"
    APPLIED = "applied"
    SUBMITTED = "submitted"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class WalletType(str, enum.Enum):
    HOT = "hot"
    COLD = "cold"
    DISPOSABLE = "disposable"  # one-time claim wallet


# ---------------------------------------------------------------------------
# Bounty tracking
# ---------------------------------------------------------------------------
class Bounty(Base):
    __tablename__ = "bounties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(Enum(BountyPlatform), nullable=False)
    external_id = Column(String(255), nullable=False)
    title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    reward_amount = Column(Float, default=0.0)
    reward_currency = Column(String(32), default="USD")
    experience_level = Column(String(64), default="")
    url = Column(String(1024), default="")
    status = Column(Enum(BountyStatus), default=BountyStatus.OPEN)
    score = Column(Float, default=0.0)  # LLM-assigned score
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_platform_external"),
        Index("ix_bounty_status", "status"),
        Index("ix_bounty_score", "score"),
        Index("ix_bounty_status_score", "status", "score"),
    )


# ---------------------------------------------------------------------------
# Wallet management
# ---------------------------------------------------------------------------
class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(128), unique=True, nullable=False, index=True)
    wallet_type = Column(Enum(WalletType), default=WalletType.HOT)
    derivation_path = Column(String(128), default="")
    chain = Column(String(32), default="ethereum")
    balance_wei = Column(
        String(64), default="0"
    )  # stored as string to avoid precision loss
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_wallet_chain_type_active", "chain", "wallet_type", "is_active"),
        Index("ix_wallet_is_active", "is_active"),
    )


# ---------------------------------------------------------------------------
# Transaction log
# ---------------------------------------------------------------------------
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_id = Column(Integer, nullable=False)
    chain = Column(String(32), default="ethereum")
    tx_hash = Column(String(256), unique=True, nullable=True)
    tx_type = Column(String(64), default="")  # "claim", "transfer", "swap", "gas"
    amount_wei = Column(String(64), default="0")
    gas_used_wei = Column(String(64), default="0")
    status = Column(String(32), default="pending")  # pending, confirmed, failed
    memo = Column(String(512), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    confirmed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_tx_wallet", "wallet_id"),
        Index("ix_tx_type", "tx_type"),
        Index("ix_tx_status", "status"),
        Index("ix_tx_chain_status", "chain", "status"),
    )


# ---------------------------------------------------------------------------
# Blacklist — unreliable sources
# ---------------------------------------------------------------------------
class BlacklistEntry(Base):
    __tablename__ = "blacklist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(String(64), nullable=False)  # "platform", "contract", "user"
    source_id = Column(String(255), nullable=False)
    reason = Column(String(512), default="")
    failed_attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_blacklist_source"),
    )


# ---------------------------------------------------------------------------
# Budget tracking
# ---------------------------------------------------------------------------
class BudgetEntry(Base):
    __tablename__ = "budget"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(16), nullable=False)  # YYYY-MM-DD
    category = Column(String(64), default="gas")  # gas, api, proxy
    amount_eur = Column(Float, default=0.0)
    memo = Column(String(256), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_budget_date", "date"),)


# ---------------------------------------------------------------------------
# Revenue / P&L tracking
# ---------------------------------------------------------------------------
class RevenueEntry(Base):
    """Track incoming revenue by module for P&L analysis."""

    __tablename__ = "revenue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(
        String(64), nullable=False, index=True
    )  # "bounties", "arbitrage", "airdrops"
    amount_eur = Column(Float, nullable=False)
    currency = Column(String(16), default="EUR")
    source = Column(String(255), default="")  # e.g. bounty URL, arb pair, airdrop name
    memo = Column(String(512), default="")
    earned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_revenue_module", "module"),
        Index("ix_revenue_earned_at", "earned_at"),
    )


# ---------------------------------------------------------------------------
# Price history (ticker snapshots for trend analysis)
# ---------------------------------------------------------------------------
class PriceSnapshot(Base):
    """Periodic ticker snapshot for trend/volatility analysis."""

    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(32), nullable=False, index=True)  # "BTC/USDT"
    exchange = Column(String(32), nullable=False)
    bid = Column(Float, default=0.0)
    ask = Column(Float, default=0.0)
    last = Column(Float, default=0.0)
    volume = Column(Float, default=0.0)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_snapshot_pair_time", "pair", "recorded_at"),)
