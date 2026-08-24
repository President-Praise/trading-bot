import asyncio
from datetime import datetime, timezone, timedelta


def utc_now() -> datetime:
    """Return current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def seconds_until_next_daily_reset() -> float:
    """Return seconds until next 00:01 UTC."""
    now = utc_now()
    # Build today's 00:01 UTC
    reset = now.replace(hour=0, minute=1, second=0, microsecond=0)
    if reset <= now:
        reset = reset + timedelta(days=1)
    return (reset - now).total_seconds()


async def wait_until_daily_reset():
    """Async sleep until the next 00:01 UTC.  Callers invoke in a loop."""
    secs = seconds_until_next_daily_reset()
    await asyncio.sleep(secs)
