"""
System Health & Self-Correction Module — monitors all subsystems,
tracks error rates, executes recovery actions, and exposes a unified
health dashboard.

Capabilities:
  - Per-module success/failure tracking with time windows
  - Circuit breaker: auto-disable module after N consecutive failures
  - Recovery actions: retry, reset RPC connections, re-cache exchanges
  - Unified health aggregation for dashboard endpoints
  - Periodic self-heal scheduler job
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger("buckgen.system")

# =============================================================================
# Constants
# =============================================================================

CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures before circuit opens
CIRCUIT_BREAKER_RESET_SEC = 3600  # auto-reset after 1 hour
ERROR_WINDOW_SEC = 3600  # track errors in last 1 hour


# =============================================================================
# Module state registry
# =============================================================================


@dataclass
class ModuleState:
    """Runtime state for a single module/sub-system."""

    name: str
    status: str = "ok"  # ok, degraded, down, circuit_open
    consecutive_failures: int = 0
    total_errors: int = 0
    last_success: float = 0.0
    last_error: float = 0.0
    last_error_msg: str = ""
    error_timestamps: list[float] = field(default_factory=list)
    circuit_opened_at: float = 0.0  # when circuit was opened (0 = closed)
    recovery_attempts: int = 0
    last_recovery: float = 0.0


class SystemMonitor:
    """
    Central health registry for all agent modules.
    Thread-safe for async context (single event loop).

    Usage:
        monitor = SystemMonitor()
        monitor.record_success("bounties")
        monitor.record_error("bounties", "API rate limited")
        status = monitor.get_summary()
    """

    def __init__(self):
        self._modules: dict[str, ModuleState] = {}
        self._started_at: float = time.time()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str) -> ModuleState:
        """Register a module for tracking. Idempotent."""
        if name not in self._modules:
            self._modules[name] = ModuleState(name=name)
            logger.debug("[system] Registered module '%s'", name)
        return self._modules[name]

    def ensure_registered(self, names: list[str]) -> None:
        """Register multiple modules at once."""
        for name in names:
            self.register(name)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_success(self, name: str) -> None:
        """Record a successful module execution."""
        state = self.register(name)
        state.status = "ok"
        state.consecutive_failures = 0
        state.last_success = time.time()

    def record_error(self, name: str, msg: str = "") -> None:
        """Record a module error. Opens circuit if threshold exceeded."""
        state = self.register(name)
        state.consecutive_failures += 1
        state.total_errors += 1
        state.last_error = time.time()
        state.last_error_msg = msg[:200]
        state.error_timestamps.append(time.time())

        # Prune old timestamps
        cutoff = time.time() - ERROR_WINDOW_SEC
        state.error_timestamps = [t for t in state.error_timestamps if t > cutoff]

        # Circuit breaker
        if state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            if state.status != "circuit_open":
                state.status = "circuit_open"
                state.circuit_opened_at = time.time()
                logger.warning(
                    "[system] Circuit OPEN for '%s' after %d consecutive failures",
                    name,
                    state.consecutive_failures,
                )
        elif state.consecutive_failures >= 3:
            state.status = "degraded"
        else:
            state.status = "degraded"

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def is_circuit_open(self, name: str) -> bool:
        """Check if module's circuit breaker is open (module disabled)."""
        state = self._modules.get(name)
        if not state or state.status != "circuit_open":
            return False
        # Auto-reset after timeout
        if state.circuit_opened_at and (
            time.time() - state.circuit_opened_at > CIRCUIT_BREAKER_RESET_SEC
        ):
            logger.info("[system] Circuit auto-reset for '%s' after timeout", name)
            state.status = "degraded"
            state.consecutive_failures = 0
            state.circuit_opened_at = 0.0
            return False
        return True

    def can_run(self, name: str) -> bool:
        """Check if a module is allowed to run (circuit not open)."""
        return not self.is_circuit_open(name)

    def reset_module(self, name: str) -> bool:
        """Manually reset a module's circuit breaker. Returns True if existed."""
        state = self._modules.get(name)
        if not state:
            return False
        state.status = "ok"
        state.consecutive_failures = 0
        state.circuit_opened_at = 0.0
        state.recovery_attempts += 1
        state.last_recovery = time.time()
        logger.info("[system] Manual reset for module '%s'", name)
        return True

    def reset_all(self) -> int:
        """Reset all modules. Returns count reset."""
        count = 0
        for name in list(self._modules.keys()):
            if self.reset_module(name):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def errors_in_window(self, minutes: int = 60) -> int:
        """Count errors across all modules in the last N minutes."""
        cutoff = time.time() - (minutes * 60)
        total = 0
        for state in self._modules.values():
            total += sum(1 for t in state.error_timestamps if t > cutoff)
        return total

    def get_state(self, name: str) -> Optional[dict]:
        """Get detailed state for a module."""
        state = self._modules.get(name)
        if not state:
            return None
        return {
            "name": state.name,
            "status": state.status,
            "consecutive_failures": state.consecutive_failures,
            "total_errors": state.total_errors,
            "last_success_ago": self._ago(state.last_success),
            "last_error_ago": self._ago(state.last_error),
            "last_error_msg": state.last_error_msg,
            "circuit_open": state.status == "circuit_open",
            "recovery_attempts": state.recovery_attempts,
        }

    def get_summary(self) -> dict:
        """Get overall system health summary."""
        total = len(self._modules)
        ok_count = sum(1 for s in self._modules.values() if s.status == "ok")
        degraded = sum(1 for s in self._modules.values() if s.status == "degraded")
        down = sum(
            1 for s in self._modules.values() if s.status in ("down", "circuit_open")
        )

        return {
            "uptime_seconds": round(time.time() - self._started_at),
            "uptime_human": self._format_uptime(),
            "modules_total": total,
            "modules_ok": ok_count,
            "modules_degraded": degraded,
            "modules_down": down,
            "errors_last_hour": self.errors_in_window(60),
            "started_at": datetime.fromtimestamp(
                self._started_at, tz=timezone.utc
            ).isoformat(),
        }

    def get_detailed(self) -> dict:
        """Get detailed status for all modules."""
        summary = self.get_summary()
        summary["modules"] = [
            self.get_state(name) for name in sorted(self._modules.keys())
        ]
        return summary

    # ------------------------------------------------------------------
    # Recovery actions
    # ------------------------------------------------------------------

    async def recover_all(self) -> dict[str, str]:
        """
        Attempt to recover all degraded/down modules.
        Returns dict: module_name -> result message.
        """
        results: dict[str, str] = {}
        for name, state in self._modules.items():
            if state.status != "ok":
                result = await self._recover_module(name)
                results[name] = result
        return results

    async def _recover_module(self, name: str) -> str:
        """Attempt to recover a specific module."""
        logger.info("[system] Attempting recovery of '%s'", name)

        try:
            if name == "rpc":
                # Clear RPC connection cache so it reconnects
                from app.modules.rpc import _w3_cache

                count = len(_w3_cache)
                _w3_cache.clear()
                self.reset_module(name)
                return f"Reset {count} RPC connections"

            elif name == "prices":
                # Clear exchange cache for reconnection
                from app.modules.prices import _exchanges

                count = len(_exchanges)
                _exchanges.clear()
                self.reset_module(name)
                return f"Reset {count} exchange connections"

            elif name == "wallets":
                # Re-derive wallet keyring
                from app.modules.wallet import (
                    derive_wallet,
                    get_all_wallets,
                    zero_keyring,
                )

                zero_keyring()
                derive_wallet(index=0, chain="ethereum")
                self.reset_module(name)
                return "Re-derived wallet keyring"

            elif name in ("bounties", "airdrops", "gas"):
                # These are stateless — just reset the circuit
                self.reset_module(name)
                return "Circuit reset (stateless module)"

            else:
                self.reset_module(name)
                return "Reset OK"

        except Exception as exc:
            logger.warning("[system] Recovery of '%s' failed: %s", name, exc)
            return f"Failed: {exc}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ago(timestamp: float) -> str:
        if timestamp == 0:
            return "never"
        seconds = time.time() - timestamp
        if seconds < 60:
            return f"{int(seconds)}s ago"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m ago"
        else:
            return f"{int(seconds / 3600)}h ago"

    def _format_uptime(self) -> str:
        seconds = time.time() - self._started_at
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h {m}m {s}s"


# =============================================================================
# Singleton
# =============================================================================

monitor = SystemMonitor()

# Register known modules
monitor.ensure_registered(
    [
        "bounties",
        "prices",
        "airdrops",
        "gas",
        "rpc",
        "wallets",
        "scheduler",
    ]
)
