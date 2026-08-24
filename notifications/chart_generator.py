"""
chart_generator.py
~~~~~~~~~~~~~~~~~~
Generates a candlestick chart image (PNG bytes) in the style of Deriv / TradingView,
annotated with entry price, stop-loss and take-profit levels.

The image is returned as raw bytes so the caller can pass it directly to
Telegram's sendPhoto endpoint without writing to disk.

Public API
----------
    build_chart_image(
        symbol:       str,
        direction:    str,         # 'BUY' | 'SELL'
        candles:      list[dict],  # each dict has 'time','open','high','low','close'
        entry_price:  float,
        sl:           float,
        tp:           float,
        reason:       str,         # e.g. "M15 liquidity sweep + rejection"
        timeframe:    str = 'M15', # label shown on the chart header
    ) -> bytes
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

import matplotlib
matplotlib.use('Agg')                   # non-interactive backend — no display needed
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.patches import FancyBboxPatch

# ── Colour palette (matches the dark Deriv / TV look) ─────────────────────────
BG_DARK      = '#0f1217'
GRID_COLOR   = '#1e2530'
BULL_BODY    = '#26a69a'   # teal
BEAR_BODY    = '#ef5350'   # red
WICK_COLOR   = '#aaaaaa'

ENTRY_COLOR  = '#2196f3'   # blue
SL_COLOR     = '#ef5350'   # red
TP_COLOR     = '#26a69a'   # teal
LABEL_BG     = '#1e2530'

TEXT_PRIMARY = '#d1d4dc'
TEXT_DIM     = '#787b86'
ACCENT       = '#ef5350'   # used for symbol name on header


def _candle_color(c: dict) -> str:
    return BULL_BODY if c['close'] >= c['open'] else BEAR_BODY


def _format_price(price: float) -> str:
    """Format price with 4 decimal places (standard for Deriv synthetics)."""
    return f'{price:.4f}'


def build_chart_image(
    symbol: str,
    direction: str,
    candles: list,
    entry_price: float,
    sl: float,
    tp: float,
    reason: str,
    timeframe: str = 'M15',
    max_candles: int = 40,
) -> bytes:
    """
    Build and return a PNG chart as raw bytes.

    Parameters
    ----------
    symbol      : e.g. '1HZ100V'
    direction   : 'BUY' or 'SELL'
    candles     : list of OHLCV dicts with keys time/open/high/low/close
    entry_price : entry level
    sl          : stop-loss level
    tp          : take-profit level
    reason      : short text explaining why the signal fired
    timeframe   : displayed in the chart header
    max_candles : how many recent candles to plot
    """
    candles = candles[-max_candles:] if len(candles) > max_candles else candles
    if not candles:
        raise ValueError('No candles provided to build_chart_image')

    n = len(candles)
    xs = list(range(n))

    # ── Figure setup ──────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor(BG_DARK)
    ax.set_facecolor(BG_DARK)

    # ── Draw candles ──────────────────────────────────────────────────────────
    width      = 0.6
    half_width = width / 2

    for i, c in enumerate(candles):
        color = _candle_color(c)
        # Wick
        ax.plot([i, i], [c['low'], c['high']],
                color=WICK_COLOR, linewidth=0.8, zorder=2)
        # Body
        body_lo = min(c['open'], c['close'])
        body_hi = max(c['open'], c['close'])
        ax.add_patch(mpatches.FancyBboxPatch(
            (i - half_width, body_lo),
            width, body_hi - body_lo,
            boxstyle='square,pad=0',
            facecolor=color,
            edgecolor=color,
            linewidth=0.5,
            zorder=3,
        ))

    # ── Horizontal level lines ────────────────────────────────────────────────
    x_end   = n - 1 + 0.5     # lines run to the right edge

    def _hline(price: float, color: str, linestyle: str = '-', lw: float = 1.0):
        ax.hlines(price, -0.5, x_end + 1.5, colors=color,
                  linewidth=lw, linestyles=linestyle, zorder=4)

    _hline(entry_price, ENTRY_COLOR, linestyle='--', lw=1.2)
    _hline(sl,          SL_COLOR,    linestyle='--', lw=1.0)
    _hline(tp,          TP_COLOR,    linestyle='--', lw=1.0)

    # ── Shade TP / SL zone from last candle close ──────────────────────────────
    last_close = candles[-1]['close']
    zone_x     = [n - 1, n - 1, x_end + 1.5, x_end + 1.5]

    # TP zone (direction-appropriate colour)
    tp_zone_color  = BULL_BODY if direction == 'BUY' else BEAR_BODY
    sl_zone_color  = BEAR_BODY if direction == 'BUY' else BULL_BODY

    ax.fill(zone_x, [entry_price, tp, tp, entry_price],
            color=tp_zone_color, alpha=0.12, zorder=1)
    ax.fill(zone_x, [entry_price, sl, sl, entry_price],
            color=sl_zone_color, alpha=0.12, zorder=1)

    # ── Right-side price badges ───────────────────────────────────────────────
    badge_x = x_end + 1.6

    def _badge(price: float, color: str, label: Optional[str] = None):
        txt = f'  {_format_price(price)}  '
        if label:
            txt = f'  {label}: {_format_price(price)}  '
        ax.text(badge_x, price, txt,
                color='white', fontsize=7.5, fontweight='bold',
                va='center', ha='left',
                bbox=dict(boxstyle='round,pad=0.25', facecolor=color,
                          edgecolor=color, alpha=0.9),
                zorder=5, clip_on=False)

    _badge(tp,          TP_COLOR,    'TP')
    _badge(entry_price, ENTRY_COLOR, 'Entry')
    _badge(sl,          SL_COLOR,    'SL')

    # ── Grid ─────────────────────────────────────────────────────────────────
    ax.yaxis.grid(True, color=GRID_COLOR, linewidth=0.5, zorder=0)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    # ── Axes styling ─────────────────────────────────────────────────────────
    ax.spines[:].set_visible(False)
    ax.tick_params(axis='y', colors=TEXT_DIM, labelsize=7.5)
    ax.tick_params(axis='x', colors=TEXT_DIM, labelsize=7)
    ax.yaxis.set_label_position('right')
    ax.yaxis.tick_right()

    # X ticks: show time labels at ~8 evenly spaced candles
    tick_step = max(1, n // 8)
    tick_positions = list(range(0, n, tick_step))
    tick_labels = []
    for i in tick_positions:
        ts = candles[i].get('time', 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        tick_labels.append(dt.strftime('%H:%M'))
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, color=TEXT_DIM)

    # Y-axis: auto-range with padding
    all_prices = [c['high'] for c in candles] + [c['low'] for c in candles]
    price_pad  = (max(all_prices) - min(all_prices)) * 0.15
    y_min = min(min(all_prices), sl, tp) - price_pad
    y_max = max(max(all_prices), sl, tp) + price_pad
    ax.set_ylim(y_min, y_max)
    ax.set_xlim(-0.8, badge_x + 3)   # leave room for badges

    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter('%.4f'))

    # ── Header bar ────────────────────────────────────────────────────────────
    # Symbol + timeframe on top-left, current price on same line
    current_price = candles[-1]['close']
    price_change  = current_price - candles[0]['open']
    pct_change    = price_change / candles[0]['open'] * 100

    header = (
        f'{symbol}  ·  {timeframe}\n'
        f'{_format_price(current_price)}  '
        f'{price_change:+.4f} ({pct_change:+.2f}%)'
    )
    ax.text(0.01, 0.97, header,
            transform=ax.transAxes,
            color=TEXT_PRIMARY, fontsize=9, fontweight='bold',
            va='top', ha='left', linespacing=1.5,
            zorder=6)

    # ── Direction badge (BUY / SELL) ──────────────────────────────────────────
    dir_color = BULL_BODY if direction == 'BUY' else BEAR_BODY
    ax.text(0.01, 0.83, f'  {direction}  ',
            transform=ax.transAxes,
            color='white', fontsize=9, fontweight='bold',
            va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.35', facecolor=dir_color,
                      edgecolor=dir_color),
            zorder=6)

    # ── Reason box at bottom ──────────────────────────────────────────────────
    reason_text = f'Reason: {reason}'
    ax.text(0.5, 0.03, reason_text,
            transform=ax.transAxes,
            color=TEXT_PRIMARY, fontsize=8,
            va='bottom', ha='center',
            bbox=dict(boxstyle='round,pad=0.4', facecolor=LABEL_BG,
                      edgecolor=GRID_COLOR, alpha=0.9),
            zorder=6)

    # ── Watermark ─────────────────────────────────────────────────────────────
    ax.text(0.99, 0.03, 'charts.deriv.com',
            transform=ax.transAxes,
            color=TEXT_DIM, fontsize=7,
            va='bottom', ha='right', zorder=6)

    plt.tight_layout(pad=0.5)

    # ── Render to bytes ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150,
                facecolor=BG_DARK, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()
