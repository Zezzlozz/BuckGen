"""
Approval-gate tests for the human-in-the-loop bounty review flow.

These lock in the invariant that a bounty solution can only be posted after
BOTH gates are cleared:

  1. the bounty is in APPROVED status (set by a deliberate human approve()), and
  2. the post call passes confirm=True.

If either is missing, nothing is posted. post_comment is patched to raise if
it is ever reached improperly, so a regression that weakens the gate fails
loudly instead of silently posting to a real GitHub issue.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import (
    Bounty,
    BountyPlatform,
    BountyStatus,
    get_session,
    init_db,
)
from app.modules import bounty_review as br


@pytest.fixture
def db():
    """Fresh in-memory DB per test."""
    init_db("sqlite:///:memory:")
    session = next(get_session())
    yield session
    session.close()


def _make_bounty(db, **overrides) -> Bounty:
    defaults = dict(
        platform=BountyPlatform.GITHUB,
        external_id="ext-1",
        title="Fix the thing",
        description="Something is broken and needs fixing.",
        reward_amount=500.0,
        reward_currency="USD",
        url="https://github.com/owner/repo/issues/42",
        status=BountyStatus.DRAFTED,
        draft_solution="Here is a reviewed draft with code.",
    )
    defaults.update(overrides)
    b = Bounty(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


# ---------------------------------------------------------------------------
# ROI math
# ---------------------------------------------------------------------------
def test_compute_roi_expected_dollars_per_hour():
    # $500 * 0.8 confidence / 5h = $80/hr
    assert br.compute_roi(500, 5, 0.8) == 80.0


def test_compute_roi_floors_effort_to_avoid_absurd_values():
    # Effort below 0.5h is floored at 0.5h, so ROI can't blow up.
    assert br.compute_roi(100, 0.0, 1.0) == br.compute_roi(100, 0.5, 1.0)


def test_compute_roi_clamps_confidence():
    # Confidence > 1 is clamped to 1.
    assert br.compute_roi(100, 1, 5.0) == br.compute_roi(100, 1, 1.0)


# ---------------------------------------------------------------------------
# The approval gate — the core safety property
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_refused_without_approval(db):
    """A drafted-but-unapproved bounty must not post, even with confirm=True."""
    b = _make_bounty(db, status=BountyStatus.DRAFTED)

    with patch.object(
        br,
        "post_comment",
        new=AsyncMock(side_effect=AssertionError("posted without approval")),
    ):
        result = await br.post_approved(db, b.id, confirm=True)

    assert result["success"] is False
    assert "approv" in result["error"].lower()
    # Status unchanged — nothing happened.
    db.refresh(b)
    assert b.status == BountyStatus.DRAFTED


@pytest.mark.asyncio
async def test_post_refused_when_approved_but_not_confirmed(db):
    """Approved but confirm=False must not post."""
    b = _make_bounty(db, status=BountyStatus.DRAFTED)
    approve_result = br.approve(db, b.id)
    assert approve_result["success"] is True

    with patch.object(
        br,
        "post_comment",
        new=AsyncMock(side_effect=AssertionError("posted without confirm")),
    ):
        result = await br.post_approved(db, b.id, confirm=False)

    assert result["success"] is False
    assert "confirm" in result["error"].lower()


@pytest.mark.asyncio
async def test_post_succeeds_only_when_approved_and_confirmed(db):
    """Approved + confirm=True is the one path that reaches post_comment."""
    b = _make_bounty(db, status=BountyStatus.DRAFTED)
    br.approve(db, b.id)

    with patch.object(br, "post_comment", new=AsyncMock(return_value=True)) as pc:
        result = await br.post_approved(db, b.id, confirm=True)

    assert result["success"] is True
    assert result["repo"] == "owner/repo"
    assert result["issue_number"] == 42
    pc.assert_awaited_once()
    # Status advanced to APPLIED after a real post.
    db.refresh(b)
    assert b.status == BountyStatus.APPLIED


# ---------------------------------------------------------------------------
# approve() preconditions
# ---------------------------------------------------------------------------
def test_approve_refused_without_a_draft(db):
    """Approval requires real reviewed content, not a blank cheque."""
    b = _make_bounty(db, status=BountyStatus.RESEARCHED, draft_solution="")
    result = br.approve(db, b.id)
    assert result["success"] is False
    assert "draft" in result["error"].lower()
    db.refresh(b)
    assert b.status != BountyStatus.APPROVED


def test_approve_sets_approved_status_and_timestamp(db):
    b = _make_bounty(db, status=BountyStatus.DRAFTED)
    result = br.approve(db, b.id)
    assert result["success"] is True
    db.refresh(b)
    assert b.status == BountyStatus.APPROVED
    assert b.approved_at is not None


@pytest.mark.asyncio
async def test_already_posted_cannot_be_reposted(db):
    """An APPLIED bounty can't be re-approved and re-posted."""
    b = _make_bounty(db, status=BountyStatus.APPLIED)
    result = br.approve(db, b.id)
    assert result["success"] is False


# ---------------------------------------------------------------------------
# research/draft never post
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_research_posts_nothing(db):
    b = _make_bounty(db, status=BountyStatus.OPEN, draft_solution="")

    with (
        patch.object(br, "call_llm", new=AsyncMock(return_value=None)),
        patch.object(
            br,
            "post_comment",
            new=AsyncMock(side_effect=AssertionError("research posted something")),
        ),
        patch.object(br, "notify_alert", new=AsyncMock()),
    ):
        result = await br.research_bounty(db, b.id)

    assert result["success"] is True
    db.refresh(b)
    assert b.status == BountyStatus.RESEARCHED  # advanced, but nothing posted


@pytest.mark.asyncio
async def test_prepare_draft_posts_nothing(db):
    b = _make_bounty(db, status=BountyStatus.RESEARCHED, draft_solution="")

    with (
        patch.object(
            br, "call_llm", new=AsyncMock(return_value="draft body with ```code```")
        ),
        patch.object(
            br,
            "post_comment",
            new=AsyncMock(side_effect=AssertionError("draft posted something")),
        ),
    ):
        result = await br.prepare_draft(db, b.id)

    assert result["success"] is True
    db.refresh(b)
    assert b.status == BountyStatus.DRAFTED
    assert b.draft_solution
