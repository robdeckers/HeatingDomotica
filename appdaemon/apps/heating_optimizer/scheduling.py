"""Pure clock-alignment and calendar helpers.

Ticks are aligned to multiples of ``interval_minutes`` past the hour. The Dutch UTC offset is a whole number of hours,
so aligning in UTC equals aligning in local time, and a fixed-interval schedule stays aligned across DST transitions.
Hour changes and gas days are evaluated in local time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

GAS_DAY_START = time(6, 0)


def _require_aware(ts: datetime) -> None:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")


def next_aligned_tick(now: datetime, interval_minutes: int) -> datetime:
    """First aligned tick strictly after ``now``, in the time zone of ``now``."""
    _require_aware(now)
    interval_s = interval_minutes * 60
    epoch = now.timestamp()
    tick = (int(epoch // interval_s) + 1) * interval_s
    return datetime.fromtimestamp(tick, tz=UTC).astimezone(now.tzinfo)


def nearest_aligned_tick(now: datetime, interval_minutes: int) -> datetime:
    """Aligned tick closest to ``now``: the tick that a callback firing slightly early or late belongs to."""
    _require_aware(now)
    interval_s = interval_minutes * 60
    tick = round(now.timestamp() / interval_s) * interval_s
    return datetime.fromtimestamp(tick, tz=UTC).astimezone(now.tzinfo)


def elapsed_s(later: datetime, earlier: datetime) -> float:
    """Elapsed seconds. Subtracting aware datetimes that share a tzinfo is wall-clock based, so use timestamps."""
    return later.timestamp() - earlier.timestamp()


def drift_ms(actual: datetime, scheduled: datetime) -> float:
    return elapsed_s(actual, scheduled) * 1000.0


def hour_start(ts: datetime) -> datetime:
    """Start of the hour containing ``ts`` (whole-hour UTC offsets, so UTC truncation equals local truncation)."""
    _require_aware(ts)
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0).astimezone(ts.tzinfo)


def quarter_start(ts: datetime) -> datetime:
    _require_aware(ts)
    utc = ts.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0).astimezone(ts.tzinfo)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Start and end of a local calendar day (23 or 25 hours on DST transition days)."""
    return local_midnight(day, tz), local_midnight(day + timedelta(days=1), tz)


def gas_day_of(ts: datetime, tz: ZoneInfo) -> date:
    """Gas day containing ``ts``: a gas day runs from 06:00 local time to 06:00 the next day."""
    _require_aware(ts)
    local = ts.astimezone(tz)
    return local.date() if local.time() >= GAS_DAY_START else local.date() - timedelta(days=1)


def gas_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, GAS_DAY_START, tzinfo=tz),
        datetime.combine(day + timedelta(days=1), GAS_DAY_START, tzinfo=tz),
    )


def add_elapsed(ts: datetime, delta: timedelta) -> datetime:
    """Add elapsed (physical) time. Aware-datetime arithmetic in Python is wall-clock arithmetic, wrong across DST."""
    _require_aware(ts)
    return (ts.astimezone(UTC) + delta).astimezone(ts.tzinfo)


def next_full_hour(ts: datetime) -> datetime:
    """Start of the next hour, in UTC (a local time in the repeated DST hour never compares equal across zones)."""
    return add_elapsed(hour_start(ts), timedelta(hours=1)).astimezone(UTC)


def at_local_time(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)
