"""
signal_engine.py
~~~~~~~~~~~~~~~~
Pure-function signal detection for the multi-timeframe ICT strategy.

Strategy rules (top-down):
  1. DAILY  — detect which liquidity was swept (BSL or SSL) → sets bias
  2. 4H     — confirm a 4H liquidity sweep in the bias direction,
               identify the next 4H liquidity TARGET (SSL or BSL)
               that becomes the trade's Take-Profit zone
  3. M5     — wait for an M5 liquidity sweep (wick through swing
               high/low, close back inside) then enter from the
               nearest unmitigated Order Block in the bias direction

POI priority (all detected, highest-priority used first):
  OB  → Order Block   (most preferred — used in identify_order_block)
  FVG → Fair Value Gap (detect_fvg)
  BB  → Breaker Block  (detect_breaker_block)

All functions are synchronous and side-effect free.
"""

from __future__ import annotations

import statistics
from typing import Optional

import config


# ─────────────────────────────────────────────────────────────────────────────
# Candle helpers
# ─────────────────────────────────────────────────────────────────────────────

def _body(candle: dict) -> float:
    return abs(candle['close'] - candle['open'])


def _upper_wick(candle: dict) -> float:
    return candle['high'] - max(candle['open'], candle['close'])


def _lower_wick(candle: dict) -> float:
    return min(candle['open'], candle['close']) - candle['low']


def _range(candle: dict) -> float:
    return candle['high'] - candle['low']


def _is_bearish(candle: dict) -> bool:
    return candle['close'] < candle['open']


def _is_bullish(candle: dict) -> bool:
    return candle['close'] > candle['open']


# ─────────────────────────────────────────────────────────────────────────────
# Rejection / sweep candle
# ─────────────────────────────────────────────────────────────────────────────

def is_rejection_candle(candle: dict, direction: str,
                         swept_level: float = None) -> bool:
    """
    Returns True if the candle shows rejection (wick through level, close back).

    Two acceptance criteria (either is sufficient):
    1. Wick ratio  : wick >= MIN_WICK_BODY_RATIO * body
    2. Displacement: candle wicked through swept_level but closed back on
                     the correct side of it.
    """
    if direction == 'BUY':
        body_size  = candle['close'] - candle['open']
        lower_wick = _lower_wick(candle)
        wick_ok    = body_size > 0 and \
                     lower_wick >= config.MIN_WICK_BODY_RATIO * body_size
        disp_ok    = (swept_level is not None and
                      candle['low'] < swept_level and
                      candle['close'] > swept_level)
        return wick_ok or disp_ok

    elif direction == 'SELL':
        body_size  = candle['open'] - candle['close']
        upper_wick = _upper_wick(candle)
        wick_ok    = body_size > 0 and \
                     upper_wick >= config.MIN_WICK_BODY_RATIO * body_size
        disp_ok    = (swept_level is not None and
                      candle['high'] > swept_level and
                      candle['close'] < swept_level)
        return wick_ok or disp_ok

    return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Daily bias
# ─────────────────────────────────────────────────────────────────────────────

def detect_daily_bias(candles: list, prev_high: float, prev_low: float,
                      _debug_log=None) -> str:
    """
    Determine daily directional bias.

    Scans the last DAILY_BIAS_LOOKBACK daily candles (most-recent first).
    A valid sweep requires:
      - Candle wicked through prev_high (BSL) or prev_low (SSL)
      - That same candle OR the next candle shows rejection back through the level

    BSL swept + bearish rejection → SELL bias
    SSL swept + bullish rejection → BUY  bias

    Returns 'BUY', 'SELL', or 'NONE'.
    """
    if len(candles) < 1:
        if _debug_log:
            _debug_log('detect_daily_bias: no candles provided')
        return 'NONE'

    closed = candles[-config.DAILY_BIAS_LOOKBACK:]

    for i in range(len(closed) - 1, -1, -1):
        sweep_candle = closed[i]

        # ── BSL sweep → SELL bias ─────────────────────────
        if sweep_candle['high'] > prev_high:
            rejection_window = [sweep_candle] + list(closed[i + 1: i + 4])
            bearish_rejections = [
                c for c in rejection_window
                if is_rejection_candle(c, 'SELL', swept_level=prev_high)
            ]
            if _debug_log:
                _debug_log(
                    f'detect_daily_bias: BSL sweep at candle {i} '
                    f'(high={sweep_candle["high"]:.5f} > prev_high={prev_high:.5f}) '
                    f'— {len(bearish_rejections)} bearish rejections'
                )
            if len(bearish_rejections) >= 1:
                return 'SELL'

        # ── SSL sweep → BUY bias ──────────────────────────
        if sweep_candle['low'] < prev_low:
            rejection_window = [sweep_candle] + list(closed[i + 1: i + 4])
            bullish_rejections = [
                c for c in rejection_window
                if is_rejection_candle(c, 'BUY', swept_level=prev_low)
            ]
            if _debug_log:
                _debug_log(
                    f'detect_daily_bias: SSL sweep at candle {i} '
                    f'(low={sweep_candle["low"]:.5f} < prev_low={prev_low:.5f}) '
                    f'— {len(bullish_rejections)} bullish rejections'
                )
            if len(bullish_rejections) >= 1:
                return 'BUY'

    if _debug_log:
        _debug_log(
            f'detect_daily_bias: NONE — scanned {len(closed)} candles. '
            f'prev_high={prev_high:.5f} prev_low={prev_low:.5f}'
        )
    return 'NONE'


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — 4H sweep confirmation + liquidity target
# ─────────────────────────────────────────────────────────────────────────────

def find_4h_swing(candles: list, direction: str,
                  lookback: int = config.SWING_LOOKBACK_4H) -> Optional[float]:
    """
    Find the most significant swing level in the last *lookback* closed 4H candles.

    SELL → highest high (BSL — price will sweep this then dump)
    BUY  → lowest low   (SSL — price will sweep this then pump)
    """
    closed = candles[:-1] if len(candles) > 1 else candles
    window = closed[-lookback:] if len(closed) >= lookback else closed

    if not window:
        return None

    if direction == 'SELL':
        return max(c['high'] for c in window)
    elif direction == 'BUY':
        return min(c['low'] for c in window)
    return None


def confirm_4h_sweep(candle: dict, swing_level: float, direction: str) -> bool:
    """
    Confirm that *candle* swept through *swing_level* and closed back on the
    correct side — matching the manual rule (wick through, close back inside).

    SELL : wick above swing_level AND close below swing_level (BSL swept)
    BUY  : wick below swing_level AND close above swing_level (SSL swept)
    """
    if direction == 'SELL':
        return candle['high'] > swing_level and candle['close'] < swing_level
    elif direction == 'BUY':
        return candle['low'] < swing_level and candle['close'] > swing_level
    return False


def find_4h_liquidity_target(candles: list, direction: str,
                              lookback: int = config.SWING_LOOKBACK_4H) -> Optional[float]:
    """
    Find the NEXT liquidity target on the 4H chart — this becomes the TP level.

    After a 4H BSL is swept (SELL bias), the next target is the nearest SSL
    (lowest swing low) below current price.
    After a 4H SSL is swept (BUY bias), the next target is the nearest BSL
    (highest swing high) above current price.

    Returns the price level or None.
    """
    closed = candles[:-1] if len(candles) > 1 else candles
    window = closed[-lookback:] if len(closed) >= lookback else closed

    if not window:
        return None

    if direction == 'SELL':
        # Target: the lowest swing low (SSL) below current price
        return min(c['low'] for c in window)
    elif direction == 'BUY':
        # Target: the highest swing high (BSL) above current price
        return max(c['high'] for c in window)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2b — M15/M5 liquidity level detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_m15_liquidity_levels(candles: list, consumed_levels: list,
                                 tolerance: float = config.LIQUIDITY_TOLERANCE) -> dict:
    """
    Scan the last SWING_LOOKBACK_M15 closed M5 candles and identify liquidity.

    Equal highs : two candles where abs(high1 - high2) < tolerance * avg_range
    Equal lows  : two candles where abs(low1  - low2)  < tolerance * avg_range
    Swing high  : highest high in the window
    Swing low   : lowest low in the window

    Returns
    -------
    {
        'equal_highs' : float | None,
        'equal_lows'  : float | None,
        'swing_high'  : float | None,
        'swing_low'   : float | None,
    }
    """
    closed = candles[:-1] if len(candles) > 1 else candles
    window = (closed[-config.SWING_LOOKBACK_M15:]
              if len(closed) >= config.SWING_LOOKBACK_M15 else closed)

    result = {
        'equal_highs': None,
        'equal_lows':  None,
        'swing_high':  None,
        'swing_low':   None,
    }

    if not window:
        return result

    ranges    = [_range(c) for c in window]
    avg_range = statistics.mean(ranges) if ranges else 1.0
    threshold = tolerance * avg_range

    def _is_consumed(level: float, side: str) -> bool:
        for entry in consumed_levels:
            if entry.get('side', '').lower() != side.lower():
                continue
            if abs(entry.get('level', float('inf')) - level) <= threshold:
                return True
        return False

    n = len(window)

    # ── Equal highs (most-recent pair wins) ──────────────
    for i in range(n - 1, 0, -1):
        for j in range(i - 1, max(i - 10, -1), -1):
            if abs(window[i]['high'] - window[j]['high']) < threshold:
                level = (window[i]['high'] + window[j]['high']) / 2.0
                if not _is_consumed(level, 'sell'):
                    result['equal_highs'] = level
                break
        if result['equal_highs'] is not None:
            break

    # ── Equal lows (most-recent pair wins) ───────────────
    for i in range(n - 1, 0, -1):
        for j in range(i - 1, max(i - 10, -1), -1):
            if abs(window[i]['low'] - window[j]['low']) < threshold:
                level = (window[i]['low'] + window[j]['low']) / 2.0
                if not _is_consumed(level, 'buy'):
                    result['equal_lows'] = level
                break
        if result['equal_lows'] is not None:
            break

    # ── Swing high / low ─────────────────────────────────
    swing_high_val = max(c['high'] for c in window)
    swing_low_val  = min(c['low']  for c in window)

    if not _is_consumed(swing_high_val, 'sell'):
        result['swing_high'] = swing_high_val
    if not _is_consumed(swing_low_val, 'buy'):
        result['swing_low'] = swing_low_val

    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — M5 entry trigger
# ─────────────────────────────────────────────────────────────────────────────

def detect_m15_entry(m15_candles: list, daily_bias: str,
                     liquidity_levels: dict,
                     h4_sweep_time: Optional[int] = None) -> Optional[dict]:
    """
    Identify a valid M5 entry signal aligned with *daily_bias*.

    Entry logic (matches your manual strategy):
      SELL: candle wicks ABOVE a swing high / equal high → closes BELOW it
      BUY : candle wicks BELOW a swing low  / equal low  → closes ABOVE it

    The confirmation is built into the sweep detection itself:
      - sweep_candle['high'] > level AND sweep_candle['close'] < level  → SELL
      - sweep_candle['low']  < level AND sweep_candle['close'] > level  → BUY

    If the sweep candle doesn't close back (only wicked), we also check the
    NEXT candle for a rejection close (two-candle sweep pattern).

    Parameters
    ----------
    h4_sweep_time : M5 candles at or before this timestamp are ignored
                    (ensures entry is after 4H confirmation).

    Returns
    -------
    {
        'direction', 'entry_price', 'sweep_candle',
        'confirmation_candle', 'swept_level'
    } or None
    """
    closed = m15_candles[:-1] if len(m15_candles) > 1 else m15_candles
    if not closed:
        return None

    if h4_sweep_time is not None:
        closed = [c for c in closed if c['time'] > h4_sweep_time]

    closed = closed[-config.M15_ENTRY_WINDOW:]

    if not closed:
        return None

    direction = daily_bias

    if direction == 'BUY':
        levels = [v for v in [
            liquidity_levels.get('equal_lows'),
            liquidity_levels.get('swing_low'),
        ] if v is not None]
    elif direction == 'SELL':
        levels = [v for v in [
            liquidity_levels.get('equal_highs'),
            liquidity_levels.get('swing_high'),
        ] if v is not None]
    else:
        return None

    if not levels:
        return None

    for i in range(len(closed) - 1):
        sweep_candle   = closed[i]
        confirm_candle = closed[i + 1]

        for level in levels:
            if direction == 'SELL':
                # Wick above level
                if sweep_candle['high'] <= level:
                    continue

                # ── Single-candle sweep: wick above, close below ──
                if sweep_candle['close'] < level:
                    return {
                        'direction':           'SELL',
                        'entry_price':         sweep_candle['close'],
                        'sweep_candle':        sweep_candle,
                        'confirmation_candle': sweep_candle,
                        'swept_level':         level,
                    }

                # ── Two-candle sweep: next candle closes below level ──
                if (is_rejection_candle(confirm_candle, 'SELL', swept_level=level)
                        and confirm_candle['close'] < level):
                    return {
                        'direction':           'SELL',
                        'entry_price':         confirm_candle['close'],
                        'sweep_candle':        sweep_candle,
                        'confirmation_candle': confirm_candle,
                        'swept_level':         level,
                    }

            elif direction == 'BUY':
                # Wick below level
                if sweep_candle['low'] >= level:
                    continue

                # ── Single-candle sweep: wick below, close above ──
                if sweep_candle['close'] > level:
                    return {
                        'direction':           'BUY',
                        'entry_price':         sweep_candle['close'],
                        'sweep_candle':        sweep_candle,
                        'confirmation_candle': sweep_candle,
                        'swept_level':         level,
                    }

                # ── Two-candle sweep: next candle closes above level ──
                if (is_rejection_candle(confirm_candle, 'BUY', swept_level=level)
                        and confirm_candle['close'] > level):
                    return {
                        'direction':           'BUY',
                        'entry_price':         confirm_candle['close'],
                        'sweep_candle':        sweep_candle,
                        'confirmation_candle': confirm_candle,
                        'swept_level':         level,
                    }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# POI Detection — Order Block (most preferred)
# ─────────────────────────────────────────────────────────────────────────────

def identify_order_block(m5_candles: list, direction: str,
                          sweep_candle_time: Optional[int] = None) -> Optional[dict]:
    """
    Find the most recent UNMITIGATED Order Block before the sweep candle.

    ICT definition used here:
      SELL OB : the last BULLISH candle (close > open) immediately before
                the impulsive move UP that created the liquidity.
                Price returns to this OB from below after the BSL sweep.

      BUY  OB : the last BEARISH candle (close < open) immediately before
                the impulsive move DOWN that created the liquidity.
                Price returns to this OB from above after the SSL sweep.

    Fix from previous version:
      Old code looked for the opposite candle colour — that was wrong.
      Correct ICT logic: the OB is the LAST candle of the OPPOSITE colour
      to the bias direction (SELL bias → last bullish candle; BUY bias →
      last bearish candle), sitting at the BASE of the swing that created
      the liquidity pool being swept.

    'Unmitigated' means price has not yet traded back through the OB body
    after it was formed. We check this by ensuring no subsequent candle
    closed inside the OB zone before the sweep candle.

    Parameters
    ----------
    sweep_candle_time : only look at candles BEFORE this timestamp.

    Returns
    -------
    {'ob_high', 'ob_low', 'ob_time'} or None
    """
    closed = m5_candles[:-1] if len(m5_candles) > 1 else m5_candles
    if not closed:
        return None

    # Only consider candles before the sweep
    if sweep_candle_time is not None:
        closed = [c for c in closed if c['time'] < sweep_candle_time]

    if not closed:
        return None

    # Scan backwards — most recent qualifying OB wins
    for idx in range(len(closed) - 1, -1, -1):
        candle = closed[idx]

        if direction == 'SELL' and _is_bullish(candle):
            ob_high = candle['high']
            ob_low  = candle['low']

            # Check it hasn't been mitigated by price returning to the OB body
            # after the impulse. Skip closed[idx+1] (the impulse candle itself —
            # its close inside the OB is expected, not mitigation).
            mitigated = any(
                c['close'] >= ob_low and c['close'] <= ob_high
                for c in closed[idx + 2:]
            )
            if not mitigated:
                return {
                    'ob_high': ob_high,
                    'ob_low':  ob_low,
                    'ob_time': candle['time'],
                }

        elif direction == 'BUY' and _is_bearish(candle):
            ob_high = candle['high']
            ob_low  = candle['low']

            # Check it hasn't been mitigated by price returning to the OB body
            # after the impulse. Skip closed[idx+1] (the impulse candle itself).
            mitigated = any(
                c['close'] >= ob_low and c['close'] <= ob_high
                for c in closed[idx + 2:]
            )
            if not mitigated:
                return {
                    'ob_high': ob_high,
                    'ob_low':  ob_low,
                    'ob_time': candle['time'],
                }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# POI Detection — Fair Value Gap (FVG)
# ─────────────────────────────────────────────────────────────────────────────

def detect_fvg(m5_candles: list, direction: str,
               sweep_candle_time: Optional[int] = None) -> Optional[dict]:
    """
    Detect the most recent unmitigated Fair Value Gap (FVG / imbalance).

    An FVG is a 3-candle pattern where candle[1]'s body leaves a gap
    between candle[0]'s high/low and candle[2]'s low/high.

    SELL FVG: candle[0].high < candle[2].low  (gap below candle[1])
              — price should fill the gap on a retrace UP then sell
    BUY  FVG: candle[0].low  > candle[2].high (gap above candle[1])
              — price should fill the gap on a retrace DOWN then buy

    Returns
    -------
    {'fvg_high', 'fvg_low', 'fvg_time'} or None
    """
    closed = m5_candles[:-1] if len(m5_candles) > 1 else m5_candles
    if len(closed) < 3:
        return None

    if sweep_candle_time is not None:
        closed = [c for c in closed if c['time'] < sweep_candle_time]

    if len(closed) < 3:
        return None

    for idx in range(len(closed) - 3, -1, -1):
        c0, c1, c2 = closed[idx], closed[idx + 1], closed[idx + 2]

        if direction == 'SELL':
            # ICT Bearish FVG: a downward gap where c0's low is above c2's high.
            # Three-candle sequence — c0 (oldest) → c1 (impulse) → c2 (newest).
            # Price fell so aggressively that c2's high never reached c0's low,
            # leaving an unfilled imbalance (sell-side FVG) between c2['high']
            # and c0['low'].  Condition: c0['low'] > c2['high'].
            if c0['low'] > c2['high']:
                fvg_low  = c2['high']
                fvg_high = c0['low']
                # Check not already mitigated (price filled the gap)
                mitigated = any(
                    c['low'] <= fvg_high and c['high'] >= fvg_low
                    for c in closed[idx + 3:]
                )
                if not mitigated:
                    return {
                        'fvg_high': fvg_high,
                        'fvg_low':  fvg_low,
                        'fvg_time': c1['time'],
                    }

        elif direction == 'BUY':
            # ICT Bullish FVG: an upward gap where c2's low is above c0's high.
            # Price rose so aggressively that c2's low never reached c0's high,
            # leaving an unfilled imbalance (buy-side FVG) between c0['high']
            # and c2['low'].  Condition: c2['low'] > c0['high'].
            if c2['low'] > c0['high']:
                fvg_low  = c0['high']
                fvg_high = c2['low']
                mitigated = any(
                    c['low'] <= fvg_high and c['high'] >= fvg_low
                    for c in closed[idx + 3:]
                )
                if not mitigated:
                    return {
                        'fvg_high': fvg_high,
                        'fvg_low':  fvg_low,
                        'fvg_time': c1['time'],
                    }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# POI Detection — Breaker Block
# ─────────────────────────────────────────────────────────────────────────────

def detect_breaker_block(m5_candles: list, direction: str,
                          sweep_candle_time: Optional[int] = None) -> Optional[dict]:
    """
    Detect the most recent Breaker Block.

    A Breaker Block is a FAILED Order Block — an OB that WAS mitigated
    (price traded through it), which then flips polarity and becomes
    support/resistance from the other side.

    SELL Breaker: a former bullish OB that price broke through to the downside.
                  On a retrace UP it now acts as resistance.
    BUY  Breaker: a former bearish OB that price broke through to the upside.
                  On a retrace DOWN it now acts as support.

    Returns
    -------
    {'bb_high', 'bb_low', 'bb_time'} or None
    """
    closed = m5_candles[:-1] if len(m5_candles) > 1 else m5_candles
    if len(closed) < 5:
        return None

    if sweep_candle_time is not None:
        closed = [c for c in closed if c['time'] < sweep_candle_time]

    if len(closed) < 5:
        return None

    for idx in range(len(closed) - 2, 1, -1):
        candle = closed[idx]

        if direction == 'SELL':
            # Look for a former bullish candle that was later broken to downside
            if _is_bullish(candle):
                ob_high = candle['high']
                ob_low  = candle['low']
                # Check if a subsequent candle broke BELOW this OB's low
                broken = any(c['close'] < ob_low for c in closed[idx + 1:])
                if broken:
                    return {
                        'bb_high': ob_high,
                        'bb_low':  ob_low,
                        'bb_time': candle['time'],
                    }

        elif direction == 'BUY':
            # Look for a former bearish candle that was later broken to upside
            if _is_bearish(candle):
                ob_high = candle['high']
                ob_low  = candle['low']
                # Check if a subsequent candle broke ABOVE this OB's high
                broken = any(c['close'] > ob_high for c in closed[idx + 1:])
                if broken:
                    return {
                        'bb_high': ob_high,
                        'bb_low':  ob_low,
                        'bb_time': candle['time'],
                    }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# POI selector — priority: OB → FVG → BB
# ─────────────────────────────────────────────────────────────────────────────

def get_best_poi(m5_candles: list, direction: str,
                 sweep_candle_time: Optional[int] = None) -> Optional[dict]:
    """
    Return the highest-priority unmitigated POI near the sweep.

    Priority order (per your strategy):
      1. Order Block (OB)  — most preferred
      2. Fair Value Gap (FVG)
      3. Breaker Block (BB)

    Returns a unified dict with keys:
      'poi_type', 'poi_high', 'poi_low', 'poi_time'
    or None if no POI found.
    """
    # 1. Order Block
    ob = identify_order_block(m5_candles, direction, sweep_candle_time)
    if ob:
        return {
            'poi_type': 'OB',
            'poi_high': ob['ob_high'],
            'poi_low':  ob['ob_low'],
            'poi_time': ob['ob_time'],
        }

    # 2. Fair Value Gap
    fvg = detect_fvg(m5_candles, direction, sweep_candle_time)
    if fvg:
        return {
            'poi_type': 'FVG',
            'poi_high': fvg['fvg_high'],
            'poi_low':  fvg['fvg_low'],
            'poi_time': fvg['fvg_time'],
        }

    # 3. Breaker Block
    bb = detect_breaker_block(m5_candles, direction, sweep_candle_time)
    if bb:
        return {
            'poi_type': 'BB',
            'poi_high': bb['bb_high'],
            'poi_low':  bb['bb_low'],
            'poi_time': bb['bb_time'],
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# SL / TP calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sl_tp(entry_price: float, sweep_candle: dict, direction: str,
                    h4_liquidity_target: Optional[float] = None,
                    rr_mode: int = config.RR_MODE) -> dict:
    """
    Calculate Stop-Loss and Take-Profit prices.

    SL placement
    ------------
    BUY  : SL = sweep_candle_low  - 0.1 * candle_range  (below the sweep wick)
    SELL : SL = sweep_candle_high + 0.1 * candle_range  (above the sweep wick)

    TP placement  (FIXED — now liquidity-target aware)
    ------------
    If h4_liquidity_target is provided and sits in the correct direction
    relative to entry, use it as TP. This matches your manual rule of
    targeting the 4H SSL/BSL level as the trade's exit.

    Fallback: if no 4H target is available (or it's on the wrong side),
    use entry ± rr_mode * risk as before.

    Returns
    -------
    {'sl': float, 'tp': float, 'tp_source': 'H4_TARGET' | 'RR_FALLBACK'}
    """
    candle_range = _range(sweep_candle)
    buffer       = 0.1 * candle_range

    if direction == 'BUY':
        sl   = sweep_candle['low'] - buffer
        risk = abs(entry_price - sl)
        # Use 4H target if it's ABOVE entry (correct direction)
        if h4_liquidity_target is not None and h4_liquidity_target > entry_price:
            tp        = h4_liquidity_target
            tp_source = 'H4_TARGET'
        else:
            tp        = entry_price + rr_mode * risk
            tp_source = 'RR_FALLBACK'

    elif direction == 'SELL':
        sl   = sweep_candle['high'] + buffer
        risk = abs(entry_price - sl)
        # Use 4H target if it's BELOW entry (correct direction)
        if h4_liquidity_target is not None and h4_liquidity_target < entry_price:
            tp        = h4_liquidity_target
            tp_source = 'H4_TARGET'
        else:
            tp        = entry_price - rr_mode * risk
            tp_source = 'RR_FALLBACK'

    else:
        raise ValueError(f'Unknown direction: {direction!r}')

    return {'sl': sl, 'tp': tp, 'tp_source': tp_source}