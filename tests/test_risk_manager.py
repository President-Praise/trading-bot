import unittest
from unittest.mock import MagicMock, patch
import config
from core.risk_manager import RiskManager
from core.state_manager import StateManager


class TestRiskManager(unittest.TestCase):

    def setUp(self):
        # Use a real StateManager but patch out file I/O
        with patch.object(StateManager, 'load_state'), \
             patch.object(StateManager, 'save_daily_stats'), \
             patch.object(StateManager, 'save_consumed_levels'):
            self.state = StateManager()
        self.state.trading_enabled   = True
        self.state.reconnecting      = False
        self.state.daily_loss_count  = 0
        self.state.daily_win_count   = 0
        # Patch saves to avoid file I/O in tests
        self.state.save_daily_stats    = MagicMock()
        self.state.save_consumed_levels = MagicMock()
        self.state.save_symbol_session = MagicMock()
        self.rm = RiskManager(self.state)

    def test_loss_increments_counter(self):
        self.rm.record_loss()
        self.assertEqual(self.state.daily_loss_count, 1)

    def test_kill_switch_at_limit(self):
        for _ in range(config.MAX_DAILY_LOSSES):
            self.rm.record_loss()
        self.assertFalse(self.state.trading_enabled)

    def test_trading_not_killed_before_limit(self):
        for _ in range(config.MAX_DAILY_LOSSES - 1):
            self.rm.record_loss()
        self.assertTrue(self.state.trading_enabled)

    def test_daily_reset_clears_counter(self):
        for _ in range(10):
            self.rm.record_loss()
        self.rm.daily_reset()
        self.assertEqual(self.state.daily_loss_count, 0)
        self.assertTrue(self.state.trading_enabled)

    def test_win_increments_counter(self):
        self.rm.record_win()
        self.assertEqual(self.state.daily_win_count, 1)

    def test_can_trade_true(self):
        self.assertTrue(self.rm.can_trade())

    def test_can_trade_false_when_disabled(self):
        self.state.trading_enabled = False
        self.assertFalse(self.rm.can_trade())

    def test_can_trade_false_when_reconnecting(self):
        self.state.reconnecting = True
        self.assertFalse(self.rm.can_trade())

    def test_daily_reset_enables_trading_after_kill_switch(self):
        for _ in range(10):
            self.rm.record_loss()
        self.assertFalse(self.state.trading_enabled)
        self.rm.daily_reset()
        self.assertTrue(self.state.trading_enabled)


if __name__ == '__main__':
    unittest.main()
