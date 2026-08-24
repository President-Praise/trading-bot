import unittest
from unittest.mock import patch, MagicMock
from core.state_manager import StateManager


class TestStateManager(unittest.TestCase):

    def setUp(self):
        with patch.object(StateManager, 'load_state'), \
             patch.object(StateManager, 'save_daily_stats'), \
             patch.object(StateManager, 'save_consumed_levels'):
            self.sm = StateManager()
        self.sm.save_daily_stats     = MagicMock()
        self.sm.save_consumed_levels = MagicMock()
        self.sm.save_symbol_session  = MagicMock()

    def test_initial_state_is_idle(self):
        self.sm.init_symbol('R_75')
        self.assertEqual(self.sm.symbol_states['R_75'], 'IDLE')

    def test_state_advances_correctly(self):
        self.sm.init_symbol('R_75')
        self.sm.set_state('R_75', 'BIAS_SET')
        self.assertEqual(self.sm.symbol_states['R_75'], 'BIAS_SET')

    def test_state_advances_to_h4_confirmed(self):
        self.sm.init_symbol('R_75')
        self.sm.daily_bias['R_75'] = 'BUY'
        self.sm.set_state('R_75', 'H4_CONFIRMED')
        self.assertEqual(self.sm.symbol_states['R_75'], 'H4_CONFIRMED')

    def test_consumed_level_marked(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        levels = self.sm.consumed_levels['R_75']
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0]['level'], 3839.52)

    def test_consumed_level_skipped_on_reentry(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        is_consumed = self.sm.is_consumed('R_75', level=3839.52, side='sell', tolerance=0.01)
        self.assertTrue(is_consumed)

    def test_consumed_level_detected_within_tolerance(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        # 3839.525 is within 0.01 of 3839.52
        is_consumed = self.sm.is_consumed('R_75', level=3839.525, side='sell', tolerance=0.01)
        self.assertTrue(is_consumed)

    def test_not_consumed_when_different_side(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        is_consumed = self.sm.is_consumed('R_75', level=3839.52, side='buy', tolerance=0.01)
        self.assertFalse(is_consumed)

    def test_not_consumed_outside_tolerance(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        is_consumed = self.sm.is_consumed('R_75', level=3900.00, side='sell', tolerance=0.01)
        self.assertFalse(is_consumed)

    def test_daily_reset_clears_consumed_levels(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', level=3839.52, side='sell')
        self.sm.daily_reset()
        self.assertEqual(self.sm.consumed_levels.get('R_75', []), [])

    def test_daily_reset_resets_symbol_state_to_idle(self):
        self.sm.init_symbol('R_75')
        self.sm.daily_bias['R_75'] = 'BUY'
        self.sm.set_state('R_75', 'H4_CONFIRMED')
        self.sm.daily_reset()
        self.assertEqual(self.sm.symbol_states['R_75'], 'IDLE')

    def test_increment_loss_updates_counter(self):
        self.sm.daily_loss_count = 0
        self.sm.increment_loss()
        self.assertEqual(self.sm.daily_loss_count, 1)

    def test_kill_switch_activates_at_limit(self):
        import config
        self.sm.trading_enabled  = True
        self.sm.daily_loss_count = 0
        for _ in range(config.MAX_DAILY_LOSSES):
            self.sm.increment_loss()
        self.assertFalse(self.sm.trading_enabled)

    def test_increment_win_updates_counter(self):
        self.sm.daily_win_count = 0
        self.sm.increment_win()
        self.assertEqual(self.sm.daily_win_count, 1)

    def test_multiple_symbols_independent(self):
        self.sm.init_symbol('R_50')
        self.sm.init_symbol('R_75')
        self.sm.set_state('R_50', 'BIAS_SET')
        self.assertEqual(self.sm.symbol_states['R_50'], 'BIAS_SET')
        self.assertEqual(self.sm.symbol_states['R_75'], 'IDLE')


if __name__ == '__main__':
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# Fix #3 — mark_consumed duplicate guard
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkConsumedDuplicateGuard(unittest.TestCase):
    """mark_consumed must silently skip entries that are already recorded."""

    def setUp(self):
        with patch.object(StateManager, 'load_state'), \
             patch.object(StateManager, 'save_daily_stats'), \
             patch.object(StateManager, 'save_consumed_levels'):
            self.sm = StateManager()
        self.sm.save_daily_stats     = MagicMock()
        self.sm.save_consumed_levels = MagicMock()
        self.sm.save_symbol_session  = MagicMock()

    def test_exact_duplicate_not_appended(self):
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 3839.52, 'sell')
        self.sm.mark_consumed('R_75', 3839.52, 'sell')
        self.assertEqual(len(self.sm.consumed_levels['R_75']), 1)

    def test_near_duplicate_within_tolerance_not_appended(self):
        # |3839.525 - 3839.52| = 0.005 which is <= the 0.01 hardcoded guard
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 3839.52, 'sell')
        self.sm.mark_consumed('R_75', 3839.525, 'sell')
        self.assertEqual(len(self.sm.consumed_levels['R_75']), 1)

    def test_different_side_is_not_treated_as_duplicate(self):
        # Same level but opposite side must be stored separately
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 3839.52, 'sell')
        self.sm.mark_consumed('R_75', 3839.52, 'buy')
        self.assertEqual(len(self.sm.consumed_levels['R_75']), 2)

    def test_outside_tolerance_is_appended(self):
        # Level far from the existing one must be stored as a new entry
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 3839.52, 'sell')
        self.sm.mark_consumed('R_75', 3900.00, 'sell')
        self.assertEqual(len(self.sm.consumed_levels['R_75']), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Fix #7 — daily_reset clears h4_sweep_candle and respects active symbols
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyResetH4SweepCandle(unittest.TestCase):
    """daily_reset must clear h4_sweep_candle and skip IN_TRADE / COOLDOWN."""

    def setUp(self):
        with patch.object(StateManager, 'load_state'), \
             patch.object(StateManager, 'save_daily_stats'), \
             patch.object(StateManager, 'save_consumed_levels'):
            self.sm = StateManager()
        self.sm.save_daily_stats     = MagicMock()
        self.sm.save_consumed_levels = MagicMock()
        self.sm.save_symbol_session  = MagicMock()

    def test_h4_sweep_candle_cleared_on_daily_reset(self):
        self.sm.init_symbol('R_75')
        self.sm.h4_sweep_candle['R_75'] = {
            'open': 100, 'high': 110, 'low': 90, 'close': 105, 'time': 0,
        }
        self.sm.daily_reset()
        self.assertIsNone(self.sm.h4_sweep_candle.get('R_75'))

    def test_daily_reset_preserves_in_trade_symbol_state(self):
        self.sm.init_symbol('R_75')
        self.sm.symbol_states['R_75'] = 'IN_TRADE'
        self.sm.daily_reset()
        self.assertEqual(self.sm.symbol_states['R_75'], 'IN_TRADE')

    def test_daily_reset_preserves_cooldown_symbol_state(self):
        self.sm.init_symbol('R_75')
        self.sm.symbol_states['R_75'] = 'COOLDOWN'
        self.sm.daily_reset()
        self.assertEqual(self.sm.symbol_states['R_75'], 'COOLDOWN')

    def test_daily_reset_resets_non_active_symbol_to_idle(self):
        self.sm.init_symbol('R_75')
        self.sm.symbol_states['R_75'] = 'H4_CONFIRMED'
        self.sm.daily_reset()
        self.assertEqual(self.sm.symbol_states['R_75'], 'IDLE')

    def test_daily_reset_preserves_consumed_levels_for_in_trade_symbol(self):
        self.sm.init_symbol('R_75')
        self.sm.symbol_states['R_75'] = 'IN_TRADE'
        self.sm.mark_consumed('R_75', 100.0, 'sell')
        self.sm.daily_reset()
        self.assertEqual(len(self.sm.consumed_levels.get('R_75', [])), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Fix #13 — is_consumed uses dynamic avg_range threshold
# ─────────────────────────────────────────────────────────────────────────────

class TestIsConsumedDynamicThreshold(unittest.TestCase):
    """is_consumed must use config.LIQUIDITY_TOLERANCE * avg_range when avg_range is given."""

    def setUp(self):
        with patch.object(StateManager, 'load_state'), \
             patch.object(StateManager, 'save_daily_stats'), \
             patch.object(StateManager, 'save_consumed_levels'):
            self.sm = StateManager()
        self.sm.save_daily_stats     = MagicMock()
        self.sm.save_consumed_levels = MagicMock()
        self.sm.save_symbol_session  = MagicMock()

    def test_level_within_dynamic_threshold_is_consumed(self):
        # LIQUIDITY_TOLERANCE=0.05, avg_range=1.0 → threshold=0.05
        # |100.03 - 100.0| = 0.03 < 0.05 → consumed
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 100.0, 'sell')
        self.assertTrue(
            self.sm.is_consumed('R_75', 100.03, 'sell', avg_range=1.0)
        )

    def test_level_outside_dynamic_threshold_is_not_consumed(self):
        # LIQUIDITY_TOLERANCE=0.05, avg_range=1.0 → threshold=0.05
        # |100.1 - 100.0| = 0.1 > 0.05 → not consumed
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 100.0, 'sell')
        self.assertFalse(
            self.sm.is_consumed('R_75', 100.1, 'sell', avg_range=1.0)
        )

    def test_dynamic_threshold_scales_with_avg_range(self):
        # avg_range=10.0 → threshold=0.5; |100.4 - 100.0| = 0.4 < 0.5 → consumed
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 100.0, 'sell')
        self.assertTrue(
            self.sm.is_consumed('R_75', 100.4, 'sell', avg_range=10.0)
        )

    def test_dynamic_threshold_does_not_match_when_legacy_would(self):
        # Legacy tolerance=0.01 would match 100.005.
        # Dynamic: avg_range=0.05 → threshold=0.05*0.05=0.0025; |100.005-100.0|=0.005 > 0.0025
        self.sm.init_symbol('R_75')
        self.sm.mark_consumed('R_75', 100.0, 'sell')
        self.assertFalse(
            self.sm.is_consumed('R_75', 100.005, 'sell', avg_range=0.05)
        )
