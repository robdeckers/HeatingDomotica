"""Pure price cache keyed by timestamp, with hour averaging and gas-day lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from heating_optimizer.prices.base import PricePoint
from heating_optimizer.scheduling import gas_day_bounds, hour_start, local_day_bounds


@dataclass(frozen=True)
class PriceLookup:
    """Price applicable at a moment, with the interval it applies to."""

    price_incl_vat: float
    applies_from: datetime
    applies_to: datetime
    resolution: str  # "hour", "quarter", or "gas_day"


class PriceCache:
    """Price points of one commodity, keyed by start time (UTC)."""

    def __init__(self) -> None:
        self._points: dict[datetime, PricePoint] = {}
        self.last_refresh: datetime | None = None

    def __len__(self) -> int:
        return len(self._points)

    def update(self, points: list[PricePoint], fetched_at: datetime) -> None:
        for point in points:
            self._points[point.start] = point
        self.last_refresh = fetched_at

    def prune(self, before: datetime) -> None:
        """Drop points that ended before ``before``."""
        self._points = {start: p for start, p in self._points.items() if p.end > before}

    def points_between(self, start: datetime, end: datetime) -> list[PricePoint]:
        return sorted((p for p in self._points.values() if start <= p.start < end), key=lambda p: p.start)

    def point_at(self, ts: datetime) -> PricePoint | None:
        utc = ts.astimezone(UTC)
        return next((p for p in self._points.values() if p.covers(utc)), None)

    def electricity_at(self, ts: datetime, resolution: str) -> PriceLookup | None:
        """Electricity price at ``ts``.

        ``hour``: the hourly point, or the average of the four quarter-hour points of the hour (None if any is
        missing). ``quarter``: the point covering ``ts`` (an hourly point also covers each of its quarters).
        """
        if resolution == "quarter":
            point = self.point_at(ts)
            if point is None:
                return None
            return PriceLookup(point.price_incl_vat, point.start, point.end, "quarter")

        start = hour_start(ts).astimezone(UTC)
        end = start + timedelta(hours=1)
        points = self.points_between(start, end)
        hourly = [p for p in points if p.start == start and p.end >= end]
        if hourly:
            return PriceLookup(hourly[0].price_incl_vat, start, end, "hour")
        covered = sum(p.duration_minutes for p in points)
        if len(points) < 4 or covered < 60:
            return None
        weighted = sum(p.price_incl_vat * p.duration_minutes for p in points) / covered
        return PriceLookup(weighted, start, end, "hour")

    def gas_for_day(self, gas_day: date, tz: ZoneInfo) -> PriceLookup | None:
        """Gas price for a gas day (06:00–06:00 local). The source repeats the day price per hour; take the first."""
        start, end = gas_day_bounds(gas_day, tz)
        points = self.points_between(start.astimezone(UTC), end.astimezone(UTC))
        if not points:
            return None
        return PriceLookup(points[0].price_incl_vat, start, end, "gas_day")

    def covers_local_day(self, day: date, tz: ZoneInfo) -> bool:
        """True if the points cover the whole local calendar day (used to check that tomorrow is loaded)."""
        start, end = local_day_bounds(day, tz)
        return self._covers(start.astimezone(UTC), end.astimezone(UTC))

    def _covers(self, start: datetime, end: datetime) -> bool:
        cursor = start
        for point in sorted(self._points.values(), key=lambda p: p.start):
            if point.end <= cursor:
                continue
            if point.start > cursor:
                return False
            cursor = point.end
            if cursor >= end:
                return True
        return cursor >= end
