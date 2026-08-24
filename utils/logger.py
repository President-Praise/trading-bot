import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

# ── Custom log levels ─────────────────────────────────────
TRADE_LEVEL = 25
RISK_LEVEL  = 26
STATE_LEVEL = 27

logging.addLevelName(TRADE_LEVEL, 'TRADE')
logging.addLevelName(RISK_LEVEL,  'RISK')
logging.addLevelName(STATE_LEVEL, 'STATE')

# ── Ensure logs directory exists ──────────────────────────
LOG_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'bot.log')
os.makedirs(LOG_DIR, exist_ok=True)


class UTCFormatter(logging.Formatter):
    """Custom formatter that prints timestamps in UTC."""

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def format(self, record):
        record.message = record.getMessage()
        asctime = self.formatTime(record)
        level   = record.levelname
        msg     = record.message
        symbol_col = getattr(record, '_symbol', None)
        if symbol_col is not None:
            line = f'[{asctime} UTC] {level:<5} | {symbol_col:<6} | {msg}'
        else:
            line = f'[{asctime} UTC] {level:<5} | {msg}'
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += '\n' + record.exc_text
        return line


def _configure_root_logger() -> None:
    """Attach handlers to the 'trading_bot' root logger exactly once."""
    root = logging.getLogger('trading_bot')
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    root.propagate = False
    formatter = UTCFormatter()

    # ── Rotating file handler ─────────────────────────────
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8',
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # ── Console handler ───────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Return a named child logger under 'trading_bot'.

    Handlers live only on the 'trading_bot' root logger so they are attached
    once and child loggers never produce duplicate lines.  Each caller gets
    its own logger (e.g. 'trading_bot.trade_manager') for per-module filtering.
    """
    _configure_root_logger()
    return logging.getLogger(f'trading_bot.{name}')


def log_event(logger: logging.Logger, level: int | str, symbol: str, message: str):
    """
    Emit a log record with the symbol field embedded in the format.

    level can be an int (e.g. logging.INFO, TRADE_LEVEL) or a string
    such as 'INFO', 'DEBUG', 'TRADE', 'RISK', 'STATE'.

    Fix #13: use logging.getLevelName() which correctly resolves
    registered custom level names, replacing the broken getattr fallback.
    """
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        # getLevelName returns the int if the name is registered,
        # or "Level %s" string if not.  Fall back to INFO only on failure.
        level = resolved if isinstance(resolved, int) else logging.INFO

    extra = {'_symbol': symbol}
    logger.log(level, message, extra=extra, stacklevel=2)
