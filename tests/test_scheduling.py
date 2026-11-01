from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heating_optimizer.scheduling import (
    add_elapsed,
    drift_ms,
    gas_day_bounds,
    gas_day_of,
    hour_start,
    local_day_bounds,
    nearest_aligned_tick,
    next_aligned_tick,
    next_full_hour,
    quarter_start,
)

AMS = ZoneInfo("Europe/Amsterdam")


def local(*args):
    return datetime(*args, tzinfo=AMS)


def test_next_tick_from_odd_minute():
    assert next_aligned_tick(local(2026, 10, 27, 14, 3, 27), 5) == local(2026, 10, 27, 14, 5)


def test_next_tick_just_before_the_hour():
    assert next_aligned_tick(local(2026, 10, 27, 14, 59, 59), 5) == local(2026, 10, 27, 15, 0)


def test_next_tick_is_strictly_after_a_boundary():
    assert next_aligned_tick(local(2026, 10, 27, 14, 5), 5) == local(2026, 10, 27, 14, 10)


def test_next_tick_before_midnight():
    assert next_aligned_tick(local(2026, 10, 27, 23, 58), 5) == local(2026, 10, 28, 0, 0)


@pytest.mark.parametrize("interval", [1, 5, 10, 15, 30, 60])
def test_ticks_land_on_multiples_in_local_time(interval):
    tick = next_aligned_tick(local(2026, 3, 29, 1, 2, 3), interval)
    assert tick.astimezone(AMS).minute % interval == 0
    assert tick.second == 0


def test_ticks_stay_aligned_across_spring_forward():
    # 2026-03-29 02:00 CET → 03:00 CEST. A fixed 5-minute interval from 01:55 lands on 03:00 local.
    start = local(2026, 3, 29, 1, 55)
    after = add_elapsed(start, timedelta(minutes=5))
    assert after.astimezone(AMS).strftime("%H:%M") == "03:00"
    assert next_aligned_tick(local(2026, 3, 29, 1, 57), 5).astimezone(AMS).strftime("%H:%M") == "03:00"


def test_ticks_stay_aligned_across_fall_back():
    # 2026-10-25 03:00 CEST → 02:00 CET. Every 5 minutes of elapsed time is still on a 5-minute local boundary.
    tick = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST
    for _ in range(36):
        tick = add_elapsed(tick, timedelta(minutes=5))
        assert tick.astimezone(AMS).minute % 5 == 0
    # 36 × 5 min = 3 h elapsed, but only 2 h of wall-clock time: 02:00 CEST → 04:00 CET
    assert tick.astimezone(AMS).strftime("%H:%M %Z") == "04:00 CET"


def test_nearest_tick_and_drift():
    actual = local(2026, 10, 27, 14, 5, 0, 800_000)
    scheduled = nearest_aligned_tick(actual, 5)
    assert scheduled == local(2026, 10, 27, 14, 5)
    assert drift_ms(actual, scheduled) == pytest.approx(800)
    early = local(2026, 10, 27, 14, 4, 59, 500_000)
    assert drift_ms(early, nearest_aligned_tick(early, 5)) == pytest.approx(-500)


def test_drift_across_fall_back_uses_elapsed_time():
    a = datetime(2026, 10, 25, 0, 59, tzinfo=UTC).astimezone(AMS)  # 02:59 CEST
    b = datetime(2026, 10, 25, 1, 1, tzinfo=UTC).astimezone(AMS)  # 02:01 CET
    assert drift_ms(b, a) == pytest.approx(120_000)


def test_hour_and_quarter_start():
    ts = local(2026, 10, 27, 14, 37, 12)
    assert hour_start(ts) == local(2026, 10, 27, 14, 0)
    assert quarter_start(ts) == local(2026, 10, 27, 14, 30)


def test_next_full_hour_on_fall_back():
    ts = datetime(2026, 10, 25, 0, 30, tzinfo=UTC).astimezone(AMS)  # 02:30 CEST
    assert next_full_hour(ts) == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 02:00 CET


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (local(2026, 10, 27, 5, 59), date(2026, 10, 26)),
        (local(2026, 10, 27, 6, 0), date(2026, 10, 27)),
        (local(2026, 10, 27, 0, 30), date(2026, 10, 26)),
        (local(2026, 10, 27, 23, 59), date(2026, 10, 27)),
    ],
)
def test_gas_day_boundary_at_six(ts, expected):
    assert gas_day_of(ts, AMS) == expected


def test_gas_day_bounds():
    start, end = gas_day_bounds(date(2026, 10, 27), AMS)
    assert start == local(2026, 10, 27, 6, 0)
    assert end == local(2026, 10, 28, 6, 0)


def test_local_day_lengths_on_dst_days():
    for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 10, 27), 24)):
        start, end = local_day_bounds(day, AMS)
        assert (end.timestamp() - start.timestamp()) / 3600 == hours


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        next_aligned_tick(datetime(2026, 1, 1), 5)
