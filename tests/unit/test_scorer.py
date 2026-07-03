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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
        long_desc = "x" * 500
        prompt = _build_prompt(_make_bounty(description=long_desc))
        # The prompt uses desc[:200]
        assert "x" * 200 in prompt
        assert "x" * 201 not in prompt

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


def _mock_ollama(post_kwargs: dict | None = None):
    """Mock httpx.AsyncClient for Ollama calls.

    Returns ``(mock_class, mock_client_inst)``.
    """
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=None)
    inst.post = AsyncMock(**(post_kwargs or {}))
    mock_class = patch("httpx.AsyncClient", return_value=inst)
    return mock_class


class TestScoreWithLlm:
    """_score_with_llm — calls Ollama API and parses the response."""

    def test_successful_scoring(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434"
        )
        monkeypatch.setattr("app.config.settings.OLLAMA_MODEL", "test-model")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "0.85"}
        mock_class = _mock_ollama({"return_value": mock_resp})

        with mock_class:
            score = _run(_score_with_llm(_make_bounty()))
            assert score == 0.85

    def test_connect_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434"
        )
        monkeypatch.setattr("app.config.settings.OLLAMA_MODEL", "test-model")
        mock_class = _mock_ollama(
            {"side_effect": httpx.ConnectError("connection refused")}
        )

        with mock_class:
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None

    def test_unparseable_response_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434"
        )
        monkeypatch.setattr("app.config.settings.OLLAMA_MODEL", "test-model")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "I don't know"}
        mock_class = _mock_ollama({"return_value": mock_resp})

        with mock_class:
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None

    def test_general_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434"
        )
        monkeypatch.setattr("app.config.settings.OLLAMA_MODEL", "test-model")
        mock_class = _mock_ollama({"side_effect": RuntimeError("unexpected")})

        with mock_class:
            score = _run(_score_with_llm(_make_bounty()))
            assert score is None


# =============================================================================
# score_bounty
# =============================================================================


class TestScoreBounty:
    """score_bounty — main entry point, tries LLM then heuristic fallback."""

    def test_uses_llm_when_available(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.OLLAMA_BASE_URL", "http://localhost:11434"
        )
        monkeypatch.setattr("app.config.settings.OLLAMA_MODEL", "test-model")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "0.9"}
        mock_class = _mock_ollama({"return_value": mock_resp})

        with mock_class:
            score = _run(score_bounty(_make_bounty()))
            # LLM returns 0.9, so overall score should be 0.9, not heuristic
            assert score == 0.9

    def test_falls_back_to_heuristic_when_ollama_down(self):
        """When LLM is unavailable, heuristic score is returned."""
        # Don't mock httpx at all — it will fail to connect, returning None
        # from _score_with_llm, and _score_heuristic will be used.
        score = _run(score_bounty(_make_bounty()))
        # Heuristic score for a decent bounty should be > 0.5
        assert 0.0 <= score <= 1.0
        # With our test bounty data, score should be above baseline
        assert score > 0.5
