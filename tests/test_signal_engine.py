import unittest
from core.signal_engine import (
    detect_daily_bias,
    confirm_4h_sweep,
    detect_m15_entry,
    is_rejection_candle,
    calculate_sl_tp,
    find_4h_swing,
    detect_m15_liquidity_levels,
    identify_order_block,
)


def make_candle(open_, high, low, close, time=0):
    return {'open': open_, 'high': high, 'low': low, 'close': close, 'time': time}


class TestDailyBias(unittest.TestCase):

    def test_buy_bias_detected(self):
        candles = [
            make_candle(100, 105, 85, 90),
            make_candle(90, 92, 87, 91),
            make_candle(91, 93, 88, 92),
        ]
        result = detect_daily_bias(candles, prev_high=110, prev_low=90)
        self.assertEqual(result, 'BUY')

    def test_sell_bias_detected(self):
        candles = [
            make_candle(100, 115, 95, 110),
            make_candle(110, 112, 106, 108),
            make_candle(108, 110, 105, 107),
        ]
        result = detect_daily_bias(candles, prev_high=110, prev_low=90)
        self.assertEqual(result, 'SELL')

    def test_no_bias_when_no_rejection(self):
        candles = [
            make_candle(100, 115, 95, 113),
            make_candle(113, 116, 111, 115),
            make_candle(115, 118, 113, 117),
        ]
        result = detect_daily_bias(candles, prev_high=110, prev_low=90)
        self.assertEqual(result, 'NONE')


class TestSweepConfirmation(unittest.TestCase):

    def test_4h_buy_sweep_confirmed(self):
        swing_low = 100.0
        candle = make_candle(open_=101, high=103, low=98, close=102)
        self.assertTrue(confirm_4h_sweep(candle, swing_low, direction='BUY'))

    def test_4h_buy_sweep_not_confirmed_close_below(self):
        swing_low = 100.0
        candle = make_candle(open_=101, high=103, low=98, close=99)
        self.assertFalse(confirm_4h_sweep(candle, swing_low, direction='BUY'))

    def test_4h_sell_sweep_confirmed(self):
        swing_high = 100.0
        candle = make_candle(open_=99, high=102, low=97, close=98)
        self.assertTrue(confirm_4h_sweep(candle, swing_high, direction='SELL'))

    def test_4h_sell_sweep_not_confirmed_close_above(self):
        swing_high = 100.0
        candle = make_candle(open_=99, high=102, low=97, close=101)
        self.assertFalse(confirm_4h_sweep(candle, swing_high, direction='SELL'))


class TestRejectionCandle(unittest.TestCase):

    def test_bullish_rejection(self):
        c = make_candle(open_=100, high=105, low=90, close=102)
        self.assertTrue(is_rejection_candle(c, 'BUY'))

    def test_bearish_rejection(self):
        c = make_candle(open_=100, high=115, low=95, close=98)
        self.assertTrue(is_rejection_candle(c, 'SELL'))

    def test_non_rejection_bullish_candle(self):
        c = make_candle(open_=100, high=115, low=99, close=114)
        self.assertFalse(is_rejection_candle(c, 'BUY'))

    def test_non_rejection_bearish_candle(self):
        c = make_candle(open_=114, high=115, low=99, close=100)
        self.assertFalse(is_rejection_candle(c, 'SELL'))


class TestSLTP(unittest.TestCase):

    def test_buy_sl_tp_rr2(self):
        """RR_MODE=2 should produce exactly 1:2 risk/reward."""
        sweep = make_candle(100, 110, 90, 103)  # range = 20
        result = calculate_sl_tp(103, sweep, 'BUY', rr_mode=2)
        expected_sl = 90 - 0.1 * 20            # 88.0
        risk        = abs(103 - expected_sl)
        expected_tp = 103 + 2 * risk            # 1:2
        self.assertAlmostEqual(result['sl'], expected_sl)
        self.assertAlmostEqual(result['tp'], expected_tp)

    def test_sell_sl_tp_rr2(self):
        """RR_MODE=2 should produce exactly 1:2 risk/reward for SELL."""
        sweep = make_candle(100, 115, 95, 98)   # range = 20
        result = calculate_sl_tp(98, sweep, 'SELL', rr_mode=2)
        expected_sl = 115 + 0.1 * 20            # 117.0
        risk        = abs(98 - expected_sl)
        expected_tp = 98 - 2 * risk              # 1:2
        self.assertAlmostEqual(result['sl'], expected_sl)
        self.assertAlmostEqual(result['tp'], expected_tp)

    def test_buy_sl_tp_rr1(self):
        """RR_MODE=1 should produce 1:1."""
        sweep = make_candle(100, 110, 80, 105)  # range = 30
        result = calculate_sl_tp(105, sweep, 'BUY', rr_mode=1)
        expected_sl = 80 - 0.1 * 30             # 77
        risk        = abs(105 - expected_sl)
        expected_tp = 105 + 1 * risk             # 1:1
        self.assertAlmostEqual(result['sl'], expected_sl)
        self.assertAlmostEqual(result['tp'], expected_tp)

    def test_buy_sl_tp_rr4(self):
        """RR_MODE=4 should produce 1:4 — previously broken by the hardcoded coercion."""
        sweep = make_candle(100, 110, 90, 103)  # range = 20
        result = calculate_sl_tp(103, sweep, 'BUY', rr_mode=4)
        expected_sl = 90 - 0.1 * 20
        risk        = abs(103 - expected_sl)
        expected_tp = 103 + 4 * risk             # strictly 1:4
        self.assertAlmostEqual(result['tp'], expected_tp)

    def test_rr3_not_coerced_to_4(self):
        """Fix #4 regression: rr_mode=3 must give 1:3, not 1:4."""
        sweep  = make_candle(100, 110, 90, 103)
        r3     = calculate_sl_tp(103, sweep, 'BUY', rr_mode=3)
        r4     = calculate_sl_tp(103, sweep, 'BUY', rr_mode=4)
        self.assertLess(r3['tp'], r4['tp'],
                        "rr_mode=3 TP should be less than rr_mode=4 TP")


class TestM15EntryTimeFilter(unittest.TestCase):
    """Fix #16: tests for the h4_sweep_time filtering path in detect_m15_entry."""

    def _make_buy_sequence(self, base_time: int, level: float):
        """Return [sweep_candle, rejection_candle, forming_candle] for a BUY setup."""
        # sweep candle: low dips below level, body is small → bullish rejection pin
        sweep = make_candle(
            open_=level + 5,
            high =level + 8,
            low  =level - 3,   # sweeps below level
            close=level + 4,   # close above level (rejection)
            time =base_time,
        )
        # confirmation candle: bullish rejection
        confirm = make_candle(
            open_=level + 3,
            high =level + 6,
            low  =level + 1,
            close=level + 5,
            time =base_time + 900,
        )
        # still-forming candle (excluded by detect_m15_entry)
        forming = make_candle(
            open_=level + 5, high=level + 7,
            low=level + 4, close=level + 6,
            time=base_time + 1800,
        )
        return [sweep, confirm, forming]

    def test_entry_detected_when_no_time_filter(self):
        level    = 100.0
        candles  = self._make_buy_sequence(base_time=1000, level=level)
        liq      = {'equal_lows': level, 'swing_low': None,
                    'equal_highs': None, 'swing_high': None}
        result   = detect_m15_entry(candles, 'BUY', liq, h4_sweep_time=None)
        self.assertIsNotNone(result)
        self.assertEqual(result['direction'], 'BUY')

    def test_entry_blocked_when_candles_before_h4_sweep(self):
        """All signal candles pre-date the H4 sweep → no entry should be returned."""
        level   = 100.0
        candles = self._make_buy_sequence(base_time=1000, level=level)
        liq     = {'equal_lows': level, 'swing_low': None,
                   'equal_highs': None, 'swing_high': None}
        # h4_sweep_time is AFTER all our candles
        result  = detect_m15_entry(candles, 'BUY', liq, h4_sweep_time=5000)
        self.assertIsNone(result,
            "Entry should be blocked when all candles pre-date the H4 sweep")

    def test_entry_detected_when_candles_after_h4_sweep(self):
        """Signal candles post-date the H4 sweep → entry should be returned."""
        level   = 100.0
        candles = self._make_buy_sequence(base_time=2000, level=level)
        liq     = {'equal_lows': level, 'swing_low': None,
                   'equal_highs': None, 'swing_high': None}
        # h4_sweep_time is BEFORE all our candles
        result  = detect_m15_entry(candles, 'BUY', liq, h4_sweep_time=500)
        self.assertIsNotNone(result,
            "Entry should be detected when candles post-date the H4 sweep")

    def test_partial_filter_uses_only_post_sweep_candles(self):
        """Only candles after h4_sweep_time contribute to the signal window."""
        level = 100.0
        # Build a sequence where candle[0] is before the sweep, candles[1..] are after.
        # The sweep+rejection pair only forms in candles[1] and [2], so the entry
        # must still be found when candle[0] is filtered out.
        old_candle = make_candle(
            open_=level + 5, high=level + 7,
            low=level + 3, close=level + 6,
            time=500,            # before h4_sweep_time = 1000
        )
        seq      = self._make_buy_sequence(base_time=1500, level=level)
        candles  = [old_candle] + seq
        liq      = {'equal_lows': level, 'swing_low': None,
                    'equal_highs': None, 'swing_high': None}
        result   = detect_m15_entry(candles, 'BUY', liq, h4_sweep_time=1000)
        self.assertIsNotNone(result,
            "Entry should be detected from post-sweep candles even when earlier candles exist")


if __name__ == '__main__':
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# is_rejection_candle — displacement-close (disp_ok) path
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectionCandleDisplacementClose(unittest.TestCase):
    """The disp_ok branch fires when the wick-ratio test fails but the candle
    swept through swept_level and closed back on the correct side."""

    def test_buy_disp_ok_fires_when_wick_ratio_fails(self):
        # Bullish candle: open=100, close=110 → body=10
        # lower_wick = min(100,110) - 99 = 1 < 0.2*10=2 → wick_ok fails
        # swept_level=99.5: low(99) < 99.5 AND close(110) > 99.5 → disp_ok fires
        c = make_candle(open_=100, high=111, low=99, close=110)
        self.assertTrue(is_rejection_candle(c, 'BUY', swept_level=99.5))

    def test_sell_disp_ok_fires_when_wick_ratio_fails(self):
        # Bearish candle: open=110, close=100 → body=10
        # upper_wick = 111 - max(110,100) = 1 < 0.2*10=2 → wick_ok fails
        # swept_level=110.5: high(111) > 110.5 AND close(100) < 110.5 → disp_ok fires
        c = make_candle(open_=110, high=111, low=99, close=100)
        self.assertTrue(is_rejection_candle(c, 'SELL', swept_level=110.5))

    def test_buy_disp_ok_false_when_close_below_swept_level(self):
        # Candle dips below swept_level but closes below it too → no recovery
        # BUY wick_ok: bearish candle (close < open) → body_size < 0 → fails
        # disp_ok: low=96 < 97.5 (yes) but close=97 > 97.5 (no) → fails
        c = make_candle(open_=100, high=104, low=96, close=97)
        self.assertFalse(is_rejection_candle(c, 'BUY', swept_level=97.5))

    def test_sell_disp_ok_false_when_close_above_swept_level(self):
        # Candle pokes above swept_level but closes above it too → no reversal
        # SELL wick_ok: bullish candle → body_size < 0 → fails
        # disp_ok: high=104 > 102.5 (yes) but close=103 < 102.5 (no) → fails
        c = make_candle(open_=100, high=104, low=96, close=103)
        self.assertFalse(is_rejection_candle(c, 'SELL', swept_level=102.5))

    def test_buy_disp_ok_requires_swept_level_argument(self):
        # Without swept_level the disp_ok path cannot fire; wick ratio also fails
        c = make_candle(open_=100, high=111, low=99, close=110)
        self.assertFalse(is_rejection_candle(c, 'BUY'))   # no swept_level


# ─────────────────────────────────────────────────────────────────────────────
# detect_daily_bias — edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyBiasEdgeCases(unittest.TestCase):
    """Edge cases and Fix #1 regression: >= 1 rejection is now sufficient."""

    def test_empty_candle_list_returns_none(self):
        result = detect_daily_bias([], prev_high=110, prev_low=90)
        self.assertEqual(result, 'NONE')

    def test_single_following_rejection_is_sufficient_for_sell_bias(self):
        # Sweep candle (i=0): high=115 > prev_high=110, but close=112 > 110
        # → wick_ok fails (bearish body would need close < open), disp_ok fails (close > 110)
        # → sweep candle itself is NOT a rejection.
        # Candle at i=1: bearish with meaningful upper wick → IS a rejection.
        # Under old code (>= 2) this would return NONE; new code (>= 1) → SELL.
        candles = [
            make_candle(100, 115, 95, 112),   # sweep only, no rejection
            make_candle(112, 113, 106, 108),  # bearish: body=4, upper_wick=1 >= 0.8 ✓
        ]
        result = detect_daily_bias(candles, prev_high=110, prev_low=90)
        self.assertEqual(result, 'SELL')

    def test_single_following_rejection_is_sufficient_for_buy_bias(self):
        # Symmetric case for BUY.
        candles = [
            make_candle(100, 105, 85, 88),   # sweep: low=85 < prev_low=90, close=88 < 90 (no self-reject)
            make_candle(88,  92, 87, 91),    # bullish: body=3, lower_wick=1 >= 0.6 ✓
        ]
        result = detect_daily_bias(candles, prev_high=120, prev_low=90)
        self.assertEqual(result, 'BUY')

    def test_sweep_candle_self_rejects_sell_via_disp_ok(self):
        # Candle sweeps above prev_high (high=115 > 110) and closes back below it
        # (close=108 < 110) → disp_ok fires on the sweep candle itself → SELL immediately.
        candles = [make_candle(100, 115, 90, 108)]
        result = detect_daily_bias(candles, prev_high=110, prev_low=80)
        self.assertEqual(result, 'SELL')

    def test_sweep_candle_self_rejects_buy_via_disp_ok(self):
        # Candle sweeps below prev_low (low=75 < 90) and closes back above it
        # (close=92 > 90) → disp_ok fires on the sweep candle itself → BUY immediately.
        candles = [make_candle(105, 115, 75, 92)]
        result = detect_daily_bias(candles, prev_high=130, prev_low=90)
        self.assertEqual(result, 'BUY')


# ─────────────────────────────────────────────────────────────────────────────
# find_4h_swing
# ─────────────────────────────────────────────────────────────────────────────

class TestFind4hSwing(unittest.TestCase):

    def test_buy_returns_lowest_low_of_closed_candles(self):
        # Last candle is still-forming and excluded by the [:-1] slice.
        candles = [
            make_candle(100, 105, 90, 103, time=0),
            make_candle(103, 108, 88, 106, time=1),  # lowest low = 88
            make_candle(106, 110, 92, 109, time=2),  # forming (excluded)
        ]
        self.assertEqual(find_4h_swing(candles, 'BUY'), 88)

    def test_sell_returns_highest_high_of_closed_candles(self):
        candles = [
            make_candle(100, 115, 90, 103, time=0),  # highest high = 115
            make_candle(103, 108, 88, 106, time=1),
            make_candle(106, 110, 92, 109, time=2),  # forming (excluded)
        ]
        self.assertEqual(find_4h_swing(candles, 'SELL'), 115)

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(find_4h_swing([], 'BUY'))

    def test_returns_none_for_unknown_direction(self):
        candles = [make_candle(100, 110, 90, 105)]
        self.assertIsNone(find_4h_swing(candles, 'SIDEWAYS'))

    def test_single_candle_uses_itself_when_no_forming_candle(self):
        # len == 1 → closed = candles (no slice), window = candles
        candles = [make_candle(100, 120, 80, 110)]
        self.assertEqual(find_4h_swing(candles, 'BUY'), 80)
        self.assertEqual(find_4h_swing(candles, 'SELL'), 120)


# ─────────────────────────────────────────────────────────────────────────────
# detect_m15_liquidity_levels
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectM15LiquidityLevels(unittest.TestCase):

    def test_swing_high_and_low_identified(self):
        candles = [
            make_candle(100, 110, 90, 105, time=0),
            make_candle(105, 120, 88, 115, time=1),  # swing_high=120, swing_low=88
            make_candle(115, 118, 93, 117, time=2),  # forming (excluded)
        ]
        result = detect_m15_liquidity_levels(candles, consumed_levels=[])
        self.assertEqual(result['swing_high'], 120)
        self.assertEqual(result['swing_low'],  88)

    def test_equal_highs_detected(self):
        # Two closed candles with identical highs.
        # ranges = [15, 14]; avg=14.5; threshold=0.05*14.5=0.725
        # |110.0 - 110.0| = 0 < 0.725 → equal_highs = 110.0
        candles = [
            make_candle(100, 110.0, 95, 108, time=0),
            make_candle(108, 110.0, 96, 109, time=1),
            make_candle(109, 111,   97, 110, time=2),  # forming
        ]
        result = detect_m15_liquidity_levels(candles, consumed_levels=[])
        self.assertIsNotNone(result['equal_highs'])
        self.assertAlmostEqual(result['equal_highs'], 110.0)

    def test_equal_lows_detected(self):
        # ranges = [20, 25]; avg=22.5; threshold=0.05*22.5=1.125
        # |90.0 - 90.0| = 0 < 1.125 → equal_lows = 90.0
        candles = [
            make_candle(100, 110, 90.0, 108, time=0),
            make_candle(108, 115, 90.0, 113, time=1),
            make_candle(113, 118, 94,   117, time=2),  # forming
        ]
        result = detect_m15_liquidity_levels(candles, consumed_levels=[])
        self.assertIsNotNone(result['equal_lows'])
        self.assertAlmostEqual(result['equal_lows'], 90.0)

    def test_consumed_swing_high_is_excluded(self):
        candles = [
            make_candle(100, 110, 90, 105, time=0),
            make_candle(105, 120, 88, 115, time=1),  # swing_high candidate = 120
            make_candle(115, 118, 93, 117, time=2),  # forming
        ]
        consumed = [{'level': 120.0, 'side': 'sell', 'timestamp': '2024-01-01T00:00:00+00:00'}]
        result = detect_m15_liquidity_levels(candles, consumed_levels=consumed)
        self.assertIsNone(result['swing_high'])
        # swing_low should still be present (different side)
        self.assertIsNotNone(result['swing_low'])

    def test_empty_candle_list_returns_all_none(self):
        result = detect_m15_liquidity_levels([], consumed_levels=[])
        self.assertIsNone(result['swing_high'])
        self.assertIsNone(result['swing_low'])
        self.assertIsNone(result['equal_highs'])
        self.assertIsNone(result['equal_lows'])


# ─────────────────────────────────────────────────────────────────────────────
# identify_order_block
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentifyOrderBlock(unittest.TestCase):

    def test_buy_ob_is_most_recent_bearish_candle(self):
        # reversed(closed) = [time=2(bullish), time=1(bearish), time=0(bullish)]
        # First bearish encountered (time=1) is the OB.
        candles = [
            make_candle(100, 105, 95, 102, time=0),  # bullish
            make_candle(102, 108, 98,  99, time=1),  # bearish ← OB
            make_candle(99,  105, 97, 103, time=2),  # bullish
            make_candle(103, 107, 100, 106, time=3), # forming (excluded)
        ]
        result = identify_order_block(candles, 'BUY')
        self.assertIsNotNone(result)
        self.assertEqual(result['ob_time'], 1)
        self.assertEqual(result['ob_high'], 108)
        self.assertEqual(result['ob_low'],   98)

    def test_sell_ob_is_most_recent_bullish_candle(self):
        # reversed(closed) = [time=2(bearish), time=1(bullish), time=0(bearish)]
        # First bullish encountered (time=1) is the OB.
        candles = [
            make_candle(100, 105, 95,  99, time=0),  # bearish
            make_candle(99,  108, 97, 106, time=1),  # bullish ← OB
            make_candle(106, 110, 100, 104, time=2), # bearish
            make_candle(104, 108, 101, 107, time=3), # forming (excluded)
        ]
        result = identify_order_block(candles, 'SELL')
        self.assertIsNotNone(result)
        self.assertEqual(result['ob_time'], 1)
        self.assertEqual(result['ob_high'], 108)
        self.assertEqual(result['ob_low'],   97)

    def test_buy_ob_returns_none_when_all_candles_bullish(self):
        candles = [
            make_candle(100, 105, 95, 103, time=0),  # bullish
            make_candle(103, 108, 100, 106, time=1), # bullish
            make_candle(106, 110, 103, 109, time=2), # forming (excluded)
        ]
        self.assertIsNone(identify_order_block(candles, 'BUY'))

    def test_sell_ob_returns_none_when_all_candles_bearish(self):
        candles = [
            make_candle(106, 110, 103, 104, time=0), # bearish
            make_candle(104, 108, 100, 101, time=1), # bearish
            make_candle(101, 105, 98,  102, time=2), # forming (excluded)
        ]
        self.assertIsNone(identify_order_block(candles, 'SELL'))

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(identify_order_block([], 'BUY'))


# ─────────────────────────────────────────────────────────────────────────────
# detect_m15_entry — SELL direction
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectM15EntrySell(unittest.TestCase):

    def test_sell_signal_detected_when_sweep_and_rejection(self):
        # Sweep candle: high=103 > level=100; bearish body=2, upper_wick=5 >= 0.4 → rejection ✓
        level   = 100.0
        sweep   = make_candle(open_=98, high=103, low=95, close=96,  time=1000)
        confirm = make_candle(open_=96, high=99,  low=94, close=95,  time=1900)
        forming = make_candle(open_=95, high=98,  low=93, close=97,  time=2800)
        liq = {'equal_highs': level, 'swing_high': None,
               'equal_lows': None,   'swing_low': None}
        result = detect_m15_entry([sweep, confirm, forming], 'SELL', liq)
        self.assertIsNotNone(result)
        self.assertEqual(result['direction'], 'SELL')
        self.assertAlmostEqual(result['swept_level'], level)
        # Entry price is the close of the confirmation (sweep itself in this case)
        self.assertAlmostEqual(result['entry_price'], 96.0)

    def test_sell_uses_swing_high_level(self):
        # Verify the swing_high liquidity level is also tried for SELL entries.
        level   = 100.0
        sweep   = make_candle(open_=98, high=103, low=95, close=96,  time=1000)
        confirm = make_candle(open_=96, high=99,  low=94, close=95,  time=1900)
        forming = make_candle(open_=95, high=98,  low=93, close=97,  time=2800)
        liq = {'equal_highs': None, 'swing_high': level,
               'equal_lows': None,  'swing_low':  None}
        result = detect_m15_entry([sweep, confirm, forming], 'SELL', liq)
        self.assertIsNotNone(result)
        self.assertEqual(result['direction'], 'SELL')

    def test_sell_no_signal_when_no_rejection_after_sweep(self):
        # Sweep: high=103 > 100, but close=102 > 100 (close above level, no disp_ok)
        # and open=98 < close=102 (bullish) → wick_ok SELL fails (body negative)
        level   = 100.0
        sweep   = make_candle(open_=98, high=103, low=95, close=102, time=1000)
        confirm = make_candle(open_=102, high=108, low=100, close=107, time=1900)
        forming = make_candle(open_=107, high=110, low=105, close=109, time=2800)
        liq = {'equal_highs': level, 'swing_high': None,
               'equal_lows': None,   'swing_low': None}
        result = detect_m15_entry([sweep, confirm, forming], 'SELL', liq)
        self.assertIsNone(result)

    def test_sell_no_signal_when_daily_bias_is_none(self):
        level = 100.0
        sweep = make_candle(open_=98, high=103, low=95, close=96, time=1000)
        liq   = {'equal_highs': level, 'swing_high': None,
                 'equal_lows': None,   'swing_low': None}
        result = detect_m15_entry([sweep], 'NONE', liq)
        self.assertIsNone(result)
