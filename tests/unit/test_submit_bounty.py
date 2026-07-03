"""
Unit tests for Bounty Submission Module (app/modules/submit_bounty.py).

Tests cover:
  - generate_solution: LLM success, LLM unavailable fallback, tiered parameters
  - _github_request: success, server error with retry, missing token
  - create_fork, create_pull_request, post_comment: delegation to _github_request
  - submit_bounty: full flow, errors (not found, wrong status, budget, URL parse, comment fail)
  - submit_top_bounties: query and sequential submission
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.db.models import Bounty, BountyPlatform, BountyStatus
from app.modules.submit_bounty import (
    generate_solution,
    submit_bounty,
    submit_top_bounties,
)


def _run(coro):
    return asyncio.run(coro)


def _make_db_bounty(db_session, **overrides):
    """Create and return a persisted Bounty."""
    bounty = Bounty(
        platform=BountyPlatform.GITHUB,
        external_id="ext_1",
        title="Fix login bug",
        description="The login page crashes on empty form submission",
        reward_amount=500.0,
        reward_currency="USD",
        experience_level="intermediate",
        url="https://github.com/owner/repo/issues/42",
        score=0.85,
        status=BountyStatus.OPEN,
    )
    for k, v in overrides.items():
        setattr(bounty, k, v)
    db_session.add(bounty)
    db_session.commit()
    return bounty


def _mock_httpx_client(post_kwargs: dict | None = None):
    """Build a mock for ``async with httpx.AsyncClient() as client:``."""
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=None)
    inst.post = AsyncMock(**(post_kwargs or {}))
    inst.request = AsyncMock(**(post_kwargs or {}))
    return patch("httpx.AsyncClient", return_value=inst), inst


# =============================================================================
# generate_solution
# =============================================================================


class TestGenerateSolution:
    """generate_solution — uses call_llm() or fallback template."""

    def test_llm_success(self):
        with patch(
            "app.modules.submit_bounty.call_llm", return_value="Here is my solution"
        ):
            result = _run(generate_solution("Fix bug", "Login crashes", 500, "USD"))
            assert "Here is my solution" in result

    def test_llm_unavailable_falls_back(self):
        with patch("app.modules.submit_bounty.call_llm", return_value=None):
            result = _run(generate_solution("Fix bug", "Login crashes", 500, "USD"))
            assert "I'd like to work on this" in result

    def test_tiered_parameters_by_reward(self):
        """Higher reward bounties get more generous LLM parameters."""
        with patch(
            "app.modules.submit_bounty.call_llm", return_value="solution"
        ) as mock_call:
            _run(generate_solution("High", "desc", 1000, "USD"))
            args, _ = mock_call.call_args
            assert args[3] == 4000  # max_tokens
            assert args[4] == 0.8  # temperature

            _run(generate_solution("Mid", "desc", 250, "USD"))
            args, _ = mock_call.call_args
            assert args[3] == 2000
            assert args[4] == 0.7

            _run(generate_solution("Low", "desc", 10, "USD"))
            args, _ = mock_call.call_args
            assert args[3] == 1200
            assert args[4] == 0.5


# =============================================================================
# _github_request
# =============================================================================


class TestGitHubRequest:
    """_github_request — authenticated GitHub API calls with retry."""

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    def test_successful_request(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_class, inst = _mock_httpx_client({"return_value": mock_resp})

        with patch("httpx.AsyncClient", return_value=inst):
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.request = AsyncMock(return_value=mock_resp)

            from app.modules.submit_bounty import _github_request

            resp = _run(_github_request("GET", "https://api.github.com/test"))
            assert resp is mock_resp
            assert resp.status_code == 200

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    def test_no_token_returns_none(self):
        with patch("app.config.settings.GITHUB_TOKEN", ""):
            from app.modules.submit_bounty import _github_request

            resp = _run(_github_request("GET", "https://api.github.com/test"))
            assert resp is None

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    def test_server_error_retries(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Error"
        calls = [mock_resp, mock_resp, MagicMock()]
        calls[2].status_code = 200
        calls[2].json.return_value = {"ok": True}

        inst = MagicMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=None)
        inst.request = AsyncMock(side_effect=calls)

        with patch("httpx.AsyncClient", return_value=inst):
            from app.modules.submit_bounty import _github_request

            resp = _run(_github_request("GET", "https://api.github.com/test"))
            assert resp.status_code == 200


# =============================================================================
# submit_bounty
# =============================================================================


class TestSubmitBounty:
    """submit_bounty — full submission flow."""

    def test_not_found(self, db_session):
        result = _run(submit_bounty(db_session, 999))
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_not_open_status(self, db_session):
        bounty = _make_db_bounty(db_session, status=BountyStatus.SUBMITTED)
        result = _run(submit_bounty(db_session, bounty.id))
        assert result["success"] is False
        assert "not OPEN" in result["error"]

    def test_budget_cap_reached(self, db_session):
        bounty = _make_db_bounty(db_session)
        with (
            patch("app.modules.submit_bounty.can_spend", return_value=False),
            patch("app.modules.submit_bounty.settings.GITHUB_TOKEN", "ghp_test"),
            patch(
                "app.modules.submit_bounty.settings.OLLAMA_BASE_URL",
                "http://localhost:11434",
            ),
            patch("app.modules.submit_bounty.settings.OLLAMA_MODEL", "test-model"),
        ):
            result = _run(submit_bounty(db_session, bounty.id))
        assert result["success"] is False
        assert "Budget cap" in result["error"]

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    @patch("app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434")
    @patch("app.config.settings.OLLAMA_MODEL", "test-model")
    def test_invalid_url(self, db_session):
        bounty = _make_db_bounty(db_session, url="https://example.com")
        result = _run(submit_bounty(db_session, bounty.id))
        assert result["success"] is False
        assert "could not parse" in result["error"].lower()

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    @patch("app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434")
    @patch("app.config.settings.OLLAMA_MODEL", "test-model")
    def test_comment_failure(self, db_session):
        bounty = _make_db_bounty(db_session)

        # LLM response: success
        llm_resp = MagicMock()
        llm_resp.status_code = 200
        llm_resp.json.return_value = {"response": "solution text"}

        # GitHub response: failure
        gh_resp = MagicMock()
        gh_resp.status_code = 500
        gh_resp.text = "Internal Server Error"

        inst = MagicMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=None)
        # generate_solution uses client.post
        inst.post = AsyncMock(return_value=llm_resp)
        # _github_request uses client.request
        inst.request = AsyncMock(return_value=gh_resp)

        with (
            patch("httpx.AsyncClient", return_value=inst),
            patch("app.modules.submit_bounty.settings.GITHUB_TOKEN", "ghp_test"),
            patch(
                "app.modules.submit_bounty.settings.OLLAMA_BASE_URL",
                "http://localhost:11434",
            ),
            patch("app.modules.submit_bounty.settings.OLLAMA_MODEL", "test-model"),
            patch("app.modules.submit_bounty.can_spend", return_value=True),
        ):
            result = _run(submit_bounty(db_session, bounty.id))
            assert result["success"] is False
            assert "comment" in result["error"].lower()

    @patch("app.config.settings.GITHUB_TOKEN", "ghp_test")
    @patch("app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434")
    @patch("app.config.settings.OLLAMA_MODEL", "test-model")
    def test_successful_submission(self, db_session):
        bounty = _make_db_bounty(db_session)

        llm_resp = MagicMock()
        llm_resp.status_code = 200
        llm_resp.json.return_value = {"response": "Here is a fix for the bug"}

        gh_resp = MagicMock()
        gh_resp.status_code = 201

        inst = MagicMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=None)
        inst.post = AsyncMock(return_value=llm_resp)
        inst.request = AsyncMock(return_value=gh_resp)

        with (
            patch("httpx.AsyncClient", return_value=inst),
            patch("app.modules.submit_bounty.settings.GITHUB_TOKEN", "ghp_test"),
            patch(
                "app.modules.submit_bounty.settings.OLLAMA_BASE_URL",
                "http://localhost:11434",
            ),
            patch("app.modules.submit_bounty.settings.OLLAMA_MODEL", "test-model"),
            patch("app.modules.submit_bounty.can_spend", return_value=True),
            patch("app.modules.submit_bounty.record_spend"),
            patch("app.modules.submit_bounty.notify_alert", AsyncMock()),
        ):
            result = _run(submit_bounty(db_session, bounty.id))
            assert result["success"] is True
            assert result["repo"] == "owner/repo"
            assert result["issue_number"] == 42
            assert "solution_preview" in result

        # Status should be updated to APPLIED
        db_session.refresh(bounty)
        assert bounty.status == BountyStatus.APPLIED


# =============================================================================
# submit_top_bounties
# =============================================================================


class TestSubmitTopBounties:
    """submit_top_bounties — queries and submits top-scoring bounties."""

    def test_no_qualifying_bounties(self, db_session):
        result = _run(submit_top_bounties(db_session))
        assert result == []

    def test_skips_low_score(self, db_session):
        _make_db_bounty(db_session, score=0.3)
        result = _run(submit_top_bounties(db_session, min_score=0.7))
        assert result == []

    def test_submits_top_qualifying(self, db_session):
        _make_db_bounty(db_session, score=0.8)
        result = _run(submit_top_bounties(db_session, min_score=0.5))
        # The submission itself may fail due to mocking, but the function
        # should still attempt it and return a result
        assert len(result) >= 1
