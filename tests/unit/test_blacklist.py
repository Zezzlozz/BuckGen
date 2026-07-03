"""
Unit tests for Blacklist Tracker (app/utils/blacklist.py).

Tests cover:
  - is_blacklisted: check before/after blacklisting
  - blacklist_source: create new entry, increment existing
  - remove_blacklist: remove existing, no-op on non-existing
  - get_blacklisted_sources: no filter, filter by type
"""

from app.db.models import BlacklistEntry
from app.utils.blacklist import (
    blacklist_source,
    get_blacklisted_sources,
    is_blacklisted,
    remove_blacklist,
)


class TestIsBlacklisted:
    """is_blacklisted — checks whether a source is blacklisted."""

    def test_not_blacklisted_when_empty(self, db_session):
        assert is_blacklisted(db_session, "platform", "github") is False

    def test_blacklisted_after_adding(self, db_session):
        blacklist_source(db_session, "platform", "github", "doesn't pay")
        assert is_blacklisted(db_session, "platform", "github") is True

    def test_different_source_not_blacklisted(self, db_session):
        blacklist_source(db_session, "platform", "github")
        assert is_blacklisted(db_session, "platform", "gitlab") is False

    def test_different_type_not_blacklisted(self, db_session):
        blacklist_source(db_session, "platform", "github")
        assert is_blacklisted(db_session, "user", "github") is False


class TestBlacklistSource:
    """blacklist_source — adds or increments a blacklist entry."""

    def test_creates_entry(self, db_session):
        entry = blacklist_source(db_session, "platform", "bad-site", "scam")
        assert isinstance(entry, BlacklistEntry)
        assert entry.id is not None
        assert entry.source_type == "platform"
        assert entry.source_id == "bad-site"
        assert entry.reason == "scam"
        assert entry.failed_attempts == 1

    def test_default_reason(self, db_session):
        entry = blacklist_source(db_session, "user", "malicious")
        assert entry.reason == ""

    def test_increments_existing(self, db_session):
        e1 = blacklist_source(db_session, "platform", "bad", "reason1")
        assert e1.failed_attempts == 1

        e2 = blacklist_source(db_session, "platform", "bad", "reason2")
        assert e2.failed_attempts == 2
        assert e2.reason == "reason2"  # updated reason
        assert e2.id == e1.id  # same row

    def test_increment_preserves_existing_reason(self, db_session):
        e1 = blacklist_source(db_session, "platform", "bad", "original")
        e2 = blacklist_source(db_session, "platform", "bad")  # no new reason
        assert e2.failed_attempts == 2
        assert e2.reason == "original"  # unchanged


class TestRemoveBlacklist:
    """remove_blacklist — removes a source from the blacklist."""

    def test_removes_existing(self, db_session):
        blacklist_source(db_session, "platform", "bad")
        assert remove_blacklist(db_session, "platform", "bad") is True
        assert is_blacklisted(db_session, "platform", "bad") is False

    def test_returns_false_for_non_existing(self, db_session):
        assert remove_blacklist(db_session, "platform", "nonexistent") is False

    def test_does_not_affect_other_entries(self, db_session):
        blacklist_source(db_session, "platform", "bad1")
        blacklist_source(db_session, "platform", "bad2")
        remove_blacklist(db_session, "platform", "bad1")
        assert is_blacklisted(db_session, "platform", "bad2") is True


class TestGetBlacklistedSources:
    """get_blacklisted_sources — lists blacklisted entries."""

    def test_empty_when_none(self, db_session):
        assert get_blacklisted_sources(db_session) == []

    def test_returns_all(self, db_session):
        blacklist_source(db_session, "platform", "bad1")
        blacklist_source(db_session, "platform", "bad2")
        blacklist_source(db_session, "user", "spammer")
        results = get_blacklisted_sources(db_session)
        assert len(results) == 3

    def test_filter_by_type(self, db_session):
        blacklist_source(db_session, "platform", "bad1")
        blacklist_source(db_session, "platform", "bad2")
        blacklist_source(db_session, "user", "spammer")
        results = get_blacklisted_sources(db_session, source_type="platform")
        assert len(results) == 2
        assert all(r.source_type == "platform" for r in results)
