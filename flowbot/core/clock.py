"""Bar-clock helpers.

Everything in the bot is anchored to the 15m grid (00:00, 00:15, ...), which is
exactly how the venues bucket their own klines. Keeping one implementation of
that arithmetic here means the live aggregator, the backtester and the UI all
agree on which bar a timestamp belongs to.
"""

from __future__ import annotations

from datetime import datetime, timezone

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def interval_ms(interval: str) -> int:
    """Parse '15m' / '1h' / '1d' into milliseconds."""
    unit = interval[-1].lower()
    value = int(interval[:-1])
    if unit == "s":
        return value * 1000
    if unit == "m":
        return value * MINUTE_MS
    if unit == "h":
        return value * HOUR_MS
    if unit == "d":
        return value * DAY_MS
    raise ValueError(f"unsupported interval: {interval}")


def bar_open(ts: int, step_ms: int) -> int:
    """Open time of the bar containing `ts`."""
    return ts - (ts % step_ms)


def bar_close(ts: int, step_ms: int) -> int:
    """Close time (exclusive edge) of the bar containing `ts`."""
    return bar_open(ts, step_ms) + step_ms


def ms_to_bar_close(ts: int, step_ms: int) -> int:
    return bar_close(ts, step_ms) - ts


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M")


def day_start(ts: int) -> int:
    return bar_open(ts, DAY_MS)
