"""
Unit tests for the GitHub Issues Bounty Scanner (app/modules/gitcoin.py).

Tests cover:
  - fetch_open_bounties with mocked httpx
  - Pagination behaviour
  - Rate limiting handling
  - normalize_bounty field extraction
  - Reward parsing from issue body/title/labels
  - Experience level detection
"""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import httpx


def _make_github_issue(
    number=1,
    title="Test Bounty",
    body="Fix this bug for $500",
    labels=None,
    state="open",
):
    """Helper to build a fake GitHub API issue response."""
    if labels is None:
        labels = [{"name": "bounty"}]
    return {
        "id": number * 1000,
        "number": number,
        "title": title,
        "body": body,
        "labels": labels,
        "state": state,
        "html_url": f"https://github.com/test/repo/issues/{number}",
        "repository_url": "https://api.github.com/repos/test/repo",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z",
    }


def _mock_response(status_code=200, json_data=None):
    """Create a mock HTTP response with sync .json()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = "mock response"
    resp.raise_for_status = MagicMock()
    return resp


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# =============================================================================
# fetch_open_bounties (mocked HTTP calls)
# =============================================================================


class TestFetchOpenBounties:
    """fetch_open_bounties core logic."""

    def _run_fetch(self, max_bounties=10):
        """Run fetch_open_bounties with app.config.settings patched."""
        from app.modules.gitcoin import fetch_open_bounties

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = MagicMock()
            inst.aclose = AsyncMock()
            mock_client_class.return_value = inst
            yield inst, fetch_open_bounties, max_bounties

    def _mock_inst(self, mock_client_class):
        """Set up a common mock client instance."""
        inst = MagicMock()
        inst.aclose = AsyncMock()
        mock_client_class.return_value = inst
        return inst

    def test_fetches_single_page(self):
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = self._mock_inst(mock_client_class)
            resp = _mock_response(
                json_data={
                    "items": [_make_github_issue(1)],
                    "total_count": 1,
                }
            )
            # Each query makes one page request; side_effect handles all calls
            inst.get = AsyncMock(side_effect=[resp] * len(SEARCH_QUERIES))

            result = _run_async(fetch_open_bounties(max_bounties=10))
            assert len(result) == 1
            assert result[0]["title"] == "Test Bounty"

    def test_deduplicates_across_queries(self):
        """Same issue returned by multiple label queries should appear once."""
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        issue = _make_github_issue(1, "Test Bounty")
        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = self._mock_inst(mock_client_class)
            resp = _mock_response(json_data={"items": [issue], "total_count": 1})
            inst.get = AsyncMock(side_effect=[resp] * len(SEARCH_QUERIES))

            result = _run_async(fetch_open_bounties(max_bounties=10))
            assert len(result) == 1  # deduplicated

    def test_paginates_across_pages(self):
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = self._mock_inst(mock_client_class)

            page_1 = _mock_response(
                json_data={
                    "items": [_make_github_issue(i) for i in range(1, 5)],
                    "total_count": 4,
                }
            )
            page_2 = _mock_response(json_data={"items": [], "total_count": 4})
            # Each query gets 2 pages: first returns 4 items, second returns empty
            pages = []
            for _ in range(len(SEARCH_QUERIES)):
                pages.extend([page_1, page_2])
            inst.get = AsyncMock(side_effect=pages)

            result = _run_async(fetch_open_bounties(max_bounties=4))
            assert len(result) == 4

    def test_rate_limited_returns_empty(self):
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = None

            inst = self._mock_inst(mock_client_class)
            resp = _mock_response(status_code=403, json_data={})
            resp.text = "rate limit exceeded"
            inst.get = AsyncMock(side_effect=[resp] * len(SEARCH_QUERIES))

            result = _run_async(fetch_open_bounties(max_bounties=10))
            assert result == []

    def test_partial_query_failure(self):
        """If one query fails, others still return results."""
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = self._mock_inst(mock_client_class)

            good_resp = _mock_response(
                json_data={
                    "items": [_make_github_issue(1)],
                    "total_count": 1,
                }
            )
            bad_resp = _mock_response(status_code=500, json_data={})
            # Mix of failures and successes
            effects = [bad_resp, good_resp] * (len(SEARCH_QUERIES) // 2)
            if len(effects) < len(SEARCH_QUERIES):
                effects.append(good_resp)
            inst.get = AsyncMock(side_effect=effects)

            result = _run_async(fetch_open_bounties(max_bounties=10))
            assert len(result) >= 1  # at least the one from successful queries

    def test_network_error_returns_empty(self):
        from app.modules.gitcoin import fetch_open_bounties, SEARCH_QUERIES

        with (
            patch("app.config.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.GITHUB_TOKEN = "test_token"

            inst = self._mock_inst(mock_client_class)
            inst.get = AsyncMock(
                side_effect=[httpx.RequestError("connection failed")]
                * len(SEARCH_QUERIES)
            )

            result = _run_async(fetch_open_bounties(max_bounties=10))
            assert result == []


# =============================================================================
# normalize_bounty
# =============================================================================


class TestNormalizeBounty:
    """normalize_bounty extracts all required fields."""

    def _call(self, raw):
        from app.modules.gitcoin import normalize_bounty

        return normalize_bounty(raw)

    def test_extracts_core_fields(self):
        raw = _make_github_issue(42, "Fix login bug", "Detailed body")
        norm = self._call(raw)
        assert norm["external_id"] == "42000"
        assert norm["title"] == "Fix login bug"
        assert norm["description"] == "Detailed body"
        assert norm["url"] == "https://github.com/test/repo/issues/42"
        assert norm["repo"] == "test/repo"

    def test_experience_level_beginner(self):
        norm = self._call(_make_github_issue(1, "Good first issue"))
        assert norm["experience_level"] == "beginner"

    def test_experience_level_intermediate(self):
        norm = self._call(_make_github_issue(1, "Medium difficulty task"))
        assert norm["experience_level"] == "intermediate"

    def test_experience_level_advanced(self):
        norm = self._call(_make_github_issue(1, "Hard: advanced cryptography"))
        assert norm["experience_level"] == "advanced"

    def test_default_experience(self):
        norm = self._call(_make_github_issue(1, "Plain task"))
        assert norm["experience_level"] == ""

    def test_includes_labels(self):
        raw = _make_github_issue(
            1, "Task", labels=[{"name": "bounty"}, {"name": "bug"}]
        )
        norm = self._call(raw)
        assert "bounty" in norm["labels"]
        assert "bug" in norm["labels"]

    def test_description_truncated_to_4000(self):
        raw = _make_github_issue(1, "Title", body="x" * 5000)
        norm = self._call(raw)
        assert len(norm["description"]) == 4000

    def test_empty_body_handled(self):
        raw = _make_github_issue(1, "Title", body=None)
        norm = self._call(raw)
        assert norm["description"] == ""


# =============================================================================
# Reward parsing
# =============================================================================


class TestParseReward:
    """_parse_reward_from_issue extracts dollar amounts."""

    def test_dollar_amount_in_body(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {"title": "Fix bug", "body": "Reward: $1,500", "labels": []}
        assert _parse_reward_from_issue(issue) == 1500.0

    def test_dollar_amount_in_title(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {"title": "$250 bounty", "body": "", "labels": []}
        assert _parse_reward_from_issue(issue) == 250.0

    def test_euro_amount(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {"title": "Bounty: 750 EUR", "body": "", "labels": []}
        assert _parse_reward_from_issue(issue) == 750.0

    def test_price_label_overrides_body(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {
            "title": "Bounty",
            "body": "Reward: $100",
            "labels": [{"name": "price: 500"}],
        }
        assert _parse_reward_from_issue(issue) == 500.0

    def test_reward_keyword_in_body(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {"title": "Task", "body": "reward: 3000", "labels": []}
        assert _parse_reward_from_issue(issue) == 3000.0

    def test_no_reward_returns_zero(self):
        from app.modules.gitcoin import _parse_reward_from_issue

        issue = {"title": "Free work", "body": "Help wanted!", "labels": []}
        assert _parse_reward_from_issue(issue) == 0.0


class TestParseCurrency:
    """_parse_currency_from_issue detects reward currency."""

    def test_detects_eth(self):
        from app.modules.gitcoin import _parse_currency_from_issue

        issue = {"title": "Reward: 10 ETH", "body": ""}
        assert _parse_currency_from_issue(issue) == "ETH"

    def test_detects_usdc(self):
        from app.modules.gitcoin import _parse_currency_from_issue

        issue = {"title": "Bounty: 1000 USDC", "body": ""}
        assert _parse_currency_from_issue(issue) == "USDC"

    def test_defaults_to_usd(self):
        from app.modules.gitcoin import _parse_currency_from_issue

        issue = {"title": "Bounty: $500", "body": ""}
        assert _parse_currency_from_issue(issue) == "USD"


class TestCleanNumber:
    """_clean_number converts formatted strings to float."""

    def test_removes_commas(self):
        from app.modules.gitcoin import _clean_number

        assert _clean_number("1,500") == 1500.0

    def test_decimal(self):
        from app.modules.gitcoin import _clean_number

        assert _clean_number("1500.50") == 1500.5

    def test_invalid_returns_zero(self):
        from app.modules.gitcoin import _clean_number

        assert _clean_number("not_a_number") == 0.0

    def test_whitespace_handling(self):
        from app.modules.gitcoin import _clean_number

        assert _clean_number(" 1,000 ") == 1000.0


class TestDetectLevel:
    """_detect_level keyword matching for experience levels."""

    def test_beginner(self):
        from app.modules.gitcoin import _detect_level

        assert _detect_level(["beginner friendly"]) == "beginner"
        assert _detect_level(["good first issue"]) == "beginner"
        assert _detect_level(["easy task"]) == "beginner"

    def test_intermediate(self):
        from app.modules.gitcoin import _detect_level

        assert _detect_level(["intermediate"]) == "intermediate"
        assert _detect_level(["medium difficulty"]) == "intermediate"

    def test_advanced(self):
        from app.modules.gitcoin import _detect_level

        assert _detect_level(["advanced coding"]) == "advanced"
        assert _detect_level(["expert"]) == "advanced"
        assert _detect_level(["hard problem"]) == "advanced"

    def test_unknown(self):
        from app.modules.gitcoin import _detect_level

        assert _detect_level(["general task"]) == ""

    def test_combined_text(self):
        from app.modules.gitcoin import _detect_level

        result = _detect_level(["task description", "label: beginner"])
        assert result == "beginner"
