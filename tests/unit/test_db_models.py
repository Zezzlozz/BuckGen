"""
Unit tests for Database Models & Session Management (app/db/models.py).

Tests cover:
  - init_db: creates engine, tables, returns sessionmaker
  - get_session: yields usable sessions
  - Enum values for BountyPlatform, BountyStatus, WalletType
"""

import gc

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db.models import (
    Bounty,
    BountyPlatform,
    BountyStatus,
    WalletType,
    get_session,
    init_db,
)


class TestInitDb:
    """init_db — creates tables and sets up global engine/session."""

    def test_creates_tables(self):
        import app.db.models as m

        init_db("sqlite://")
        assert m._engine is not None
        inspector = inspect(m._engine)
        tables = inspector.get_table_names()
        assert "bounties" in tables
        assert "wallets" in tables
        assert "budget" in tables
        assert "revenue" in tables
        assert "transactions" in tables
        assert "blacklist" in tables
        assert "price_snapshots" in tables

    def test_global_session_works(self):
        import app.db.models as m

        init_db("sqlite://")
        assert m._SessionLocal is not None
        session = m._SessionLocal()
        assert isinstance(session, Session)
        session.close()

    def test_can_write_and_read(self):
        import app.db.models as m

        init_db("sqlite://")
        session = m._SessionLocal()

        bounty = Bounty(
            platform=BountyPlatform.GITHUB,
            external_id="ext_1",
            title="Test bounty",
            description="desc",
            reward_amount=100.0,
            reward_currency="USD",
            url="https://example.com",
            score=0.8,
            status=BountyStatus.OPEN,
        )
        session.add(bounty)
        session.commit()
        assert bounty.id is not None

        loaded = session.query(Bounty).filter_by(external_id="ext_1").first()
        assert loaded is not None
        assert loaded.title == "Test bounty"

        session.close()


class TestGetSession:
    """get_session — yields database sessions."""

    def test_yields_session(self):
        gen = get_session()
        session = next(gen)
        assert isinstance(session, Session)
        try:
            next(gen)
        except StopIteration:
            pass
        session.close()


class TestEnumValues:
    """Enum definitions have expected values."""

    def test_bounty_platform_values(self):
        assert BountyPlatform.GITHUB.value == "github"
        assert BountyPlatform.DEWORK.value == "dework"
        assert BountyPlatform.LAYER3.value == "layer3"
        assert BountyPlatform.HACKQUEST.value == "hackquest"

    def test_bounty_status_values(self):
        assert BountyStatus.OPEN.value == "open"
        assert BountyStatus.APPLIED.value == "applied"
        assert BountyStatus.SUBMITTED.value == "submitted"
        assert BountyStatus.PAID.value == "paid"
        assert BountyStatus.EXPIRED.value == "expired"
        assert BountyStatus.FAILED.value == "failed"

    def test_wallet_type_values(self):
        assert WalletType.HOT.value == "hot"
        assert WalletType.COLD.value == "cold"
        assert WalletType.DISPOSABLE.value == "disposable"
