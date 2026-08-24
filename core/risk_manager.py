"""
risk_manager.py
~~~~~~~~~~~~~~~
Thin adapter that exposes risk-related operations through the StateManager.
"""

from __future__ import annotations

from core.state_manager import StateManager


class RiskManager:
    """Wraps StateManager to provide a clean risk-management interface."""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager

    # ── Gate check ────────────────────────────────────────

    def can_trade(self) -> bool:
        """
        Return True only when the bot is allowed to open new positions.

        Returns False if:
        - trading_enabled is False (kill switch activated), or
        - reconnecting is True (WebSocket is not stable).
        """
        return self.state.trading_enabled and not self.state.reconnecting

    # ── Outcome recording ─────────────────────────────────

    def record_loss(self):
        """
        Increment the daily loss counter.
        Triggers the kill switch automatically if MAX_DAILY_LOSSES is reached.
        """
        self.state.increment_loss()

    def record_win(self):
        """Increment the daily win counter."""
        self.state.increment_win()

    # ── Daily reset ───────────────────────────────────────

    def daily_reset(self):
        """
        Delegate to StateManager.daily_reset():
        - clears daily loss/win counters
        - re-enables trading
        - resets all symbol states to IDLE
        - clears consumed levels
        """
        self.state.daily_reset()
