from datetime import UTC, datetime, timedelta

import pytest
from fakes import make_config

from heating_optimizer.devices.safety import SafetyMonitor, expected_power_w

NOW = datetime(2026, 10, 27, 13, 5, tzinfo=UTC)


@pytest.fixture
def monitor():
    return SafetyMonitor(make_config().house.plugs[0])


def test_expected_power_scales_with_voltage_squared():
    assert expected_power_w(3000, 230) == pytest.approx(3000)
    assert expected_power_w(3000, 240) == pytest.approx(3266.5, abs=0.1)
    assert expected_power_w(3000, None) == pytest.approx(3000)


def test_3200w_at_238v_does_not_trip_on_power(monitor):
    # A healthy 3000 W heater at 238 V: 3212 W is above the plug's 3000 W rating, but must not trip on power.
    assert monitor.evaluate(NOW, 3212, 12.9, 238, 45) is None


def test_healthy_3000w_heater_above_235v_trips_the_13a_limit(monitor):
    # Known consequence of running 3000 W heaters on a 13 A plug: at 238 V the heater draws 13.5 A continuously,
    # so the configured 13 A limit trips after overcurrent_min_duration_s. See README "3000 W heaters".
    assert monitor.evaluate(NOW, 3212, 13.5, 238, 45) is None
    assert monitor.evaluate(NOW + timedelta(seconds=10), 3212, 13.5, 238, 45) is not None


def test_short_overcurrent_does_not_trip(monitor):
    assert monitor.evaluate(NOW, 3200, 13.5, 238, 45) is None
    assert monitor.evaluate(NOW + timedelta(seconds=9), 3200, 13.5, 238, 45) is None
    assert monitor.evaluate(NOW + timedelta(seconds=10), 3000, 12.9, 232, 45) is None  # back below: resets
    assert monitor.evaluate(NOW + timedelta(seconds=15), 3200, 13.5, 238, 45) is None


def test_sustained_overcurrent_trips(monitor):
    assert monitor.evaluate(NOW, 3200, 13.5, 238, 45) is None
    reason = monitor.evaluate(NOW + timedelta(seconds=10), 3200, 13.6, 238, 45)
    assert reason is not None and "current 13.60 A" in reason


def test_overtemperature_trips_immediately(monitor):
    assert "temperature" in monitor.evaluate(NOW, 3000, 13.0, 230, 71)


def test_wrong_appliance_overpower_trips(monitor):
    # Expected at 230 V is 3000 W; +15 % = 3450 W.
    assert monitor.evaluate(NOW, 3449, 12.0, 230, 40) is None
    assert "power 3460 W" in monitor.evaluate(NOW, 3460, 12.0, 230, 40)


def test_overpower_limit_follows_voltage(monitor):
    # At 240 V the limit is 3266.5 × 1.15 ≈ 3756 W.
    assert monitor.evaluate(NOW, 3700, 12.0, 240, 40) is None
    assert monitor.evaluate(NOW, 3800, 12.0, 240, 40) is not None


def test_missing_readings_do_not_trip(monitor):
    assert monitor.evaluate(NOW, None, None, None, None) is None
