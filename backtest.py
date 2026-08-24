#!/usr/bin/env python3
"""
backtest.py
~~~~~~~~~~~
Standalone backtester for the ICT/liquidity-sweep trading strategy.

Fetches historical candle data from the Deriv API, replays it through
the same signal_engine functions the live bot uses, and generates a
performance report with equity curve.

Usage:
    python3 backtest.py

No arguments needed — uses the same config.py and credentials.py
as the live bot.  Does NOT place any real trades.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import credentials
import core.signal_engine as se
from core.trade_manager import calculate_stake

# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

STARTING_BALANCE = 10_000.0
FETCH_M5_COUNT   = 5000
FETCH_H4_COUNT   = 1000
FETCH_D_COUNT    = 365
FETCH_MONTHS     = 3

# ─────────────────────────────────────────────────────────────────────────────
# Deriv Data Fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_candles(ws, symbol: str, granularity: int, count: int,
                        end_epoch: Optional[int] = None) -> list:
    req = {
        'ticks_history': symbol,
        'granularity': granularity,
        'style': 'candles',
        'count': count,
        'end': end_epoch or 'latest',
        'req_id': 9999,
    }
    await ws.send(json.dumps(req))
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get('req_id') == 9999:
            if 'error' in msg:
                print(f"  ⚠ API error for {symbol} g={granularity}: "
                      f"{msg['error'].get('message')}")
                return []
            candles = []
            for c in msg.get('candles', []):
                candles.append({
                    'time': int(c['epoch']),
                    'open': float(c['open']),
                    'high': float(c['high']),
                    'low': float(c['low']),
                    'close': float(c['close']),
                })
            return candles
    return []


async def fetch_all_m5(ws, symbol: str, months: int = FETCH_MONTHS) -> list:
    all_candles = []
    end_epoch = int(time.time())
    for _ in range(months * 2):
        chunk = await fetch_candles(ws, symbol, 300, FETCH_M5_COUNT, end_epoch)
        if not chunk:
            break
        all_candles = chunk + all_candles
        end_epoch = chunk[0]['time'] - 1
        await asyncio.sleep(0.5)
    seen = set()
    unique = []
    for c in all_candles:
        if c['time'] not in seen:
            seen.add(c['time'])
            unique.append(c)
    unique.sort(key=lambda c: c['time'])
    return unique


async def fetch_symbol_data(ws, symbol: str) -> dict:
    print(f"  Fetching {symbol} daily candles...")
    d_candles = await fetch_candles(ws, symbol, 86400, FETCH_D_COUNT)
    await asyncio.sleep(0.5)
    print(f"  Fetching {symbol} H4 candles...")
    h4_candles = await fetch_candles(ws, symbol, 14400, FETCH_H4_COUNT)
    await asyncio.sleep(0.5)
    print(f"  Fetching {symbol} M5 candles ({FETCH_MONTHS} months)...")
    m5_candles = await fetch_all_m5(ws, symbol)
    print(f"  ✓ {symbol}: D={len(d_candles)} H4={len(h4_candles)} M5={len(m5_candles)}")
    return {'D': d_candles, 'H4': h4_candles, 'M5': m5_candles}


# ─────────────────────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────────────────────

class BacktestResult:
    def __init__(self, symbol, direction, entry_price, sl, tp, stake,
                 multiplier, entry_time):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.sl = sl
        self.tp = tp
        self.stake = stake
        self.multiplier = multiplier
        self.entry_time = entry_time
        self.exit_price = None
        self.exit_time = None
        self.outcome = None
        self.pnl = 0.0


def _epoch_to_day(epoch: int) -> str:
    """Convert epoch to YYYY-MM-DD string for daily boundary tracking."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%d')


def run_backtest_for_symbol(symbol: str, data: dict) -> list:
    """Replay historical candles through the signal engine state machine."""
    d_all = data['D']
    h4_all = data['H4']
    m5_all = data['M5']

    if len(d_all) < 5 or len(h4_all) < 20 or len(m5_all) < 50:
        print(f"  ⚠ {symbol}: Not enough data")
        return []

    trades = []
    balance = STARTING_BALANCE
    multiplier = config.SYMBOL_MULTIPLIERS.get(symbol, 200)
    consumed_levels = []

    # State machine
    state = 'IDLE'
    daily_bias = 'NONE'
    h4_sweep_candle = None
    h4_liquidity_target = None
    cooldown_until = 0
    current_day = ''
    last_entry_day = ''  # prevent multiple entries per day

    # Candle buffers
    d_buffer = deque(maxlen=config.BUFFER['D'])
    h4_buffer = deque(maxlen=config.BUFFER['H4'])
    m5_buffer = deque(maxlen=config.BUFFER['M5'])

    # Pre-fill D and H4 using candles BEFORE the M5 window
    m5_start_time = m5_all[0]['time']
    for c in d_all:
        if c['time'] < m5_start_time:
            d_buffer.append(c)
    for c in h4_all:
        if c['time'] < m5_start_time:
            h4_buffer.append(c)

    in_trade = False
    current_trade = None
    d_idx = 0
    h4_idx = 0

    # Advance D/H4 indices past pre-filled candles
    for ci, c in enumerate(d_all):
        if c['time'] >= m5_start_time:
            d_idx = ci
            break
    for ci, c in enumerate(h4_all):
        if c['time'] >= m5_start_time:
            h4_idx = ci
            break

    for i, m5_candle in enumerate(m5_all):
        ct = m5_candle['time']
        today = _epoch_to_day(ct)

        # ── Daily reset ──
        if today != current_day:
            current_day = today
            if not in_trade:
                state = 'IDLE'
                daily_bias = 'NONE'
                h4_sweep_candle = None
                h4_liquidity_target = None
                consumed_levels = []

        # Add D/H4 candles that closed at or before this M5 candle
        while d_idx < len(d_all) and d_all[d_idx]['time'] <= ct:
            d_buffer.append(d_all[d_idx])
            d_idx += 1
        while h4_idx < len(h4_all) and h4_all[h4_idx]['time'] <= ct:
            h4_buffer.append(h4_all[h4_idx])
            h4_idx += 1
        m5_buffer.append(m5_candle)

        # ── Check active trade exit ──
        if in_trade and current_trade:
            if ct > current_trade.entry_time:
                hit = False
                if current_trade.direction == 'BUY':
                    if m5_candle['low'] <= current_trade.sl:
                        current_trade.outcome = 'LOSS'
                        current_trade.exit_price = current_trade.sl
                        hit = True
                    elif m5_candle['high'] >= current_trade.tp:
                        current_trade.outcome = 'WIN'
                        current_trade.exit_price = current_trade.tp
                        hit = True
                else:
                    if m5_candle['high'] >= current_trade.sl:
                        current_trade.outcome = 'LOSS'
                        current_trade.exit_price = current_trade.sl
                        hit = True
                    elif m5_candle['low'] <= current_trade.tp:
                        current_trade.outcome = 'WIN'
                        current_trade.exit_price = current_trade.tp
                        hit = True

                if hit:
                    current_trade.exit_time = ct
                    price_change = current_trade.exit_price - current_trade.entry_price
                    if current_trade.direction == 'SELL':
                        price_change = -price_change
                    current_trade.pnl = round(
                        current_trade.stake * current_trade.multiplier *
                        (price_change / current_trade.entry_price), 2)
                    balance += current_trade.pnl
                    trades.append(current_trade)
                    in_trade = False
                    current_trade = None
                    state = 'COOLDOWN'
                    cooldown_until = ct + config.COOLDOWN_MINUTES * 60
                    # Fall through so the COOLDOWN block processes this same candle
                else:
                    continue  # trade still active
            else:
                continue  # entry candle — skip state machine

        # ── Cooldown ──
        if state == 'COOLDOWN':
            if ct >= cooldown_until:
                # Always clear the stale 4H sweep so the next phase
                # requires a fresh sweep confirmation, not the old value.
                h4_sweep_candle      = None
                h4_liquidity_target  = None
                if daily_bias in ('BUY', 'SELL'):
                    state = 'BIAS_SET'   # re-enter H4 scan; fresh sweep needed
                else:
                    state = 'IDLE'
            else:
                continue

        # ── State machine ──
        d_closed = list(d_buffer)[:-1] if len(d_buffer) > 1 else []

        if state == 'IDLE':
            if len(d_closed) >= 3:
                lookback = d_closed[-config.DAILY_BIAS_LOOKBACK - 1:-1]
                prev_high = max(c['high'] for c in lookback)
                prev_low = min(c['low'] for c in lookback)
                bias = se.detect_daily_bias(
                    d_closed, prev_high, prev_low)
                if bias in ('BUY', 'SELL'):
                    daily_bias = bias
                    state = 'BIAS_SET'

        elif state == 'BIAS_SET':
            h4_closed = list(h4_buffer)[:-1] if len(h4_buffer) > 1 else []
            if h4_closed:
                swing = se.find_4h_swing(h4_closed, daily_bias,
                                          config.SWING_LOOKBACK_4H)
                if swing:
                    if se.confirm_4h_sweep(h4_closed[-1], swing, daily_bias):
                        h4_sweep_candle = h4_closed[-1]
                        h4_liquidity_target = se.find_4h_liquidity_target(
                            h4_closed, daily_bias, config.SWING_LOOKBACK_4H)
                        state = 'H4_CONFIRMED'

        elif state == 'H4_CONFIRMED':
            # Only one trade per day per symbol
            if today == last_entry_day:
                continue

            m5_closed = list(m5_buffer)[:-1] if len(m5_buffer) > 1 else []
            if len(m5_closed) < 10:
                continue

            liq = se.detect_m15_liquidity_levels(
                m5_closed, consumed_levels, config.LIQUIDITY_TOLERANCE)

            h4_time = h4_sweep_candle['time'] if h4_sweep_candle else None
            entry = se.detect_m15_entry(m5_closed, daily_bias, liq,
                                         h4_sweep_time=h4_time)

            if entry and entry['direction'] == daily_bias:
                direction = entry['direction']
                entry_price = entry['entry_price']
                sl_tp = se.calculate_sl_tp(entry_price, entry['sweep_candle'],
                                            direction,
                                            h4_liquidity_target=h4_liquidity_target,
                                            rr_mode=config.RR_MODE)

                sweep_time = entry['sweep_candle'].get('time')
                poi = se.get_best_poi(m5_closed, direction, sweep_candle_time=sweep_time)
                if config.REQUIRE_ORDER_BLOCK:
                    if (not poi or
                            not (poi['poi_low'] <= entry_price <= poi['poi_high'])):
                        continue

                # Use STARTING balance for stake (fixed fractional, not compounding)
                stake = calculate_stake(entry_price, sl_tp['sl'],
                                        multiplier, STARTING_BALANCE)

                if balance <= 0:
                    continue

                current_trade = BacktestResult(
                    symbol, direction, entry_price,
                    sl_tp['sl'], sl_tp['tp'], stake,
                    multiplier, ct)
                in_trade = True
                state = 'IN_TRADE'
                last_entry_day = today

                # Mark the swept level as consumed
                swept = entry.get('swept_level')
                if swept:
                    consumed_levels.append({
                        'level': swept,
                        'side': direction.lower(),
                    })

    if in_trade and current_trade and current_trade.outcome is None:
        current_trade.outcome = 'OPEN'
        trades.append(current_trade)

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Report Generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(all_trades: dict, starting_balance: float):
    total_trades = []
    for sym_trades in all_trades.values():
        total_trades.extend(sym_trades)

    closed = [t for t in total_trades if t.outcome in ('WIN', 'LOSS')]
    wins = [t for t in closed if t.outcome == 'WIN']
    losses = [t for t in closed if t.outcome == 'LOSS']

    print("\n" + "=" * 70)
    print("  📊  BACKTEST REPORT — ICT Liquidity Sweep Strategy")
    print("=" * 70)
    print(f"  Starting Balance  : ${starting_balance:,.2f}")
    print(f"  Symbols Tested    : {', '.join(all_trades.keys())}")
    print(f"  RR Mode           : {config.RR_MODE}:1")
    print(f"  Risk Per Trade    : {config.RISK_PER_TRADE * 100:.1f}%")
    print(f"  Period            : ~{FETCH_MONTHS} months of M5 data")
    print()

    if not closed:
        print("  ⚠ No trades were generated during the backtest period.")
        print("    This could mean:")
        print("    - Strategy filters are too strict")
        print("    - Not enough historical data")
        print("    - Order block requirement is filtering all signals")
        print("=" * 70)
        return

    # ── Summary stats ──
    win_rate = len(wins) / len(closed) * 100
    total_pnl = sum(t.pnl for t in closed)
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    final_balance = starting_balance + total_pnl

    avg_win = (gross_profit / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0

    # Max drawdown
    equity = starting_balance
    peak = equity
    max_dd = 0
    max_dd_pct = 0
    equity_curve = [starting_balance]
    for t in closed:
        equity += t.pnl
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    # Streaks
    max_win_streak = max_loss_streak = 0
    cur_w = cur_l = 0
    for t in closed:
        if t.outcome == 'WIN':
            cur_w += 1; cur_l = 0
            max_win_streak = max(max_win_streak, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_loss_streak = max(max_loss_streak, cur_l)

    # Date range
    if closed:
        first_dt = datetime.fromtimestamp(closed[0].entry_time, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(closed[-1].entry_time, tz=timezone.utc)
        days_span = (last_dt - first_dt).days or 1
        trades_per_week = len(closed) / (days_span / 7)
    else:
        trades_per_week = 0

    print("  ── Performance Summary ─────────────────────────────")
    print(f"  Total Trades      : {len(closed)}")
    print(f"  Wins              : {len(wins)}")
    print(f"  Losses            : {len(losses)}")
    print(f"  Win Rate          : {win_rate:.1f}%")
    print(f"  Trades/Week       : {trades_per_week:.1f}")
    print()
    print(f"  Total P&L         : ${total_pnl:+,.2f}")
    print(f"  Final Balance     : ${final_balance:,.2f}")
    print(f"  Return            : {(total_pnl / starting_balance) * 100:+.1f}%")
    print()
    print(f"  Gross Profit      : ${gross_profit:,.2f}")
    print(f"  Gross Loss        : ${gross_loss:,.2f}")
    print(f"  Profit Factor     : {profit_factor:.2f}")
    print()
    print(f"  Avg Win           : ${avg_win:,.2f}")
    print(f"  Avg Loss          : ${avg_loss:,.2f}")
    print(f"  Avg Win/Loss Ratio: {(avg_win / avg_loss) if avg_loss > 0 else 0:.2f}")
    print()
    print(f"  Max Drawdown      : ${max_dd:,.2f} ({max_dd_pct:.1f}%)")
    print(f"  Max Win Streak    : {max_win_streak}")
    print(f"  Max Loss Streak   : {max_loss_streak}")
    print()

    # ── win rate needed to breakeven at this RR ──
    be_wr = 100 / (1 + config.RR_MODE)
    print(f"  Breakeven Win Rate: {be_wr:.1f}% (at {config.RR_MODE}:1 RR)")
    if win_rate >= be_wr:
        print(f"  → Your {win_rate:.1f}% exceeds breakeven ✅")
    else:
        print(f"  → Your {win_rate:.1f}% is BELOW breakeven ❌")
    print()

    # ── Per-symbol breakdown ──
    print("  ── Per-Symbol Breakdown ────────────────────────────")
    print(f"  {'Symbol':<10} {'Trades':>7} {'Wins':>6} {'Losses':>7} "
          f"{'WinRate':>8} {'P&L':>10}")
    print("  " + "-" * 52)
    for sym, sym_trades in all_trades.items():
        sc = [t for t in sym_trades if t.outcome in ('WIN', 'LOSS')]
        sw = [t for t in sc if t.outcome == 'WIN']
        sl = [t for t in sc if t.outcome == 'LOSS']
        wr = len(sw) / len(sc) * 100 if sc else 0
        sp = sum(t.pnl for t in sc)
        print(f"  {sym:<10} {len(sc):>7} {len(sw):>6} {len(sl):>7} "
              f"{wr:>7.1f}% ${sp:>9,.2f}")
    print()

    # ── Trade log (first 50 + last 10) ──
    print("  ── Trade Log ───────────────────────────────────────")
    print(f"  {'#':>3} {'Date':<12} {'Symbol':<10} {'Dir':<5} "
          f"{'Entry':>10} {'SL':>10} {'TP':>10} {'Out':>5} {'P&L':>10}")
    print("  " + "-" * 80)

    closed_index = {id(t): i for i, t in enumerate(closed)}

    display = closed[:50]
    if len(closed) > 60:
        display.append(None)  # separator
        display.extend(closed[-10:])
    elif len(closed) > 50:
        display = closed

    for idx, t in enumerate(display):
        if t is None:
            print(f"  {'...':>3} {'':>12} {'... ' + str(len(closed) - 60) + ' more trades ...':^60}")
            continue
        real_idx = closed_index[id(t)] + 1
        dt = datetime.fromtimestamp(t.entry_time, tz=timezone.utc).strftime('%m/%d %H:%M')
        print(f"  {real_idx:>3} {dt:<12} {t.symbol:<10} {t.direction:<5} "
              f"{t.entry_price:>10.2f} {t.sl:>10.2f} {t.tp:>10.2f} "
              f"{t.outcome:>5} ${t.pnl:>9,.2f}")
    print()

    # ── Equity curve (ASCII) ──
    if len(equity_curve) > 2:
        print("  ── Equity Curve ────────────────────────────────────")
        _print_ascii_chart(equity_curve)

    # ── Verdict ──
    print()
    if len(closed) < 20:
        print("  ⚠️  Sample size too small (<20 trades) for reliable conclusions")
    elif profit_factor >= 1.5 and win_rate >= be_wr:
        print("  ✅ VERDICT: Strategy looks PROFITABLE")
    elif profit_factor >= 1.0:
        print("  ⚠️  VERDICT: Strategy is near BREAKEVEN — needs refinement")
    else:
        print("  ❌ VERDICT: Strategy is LOSING — do NOT trade live with real money")
    print("=" * 70)


def _print_ascii_chart(values: list, width: int = 50, height: int = 12):
    if len(values) < 2:
        return
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    step = max(1, len(values) // width)
    sampled = [values[i] for i in range(0, len(values), step)]

    for row in range(height, -1, -1):
        if row == height:
            line = f"  ${mx:>10,.0f} │"
        elif row == 0:
            line = f"  ${mn:>10,.0f} │"
        elif row == height // 2:
            line = f"  ${mn + rng / 2:>10,.0f} │"
        else:
            line = "              │"
        for v in sampled:
            level = (v - mn) / rng * height
            if level >= row:
                line += "█"
            else:
                line += " "
        print(line)
    print("              └" + "─" * len(sampled))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  🔄 ICT Strategy Backtester — Fetching Historical Data")
    print("=" * 70)

    url = f"{config.WS_ENDPOINT}?app_id={credentials.DERIV_APP_ID}"
    async with websockets.connect(url) as ws:
        auth_req = json.dumps({'authorize': credentials.DERIV_API_TOKEN, 'req_id': 9999})
        await ws.send(auth_req)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get('req_id') == 9999:
                if 'error' in msg:
                    print(f"❌ Auth failed: {msg['error'].get('message')}")
                    return
                print(f"  ✓ Authorized as {msg['authorize'].get('loginid')}")
                break

        all_data = {}
        for symbol in config.SYMBOLS:
            print(f"\n  📥 {symbol}:")
            all_data[symbol] = await fetch_symbol_data(ws, symbol)

    print("\n" + "=" * 70)
    print("  🔍 Running Backtest...")
    print("=" * 70)

    all_trades = {}
    for symbol in config.SYMBOLS:
        print(f"  Simulating {symbol}...")
        trades = run_backtest_for_symbol(symbol, all_data[symbol])
        all_trades[symbol] = trades
        closed = [t for t in trades if t.outcome in ('WIN', 'LOSS')]
        print(f"  ✓ {symbol}: {len(closed)} trades")

    generate_report(all_trades, STARTING_BALANCE)


if __name__ == '__main__':
    asyncio.run(main())
