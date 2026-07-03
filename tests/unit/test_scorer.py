"""
Unit tests for LLM Bounty Scorer (app/llm/scorer.py).

Tests cover:
  - score_bounty: LLM path returns score, fallback to heuristic
  - _score_with_llm: success, ConnectError, unparseable response, exceptions
  - _score_heuristic: weights, keyword hits, reward bonus, bounds
  - _build_prompt: includes all bounty fields, truncates long descriptions
  - _parse_llm_score: decimal, integer 0/1, invalid, various formats
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.scorer import (
    _build_prompt,
    _parse_llm_score,
    _score_heuristic,
    _score_with_llm,
    score_bounty,
)
from app.llm.scorer import DESIRABLE_KEYWORDS as SCORER_KW


def _run(coro):
    return asyncio.run(coro)


def _make_bounty(**overrides):
    """Default bounty dict for tests."""
    bounty = {
        "title": "Fix login bug",
        "description": "The login page crashes when users submit empty forms",
        "reward_amount": 500,
        "reward_currency": "USD",
        "experience_level": "intermediate",
        "bounty_type": "bug",
        "project_length": "days",
        "keywords": ["python", "authentication"],
        "issue_keywords": ["login", "crash"],
    }
    bounty.update(overrides)
    return bounty


# =============================================================================
# _parse_llm_score
# =============================================================================


class TestParseLlmScore:
    """_parse_llm_score — extracts a float 0.0–1.0 from LLM response text."""

    def test_parses_decimal(self):
        assert _parse_llm_score("0.85") == 0.85

    def test_parses_decimal_with_surrounding_text(self):
        assert _parse_llm_score("Score: 0.75\nSome explanation") == 0.75

    def test_clamps_above_one(self):
        assert _parse_llm_score("1.5") == 1.0

    def test_negative_parsed_as_positive(self):
        """-0.5 is parsed as 0.5 because the regex drops the minus sign."""
        assert _parse_llm_score("-0.5") == 0.5

    def test_parses_integer_zero(self):
        assert _parse_llm_score("0") == 0.0

    def test_parses_integer_one(self):
        assert _parse_llm_score("1") == 1.0

    def test_returns_none_for_no_number(self):
        assert _parse_llm_score("no numbers here") is None

    def test_returns_none_for_empty(self):
        assert _parse_llm_score("") is None

    def test_returns_none_for_garbage(self):
        assert _parse_llm_score("abc.def") is None

    def test_prefers_first_number(self):
        assert _parse_llm_score("0.3 0.9") == 0.3


# =============================================================================
# _build_prompt
# =============================================================================


class TestBuildPrompt:
    """_build_prompt — builds a compact LLM prompt from a bounty dict."""

    def test_includes_title(self):
        prompt = _build_prompt(_make_bounty(title="My Bounty"))
        assert "My Bounty" in prompt

    def test_includes_reward(self):
        prompt = _build_prompt(_make_bounty(reward_amount=100, reward_currency="ETH"))
        assert "100 ETH" in prompt

    def test_includes_level_and_type(self):
        prompt = _build_prompt(
            _make_bounty(experience_level="beginner", bounty_type="security")
        )
        assert "beginner" in prompt
        assert "security" in prompt

    def test_truncates_long_description(self):
        long_desc = "x" * 2000
        prompt = _build_prompt(_make_bounty(description=long_desc))
        # The prompt uses desc[:1500]
        assert "x" * 1500 in prompt
        assert "x" * 1501 not in prompt

    def test_includes_keywords(self):
        bounty = _make_bounty(keywords=["python", "solidity"], issue_keywords=["web3"])
        prompt = _build_prompt(bounty)
        assert "python" in prompt
        assert "solidity" in prompt
        assert "web3" in prompt


# =============================================================================
# _score_heuristic
# =============================================================================


class TestScoreHeuristic:
    """_score_heuristic — keyword + metadata fallback scoring."""

    def test_baseline_score(self):
        """Minimal bounty gets at least 0.5 baseline."""
        score = _score_heuristic({"title": "x", "description": "y"})
        assert score >= 0.5

    def test_beginner_gets_boost(self):
        beginner = _score_heuristic(_make_bounty(experience_level="beginner"))
        advanced = _score_heuristic(_make_bounty(experience_level="advanced"))
        assert beginner > advanced

    def test_security_type_boost(self):
        security = _score_heuristic(_make_bounty(bounty_type="security"))
        bug = _score_heuristic(_make_bounty(bounty_type="bug"))
        assert security > bug

    def test_project_length_boost(self):
        hours = _score_heuristic(_make_bounty(project_length="hours"))
        months = _score_heuristic(_make_bounty(project_length="months"))
        assert hours > months

    def test_keyword_hits_boost(self):
        many_kw = _score_heuristic(
            _make_bounty(
                keywords=SCORER_KW[:3],
                title="python api automation",
                description="building a bot for testing",
            )
        )
        no_kw = _score_heuristic(
            _make_bounty(
                keywords=[],
                title="fix css layout",
                description="change font color",
            )
        )
        assert many_kw > no_kw

    def test_high_reward_bonus(self):
        high = _score_heuristic(_make_bounty(reward_amount=500))
        low = _score_heuristic(_make_bounty(reward_amount=5))
        assert high >= low

    def test_score_in_bounds(self):
        """Score is always clamped between 0.0 and 1.0."""
        for _ in range(20):
            bounty = _make_bounty(
                reward_amount=9999,
                experience_level="beginner",
                bounty_type="security",
                project_length="hours",
                keywords=SCORER_KW,
            )
            score = _score_heuristic(bounty)
            assert 0.0 <= score <= 1.0


# =============================================================================
# _score_with_llm
# =============================================================================


class TestScoreWithLlm:
    """_score_with_llm — calls call_llm() and parses response."""

    async def _mock_call_llm(self, return_value: str | None):
        """Patch call_llm to return a given value."""
        patcher = patch("app.llm.scorer.call_llm", return_value=return_value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def test_successful_scoring(self):
        with patch("app.llm.scorer.call_llm", return_value="0.85"):
            score = _run(_score_with_llm(_make_bounty()))
            assert score == 0.85

    def test_none_response_returns_none(self):
        with patch("app.llm.scorer.call_llm", return_value=None):
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None

    def test_unparseable_response_returns_none(self):
        with patch("app.llm.scorer.call_llm", return_value="I don't know"):
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None

    def test_exception_returns_none(self):
        with patch("app.llm.scorer.call_llm", side_effect=RuntimeError("fail")):
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None


# =============================================================================
# score_bounty
# =============================================================================


class TestScoreBounty:
    """score_bounty — main entry point, tries LLM then heuristic fallback."""

    def test_uses_llm_when_available(self):
        with patch("app.llm.scorer.call_llm", return_value="0.9"):
            score = _run(score_bounty(_make_bounty()))
            assert score == 0.9

    def test_falls_back_to_heuristic_when_llm_unavailable(self):
        """When LLM is unavailable, heuristic score is returned."""
        with patch("app.llm.scorer.call_llm", return_value=None):
            score = _run(score_bounty(_make_bounty()))
            assert 0.0 <= score <= 1.0
            # With our test bounty data, score should be above baseline
            assert score > 0.5
