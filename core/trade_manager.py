"""
trade_manager.py
~~~~~~~~~~~~~~~~
Handles trade execution and lifecycle management via the Deriv API.
Only MULTUP / MULTDOWN contract types are used — never CALL / PUT.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import config
from core.state_manager import IN_TRADE, COOLDOWN, IDLE, H4_CONFIRMED
from utils.logger import get_logger, log_event, TRADE_LEVEL


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def calculate_stake(entry_price: float, sl: float, multiplier: int,
                    account_balance: Optional[float] = None) -> float:
    """
    Calculate the stake for a multiplier contract so that the maximum loss
    equals exactly ACCOUNT_BALANCE * RISK_PER_TRADE USD.

    For Deriv multiplier contracts the P&L formula is:
        P&L = stake * multiplier * (price_change / entry_price)

    When price hits SL the loss equals:
        loss = stake * multiplier * (sl_distance / entry_price)

    Rearranging for stake:
        stake = risk_amount * entry_price / (multiplier * sl_distance)

    Floored to Deriv's minimum of 1.00 USD and capped at
    ACCOUNT_BALANCE * 0.25 (never more than 25% of balance as stake).
    Rounded to 2 decimal places.
    """
    logger = get_logger('trade_manager')

    balance = account_balance if account_balance is not None else config.ACCOUNT_BALANCE.value

    if balance <= 0:
        raise RuntimeError(
            f'calculate_stake: invalid account balance ({balance}) — '
            'must be positive. Trade aborted.'
        )

    risk_amount = balance * config.RISK_PER_TRADE
    sl_distance = abs(entry_price - sl)

    if sl_distance == 0 or entry_price == 0 or multiplier == 0:
        log_event(logger, logging.WARNING, '------',
                  'Invalid inputs for stake calculation — using minimum stake')
        return 1.0

    stake = risk_amount * entry_price / (multiplier * sl_distance)

    # Cap first, then floor — ensures minimum $1.00 is always honoured
    stake = min(stake, balance * 0.25)
    stake = max(stake, 1.0)
    stake = round(stake, 2)

    log_event(logger, logging.DEBUG, '------',
              f'Stake calc: risk=${risk_amount:.2f} entry={entry_price:.5f} '
              f'sl_dist={sl_distance:.5f} mult={multiplier} → stake=${stake:.2f}')
    return stake


# ─────────────────────────────────────────────────────────────────────────────
# TradeManager
# ─────────────────────────────────────────────────────────────────────────────

class TradeManager:
    """
    Orchestrates trade entry, monitoring, and outcome recording.

    Relies on DerivClient for WebSocket communication, StateManager for
    persistent state, RiskManager for outcome tracking, and TelegramAlerter
    for notifications.
    """

    def __init__(self, deriv_client, state_manager, risk_manager, telegram):
        self.client   = deriv_client
        self.state    = state_manager
        self.risk     = risk_manager
        self.telegram = telegram
        self.logger   = get_logger('trade_manager')

    # ── Trade entry ───────────────────────────────────────

    async def open_trade(self, symbol: str, direction: str, entry_price: float,
                         sl: float, tp: float, stake: float,
                         order_block: Optional[dict]):
        """
        Send a proposal request then buy via the Deriv API (two-step flow).

        On success  : stores position in state.open_positions[symbol],
                      subscribes to proposal_open_contract for live tracking.
        On API error: logs the error, sends a Telegram alert, and resets
                      the symbol to H4_CONFIRMED so scanning resumes.
        """
        # ── Balance guard ─────────────────────────────────
        if config.ACCOUNT_BALANCE.value <= 0:
            log_event(self.logger, logging.CRITICAL, symbol,
                      f'ACCOUNT_BALANCE is {config.ACCOUNT_BALANCE.value} — '
                      'cannot open trade with zero or negative balance')
            self.telegram.send(
                f'<b>TRADE BLOCKED</b> — {symbol}\n'
                f'Reason: ACCOUNT_BALANCE is {config.ACCOUNT_BALANCE.value} '
                f'(must be positive)'
            )
            return

        # ── Reconnection guard ────────────────────────────
        if self.state.reconnecting:
            log_event(self.logger, logging.WARNING, symbol,
                      'WebSocket reconnecting — trade blocked')
            return

        # ── Concurrent trade guard ────────────────────────
        open_count = sum(
            1 for p in self.state.open_positions.values()
            if isinstance(p, dict) and p.get('status') == 'open'
        )
        if open_count >= config.MAX_CONCURRENT_TRADES:
            log_event(self.logger, logging.WARNING, symbol,
                      f'Max concurrent trades reached ({open_count}/{config.MAX_CONCURRENT_TRADES}) '
                      f'— skipping entry')
            self.telegram.send(
                f'<b>TRADE SKIPPED</b> — {symbol}\n'
                f'Reason: {open_count} trades already open '
                f'(max {config.MAX_CONCURRENT_TRADES})'
            )
            self.state.set_state(symbol, COOLDOWN)
            self.state.cooldown_until[symbol] = time.time() + 300
            return

        contract_type = 'MULTUP' if direction == 'BUY' else 'MULTDOWN'
        multiplier    = config.SYMBOL_MULTIPLIERS.get(symbol, 200)

        try:
            # ── Step 1: Request proposal ──
            # NOTE: Deriv multiplier contracts do NOT use duration_unit/duration.
            # Passing those fields causes "Properties not allowed" API errors.
            # subscribe is also omitted — we only need a one-shot proposal id.
            log_event(self.logger, TRADE_LEVEL, symbol,
                      f'Sending proposal: {contract_type} stake=${stake:.2f} mult={multiplier}')

            proposal_resp = await self.client.send({
                'proposal': 1,
                'amount': stake,
                'basis': 'stake',
                'contract_type': contract_type,
                'currency': 'USD',
                'multiplier': multiplier,
                'symbol': symbol,
            })

            if proposal_resp is None:
                raise RuntimeError('No response to proposal request')

            if 'error' in proposal_resp:
                raise RuntimeError(
                    f'Proposal error: {proposal_resp["error"].get("message", "Unknown")}')

            proposal_id = proposal_resp['proposal']['id']
            log_event(self.logger, logging.DEBUG, symbol,
                      f'Got proposal id={proposal_id}')

            # ── Step 2: Buy using proposal ID ──
            buy_resp = await self.client.send({
                'buy': proposal_id,
                'price': stake,
            })

            if buy_resp is None:
                raise RuntimeError('No response to buy request')

            if 'error' in buy_resp:
                raise RuntimeError(
                    f'Buy error: {buy_resp["error"].get("message", "Unknown")}')

            buy_data    = buy_resp.get('buy', {})
            contract_id = buy_data.get('contract_id')

            if not contract_id:
                raise RuntimeError('No contract_id in buy response')

            log_event(self.logger, TRADE_LEVEL, symbol,
                      f'Contract opened: id={contract_id}')

            # ── Step 3: Apply SL/TP via contract_update ──
            MAX_USD_LIMIT = 500
            sl_amount = round(abs(entry_price - sl) / entry_price * stake * multiplier, 2)
            tp_amount = round(abs(entry_price - tp) / entry_price * stake * multiplier, 2)

            # Cap SL: cannot exceed stake or hard limit
            sl_amount = round(min(sl_amount, MAX_USD_LIMIT, stake), 2)
            # Cap TP: cannot exceed hard limit
            tp_amount = round(min(tp_amount, MAX_USD_LIMIT), 2)

            log_event(self.logger, logging.DEBUG, symbol,
                      f'SL/TP amounts (capped): sl=${sl_amount:.2f} tp=${tp_amount:.2f}')

            update_resp = await self.client.send({
                'contract_update': 1,
                'contract_id': contract_id,
                'limit_order': {
                    'stop_loss': sl_amount,
                    'take_profit': tp_amount,
                },
            })
            if update_resp and 'error' in update_resp:
                err_msg = update_resp['error'].get('message', 'Unknown')
                err_code = update_resp['error'].get('code', '')
                log_event(self.logger, logging.WARNING, symbol,
                          f'SL/TP update failed: {err_msg} — retrying with halved amounts')

                # Retry with reduced amounts
                sl_amount = round(sl_amount * 0.5, 2)
                tp_amount = round(tp_amount * 0.5, 2)

                retry_resp = await self.client.send({
                    'contract_update': 1,
                    'contract_id': contract_id,
                    'limit_order': {
                        'stop_loss': sl_amount,
                        'take_profit': tp_amount,
                    },
                })
                if retry_resp is None or 'error' in retry_resp:
                    retry_err = (retry_resp or {}).get('error', {}).get('message', 'No response')
                    retry_code = (retry_resp or {}).get('error', {}).get('code', '')

                    # Only trigger kill-switch for critical errors (e.g. InsufficientFunds)
                    # For invalid proposal / parameter errors, just skip this symbol
                    is_critical = 'InsufficientFund' in retry_code or 'InsufficientFund' in retry_err

                    if is_critical:
                        log_event(self.logger, logging.CRITICAL, symbol,
                                  f'SL/TP retry failed (CRITICAL): {retry_err} — '
                                  f'sending emergency close and activating kill-switch')
                        sell_resp = await self.client.send({'sell': contract_id, 'price': 0})
                        if sell_resp and 'error' in sell_resp:
                            log_event(self.logger, logging.CRITICAL, symbol,
                                      f'Emergency sell failed: {sell_resp["error"].get("message")}')
                        self.state.trading_enabled = False
                        self.state.save_daily_stats()
                        self.telegram.send(
                            f'<b>KILL SWITCH ACTIVATED</b>\n'
                            f'Symbol    : {symbol}\n'
                            f'Contract  : {contract_id}\n'
                            f'Reason    : {retry_err}'
                        )
                        return
                    else:
                        log_event(self.logger, logging.WARNING, symbol,
                                  f'SL/TP retry also failed: {retry_err} — '
                                  f'skipping symbol (no kill-switch)')
                        self.telegram.send(
                            f'<b>SL/TP FAILED — SKIPPED</b>\n'
                            f'Symbol    : {symbol}\n'
                            f'Contract  : {contract_id}\n'
                            f'Reason    : {retry_err}'
                        )
                        self.state.set_state(symbol, COOLDOWN)
                        self.state.cooldown_until[symbol] = \
                            time.time() + (config.COOLDOWN_MINUTES * 60)
                        return

            # ── Step 4: Subscribe to contract updates for close tracking ──
            await self.client.subscribe_to_contract(contract_id)

            # ── Step 5: Record position in state ──
            position = {
                'contract_id':   contract_id,
                'symbol':        symbol,
                'direction':     direction,
                'contract_type': contract_type,
                'entry_price':   buy_data.get('start_spot', entry_price),
                'stake':         stake,
                'sl':            sl,
                'tp':            tp,
                'order_block':   order_block,
                'status':        'open',
            }
            self.state.open_positions[symbol] = position
            self.state.save_open_positions()
            self.state.set_state(symbol, IN_TRADE)

            msg = (
                f'<b>TRADE OPENED</b>\n'
                f'Symbol    : {symbol}\n'
                f'Direction : {direction}\n'
                f'Entry     : {entry_price:.5f}\n'
                f'SL        : {sl:.5f}\n'
                f'TP        : {tp:.5f}\n'
                f'Stake     : ${stake:.2f}\n'
                f'Contract  : {contract_id}'
            )
            self.telegram.send(msg)

        except Exception as exc:
            log_event(self.logger, logging.ERROR, symbol,
                      f'Failed to open trade: {exc}')
            self.telegram.send(
                f'<b>TRADE ERROR</b>\nSymbol: {symbol}\nReason: {exc}'
            )
            # Reset to COOLDOWN so the bot pauses before scanning again
            # rather than hammering the API with repeated failed attempts.
            self.state.set_state(symbol, COOLDOWN)
            self.state.cooldown_until[symbol] = \
                time.time() + (config.COOLDOWN_MINUTES * 60)

    # ── Position updates ──────────────────────────────────

    async def handle_position_update(self, symbol: str, contract_data: dict):
        """
        Called when a proposal_open_contract update is received.

        If the contract is now closed (won or loss), updates state, records the
        outcome in the RiskManager, and sends a Telegram notification.
        """
        position = self.state.open_positions.get(symbol)
        if position is None:
            return

        contract_id = contract_data.get('contract_id')
        stored_id   = position.get('contract_id')
        if contract_id and stored_id and contract_id != stored_id:
            return

        status = contract_data.get('status', '')

        if status in ('won', 'lost', 'sold'):
            profit   = float(contract_data.get('profit', 0))
            pnl_sign = '+' if profit >= 0 else ''

            log_event(self.logger, TRADE_LEVEL, symbol,
                      f'Contract {contract_id} {status.upper()} | P&L: {pnl_sign}{profit:.2f}')

            # Record outcome in risk manager for daily loss tracking
            if status == 'won' or profit > 0:
                self.risk.record_win()
                outcome_emoji = 'WIN ✅'
            else:
                self.risk.record_loss()
                outcome_emoji = 'LOSS ❌'

            self.state.open_positions[symbol] = None
            self.state.save_open_positions()
            self.state.h4_sweep_candle[symbol] = None
            self.state.h4_swing_level[symbol] = None
            self.state.h4_liquidity_target[symbol] = None
            self.state.set_state(symbol, COOLDOWN)

            msg = (
                f'<b>TRADE CLOSED — {outcome_emoji}</b>\n'
                f'Symbol    : {symbol}\n'
                f'Direction : {position.get("direction", "?")}\n'
                f'P&amp;L      : {pnl_sign}{profit:.2f}\n'
                f'Contract  : {contract_id}'
            )
            self.telegram.send(msg)
