#!/usr/bin/env python3
"""
dashboard.py
~~~~~~~~~~~~
Live performance dashboard for the trading bot.
Parses bot.log and state files to show wins, losses, PnL, and per-symbol stats.

Usage:
    python3 /opt/trading_bot/dashboard.py
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────
_BASE            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE         = os.path.join(_BASE, 'logs', 'bot.log')
DAILY_STATS_FILE = os.path.join(_BASE, 'state', 'daily_stats.json')
OPEN_POSITIONS   = os.path.join(_BASE, 'state', 'open_positions.json')
SYMBOL_SESSION   = os.path.join(_BASE, 'state', 'symbol_session.json')

# ── Colours ───────────────────────────────────────────────
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
RESET  = '\033[0m'
DIM    = '\033[2m'

def color(text, c):
    return f'{c}{text}{RESET}'

# ── Log parsing ───────────────────────────────────────────
# Matches lines like:
# [2026-04-06 18:45:20 UTC] TRADE | R_75 | Contract 123 WON | P&L: +5.20
# [2026-04-06 18:45:20 UTC] TRADE | R_75 | Contract 123 LOST | P&L: -2.00
# [2026-04-06 18:45:20 UTC] TRADE | R_75 | Contract opened: id=...
TRADE_CLOSE_RE = re.compile(
    r'\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\].*?TRADE.*?\|\s+(\w[\w_]*)\s+\|.*?'
    r'Contract \S+ (WON|LOST|SOLD).*?P&L: ([+-]?\d+\.?\d*)'
)
TRADE_OPEN_RE = re.compile(
    r'\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\].*?TRADE.*?\|\s+(\w[\w_]*)\s+\|.*?'
    r'Contract opened'
)
WIN_RE  = re.compile(r'\[(\d{4}-\d{2}-\d{2}).*?\] INFO.*?Win recorded')
LOSS_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2}).*?\] WARNING.*?Loss recorded — daily count: (\d+)/\d+')

def parse_log():
    trades_by_date   = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0})
    trades_by_symbol = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'opened': 0})
    all_trades = []

    if not os.path.exists(LOG_FILE):
        return trades_by_date, trades_by_symbol, all_trades

    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            # Count opened trades per symbol
            m = TRADE_OPEN_RE.search(line)
            if m:
                symbol = m.group(3)
                trades_by_symbol[symbol]['opened'] += 1
                continue

            # Count closed trades
            m = TRADE_CLOSE_RE.search(line)
            if m:
                date, time_, symbol, outcome, pnl_str = m.groups()
                pnl = float(pnl_str)
                won = outcome == 'WON' or pnl > 0

                trades_by_date[date]['pnl'] += pnl
                trades_by_symbol[symbol]['pnl'] += pnl

                if won:
                    trades_by_date[date]['wins'] += 1
                    trades_by_symbol[symbol]['wins'] += 1
                else:
                    trades_by_date[date]['losses'] += 1
                    trades_by_symbol[symbol]['losses'] += 1

                all_trades.append({
                    'date': date, 'time': time_,
                    'symbol': symbol, 'outcome': outcome,
                    'pnl': pnl
                })

    return trades_by_date, trades_by_symbol, all_trades


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def win_rate(wins, losses):
    total = wins + losses
    if total == 0:
        return 0.0
    return wins / total * 100


def bar(value, max_val, width=20, positive_color=GREEN, negative_color=RED):
    if max_val == 0:
        return '─' * width
    filled = int(abs(value) / max_val * width)
    ch = '█' * filled + '░' * (width - filled)
    c = positive_color if value >= 0 else negative_color
    return color(ch, c)


def print_dashboard():
    trades_by_date, trades_by_symbol, all_trades = parse_log()
    daily_stats  = load_json(DAILY_STATS_FILE)
    open_pos     = load_json(OPEN_POSITIONS)
    symbol_sess  = load_json(SYMBOL_SESSION)

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    print()
    print(color('━' * 60, CYAN))
    print(color(f'  🤖  TRADING BOT DASHBOARD   {now}', BOLD))
    print(color('━' * 60, CYAN))

    # ── Today's summary ───────────────────────────────────
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_wins   = daily_stats.get('daily_win_count', 0)
    today_losses = daily_stats.get('daily_loss_count', 0)
    today_pnl    = trades_by_date.get(today, {}).get('pnl', 0.0)
    today_wr     = win_rate(today_wins, today_losses)
    pnl_color    = GREEN if today_pnl >= 0 else RED

    print()
    print(color('  TODAY', BOLD))
    print(f'  Wins    : {color(today_wins, GREEN)}')
    print(f'  Losses  : {color(today_losses, RED)}')
    print(f'  Win Rate: {color(f"{today_wr:.0f}%", YELLOW)}')
    print(f'  PnL     : {color(f"{today_pnl:+.2f}", pnl_color)}')

    # ── Open positions ────────────────────────────────────
    open_count = sum(1 for p in open_pos.values() if isinstance(p, dict) and p.get('status') == 'open')
    print()
    print(color('  OPEN POSITIONS', BOLD))
    if open_count == 0:
        print(color('  None', DIM))
    else:
        for sym, pos in open_pos.items():
            if isinstance(pos, dict) and pos.get('status') == 'open':
                direction = pos.get('direction', '?')
                entry     = pos.get('entry_price', 0)
                stake     = pos.get('stake', 0)
                d_color   = GREEN if direction == 'BUY' else RED
                print(f'  {sym}  {color(direction, d_color)}  entry={entry:.5f}  stake=${stake:.2f}')

    # ── All-time by date ──────────────────────────────────
    if trades_by_date:
        print()
        print(color('  DAILY HISTORY', BOLD))
        print(f'  {"Date":<12} {"W":>4} {"L":>4} {"WR":>6} {"PnL":>8}')
        print(f'  {"─"*12} {"─"*4} {"─"*4} {"─"*6} {"─"*8}')

        max_pnl = max(abs(d['pnl']) for d in trades_by_date.values()) or 1
        for date in sorted(trades_by_date.keys(), reverse=True)[:14]:  # last 2 weeks
            d     = trades_by_date[date]
            w, l  = d['wins'], d['losses']
            wr    = win_rate(w, l)
            pnl   = d['pnl']
            pc    = GREEN if pnl >= 0 else RED
            label = color('◀ today', CYAN) if date == today else ''
            print(f'  {date:<12} {color(w, GREEN):>4} {color(l, RED):>4} '
                  f'{color(f"{wr:.0f}%", YELLOW):>6} {color(f"{pnl:+.2f}", pc):>8}  {label}')

    # ── Per-symbol breakdown ──────────────────────────────
    if trades_by_symbol:
        print()
        print(color('  PER SYMBOL', BOLD))
        print(f'  {"Symbol":<10} {"Opened":>7} {"Won":>5} {"Lost":>5} {"WR":>6} {"PnL":>8}')
        print(f'  {"─"*10} {"─"*7} {"─"*5} {"─"*5} {"─"*6} {"─"*8}')

        for sym in sorted(trades_by_symbol.keys()):
            s   = trades_by_symbol[sym]
            w   = s['wins']
            l   = s['losses']
            op  = s['opened']
            wr  = win_rate(w, l)
            pnl = s['pnl']
            pc  = GREEN if pnl >= 0 else RED
            print(f'  {sym:<10} {op:>7} {color(w, GREEN):>5} {color(l, RED):>5} '
                  f'{color(f"{wr:.0f}%", YELLOW):>6} {color(f"{pnl:+.2f}", pc):>8}')

    # ── Overall totals ────────────────────────────────────
    total_wins   = sum(d['wins']   for d in trades_by_date.values())
    total_losses = sum(d['losses'] for d in trades_by_date.values())
    total_pnl    = sum(d['pnl']    for d in trades_by_date.values())
    total_wr     = win_rate(total_wins, total_losses)
    total_pc     = GREEN if total_pnl >= 0 else RED

    print()
    print(color('  OVERALL (all-time from log)', BOLD))
    print(f'  Trades   : {total_wins + total_losses}  '
          f'({color(total_wins, GREEN)} wins / {color(total_losses, RED)} losses)')
    print(f'  Win Rate : {color(f"{total_wr:.1f}%", YELLOW)}')
    print(f'  Total PnL: {color(f"{total_pnl:+.2f}", total_pc)}')

    # ── Symbol states ─────────────────────────────────────
    sym_states = symbol_sess.get('symbol_states', {})
    if sym_states:
        print()
        print(color('  BOT STATE PER SYMBOL', BOLD))
        for sym, state in sorted(sym_states.items()):
            sc = CYAN if state == 'IN_TRADE' else (GREEN if state == 'H4_CONFIRMED' else DIM)
            bias = symbol_sess.get('daily_bias', {}).get(sym, 'NONE')
            bc   = GREEN if bias == 'BUY' else (RED if bias == 'SELL' else DIM)
            print(f'  {sym:<10}  {color(state, sc):<20}  bias={color(bias, bc)}')

    print()
    print(color('━' * 60, CYAN))
    print()


if __name__ == '__main__':
    print_dashboard()
