"""
Pytest fixtures for unit tests.

Sets up:
  - A clean in-memory SQLite database with all tables created.
  - A reusable DB session.
  - Preserved environment variables (SEED_PHRASE, etc.).
"""

import gc
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Ensure SEED_PHRASE is set before any imports that read settings
os.environ.setdefault(
    "SEED_PHRASE",
    "test test test test test test test test test test test junk",
)
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_unit.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")


@pytest.fixture(scope="function")
def db_session():
    """Provide a clean database session for each test.

    All tables are created fresh and rolled back / disposed after the test.
    This keeps tests fully isolated from each other.
    """
    from app.db.models import Base

    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session: Session = SessionLocal()

    yield session

    session.close()
    engine.dispose()
    gc.collect()


@pytest.fixture(scope="session", autouse=True)
def _preserve_env():
    """Ensure key env vars exist for the whole test session."""
    os.environ.setdefault(
        "SEED_PHRASE",
        "test test test test test test test test test test test junk",
    )
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
    yield


@pytest.fixture(scope="function")
def sample_budget_entry(db_session):
    """Pre-seed a BudgetEntry for spend-related tests."""
    from app.utils.budget import record_spend

    entry = record_spend(db_session, 10.0, "gas", "test spend")
    return entry


@pytest.fixture(scope="function")
def sample_revenue_entry(db_session):
    """Pre-seed a RevenueEntry for P&L-related tests."""
    from app.utils.pnl import record_revenue

    entry = record_revenue(
        db_session,
        module="arbitrage",
        amount_eur=50.0,
        source="BTC/USDT binance->kraken",
        memo="test revenue",
    )
    return entry


@pytest.fixture(scope="function")
def sample_ticker_prices():
    """TickerPrice-like dicts for arbitrage detection tests.

    Prices are set so that a profitable gap exists between binance (buy)
    and kraken (sell) after accounting for fees (binance 0.1% + kraken
    0.26% + 0.2% threshold = 0.56% minimum gap).
    """
    from app.modules.prices import TickerPrice

    return [
        TickerPrice(
            exchange="binance",
            symbol="BTC/USDT",
            bid=100.0,
            ask=100.0,  # buy at 100
            last=100.0,
            volume=1_000_000,
            timestamp=1_000_000_000,
        ),
        TickerPrice(
            exchange="kraken",
            symbol="BTC/USDT",
            bid=101.5,  # sell at 101.5 -> 1.5% gap before fees
            ask=101.5,
            last=101.5,
            volume=500_000,
            timestamp=1_000_000_000,
        ),
        TickerPrice(
            exchange="coinbase",
            symbol="BTC/USDT",
            bid=99.0,
            ask=99.0,
            last=99.0,
            volume=100_000,
            timestamp=1_000_000_000,
        ),
    ]


@pytest.fixture(scope="function")
def mock_settings(monkeypatch):
    """Set known config values for deterministic tests."""
    monkeypatch.setattr("app.config.settings.DAILY_GAS_CAP_EUR", 50.0)
    monkeypatch.setattr("app.config.settings.STOP_LOSS_EUR", 500.0)
    monkeypatch.setattr("app.config.settings.BINANCE_TRADE_KEY", "test_key_binance")
    monkeypatch.setattr(
        "app.config.settings.BINANCE_TRADE_SECRET", "test_secret_binance"
    )
    monkeypatch.setattr("app.config.settings.KRAKEN_TRADE_KEY", "test_key_kraken")
    monkeypatch.setattr("app.config.settings.KRAKEN_TRADE_SECRET", "test_secret_kraken")
    monkeypatch.setattr("app.config.settings.BYBIT_TRADE_KEY", "test_key_bybit")
    monkeypatch.setattr("app.config.settings.BYBIT_TRADE_SECRET", "test_secret_bybit")
    yield
