"""
Unit tests for Telegram Notification Utility (app/utils/notify.py).

Tests cover:
  - send_telegram: config guard, HTTP success, HTTP errors, network errors
  - notify_bounty_found: message construction, delegation to send_telegram
  - notify_error: message construction, delegation to send_telegram
  - notify_alert: message construction, delegation to send_telegram
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.utils.notify import (
    notify_alert,
    notify_bounty_found,
    notify_error,
    send_telegram,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# =============================================================================
# Helpers
# =============================================================================


def _mock_async_client(post_kwargs: dict | None = None):
    """Build a mock that mimics ``async with httpx.AsyncClient() as client:``.

    Returns ``(mock_class, mock_instance)`` where ``mock_instance.post`` is
    configured according to *post_kwargs* (e.g. ``return_value=resp`` or
    ``side_effect=exc``).
    """
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=None)
    inst.post = AsyncMock(**(post_kwargs or {}))
    mock_class = patch("httpx.AsyncClient", return_value=inst)
    return mock_class, inst


def _configured_env(monkeypatch):
    """Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to valid values."""
    monkeypatch.setattr("app.config.settings.TELEGRAM_BOT_TOKEN", "123:valid")
    monkeypatch.setattr("app.config.settings.TELEGRAM_CHAT_ID", "456")


# =============================================================================
# send_telegram
# =============================================================================


class TestSendTelegram:
    """send_telegram — sends a message via Telegram Bot API."""

    def test_not_configured_no_token(self):
        """Returns False when TELEGRAM_BOT_TOKEN is empty."""
        result = _run(send_telegram("test"))
        assert result is False

    def test_not_configured_no_chat_id(self):
        """Returns False when TELEGRAM_CHAT_ID is empty."""
        result = _run(send_telegram("test"))
        assert result is False

    def test_successful_send(self, monkeypatch):
        """Returns True when the API call succeeds."""
        _configured_env(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_class, inst = _mock_async_client({"return_value": mock_resp})

        with mock_class:
            result = _run(send_telegram("Hello"))
            assert result is True
            inst.post.assert_awaited_once()

    def test_http_status_error(self, monkeypatch):
        """Returns False when the API returns a non-2xx code."""
        _configured_env(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=MagicMock()
        )
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_class, inst = _mock_async_client({"return_value": mock_resp})

        with mock_class:
            result = _run(send_telegram("Hello"))
            assert result is False

    def test_network_error(self, monkeypatch):
        """Returns False when a network error occurs."""
        _configured_env(monkeypatch)
        mock_class, inst = _mock_async_client(
            {"side_effect": httpx.RequestError("connection failed")}
        )

        with mock_class:
            result = _run(send_telegram("Hello"))
            assert result is False

    def test_passes_parse_mode(self, monkeypatch):
        """Custom parse_mode is passed through to the API."""
        _configured_env(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_class, inst = _mock_async_client({"return_value": mock_resp})

        with mock_class:
            _run(send_telegram("Hello", parse_mode="HTML"))
            _, kwargs = inst.post.call_args
            assert kwargs["json"]["parse_mode"] == "HTML"


# =============================================================================
# notify_bounty_found
# =============================================================================


class TestNotifyBountyFound:
    """notify_bounty_found — alerts on a high-scoring bounty."""

    def test_sends_message(self):
        """Constructs a proper message and delegates to send_telegram."""
        bounty = {
            "title": "Fix critical bug",
            "reward_amount": 500,
            "reward_currency": "USD",
            "url": "https://github.com/org/repo/issues/1",
            "score": 0.85,
        }
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_bounty_found(bounty))
            mock_send.assert_awaited_once()
            msg = mock_send.await_args[0][0]
            assert "High-Value Bounty Found" in msg
            assert "Fix critical bug" in msg
            assert "500 USD" in msg
            assert "0.85" in msg

    def test_handles_missing_fields(self):
        """Uses defaults for missing fields."""
        bounty = {}
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_bounty_found(bounty))
            mock_send.assert_awaited_once()


# =============================================================================
# notify_error
# =============================================================================


class TestNotifyError:
    """notify_error — alerts on a module error."""

    def test_sends_message(self):
        """Constructs a proper error message and delegates to send_telegram."""
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_error("prices", "API rate limited"))
            mock_send.assert_awaited_once()
            msg = mock_send.await_args[0][0]
            assert "BuckGen Error" in msg
            assert "prices" in msg
            assert "API rate limited" in msg

    def test_truncates_long_detail(self):
        """Detail longer than 200 chars is truncated."""
        long_detail = "x" * 300
        context = "test"
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_error(context, long_detail))
            msg = mock_send.await_args[0][0]
            # Format: "[WARN] *BuckGen Error*\\n`test`\\n{detail[:200]}"
            # Constant overhead: len("[WARN] *BuckGen Error*\n``\n") = 30
            overhead = 30
            assert len(msg) == overhead + 200


# =============================================================================
# notify_alert
# =============================================================================


class TestNotifyAlert:
    """notify_alert — sends a general alert."""

    def test_sends_alert_with_body(self):
        """Includes body when provided."""
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_alert("System OK", "All modules healthy"))
            mock_send.assert_awaited_once()
            msg = mock_send.await_args[0][0]
            assert "ALERT" in msg
            assert "System OK" in msg
            assert "All modules healthy" in msg

    def test_sends_alert_without_body(self):
        """Works with title only."""
        with patch("app.utils.notify.send_telegram", AsyncMock()) as mock_send:
            _run(notify_alert("System OK"))
            mock_send.assert_awaited_once()
