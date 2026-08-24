#!/usr/bin/env python3
"""
analyse_losses.py
~~~~~~~~~~~~~~~~~
Deep-dive analysis of losing trades from the 90-day backtest.

Runs the same data fetch + backtest engine as backtest.py, then produces:
  1. Losses by UTC hour
  2. BUY vs SELL direction accuracy
  3. Loss clustering by date
  4. Daily bias direction during the 16-trade losing streak
  5. Equity curve with peak/trough annotations (ASCII + PNG if matplotlib available)
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import time
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import credentials
import core.signal_engine as se
from core.trade_manager import calculate_stake

# ── Same constants as backtest.py ────────────────────────────────────────────
STARTING_BALANCE = 10_000.0
FETCH_M5_COUNT   = 5000
FETCH_H4_COUNT   = 1000
FETCH_D_COUNT    = 365
FETCH_MONTHS     = 3


# ─────────────────────────────────────────────────────────────────────────────
# Extended BacktestResult — adds daily_bias field
# ─────────────────────────────────────────────────────────────────────────────

class TradeRecord:
    def __init__(self, symbol, direction, entry_price, sl, tp, stake,
                 multiplier, entry_time):
        self.symbol      = symbol
        self.direction   = direction   # == daily_bias at trade time
        self.entry_price = entry_price
        self.sl          = sl
        self.tp          = tp
        self.stake       = stake
        self.multiplier  = multiplier
        self.entry_time  = entry_time
        self.exit_price  = None
        self.exit_time   = None
        self.outcome     = None
        self.pnl         = 0.0

    @property
    def entry_dt(self):
        return datetime.fromtimestamp(self.entry_time, tz=timezone.utc)

    @property
    def entry_hour(self):
        return self.entry_dt.hour

    @property
    def entry_date(self):
        return self.entry_dt.strftime('%Y-%m-%d')


# ─────────────────────────────────────────────────────────────────────────────
# Deriv data fetcher (identical to backtest.py)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_candles(ws, symbol: str, granularity: int, count: int,
                        end_epoch: Optional[int] = None) -> list:
    req = {
        'ticks_history': symbol,
        'granularity':   granularity,
        'style':         'candles',
        'count':         count,
        'end':           end_epoch or 'latest',
        'req_id':        9999,
    }
    await ws.send(json.dumps(req))
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get('req_id') == 9999:
            if 'error' in msg:
                return []
            return [
                {'time': int(c['epoch']), 'open': float(c['open']),
                 'high': float(c['high']), 'low': float(c['low']),
                 'close': float(c['close'])}
                for c in msg.get('candles', [])
            ]
    return []


async def fetch_all_m5(ws, symbol: str) -> list:
    all_candles = []
    end_epoch = int(time.time())
    for _ in range(FETCH_MONTHS * 2):
        chunk = await fetch_candles(ws, symbol, 300, FETCH_M5_COUNT, end_epoch)
        if not chunk:
            break
        all_candles = chunk + all_candles
        end_epoch = chunk[0]['time'] - 1
        await asyncio.sleep(0.5)
    seen, unique = set(), []
    for c in all_candles:
        if c['time'] not in seen:
            seen.add(c['time'])
            unique.append(c)
    unique.sort(key=lambda c: c['time'])
    return unique


async def fetch_symbol_data(ws, symbol: str) -> dict:
    d   = await fetch_candles(ws, symbol, 86400, FETCH_D_COUNT)
    await asyncio.sleep(0.5)
    h4  = await fetch_candles(ws, symbol, 14400, FETCH_H4_COUNT)
    await asyncio.sleep(0.5)
    m5  = await fetch_all_m5(ws, symbol)
    print(f"    {symbol}: D={len(d)} H4={len(h4)} M5={len(m5)}")
    return {'D': d, 'H4': h4, 'M5': m5}


# ─────────────────────────────────────────────────────────────────────────────
# Backtest engine (identical logic to backtest.py, extended TradeRecord)
# ─────────────────────────────────────────────────────────────────────────────

def _epoch_to_day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%d')


def run_backtest(symbol: str, data: dict) -> list:
    d_all, h4_all, m5_all = data['D'], data['H4'], data['M5']
    if len(d_all) < 5 or len(h4_all) < 20 or len(m5_all) < 50:
        return []

    trades          = []
    balance         = STARTING_BALANCE
    multiplier      = config.SYMBOL_MULTIPLIERS.get(symbol, 200)
    consumed_levels = []

    state               = 'IDLE'
    daily_bias          = 'NONE'
    h4_sweep_candle     = None
    h4_liquidity_target = None
    cooldown_until      = 0
    current_day     = ''
    last_entry_day  = ''

    d_buffer  = deque(maxlen=config.BUFFER['D'])
    h4_buffer = deque(maxlen=config.BUFFER['H4'])
    m5_buffer = deque(maxlen=config.BUFFER['M5'])

    m5_start_time = m5_all[0]['time']
    for c in d_all:
        if c['time'] < m5_start_time:
            d_buffer.append(c)
    for c in h4_all:
        if c['time'] < m5_start_time:
            h4_buffer.append(c)

    d_idx = h4_idx = 0
    for ci, c in enumerate(d_all):
        if c['time'] >= m5_start_time:
            d_idx = ci; break
    for ci, c in enumerate(h4_all):
        if c['time'] >= m5_start_time:
            h4_idx = ci; break

    in_trade = False
    current_trade = None

    for m5_candle in m5_all:
        ct    = m5_candle['time']
        today = _epoch_to_day(ct)

        if today != current_day:
            current_day = today
            if not in_trade:
                state           = 'IDLE'
                daily_bias      = 'NONE'
                h4_sweep_candle    = None
                h4_liquidity_target = None
                consumed_levels = []

        while d_idx < len(d_all) and d_all[d_idx]['time'] <= ct:
            d_buffer.append(d_all[d_idx]); d_idx += 1
        while h4_idx < len(h4_all) and h4_all[h4_idx]['time'] <= ct:
            h4_buffer.append(h4_all[h4_idx]); h4_idx += 1
        m5_buffer.append(m5_candle)

        if in_trade and current_trade:
            if ct > current_trade.entry_time:
                hit = False
                if current_trade.direction == 'BUY':
                    if m5_candle['low'] <= current_trade.sl:
                        current_trade.outcome = 'LOSS'; current_trade.exit_price = current_trade.sl; hit = True
                    elif m5_candle['high'] >= current_trade.tp:
                        current_trade.outcome = 'WIN';  current_trade.exit_price = current_trade.tp; hit = True
                else:
                    if m5_candle['high'] >= current_trade.sl:
                        current_trade.outcome = 'LOSS'; current_trade.exit_price = current_trade.sl; hit = True
                    elif m5_candle['low'] <= current_trade.tp:
                        current_trade.outcome = 'WIN';  current_trade.exit_price = current_trade.tp; hit = True

                if hit:
                    current_trade.exit_time = ct
                    pc = current_trade.exit_price - current_trade.entry_price
                    if current_trade.direction == 'SELL':
                        pc = -pc
                    current_trade.pnl = round(
                        current_trade.stake * current_trade.multiplier *
                        (pc / current_trade.entry_price), 2)
                    balance += current_trade.pnl
                    trades.append(current_trade)
                    in_trade = False; current_trade = None
                    state = 'COOLDOWN'
                    cooldown_until = ct + config.COOLDOWN_MINUTES * 60
            continue

        if state == 'COOLDOWN':
            if ct >= cooldown_until:
                h4_sweep_candle     = None
                h4_liquidity_target = None
                if daily_bias in ('BUY', 'SELL'):
                    state = 'BIAS_SET'
                else:
                    state = 'IDLE'
            else:
                continue

        d_closed = list(d_buffer)[:-1] if len(d_buffer) > 1 else []

        if state == 'IDLE':
            if len(d_closed) >= 3:
                lookback = d_closed[-config.DAILY_BIAS_LOOKBACK - 1:-1]
                prev_high = max(c['high'] for c in lookback)
                prev_low = min(c['low'] for c in lookback)
                bias = se.detect_daily_bias(d_closed, prev_high, prev_low)
                if bias in ('BUY', 'SELL'):
                    daily_bias = bias; state = 'BIAS_SET'

        elif state == 'BIAS_SET':
            h4_closed = list(h4_buffer)[:-1] if len(h4_buffer) > 1 else []
            if h4_closed:
                swing = se.find_4h_swing(h4_closed, daily_bias, config.SWING_LOOKBACK_4H)
                if swing:
                    if se.confirm_4h_sweep(h4_closed[-1], swing, daily_bias):
                        h4_sweep_candle     = h4_closed[-1]
                        h4_liquidity_target = se.find_4h_liquidity_target(
                            h4_closed, daily_bias, config.SWING_LOOKBACK_4H)
                        state = 'H4_CONFIRMED'

        elif state == 'H4_CONFIRMED':
            if today == last_entry_day:
                continue
            m5_closed = list(m5_buffer)[:-1] if len(m5_buffer) > 1 else []
            if len(m5_closed) < 10:
                continue
            liq = se.detect_m15_liquidity_levels(m5_closed, consumed_levels, config.LIQUIDITY_TOLERANCE)
            h4_time = h4_sweep_candle['time'] if h4_sweep_candle else None
            entry = se.detect_m15_entry(m5_closed, daily_bias, liq, h4_sweep_time=h4_time)
            if entry and entry['direction'] == daily_bias:
                direction   = entry['direction']
                entry_price = entry['entry_price']
                sl_tp       = se.calculate_sl_tp(entry_price, entry['sweep_candle'], direction,
                                                  h4_liquidity_target=h4_liquidity_target,
                                                  rr_mode=config.RR_MODE)
                sweep_time  = entry['sweep_candle'].get('time')
                poi         = se.get_best_poi(m5_closed, direction, sweep_candle_time=sweep_time)
                if config.REQUIRE_ORDER_BLOCK:
                    if not poi or not (poi['poi_low'] <= entry_price <= poi['poi_high']):
                        continue
                stake = calculate_stake(entry_price, sl_tp['sl'], multiplier, STARTING_BALANCE)
                if balance <= 0:
                    continue
                current_trade = TradeRecord(
                    symbol, direction, entry_price,
                    sl_tp['sl'], sl_tp['tp'], stake, multiplier, ct)
                in_trade       = True
                state          = 'IN_TRADE'
                last_entry_day = today
                swept = entry.get('swept_level')
                if swept:
                    consumed_levels.append({'level': swept, 'side': direction.lower()})

    if in_trade and current_trade and current_trade.outcome is None:
        current_trade.outcome = 'OPEN'
        trades.append(current_trade)

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Analysis functions
# ─────────────────────────────────────────────────────────────────────────────

def bar(count: int, max_count: int, width: int = 30) -> str:
    filled = int(round(count / max_count * width)) if max_count else 0
    return '█' * filled + '░' * (width - filled)


def analyse(all_trades: dict):
    closed = []
    for sym_trades in all_trades.values():
        closed.extend(t for t in sym_trades if t.outcome in ('WIN', 'LOSS'))
    closed.sort(key=lambda t: t.entry_time)

    losses = [t for t in closed if t.outcome == 'LOSS']
    wins   = [t for t in closed if t.outcome == 'WIN']

    W = 72
    print()
    print('=' * W)
    print('  LOSS ANALYSIS — 90-Day Backtest')
    print(f'  {len(losses)} losing trades | {len(wins)} wins | {len(closed)} total')
    print('=' * W)

    # ── 1. Losses by UTC hour ─────────────────────────────────────────────
    print()
    print('  ── 1. Losses by UTC Hour ───────────────────────────────────────')
    hour_losses = defaultdict(int)
    hour_wins   = defaultdict(int)
    for t in losses: hour_losses[t.entry_hour] += 1
    for t in wins:   hour_wins[t.entry_hour]   += 1

    max_l = max(hour_losses.values()) if hour_losses else 1
    print(f'  {"Hour (UTC)":<12} {"Losses":>7}  {"Bar":<32} {"WinRate":>8}')
    print('  ' + '-' * 62)
    for h in range(24):
        l = hour_losses[h]
        w = hour_wins[h]
        total_h = l + w
        wr = f'{w/total_h*100:.0f}%' if total_h else '  —'
        marker = ' ◄ peak' if l == max_l and l > 0 else ''
        print(f'  {h:02d}:00–{h:02d}:59   {l:>5}   {bar(l, max_l)}{marker:8}  {wr:>6} wr')

    # Top 3 worst hours
    worst_hours = sorted(hour_losses, key=lambda h: hour_losses[h], reverse=True)[:3]
    print()
    print(f'  Top-3 highest-loss hours: ' +
          ', '.join(f'{h:02d}:xx UTC ({hour_losses[h]} losses)' for h in worst_hours))

    # ── 2. BUY vs SELL direction accuracy ────────────────────────────────
    print()
    print('  ── 2. BUY vs SELL Accuracy ─────────────────────────────────────')
    for direction in ('BUY', 'SELL'):
        d_trades = [t for t in closed if t.direction == direction]
        d_wins   = [t for t in d_trades if t.outcome == 'WIN']
        d_losses = [t for t in d_trades if t.outcome == 'LOSS']
        wr = len(d_wins) / len(d_trades) * 100 if d_trades else 0
        pnl = sum(t.pnl for t in d_trades)
        print(f'  {direction:<6}: {len(d_trades):>3} trades | '
              f'{len(d_wins):>2}W / {len(d_losses):>2}L | '
              f'WR={wr:.1f}% | P&L=${pnl:+,.2f}')

    # ── 3. Loss clustering by date ────────────────────────────────────────
    print()
    print('  ── 3. Loss Clustering by Date ──────────────────────────────────')
    # Group losses by ISO-week for clustering view
    date_losses = defaultdict(int)
    date_wins   = defaultdict(int)
    for t in losses: date_losses[t.entry_date] += 1
    for t in wins:   date_wins[t.entry_date]   += 1

    # Show by calendar week
    week_losses = defaultdict(int)
    week_trades = defaultdict(int)
    for t in closed:
        wk = t.entry_dt.strftime('%Y-W%V')
        week_trades[wk] += 1
        if t.outcome == 'LOSS':
            week_losses[wk] += 1

    max_wl = max(week_losses.values()) if week_losses else 1
    all_weeks = sorted(set(list(week_losses.keys()) + [t.entry_dt.strftime('%Y-W%V') for t in wins]))
    print(f'  {"Week":<12} {"Losses":>7}  {"Bar":<28} {"L/Total":>8}')
    print('  ' + '-' * 60)
    for wk in all_weeks:
        l = week_losses[wk]
        tot = week_trades[wk]
        marker = ' ◄ worst' if l == max_wl and l > 0 else ''
        print(f'  {wk:<12} {l:>5}   {bar(l, max_wl, 28)}{marker:9} {l}/{tot}')

    # Also show the raw run of consecutive losses with dates
    print()
    print('  Consecutive runs of losses (≥ 3 in a row):')
    run_start = None
    run_count = 0
    runs = []
    for t in closed:
        if t.outcome == 'LOSS':
            if run_start is None:
                run_start = t
            run_count += 1
        else:
            if run_count >= 3:
                runs.append((run_count, run_start, t))
            run_start = None
            run_count = 0
    if run_count >= 3:
        runs.append((run_count, run_start, None))

    for cnt, start, end_t in sorted(runs, reverse=True):
        end_str = end_t.entry_date if end_t else 'streak end'
        start_dir = start.direction
        print(f'    {cnt:>2}-trade losing run: {start.entry_date} → {end_str} '
              f'(started {start.symbol} {start_dir})')

    # ── 4. The 16-trade losing streak — daily bias per trade ─────────────
    print()
    print('  ── 4. The 16-Trade Losing Streak — Bias Per Trade ──────────────')

    # Find the longest loss streak in the sorted list
    best_run_len = 0
    best_run_start = 0
    cur_run_len = 0
    cur_run_start = 0
    for i, t in enumerate(closed):
        if t.outcome == 'LOSS':
            if cur_run_len == 0:
                cur_run_start = i
            cur_run_len += 1
            if cur_run_len > best_run_len:
                best_run_len  = cur_run_len
                best_run_start = cur_run_start
        else:
            cur_run_len = 0

    streak = closed[best_run_start: best_run_start + best_run_len]

    if not streak:
        print('  No losing streak found.')
    else:
        buy_in_streak  = sum(1 for t in streak if t.direction == 'BUY')
        sell_in_streak = sum(1 for t in streak if t.direction == 'SELL')

        print(f'  Streak length : {best_run_len} trades  '
              f'({buy_in_streak} BUY bias, {sell_in_streak} SELL bias)')
        print(f'  Date range    : {streak[0].entry_date} → {streak[-1].entry_date}')
        print()
        print(f'  {"#":>3} {"Date":>12} {"Symbol":<10} {"Bias":<5} {"Entry":>10}  {"SL":>10}  Outcome')
        print('  ' + '-' * 62)
        for i, t in enumerate(streak, 1):
            dt = t.entry_dt.strftime('%m/%d %H:%M')
            print(f'  {i:>3} {dt:>12}  {t.symbol:<10} {t.direction:<5} '
                  f'{t.entry_price:>10.4f}  {t.sl:>10.4f}  {t.outcome}')

        bias_summary = defaultdict(lambda: defaultdict(int))
        for t in streak:
            bias_summary[t.direction]['count'] += 1
        print()
        print(f'  Summary: during this streak the strategy was biased '
              f'{"BUY only" if sell_in_streak == 0 else "SELL only" if buy_in_streak == 0 else f"{buy_in_streak}× BUY / {sell_in_streak}× SELL"}')

    # ── 5. Equity curve with peak/trough annotations ─────────────────────
    print()
    print('  ── 5. Equity Curve — Peak & Trough ─────────────────────────────')

    equity    = STARTING_BALANCE
    peak_eq   = equity
    trough_eq = equity
    peak_time = closed[0].entry_time if closed else 0
    trough_time = peak_time
    peak_idx  = 0
    trough_idx = 0

    curve = [equity]
    times = [closed[0].entry_time] if closed else []
    for i, t in enumerate(closed):
        equity += t.pnl
        curve.append(equity)
        times.append(t.entry_time)
        if equity > peak_eq:
            peak_eq = equity; peak_time = t.entry_time; peak_idx = i + 1
        if equity < trough_eq:
            trough_eq = equity; trough_time = t.entry_time; trough_idx = i + 1

    peak_dt   = datetime.fromtimestamp(peak_time,   tz=timezone.utc).strftime('%d %b %Y %H:%M')
    trough_dt = datetime.fromtimestamp(trough_time, tz=timezone.utc).strftime('%d %b %Y %H:%M')

    print(f'  Peak equity   : ${peak_eq:>10,.2f}  at trade #{peak_idx} ({peak_dt})')
    print(f'  Trough equity : ${trough_eq:>10,.2f}  at trade #{trough_idx} ({trough_dt})')
    print(f'  Peak→Trough DD: ${peak_eq - trough_eq:>10,.2f}  ({(peak_eq - trough_eq)/peak_eq*100:.1f}%)')
    print()

    # ASCII equity curve with peak/trough markers
    _ascii_equity_curve(curve, times, peak_idx, trough_idx, peak_eq, trough_eq)

    # Try to save PNG
    try:
        _save_equity_png(curve, times, closed, peak_idx, trough_idx, peak_eq, trough_eq)
        print()
        print('  PNG saved → equity_curve.png')
    except Exception as exc:
        print(f'  (PNG skipped: {exc})')

    print('=' * W)


def _ascii_equity_curve(curve: list, times: list, peak_idx: int, trough_idx: int,
                         peak_eq: float, trough_eq: float,
                         width: int = 64, height: int = 16):
    mn, mx = min(curve), max(curve)
    rng = mx - mn if mx != mn else 1

    step    = max(1, len(curve) // width)
    sampled = list(range(0, len(curve), step))
    vals    = [curve[i] for i in sampled]

    # Mark peak and trough columns
    peak_col   = next((ci for ci, i in enumerate(sampled) if i >= peak_idx),   len(sampled) - 1)
    trough_col = next((ci for ci, i in enumerate(sampled) if i >= trough_idx), len(sampled) - 1)

    for row in range(height, -1, -1):
        if row == height:
            label = f'  ${mx:>10,.0f} │'
        elif row == 0:
            label = f'  ${mn:>10,.0f} │'
        elif row == height // 2:
            label = f'  ${(mn+rng/2):>10,.0f} │'
        else:
            label = '              │'

        line = label
        for ci, v in enumerate(vals):
            level = (v - mn) / rng * height
            if ci == peak_col and row == int((v - mn) / rng * height):
                line += 'P'   # Peak marker
            elif ci == trough_col and row == int((v - mn) / rng * height):
                line += 'T'   # Trough marker
            elif level >= row:
                line += '█'
            else:
                line += ' '
        print(line)

    print('              └' + '─' * len(vals))
    print(f'              {"P=Peak, T=Trough":^{len(vals)}}')


def _save_equity_png(curve: list, times: list, closed: list,
                     peak_idx: int, trough_idx: int,
                     peak_eq: float, trough_eq: float):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.patches as mpatches

    dts = [datetime.fromtimestamp(t, tz=timezone.utc) for t in times]

    fig, axes = plt.subplots(2, 1, figsize=(14, 9),
                              gridspec_kw={'height_ratios': [3, 1]},
                              sharex=True)
    ax, ax2 = axes

    # ── Equity curve ──
    ax.plot(dts, curve, color='#2196F3', linewidth=1.6, zorder=3, label='Equity')
    ax.axhline(STARTING_BALANCE, color='#888', linewidth=0.8, linestyle='--', label='Start ($10k)')

    # Shade under curve: green above start, red below
    ax.fill_between(dts, STARTING_BALANCE, curve,
                    where=[v >= STARTING_BALANCE for v in curve],
                    alpha=0.15, color='green', interpolate=True)
    ax.fill_between(dts, STARTING_BALANCE, curve,
                    where=[v < STARTING_BALANCE for v in curve],
                    alpha=0.20, color='red', interpolate=True)

    # Peak annotation
    peak_dt   = dts[peak_idx]
    trough_dt = dts[trough_idx]
    ax.annotate(f'Peak\n${peak_eq:,.0f}',
                xy=(peak_dt, peak_eq),
                xytext=(peak_dt, peak_eq + 150),
                fontsize=8, color='green', ha='center',
                arrowprops=dict(arrowstyle='->', color='green', lw=1))
    ax.annotate(f'Trough\n${trough_eq:,.0f}',
                xy=(trough_dt, trough_eq),
                xytext=(trough_dt, trough_eq - 200),
                fontsize=8, color='red', ha='center',
                arrowprops=dict(arrowstyle='->', color='red', lw=1))

    # Individual trade markers
    for i, t in enumerate(closed):
        dt = datetime.fromtimestamp(t.entry_time, tz=timezone.utc)
        eq_after = curve[i + 1]
        if t.outcome == 'WIN':
            ax.scatter(dt, eq_after, color='#4CAF50', s=20, zorder=5, alpha=0.7)
        else:
            ax.scatter(dt, eq_after, color='#F44336', s=10, zorder=5, alpha=0.5)

    ax.set_ylabel('Equity (USD)', fontsize=10)
    ax.set_title('90-Day Backtest — Equity Curve with Trade Outcomes', fontsize=12, fontweight='bold')
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax.grid(True, alpha=0.3)
    win_patch  = mpatches.Patch(color='#4CAF50', label='Win')
    loss_patch = mpatches.Patch(color='#F44336', label='Loss')
    ax.legend(handles=[win_patch, loss_patch,
                        mpatches.Patch(color='#2196F3', label='Equity')],
              loc='upper right', fontsize=8)

    # ── Loss heatmap (per-week bar chart) ────────────────────────────────
    from collections import defaultdict
    week_losses = defaultdict(int)
    week_dates  = {}
    for t in closed:
        wk = t.entry_dt.strftime('%Y-W%V')
        week_dates[wk] = t.entry_dt - timedelta(days=t.entry_dt.weekday())
        if t.outcome == 'LOSS':
            week_losses[wk] += 1

    sorted_weeks = sorted(week_dates)
    wk_dts   = [week_dates[w] for w in sorted_weeks]
    wk_vals  = [week_losses[w] for w in sorted_weeks]
    colors   = ['#F44336' if v >= 4 else '#FF9800' if v >= 2 else '#FFC107' for v in wk_vals]

    ax2.bar(wk_dts, wk_vals, width=5, color=colors, align='edge', alpha=0.85)
    ax2.set_ylabel('Losses/week', fontsize=9)
    ax2.set_xlabel('Date (UTC)', fontsize=9)
    ax2.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax2.grid(True, axis='y', alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))

    fig.autofmt_xdate(rotation=30)
    plt.tight_layout()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'equity_curve.png')
    plt.savefig(out, dpi=150)
    plt.close()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print('=' * 72)
    print('  Fetching historical data...')
    print('=' * 72)
    url = f"{config.WS_ENDPOINT}?app_id={credentials.DERIV_APP_ID}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({'authorize': credentials.DERIV_API_TOKEN, 'req_id': 9999}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get('req_id') == 9999:
                if 'error' in msg:
                    print(f'Auth failed: {msg["error"].get("message")}'); return
                print(f'  Authorized as {msg["authorize"].get("loginid")}')
                break

        all_data = {}
        for sym in config.SYMBOLS:
            all_data[sym] = await fetch_symbol_data(ws, sym)

    print('\n  Running backtest engine...')
    all_trades = {sym: run_backtest(sym, all_data[sym]) for sym in config.SYMBOLS}
    for sym, trades in all_trades.items():
        closed = [t for t in trades if t.outcome in ('WIN','LOSS')]
        print(f'  {sym}: {len(closed)} closed trades')

    analyse(all_trades)


if __name__ == '__main__':
    import logging
    logging.disable(logging.CRITICAL)   # suppress state_manager log spam
    asyncio.run(main())
