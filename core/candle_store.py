import logging
from collections import deque

import config
from utils.logger import get_logger, log_event

_logger = get_logger('candle_store')


class CandleStore:
    """Thread-safe (asyncio) store for OHLC candle data across all symbols and timeframes."""

    def __init__(self):
        self.candles: dict = {}
        for symbol in config.SYMBOLS:
            self.candles[symbol] = {
                tf: deque(maxlen=config.BUFFER[tf])
                for tf in config.TIMEFRAMES
            }

    def add_candle(self, symbol: str, timeframe: str, candle: dict):
        """
        Add or update a candle.

        candle = {'time': int, 'open': float, 'high': float, 'low': float, 'close': float}

        If the last candle in the buffer shares the same epoch (forming candle), it is
        replaced in-place.  Otherwise the new candle is appended.
        """
        if symbol not in self.candles or timeframe not in self.candles[symbol]:
            log_event(_logger, logging.WARNING, '------',
                      f'add_candle: unknown symbol={symbol!r} or timeframe={timeframe!r} — ignoring')
            return
        buf = self.candles[symbol][timeframe]
        if buf and buf[-1]['time'] == candle['time']:
            buf[-1] = candle
        else:
            buf.append(candle)

    def get_candles(self, symbol: str, timeframe: str) -> list:
        """
        Return all buffered candles as a list (most-recent last).
        Index [-1] is the currently forming candle; [-2] is the last *closed* candle.
        """
        return list(self.candles[symbol][timeframe])

    def get_closed_candles(self, symbol: str, timeframe: str) -> list:
        """
        Return all candles except the last (still-forming) candle.
        Safe to use for indicator calculations.
        """
        candles = list(self.candles[symbol][timeframe])
        return candles[:-1] if len(candles) > 1 else []

    def is_ready(self, symbol: str) -> bool:
        """
        Return True when all three timeframe buffers hold enough candles to trade.

        Thresholds are derived from config.BUFFER so they scale automatically
        if buffer sizes change.  Each timeframe requires at least 1/4 of its
        buffer to be filled before the strategy is allowed to run; the floor of
        3 guards against misconfigured tiny buffers.
        """
        # 1/4 of each buffer is a reasonable warm-up minimum for indicator
        # calculations (EMA, swing-point detection, etc.).  Adjust the divisor
        # here if the strategy needs a longer or shorter warm-up period.
        READY_THRESHOLDS = {
            'D':  max(3, config.BUFFER['D']  // 4),   # e.g. 50  // 4 = 12
            'H4': max(3, config.BUFFER['H4'] // 4),   # e.g. 100 // 4 = 25
            'M5': max(3, config.BUFFER['M5'] // 4),   # e.g. 500 // 4 = 125
        }
        for tf, buf in self.candles[symbol].items():
            if len(buf) < READY_THRESHOLDS.get(tf, 3):
                return False
        return True
