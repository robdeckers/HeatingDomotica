from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heating_optimizer.prices.base import PricePoint, extract_price_entries, points_from_raw
from heating_optimizer.prices.cache import PriceCache

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)  # 14:00 CET


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def quarters(start, prices):
    return [
        PricePoint(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), p)
        for i, p in enumerate(prices)
    ]


def test_points_from_raw_hourly_and_end_times():
    raw = [
        {"timestamp": "2026-10-27 13:00:00+00:00", "price": 0.2},
        {"timestamp": "2026-10-27 12:00:00+00:00", "price": 0.1},
        {"timestamp": "2026-10-27 14:00:00+00:00", "price": 0.3},
    ]
    points = points_from_raw(raw)
    assert [p.price_incl_vat for p in points] == [0.1, 0.2, 0.3]
    assert points[0].end == utc(2026, 10, 27, 13)
    assert points[-1].end == utc(2026, 10, 27, 15)  # last point lasts the typical spacing


def test_points_from_raw_uses_explicit_start_and_end():
    raw = [
        {
            "price": 1.24,
            "timestamp": "2026-10-31 05:00:00+00:00",
            "start": "2026-10-31 05:00:00+00:00",
            "end": "2026-11-01 05:00:00+00:00",
        },
        {
            "price": 1.18,
            "timestamp": "2026-10-30 05:00:00+00:00",
            "start": "2026-10-30 05:00:00+00:00",
            "end": "2026-10-31 05:00:00+00:00",
        },
    ]
    points = points_from_raw(raw)
    assert [p.price_incl_vat for p in points] == [1.18, 1.24]
    assert points[1].end == utc(2026, 11, 1, 5)
    assert points[1].duration_minutes == 24 * 60


def test_points_from_raw_quarters():
    raw = [{"timestamp": (utc(2026, 10, 27, 13) + timedelta(minutes=15 * i)).isoformat(), "price": i} for i in range(4)]
    points = points_from_raw(raw)
    assert all(p.duration_minutes == 15 for p in points)


def test_points_from_raw_naive_timestamp_is_utc():
    assert points_from_raw([{"timestamp": "2026-10-27T13:00:00", "price": 1}])[0].start == utc(2026, 10, 27, 13)


@pytest.mark.parametrize(
    "response",
    [
        {"prices": [{"timestamp": "2026-10-27T13:00:00+00:00", "price": 1}]},
        {"response": {"prices": [{"timestamp": "2026-10-27T13:00:00+00:00", "price": 1}]}},
        {"success": True, "result": {"response": {"prices": [{"timestamp": "2026-10-27T13:00:00+00:00", "price": 1}]}}},
    ],
)
def test_extract_price_entries_handles_wrapping(response):
    assert len(extract_price_entries(response)) == 1


def test_extract_price_entries_none_when_absent():
    assert extract_price_entries({"result": {"foo": 1}}) is None


def test_hourly_lookup():
    cache = PriceCache()
    cache.update([PricePoint(utc(2026, 10, 27, 13), utc(2026, 10, 27, 14), 0.25)], NOW)
    lookup = cache.electricity_at(utc(2026, 10, 27, 13, 37), "hour")
    assert lookup.price_incl_vat == 0.25
    assert lookup.applies_from == utc(2026, 10, 27, 13)
    assert cache.electricity_at(utc(2026, 10, 27, 14, 0), "hour") is None


def test_lookup_exactly_at_hour_boundary_uses_new_hour():
    cache = PriceCache()
    cache.update(
        [
            PricePoint(utc(2026, 10, 27, 12), utc(2026, 10, 27, 13), 0.1),
            PricePoint(utc(2026, 10, 27, 13), utc(2026, 10, 27, 14), 0.2),
        ],
        NOW,
    )
    assert cache.electricity_at(utc(2026, 10, 27, 13, 0, 0), "hour").price_incl_vat == 0.2


def test_quarters_averaged_into_hour():
    cache = PriceCache()
    cache.update(quarters(utc(2026, 10, 27, 13), [0.10, 0.20, 0.30, 0.40]), NOW)
    lookup = cache.electricity_at(utc(2026, 10, 27, 13, 50), "hour")
    assert lookup.price_incl_vat == pytest.approx(0.25)
    assert lookup.resolution == "hour"


def test_hour_with_missing_quarter_is_missing():
    cache = PriceCache()
    cache.update(quarters(utc(2026, 10, 27, 13), [0.10, 0.20, 0.30]), NOW)
    assert cache.electricity_at(utc(2026, 10, 27, 13, 10), "hour") is None


def test_quarter_resolution():
    cache = PriceCache()
    cache.update(quarters(utc(2026, 10, 27, 13), [0.10, 0.20, 0.30, 0.40]), NOW)
    lookup = cache.electricity_at(utc(2026, 10, 27, 13, 35), "quarter")
    assert lookup.price_incl_vat == 0.30
    assert lookup.applies_from == utc(2026, 10, 27, 13, 30)


def test_quarter_resolution_with_hourly_source():
    cache = PriceCache()
    cache.update([PricePoint(utc(2026, 10, 27, 13), utc(2026, 10, 27, 14), 0.25)], NOW)
    assert cache.electricity_at(utc(2026, 10, 27, 13, 35), "quarter").price_incl_vat == 0.25


def _gas_points():
    # EnergyZero repeats the gas-day price per hour; gas day 2026-10-26 runs 06:00 CET (05:00 UTC) → 06:00 next day.
    points = []
    for day, price in ((date(2026, 10, 26), 0.50), (date(2026, 10, 27), 0.60)):
        start = datetime(day.year, day.month, day.day, 5, tzinfo=UTC)
        points += [PricePoint(start + timedelta(hours=h), start + timedelta(hours=h + 1), price) for h in range(24)]
    return points


def test_gas_day_lookup_before_six_uses_previous_day():
    cache = PriceCache()
    cache.update(_gas_points(), NOW)
    assert cache.gas_for_day(date(2026, 10, 26), AMS).price_incl_vat == 0.50
    assert cache.gas_for_day(date(2026, 10, 27), AMS).price_incl_vat == 0.60
    assert cache.gas_for_day(date(2026, 10, 28), AMS) is None


def test_covers_local_day():
    cache = PriceCache()
    start = datetime(2026, 10, 27, 23, tzinfo=UTC)  # 2026-10-28 00:00 CET
    cache.update([PricePoint(start + timedelta(hours=h), start + timedelta(hours=h + 1), 0.1) for h in range(24)], NOW)
    assert cache.covers_local_day(date(2026, 10, 28), AMS)
    cache2 = PriceCache()
    cache2.update([PricePoint(start + timedelta(hours=h), start + timedelta(hours=h + 1), 0.1) for h in range(23)], NOW)
    assert not cache2.covers_local_day(date(2026, 10, 28), AMS)


def test_covers_25_hour_fall_back_day():
    cache = PriceCache()
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)  # 2026-10-25 00:00 CEST
    cache.update([PricePoint(start + timedelta(hours=h), start + timedelta(hours=h + 1), 0.1) for h in range(25)], NOW)
    assert cache.covers_local_day(date(2026, 10, 25), AMS)


def test_prune_drops_old_points():
    cache = PriceCache()
    cache.update([PricePoint(utc(2026, 10, 25, h), utc(2026, 10, 25, h + 1), 0.1) for h in range(5)], NOW)
    cache.prune(utc(2026, 10, 25, 3))
    assert len(cache) == 2
