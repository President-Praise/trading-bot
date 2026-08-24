import asyncio


class BalanceStore:
    """Thread-safe (asyncio-safe) wrapper around the account balance float.

    Writers must call ``await store.set(value)``, which acquires the lock
    before mutating.  Readers that need an atomic snapshot for stake
    calculation should do::

        async with config.ACCOUNT_BALANCE.lock:
            balance = config.ACCOUNT_BALANCE.value
            stake   = calculate_stake(..., balance)

    Plain reads of ``.value`` (e.g. for logging or guards) are fine without
    the lock because asyncio is single-threaded — no context switch can occur
    between two Python statements.
    """

    def __init__(self, initial: float = 0.0) -> None:
        self._value: float = initial
        self.lock: asyncio.Lock = asyncio.Lock()

    @property
    def value(self) -> float:
        return self._value

    async def set(self, value: float) -> None:
        async with self.lock:
            self._value = value

    # Allow float() coercion so legacy log f-strings still work.
    def __float__(self) -> float:
        return self._value

    def __format__(self, spec: str) -> str:
        return format(self._value, spec)


# Global Trading Settings
WS_ENDPOINT = "wss://ws.binaryws.com/websockets/v3"
TIMEFRAMES = {'D': 86400, 'H4': 14400, 'M5': 300}
BUFFER = {'D': 50, 'H4': 100, 'M5': 500}
SYMBOLS = ['R_25', 'R_50', 'R_75', 'R_100', '1HZ100V', 'JD75', '1HZ10V']
SYMBOL_MULTIPLIERS = {
    'R_25': 200, 'R_50': 200, 'R_75': 200,
    'R_100': 200, '1HZ100V': 400,
    'JD75': 75, '1HZ10V': 400,
}

# Strategy Settings
DAILY_BIAS_LOOKBACK = 5
SWING_LOOKBACK_4H = 20
SWING_LOOKBACK_M15 = 40
M15_ENTRY_WINDOW = 20
LIQUIDITY_TOLERANCE = 0.05
MIN_WICK_BODY_RATIO = 0.2
REQUIRE_ORDER_BLOCK = True
RR_MODE = 4

# Risk Management
ACCOUNT_BALANCE = BalanceStore(0.0)
RISK_PER_TRADE = 0.02
MAX_CONCURRENT_TRADES = 3
MAX_DAILY_LOSSES = 2
COOLDOWN_MINUTES = 60

# Connection & Technical
SUBSCRIBE_DELAY = 0.5
RECONNECT_DELAY = 5
RECONNECT_MAX = 10
HEARTBEAT_SECS = 20
