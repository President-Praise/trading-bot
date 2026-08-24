import os
import json
import logging
from datetime import datetime, timezone

import config
from utils.logger import get_logger, log_event, STATE_LEVEL

# ── State constants ───────────────────────────────────────
IDLE          = 'IDLE'
BIAS_SET      = 'BIAS_SET'
H4_CONFIRMED  = 'H4_CONFIRMED'
SIGNAL_READY  = 'SIGNAL_READY'
IN_TRADE      = 'IN_TRADE'
COOLDOWN      = 'COOLDOWN'

# ── File paths ────────────────────────────────────────────
_BASE_DIR            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR            = os.path.join(_BASE_DIR, 'state')
DAILY_STATS_FILE     = os.path.join(STATE_DIR, 'daily_stats.json')
CONSUMED_LEVELS_FILE = os.path.join(STATE_DIR, 'consumed_levels.json')
OPEN_POSITIONS_FILE  = os.path.join(STATE_DIR, 'open_positions.json')
SYMBOL_SESSION_FILE  = os.path.join(STATE_DIR, 'symbol_session.json')


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


class StateManager:
    """Single source of truth for all bot runtime state."""

    def __init__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.logger = get_logger('state_manager')

        # ── Core flags ────────────────────────────────────
        self.trading_enabled: bool = True
        self.reconnecting:    bool = False
        self.balance_ready:   bool = False

        # ── Daily counters ────────────────────────────────
        self.daily_loss_count: int = 0
        self.daily_win_count:  int = 0

        # ── Per-symbol state ──────────────────────────────
        self.symbol_states:    dict = {}
        self.open_positions:   dict = {}
        self.consumed_levels:  dict = {}
        self.symbol_ready:     dict = {}

        # ── Bias & swing cache ────────────────────────────
        self.daily_bias:          dict = {}
        self.h4_swing_level:      dict = {}
        self.h4_sweep_candle:     dict = {}
        self.h4_liquidity_target: dict = {}   # ← NEW: 4H TP target level
        self.pending_signal:      dict = {}
        self.cooldown_until:      dict = {}
        self.last_entry_day:      dict = {}  # one-trade-per-day guard

        self.load_state()

    # ── Symbol initialisation ─────────────────────────────

    def init_symbol(self, symbol: str):
        """Initialise a symbol with default state (idempotent)."""
        self.symbol_states.setdefault(symbol, IDLE)
        self.consumed_levels.setdefault(symbol, [])
        self.symbol_ready.setdefault(symbol, False)
        self.open_positions.setdefault(symbol, None)
        self.daily_bias.setdefault(symbol, 'NONE')
        self.h4_swing_level.setdefault(symbol, None)
        self.h4_sweep_candle.setdefault(symbol, None)
        self.h4_liquidity_target.setdefault(symbol, None)   # ← NEW
        self.pending_signal.setdefault(symbol, None)
        self.cooldown_until.setdefault(symbol, 0)
        self.last_entry_day.setdefault(symbol, None)

    # ── State machine helpers ─────────────────────────────

    def set_state(self, symbol: str, state: str):
        if state == H4_CONFIRMED:
            bias = self.daily_bias.get(symbol, 'NONE')
            if bias == 'NONE':
                log_event(self.logger, logging.WARNING, symbol,
                          f'Prerequisite failed: cannot enter {H4_CONFIRMED} — daily_bias is NONE; resetting to IDLE')
                self.symbol_states[symbol] = IDLE
                self.save_symbol_session()
                return

        self.symbol_states[symbol] = state
        log_event(self.logger, STATE_LEVEL, symbol, f'State -> {state}')
        self.save_symbol_session()

    def get_state(self, symbol: str) -> str:
        return self.symbol_states.get(symbol, IDLE)

    # ── Consumed levels ───────────────────────────────────

    def mark_consumed(self, symbol: str, level: float, side: str):
        """Record a liquidity level as consumed and persist immediately."""
        self.consumed_levels.setdefault(symbol, [])

        for existing in self.consumed_levels[symbol]:
            if existing['side'] == side.lower() and abs(existing['level'] - level) <= 0.01:
                log_event(self.logger, logging.DEBUG, symbol,
                          f'Skipping duplicate consumed level {level:.5f} ({side})')
                return

        entry = {
            'level':     level,
            'side':      side.lower(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        self.consumed_levels[symbol].append(entry)
        log_event(self.logger, STATE_LEVEL, symbol,
                  f'Consumed level {level:.5f} ({side})')
        self.save_consumed_levels()

    def is_consumed(self, symbol: str, level: float, side: str,
                    tolerance: float = 0.01,
                    avg_range: float = None) -> bool:
        if avg_range is not None:
            threshold = config.LIQUIDITY_TOLERANCE * avg_range
        else:
            threshold = tolerance

        for entry in self.consumed_levels.get(symbol, []):
            if entry['side'] != side.lower():
                continue
            if abs(entry['level'] - level) <= threshold:
                return True
        return False

    # ── Daily counters ────────────────────────────────────

    def increment_loss(self):
        self.daily_loss_count += 1
        log_event(self.logger, logging.WARNING, '------',
                  f'Loss recorded — daily count: {self.daily_loss_count}/{config.MAX_DAILY_LOSSES}')
        if self.daily_loss_count >= config.MAX_DAILY_LOSSES:
            self.trading_enabled = False
            log_event(self.logger, logging.CRITICAL, '------',
                      'Kill switch activated — max daily losses reached')
        self.save_daily_stats()

    def increment_win(self):
        self.daily_win_count += 1
        log_event(self.logger, logging.INFO, '------',
                  f'Win recorded — daily count: {self.daily_win_count}')
        self.save_daily_stats()

    # ── Daily reset ───────────────────────────────────────

    def daily_reset(self):
        """Reset all daily counters and symbol states at 00:01 UTC.

        Symbols IN_TRADE or COOLDOWN are skipped to protect live positions.
        """
        self.daily_loss_count = 0
        self.daily_win_count  = 0
        self.trading_enabled  = True

        skipped = 0
        for symbol in list(self.symbol_states.keys()):
            if self.symbol_states[symbol] in (IN_TRADE, COOLDOWN):
                skipped += 1
                continue
            self.symbol_states[symbol]          = IDLE
            self.daily_bias[symbol]             = 'NONE'
            self.h4_swing_level[symbol]         = None
            self.h4_sweep_candle[symbol]        = None
            self.h4_liquidity_target[symbol]    = None   # ← NEW: clear on reset
            self.last_entry_day[symbol]         = None
            self.pending_signal.pop(symbol, None)

        for symbol in list(self.consumed_levels.keys()):
            if self.symbol_states.get(symbol) in (IN_TRADE, COOLDOWN):
                continue
            self.consumed_levels[symbol] = []

        if skipped:
            log_event(self.logger, STATE_LEVEL, '------',
                      f'Daily reset skipped {skipped} symbol(s) with active trade/cooldown')
        log_event(self.logger, STATE_LEVEL, '------', 'Daily reset complete')
        self.save_daily_stats()
        self.save_consumed_levels()
        self.save_symbol_session()

    # ── Persistence ───────────────────────────────────────

    def save_daily_stats(self):
        data = {
            'date':              _utc_today_str(),
            'daily_loss_count':  self.daily_loss_count,
            'daily_win_count':   self.daily_win_count,
            'trading_enabled':   self.trading_enabled,
        }
        try:
            with open(DAILY_STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            log_event(self.logger, logging.ERROR, '------',
                      f'Failed to save daily_stats.json: {exc}')

    def save_consumed_levels(self):
        try:
            with open(CONSUMED_LEVELS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.consumed_levels, f, indent=2)
        except OSError as exc:
            log_event(self.logger, logging.ERROR, '------',
                      f'Failed to save consumed_levels.json: {exc}')

    def save_open_positions(self):
        try:
            with open(OPEN_POSITIONS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.open_positions, f, indent=2)
        except OSError as exc:
            log_event(self.logger, logging.ERROR, '------',
                      f'Failed to save open_positions.json: {exc}')

    def save_symbol_session(self):
        """Persist per-symbol intraday state so a manual redeploy can resume."""
        data = {
            'date':                  _utc_today_str(),
            'symbol_states':         self.symbol_states,
            'daily_bias':            self.daily_bias,
            'h4_swing_level':        self.h4_swing_level,
            'h4_sweep_candle':       self.h4_sweep_candle,
            'h4_liquidity_target':   self.h4_liquidity_target,   # ← NEW: persisted
            'pending_signal':        self.pending_signal,
            'cooldown_until':        self.cooldown_until,
            'last_entry_day':        self.last_entry_day,
        }
        try:
            with open(SYMBOL_SESSION_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            log_event(self.logger, logging.ERROR, '------',
                      f'Failed to save symbol_session.json: {exc}')

    def load_state(self):
        """Load persisted state on startup. Resets if date mismatch or file corrupt."""
        today = _utc_today_str()

        # ── daily_stats.json ──────────────────────────────
        try:
            with open(DAILY_STATS_FILE, 'r', encoding='utf-8') as f:
                stats = json.load(f)
            if stats.get('date') != today:
                log_event(self.logger, logging.WARNING, '------',
                          'daily_stats.json is from a previous day — resetting counters')
                self.daily_loss_count = 0
                self.daily_win_count  = 0
                self.trading_enabled  = True
            else:
                self.daily_loss_count = stats.get('daily_loss_count', 0)
                self.daily_win_count  = stats.get('daily_win_count', 0)
                self.trading_enabled  = stats.get('trading_enabled', True)
                log_event(self.logger, logging.INFO, '------',
                          f'Loaded daily stats: losses={self.daily_loss_count} '
                          f'wins={self.daily_win_count} '
                          f'trading_enabled={self.trading_enabled}')
        except FileNotFoundError:
            log_event(self.logger, logging.WARNING, '------',
                      'daily_stats.json not found — starting fresh')
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log_event(self.logger, logging.WARNING, '------',
                      f'daily_stats.json corrupt ({exc}) — starting fresh')

        # ── consumed_levels.json ──────────────────────────
        try:
            with open(CONSUMED_LEVELS_FILE, 'r', encoding='utf-8') as f:
                self.consumed_levels = json.load(f)
            log_event(self.logger, logging.INFO, '------',
                      f'Loaded consumed levels for {len(self.consumed_levels)} symbol(s)')
        except FileNotFoundError:
            log_event(self.logger, logging.WARNING, '------',
                      'consumed_levels.json not found — starting fresh')
        except (json.JSONDecodeError, TypeError) as exc:
            log_event(self.logger, logging.WARNING, '------',
                      f'consumed_levels.json corrupt ({exc}) — starting fresh')

        # ── open_positions.json ───────────────────────────
        try:
            with open(OPEN_POSITIONS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for symbol, pos in saved.items():
                if isinstance(pos, dict) and pos.get('status') == 'open':
                    self.open_positions[symbol] = pos
            restored = sum(1 for p in self.open_positions.values() if p is not None)
            log_event(self.logger, logging.INFO, '------',
                      f'Restored {restored} open position(s) from disk')
        except FileNotFoundError:
            log_event(self.logger, logging.WARNING, '------',
                      'open_positions.json not found — starting fresh')
        except (json.JSONDecodeError, TypeError) as exc:
            log_event(self.logger, logging.WARNING, '------',
                      f'open_positions.json corrupt ({exc}) — starting fresh')

        # ── symbol_session.json ───────────────────────────
        try:
            with open(SYMBOL_SESSION_FILE, 'r', encoding='utf-8') as f:
                session = json.load(f)
            if session.get('date') != today:
                log_event(self.logger, logging.WARNING, '------',
                          'symbol_session.json is from a previous day — ignoring')
            else:
                self.symbol_states        = session.get('symbol_states',       {})
                self.daily_bias           = session.get('daily_bias',          {})
                self.h4_swing_level       = session.get('h4_swing_level',      {})
                self.h4_sweep_candle      = session.get('h4_sweep_candle',     {})
                self.h4_liquidity_target  = session.get('h4_liquidity_target', {})  # ← NEW
                self.pending_signal       = session.get('pending_signal',      {})
                self.cooldown_until       = session.get('cooldown_until',      {})
                self.last_entry_day       = session.get('last_entry_day',      {})
                log_event(self.logger, logging.INFO, '------',
                          f'Restored symbol session for: {list(self.symbol_states.keys())}')

                # Validate loaded state — detect and repair contradictions
                for symbol, sym_state in list(self.symbol_states.items()):
                    contradiction = None

                    if sym_state == H4_CONFIRMED:
                        if self.h4_swing_level.get(symbol) is None:
                            contradiction = f'{sym_state} but h4_swing_level is None'
                        elif self.daily_bias.get(symbol, 'NONE') == 'NONE':
                            contradiction = f'{sym_state} but daily_bias is NONE'

                    elif sym_state == BIAS_SET:
                        if self.daily_bias.get(symbol, 'NONE') in ('NONE', None):
                            contradiction = f'{sym_state} but daily_bias is NONE'

                    elif sym_state == IN_TRADE:
                        if self.open_positions.get(symbol) is None:
                            contradiction = f'{sym_state} but open_positions entry is None'

                    elif sym_state == SIGNAL_READY:
                        contradiction = f'{sym_state} on startup — signal no longer valid'

                    if contradiction:
                        valid_bias = self.daily_bias.get(symbol) in ('BUY', 'SELL')
                        if sym_state == SIGNAL_READY:
                            reset_to = H4_CONFIRMED
                        else:
                            reset_to = BIAS_SET if valid_bias else IDLE
                        log_event(self.logger, logging.WARNING, symbol,
                                  f'Contradictory state ({contradiction}) — resetting to {reset_to}')
                        self.symbol_states[symbol]       = reset_to
                        self.h4_swing_level[symbol]      = None
                        self.h4_sweep_candle[symbol]     = None
                        self.h4_liquidity_target[symbol] = None   # ← NEW: clear on contradiction
                        self.pending_signal.pop(symbol, None)

        except FileNotFoundError:
            log_event(self.logger, logging.WARNING, '------',
                      'symbol_session.json not found — starting fresh')
        except (json.JSONDecodeError, TypeError) as exc:
            log_event(self.logger, logging.WARNING, '------',
                      f'symbol_session.json corrupt ({exc}) — starting fresh')