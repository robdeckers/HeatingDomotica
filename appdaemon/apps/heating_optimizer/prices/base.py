"""Price data types and the provider protocol."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class Commodity(StrEnum):
    ELECTRICITY = "electricity"
    GAS = "gas"


@dataclass(frozen=True)
class PricePoint:
    """Market price incl. VAT for the interval [start, end). Timestamps are aware, stored in UTC."""

    start: datetime
    end: datetime
    price_incl_vat: float

    def covers(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    @property
    def duration_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass(frozen=True)
class SensorReading:
    """Current-price sensor value, used only as a fallback when the cache has no point for now."""

    price_incl_vat: float
    last_updated: datetime


class PriceProvider(Protocol):
    def fetch(self, commodity: Commodity, start: datetime, end: datetime) -> list[PricePoint]:
        """Market prices incl. VAT covering [start, end). Raises on failure."""
        ...

    def read_sensor(self, commodity: Commodity) -> SensorReading | None:
        """Current-price sensor (incl. VAT), or None if unavailable."""
        ...


def _parse_ts(value: Any) -> datetime:
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def points_from_raw(entries: Iterable[Mapping[str, Any]]) -> list[PricePoint]:
    """Build price points from ``[{"timestamp" | "start": ..., "end": ..., "price": ...}]``.

    Current EnergyZero responses give explicit ``start``/``end`` (one point per gas day for gas). When only start
    times are given, each point ends at the next point's start when that is at most one hour later; otherwise
    (last point, or a gap) the point lasts as long as the typical spacing (15 or 60 minutes).
    """
    entries = list(entries)
    if entries and all("start" in e and "end" in e for e in entries):
        points = [PricePoint(_parse_ts(e["start"]), _parse_ts(e["end"]), float(e["price"])) for e in entries]
        return sorted(points, key=lambda p: p.start)
    parsed = sorted(((_parse_ts(e["timestamp"]), float(e["price"])) for e in entries), key=lambda item: item[0])
    if not parsed:
        return []
    gaps = [b[0] - a[0] for a, b in zip(parsed, parsed[1:], strict=False) if b[0] > a[0]]
    spacing = min(gaps) if gaps else timedelta(hours=1)
    spacing = min(max(spacing, timedelta(minutes=15)), timedelta(hours=1))
    points = []
    for i, (start, price) in enumerate(parsed):
        nxt = parsed[i + 1][0] if i + 1 < len(parsed) else None
        end = nxt if nxt is not None and start < nxt <= start + timedelta(hours=1) else start + spacing
        points.append(PricePoint(start, end, price))
    return points


def extract_price_entries(result: Any, depth: int = 0) -> list[Mapping[str, Any]] | None:
    """Find the ``prices`` list in a service response, whatever wrapping AppDaemon/HA put around it."""
    if depth > 5:
        return None
    if isinstance(result, Mapping):
        prices = result.get("prices")
        if isinstance(prices, list):
            return prices
        for value in result.values():
            found = extract_price_entries(value, depth + 1)
            if found is not None:
                return found
    return None
