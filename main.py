"""
main.py - Fixed Master Version
Wired to corrected signal_engine:
  - get_best_poi()            replaces identify_order_block()
  - find_4h_liquidity_target() feeds TP in calculate_sl_tp()
  - POI type logged in Telegram alerts
"""
from __future__ import annotations
import asyncio
from datetime import datetime
import logging
import os
import signal
import sys
import time
from dotenv import load_dotenv
load_dotenv()

import config
import core.signal_engine as se
from core.candle_store  import CandleStore
from core.deriv_client  import DerivClient
from core.risk_manager  import RiskManager
from core.state_manager import StateManager, IDLE, BIAS_SET, H4_CONFIRMED, SIGNAL_READY, IN_TRADE, COOLDOWN
from core.trade_manager import TradeManager, calculate_stake
from notifications.telegram_alerts  import TelegramAlerter
from notifications.chart_generator  import build_chart_image
from utils.logger    import get_logger, log_event
from utils.time_utils import wait_until_daily_reset

logger = get_logger('main')


async def on_candle(
    symbol: str,
    tf: str,
    closed_candle: dict,
    state: StateManager,
    candle_store: CandleStore,
    trade_manager: TradeManager,
    risk_manager: RiskManager,
    telegram: TelegramAlerter,
    trade_locks: dict,
):
    if not state.symbol_ready.get(symbol, False) or state.reconnecting:
        return
    if not risk_manager.can_trade() and state.get_state(symbol) not in (IN_TRADE, COOLDOWN):
        return
    if not state.balance_ready and state.get_state(symbol) not in (IN_TRADE, COOLDOWN):
        return

    current_state = state.get_state(symbol)

    # ── COOLDOWN ──────────────────────────────────────────────────────────────
    if current_state == COOLDOWN and tf == 'M5':
        if time.time() < state.cooldown_until.get(symbol, 0):
            return
        bias = state.daily_bias.get(symbol, 'NONE')
        if bias in ('BUY', 'SELL'):
            state.set_state(symbol, BIAS_SET)
        else:
            state.set_state(symbol, IDLE)
        return

    # ── IDLE — startup bias detection ─────────────────────────────────────────
    elif current_state == IDLE and tf == 'D':
        d_candles = candle_store.get_closed_candles(symbol, 'D')
        if len(d_candles) >= 3:
            lookback = d_candles[-config.DAILY_BIAS_LOOKBACK - 1:-1]
            prev_high = max(c['high'] for c in lookback)
            prev_low = min(c['low'] for c in lookback)
            bias = se.detect_daily_bias(
                d_candles,
                prev_high,
                prev_low,
            )
            if bias in ('BUY', 'SELL'):
                state.daily_bias[symbol] = bias
                state.set_state(symbol, BIAS_SET)
                telegram.send(
                    f'<b>{symbol}</b> — Daily bias detected: <b>{bias}</b>'
                )
        return

    # ── BIAS_SET — wait for 4H sweep confirmation ─────────────────────────────
    elif current_state == BIAS_SET and tf == 'H4':
        h4_closed = candle_store.get_closed_candles(symbol, 'H4')
        if not h4_closed:
            return

        bias  = state.daily_bias.get(symbol)
        swing = se.find_4h_swing(h4_closed, bias, config.SWING_LOOKBACK_4H)

        if not swing:
            return

        state.h4_swing_level[symbol] = swing

        if se.confirm_4h_sweep(h4_closed[-1], swing, bias):
            state.h4_sweep_candle[symbol] = h4_closed[-1]

            # ── Find the 4H liquidity TARGET (TP level) ──
            # This is the NEXT liquidity pool in the bias direction —
            # the SSL (for SELL) or BSL (for BUY) that price is heading toward.
            h4_liq_target = se.find_4h_liquidity_target(
                h4_closed, bias, config.SWING_LOOKBACK_4H
            )
            state.h4_liquidity_target[symbol] = h4_liq_target

            state.set_state(symbol, H4_CONFIRMED)

            target_str = (
                f'{h4_liq_target:.5f}' if h4_liq_target else 'not found'
            )
            telegram.send(
                f'<b>{symbol}</b> — 4H sweep confirmed @ {swing:.5f}\n'
                f'4H liquidity target (TP): <b>{target_str}</b>'
            )
        return

    # ── H4_CONFIRMED — scan M5 for entry ─────────────────────────────────────
    elif current_state == H4_CONFIRMED and tf == 'M5':
        m5_closed = candle_store.get_closed_candles(symbol, 'M5')
        if len(m5_closed) < 10:
            return

        today = datetime.utcnow().date().isoformat()
        if state.last_entry_day.get(symbol) == today:
            return

        bias    = state.daily_bias.get(symbol)
        h4_time = (state.h4_sweep_candle.get(symbol) or {}).get('time')

        # ── Step 1: detect M5 liquidity levels ──
        liq = se.detect_m15_liquidity_levels(
            m5_closed,
            state.consumed_levels.get(symbol, []),
            config.LIQUIDITY_TOLERANCE,
        )

        # ── Step 2: detect M5 entry trigger (sweep + close back) ──
        entry = se.detect_m15_entry(
            m5_closed, bias, liq, h4_sweep_time=h4_time
        )

        if not entry or entry['direction'] != bias:
            return

        direction   = entry['direction']
        entry_price = entry['entry_price']
        sweep_time  = entry['sweep_candle'].get('time')

        # ── Step 3: find best POI (OB → FVG → BB) ──
        poi = se.get_best_poi(m5_closed, direction, sweep_candle_time=sweep_time)

        if config.REQUIRE_ORDER_BLOCK:
            if poi is None:
                log_event(logger, logging.DEBUG, symbol,
                          'No POI found after sweep — skipping entry')
                return

            # Entry price must be inside the POI zone
            if not (poi['poi_low'] <= entry_price <= poi['poi_high']):
                log_event(logger, logging.DEBUG, symbol,
                          f'Entry {entry_price:.5f} outside POI '
                          f'[{poi["poi_low"]:.5f} – {poi["poi_high"]:.5f}] '
                          f'({poi["poi_type"]}) — skipping')
                return

        # ── Step 4: calculate SL/TP ──
        # TP uses the 4H liquidity target if available, else falls back to RR
        h4_liq_target = state.h4_liquidity_target.get(symbol)
        sl_tp = se.calculate_sl_tp(
            entry_price,
            entry['sweep_candle'],
            direction,
            h4_liquidity_target=h4_liq_target,
            rr_mode=config.RR_MODE,
        )

        # ── Lock state before async trade attempt ──
        state.set_state(symbol, SIGNAL_READY)

        lock = trade_locks[symbol]
        if lock.locked():
            log_event(logger, logging.WARNING, symbol,
                      'Trade lock held — duplicate open_trade skipped')
            state.set_state(symbol, H4_CONFIRMED)
            return

        acquired = False
        await lock.acquire()
        acquired = True
        try:
            m5_candles = candle_store.get_candles(symbol, 'M5')

            # Build chart — exclude the still-forming (current) candle
            chart_png = build_chart_image(
                symbol, direction, m5_candles[:-1],
                entry_price, sl_tp['sl'], sl_tp['tp'],
                'M5 LQ Sweep', 'M5',
            )

            poi_label  = poi['poi_type'] if poi else 'No POI'
            tp_source  = sl_tp.get('tp_source', 'RR_FALLBACK')
            signal_msg = (
                f'<b>{symbol}</b> — Signal READY\n'
                f'Direction : <b>{direction}</b>\n'
                f'Entry     : {entry_price:.5f}\n'
                f'SL        : {sl_tp["sl"]:.5f}\n'
                f'TP        : {sl_tp["tp"]:.5f} ({tp_source})\n'
                f'POI       : {poi_label}\n'
                f'Swept lvl : {entry["swept_level"]:.5f}'
            )
            telegram.send_chart(chart_png, signal_msg)

            # Acquire the balance lock so the value cannot be updated
            # between the read and the stake calculation.  calculate_stake
            # is synchronous — no awaits occur while the lock is held.
            async with config.ACCOUNT_BALANCE.lock:
                stake = calculate_stake(
                    entry_price,
                    sl_tp['sl'],
                    config.SYMBOL_MULTIPLIERS.get(symbol, 200),
                    config.ACCOUNT_BALANCE.value,
                )

            await trade_manager.open_trade(
                symbol, direction, entry_price,
                sl_tp['sl'], sl_tp['tp'], stake,
                poi,   # pass full POI dict (replaces old order_block dict)
            )
            state.last_entry_day[symbol] = today

        except Exception as e:
            log_event(logger, logging.ERROR, symbol, f'Trade launch failed: {e}')
            state.set_state(symbol, H4_CONFIRMED)
        finally:
            if acquired:
                lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# Daily reset
# ─────────────────────────────────────────────────────────────────────────────

async def daily_reset_task(risk_manager, telegram, deriv_client, state):
    while True:
        await wait_until_daily_reset()
        risk_manager.daily_reset()

        if deriv_client:
            try:
                await config.ACCOUNT_BALANCE.set(await deriv_client.fetch_balance())
                state.balance_ready = True
            except Exception as e:
                logger.error(f'Balance fetch failed: {e}')

            if config.ACCOUNT_BALANCE.value == 0.0:
                logger.warning(
                    'ACCOUNT_BALANCE is 0.0 after daily reset — '
                    'skipping trading this cycle'
                )
                state.balance_ready = False

        telegram.send('<b>Daily reset complete.</b>')


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state        = StateManager()
    candle_store = CandleStore()
    telegram     = TelegramAlerter()
    risk_manager = RiskManager(state)

    for sym in config.SYMBOLS:
        state.init_symbol(sym)

    trade_manager = TradeManager(None, state, risk_manager, telegram)
    trade_locks   = {sym: asyncio.Lock() for sym in config.SYMBOLS}

    async def _on_candle(symbol, tf, candle):
        await on_candle(
            symbol, tf, candle,
            state, candle_store, trade_manager,
            risk_manager, telegram, trade_locks,
        )

    deriv_client          = DerivClient(
        state, candle_store, _on_candle,
        trade_manager.handle_position_update,
    )
    trade_manager.client  = deriv_client

    telegram.send(
        f'<b>Bot started.</b> Monitoring {len(config.SYMBOLS)} symbols.'
    )
    await telegram.start()

    stop_event = asyncio.Event()
    loop       = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop_event.set())

    tasks = [
        asyncio.create_task(deriv_client.connect()),
        asyncio.create_task(
            daily_reset_task(risk_manager, telegram, deriv_client, state)
        ),
    ]

    await stop_event.wait()
    for t in tasks:
        t.cancel()
    await telegram.stop()


if __name__ == '__main__':
    asyncio.run(main())