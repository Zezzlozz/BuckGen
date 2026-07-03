"""
Unit tests for System Monitor & Self-Correction (app/modules/system.py).

Tests cover:
  - Module registration
  - Success/error recording with status transitions
  - Circuit breaker (open on threshold, auto-reset after timeout)
  - Manual reset and reset_all
  - Query methods: get_state, get_summary, get_detailed, errors_in_window
  - Recovery dispatching
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.system import (
    CIRCUIT_BREAKER_RESET_SEC,
    CIRCUIT_BREAKER_THRESHOLD,
    SystemMonitor,
)


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# Registration
# =============================================================================


class TestRegistration:
    """register / ensure_registered — add modules for tracking."""

    def test_register_creates_state(self):
        m = SystemMonitor()
        state = m.register("bounties")
        assert state.name == "bounties"
        assert state.status == "ok"
        assert state.consecutive_failures == 0

    def test_register_is_idempotent(self):
        m = SystemMonitor()
        s1 = m.register("bounties")
        s2 = m.register("bounties")
        assert s1 is s2

    def test_ensure_registered_adds_all(self):
        m = SystemMonitor()
        m.ensure_registered(["a", "b", "c"])
        assert len(m._modules) == 3


# =============================================================================
# Recording
# =============================================================================


class TestRecordSuccess:
    """record_success — resets failure counters and sets status to ok."""

    def test_sets_status_ok(self):
        m = SystemMonitor()
        m.register("test")
        m.record_success("test")
        assert m._modules["test"].status == "ok"
        assert m._modules["test"].consecutive_failures == 0

    def test_auto_registers(self):
        m = SystemMonitor()
        m.record_success("new_module")
        assert "new_module" in m._modules

    def test_resets_after_errors(self):
        m = SystemMonitor()
        m.record_error("test", "err1")
        m.record_error("test", "err2")
        assert m._modules["test"].consecutive_failures == 2
        m.record_success("test")
        assert m._modules["test"].consecutive_failures == 0


class TestRecordError:
    """record_error — increments counters and transitions status."""

    def test_increments_counters(self):
        m = SystemMonitor()
        m.record_error("test", "something broke")
        assert m._modules["test"].consecutive_failures == 1
        assert m._modules["test"].total_errors == 1
        assert m._modules["test"].last_error_msg == "something broke"

    def test_status_degraded_after_three(self):
        m = SystemMonitor()
        for _ in range(3):
            m.record_error("test", "x")
        assert m._modules["test"].status == "degraded"

    def test_circuit_opens_at_threshold(self):
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            m.record_error("test", "x")
        assert m._modules["test"].status == "circuit_open"

    def test_degraded_before_threshold(self):
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD - 1):
            m.record_error("test", "x")
        # At 4 failures, should still be degraded (not circuit_open)
        assert m._modules["test"].status == "degraded"
        assert m._modules["test"].circuit_opened_at == 0.0

    def test_circuit_only_opens_once(self):
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD + 3):
            m.record_error("test", "x")
        # circuit_opened_at should be set only once
        assert m._modules["test"].circuit_opened_at > 0

    def test_auto_registers(self):
        m = SystemMonitor()
        m.record_error("new_module", "err")
        assert "new_module" in m._modules


# =============================================================================
# Circuit breaker
# =============================================================================


class TestCircuitBreaker:
    """is_circuit_open / can_run — check and auto-reset."""

    def test_closed_for_unknown_module(self):
        m = SystemMonitor()
        assert m.is_circuit_open("unknown") is False

    def test_closed_when_ok(self):
        m = SystemMonitor()
        m.register("test")
        assert m.is_circuit_open("test") is False

    def test_open_after_threshold(self):
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            m.record_error("test", "x")
        assert m.is_circuit_open("test") is True
        assert m.can_run("test") is False

    def test_auto_resets_after_timeout(self):
        """Circuit auto-resets when enough time has passed."""
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            m.record_error("test", "x")
        assert m.is_circuit_open("test") is True

        # Simulate time passing beyond the reset window
        with patch(
            "time.time", return_value=time.time() + CIRCUIT_BREAKER_RESET_SEC + 1
        ):
            assert m.is_circuit_open("test") is False
            assert m._modules["test"].status == "degraded"
            assert m._modules["test"].consecutive_failures == 0
            assert m._modules["test"].circuit_opened_at == 0.0


class TestResetModule:
    """reset_module — manually reset a module."""

    def test_resets_status_and_counters(self):
        m = SystemMonitor()
        m.record_error("test", "x")
        m.record_error("test", "x")
        result = m.reset_module("test")
        assert result is True
        assert m._modules["test"].status == "ok"
        assert m._modules["test"].consecutive_failures == 0
        assert m._modules["test"].circuit_opened_at == 0.0

    def test_returns_false_for_unknown(self):
        m = SystemMonitor()
        assert m.reset_module("unknown") is False

    def test_increments_recovery_attempts(self):
        m = SystemMonitor()
        m.record_error("test", "x")
        before = m._modules["test"].recovery_attempts
        m.reset_module("test")
        assert m._modules["test"].recovery_attempts == before + 1


class TestResetAll:
    """reset_all — resets every registered module."""

    def test_resets_all(self):
        m = SystemMonitor()
        m.ensure_registered(["a", "b", "c"])
        m.record_error("a", "err")
        m.record_error("b", "err")
        count = m.reset_all()
        assert count == 3
        assert m._modules["a"].status == "ok"
        assert m._modules["b"].status == "ok"
        assert m._modules["c"].status == "ok"


# =============================================================================
# Queries
# =============================================================================


class TestErrorsInWindow:
    """errors_in_window — counts errors in the last N minutes."""

    def test_zero_when_no_errors(self):
        m = SystemMonitor()
        assert m.errors_in_window(60) == 0

    def test_counts_recent_errors(self):
        m = SystemMonitor()
        m.record_error("test", "e1")
        m.record_error("test", "e2")
        assert m.errors_in_window(60) == 2

    def test_excludes_old_errors(self):
        m = SystemMonitor()
        with patch("time.time", return_value=1000):
            m.record_error("test", "old")
        # 61 minutes later
        with patch("time.time", return_value=1000 + 61 * 60):
            assert m.errors_in_window(60) == 0


class TestGetState:
    """get_state — returns detailed state for a module."""

    def test_returns_none_for_unknown(self):
        m = SystemMonitor()
        assert m.get_state("unknown") is None

    def test_returns_state_dict(self):
        m = SystemMonitor()
        m.record_error("test", "something went wrong")
        state = m.get_state("test")
        assert state["name"] == "test"
        assert "status" in state
        assert "consecutive_failures" in state
        assert "total_errors" in state
        assert "last_error_msg" in state
        assert state["last_error_msg"] == "something went wrong"


class TestGetSummary:
    """get_summary — overall system health."""

    def test_empty_monitor(self):
        m = SystemMonitor()
        s = m.get_summary()
        assert s["modules_total"] == 0
        assert s["modules_ok"] == 0
        assert s["modules_degraded"] == 0
        assert s["modules_down"] == 0
        assert s["errors_last_hour"] == 0
        assert s["uptime_seconds"] >= 0

    def test_counts_correctly(self):
        m = SystemMonitor()
        m.ensure_registered(["a", "b", "c"])
        m.record_error("a", "err1")  # degraded (1 failure)
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            m.record_error("b", "err")  # circuit_open
        # c is ok
        s = m.get_summary()
        assert s["modules_total"] == 3
        assert s["modules_ok"] == 1  # only c
        assert s["modules_degraded"] == 1  # a
        assert s["modules_down"] == 1  # b


class TestGetDetailed:
    """get_detailed — includes per-module state list."""

    def test_includes_modules_list(self):
        m = SystemMonitor()
        m.register("test")
        d = m.get_detailed()
        assert "modules" in d
        assert len(d["modules"]) == 1
        assert d["modules"][0]["name"] == "test"

    def test_sorted_by_name(self):
        m = SystemMonitor()
        m.ensure_registered(["z", "a", "m"])
        d = m.get_detailed()
        names = [mod["name"] for mod in d["modules"]]
        assert names == ["a", "m", "z"]


# =============================================================================
# Recovery
# =============================================================================


class TestRecoverAll:
    """recover_all — dispatches recovery for degraded/down modules."""

    def test_skips_ok_modules(self):
        m = SystemMonitor()
        m.register("ok_module")
        results = _run(m.recover_all())
        assert results == {}

    def test_recovers_degraded_modules(self):
        m = SystemMonitor()
        m.record_error("bounties", "err")
        m.record_error("bounties", "err")
        m.record_error("bounties", "err")
        assert m._modules["bounties"].status == "degraded"

        results = _run(m.recover_all())
        assert "bounties" in results
        assert "Reset" in results["bounties"] or "Circuit" in results["bounties"]
        # After recovery, should be reset
        assert m._modules["bounties"].status == "ok"

    def test_recovers_circuit_open_modules(self):
        m = SystemMonitor()
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            m.record_error("airdrops", "err")
        assert m._modules["airdrops"].status == "circuit_open"

        results = _run(m.recover_all())
        assert "airdrops" in results

    def test_recovers_rpc_by_clearing_cache(self):
        m = SystemMonitor()
        with patch("app.modules.rpc._w3_cache", {"eth": "mock", "bsc": "mock2"}):
            m.record_error("rpc", "err")
            results = _run(m.recover_all())
            assert "rpc" in results
            assert "Reset 2 RPC connections" in results["rpc"]
            # Cache should be cleared
            from app.modules.rpc import _w3_cache

            assert len(_w3_cache) == 0

    def test_recovers_prices_by_clearing_exchanges(self):
        m = SystemMonitor()
        with patch("app.modules.prices._exchanges", {"binance": "mock"}):
            m.record_error("prices", "err")
            results = _run(m.recover_all())
            assert "prices" in results
            assert "Reset 1 exchange" in results["prices"]
