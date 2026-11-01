from datetime import UTC, datetime, timedelta

import pytest
from fakes import LOG, FakePriceProvider, make_config

from heating_optimizer.prices.base import Commodity, SensorReading
from heating_optimizer.prices.service import PriceService

NOW = datetime(2026, 10, 27, 13, 5, tzinfo=UTC)  # 14:05 CET
TODAY_START = datetime(2026, 10, 26, 23, tzinfo=UTC)  # 2026-10-27 00:00 CET
GAS_START = datetime(2026, 10, 27, 5, tzinfo=UTC)  # gas day 2026-10-27 06:00 CET


@pytest.fixture
def provider():
    p = FakePriceProvider()
    p.set_hourly(Commodity.ELECTRICITY, TODAY_START, 24, 0.05)
    p.set_hourly(Commodity.GAS, GAS_START - timedelta(hours=24), 48, [0.55] * 24 + [0.60] * 24)
    return p


@pytest.fixture
def service(provider):
    s = PriceService(provider, make_config(), LOG)
    assert s.refresh(NOW)
    return s


def test_heat_prices_from_cache(service):
    heat = service.heat_prices(NOW)
    assert heat.complete
    assert heat.elec.source == "cache"
    assert heat.elec.consumer_incl_vat == pytest.approx(0.05 + 0.018 + 0.11085)
    assert heat.gas.consumer_incl_vat == pytest.approx(0.60 + 0.0768 + 0.7268)
    assert heat.gas_per_kwh_heat == pytest.approx((0.60 + 0.0768 + 0.7268) / 9.769 / 0.90)
    assert heat.delta == pytest.approx(heat.gas_per_kwh_heat - heat.elec_per_kwh_heat)
    assert heat.break_even_elec_consumer == pytest.approx(heat.gas_per_kwh_heat)


def test_gas_before_six_uses_previous_gas_day(provider):
    early = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)  # 05:30 CET
    service = PriceService(provider, make_config(), LOG)
    service.refresh(early)
    assert service.gas(early).consumer_incl_vat == pytest.approx(0.55 + 0.0768 + 0.7268)


def test_missing_prices_give_incomplete_heat_prices(provider):
    provider.fail = True
    service = PriceService(provider, make_config(), LOG)
    assert not service.refresh(NOW)
    heat = service.heat_prices(NOW)
    assert not heat.complete and heat.delta is None
    assert "EnergyZero down" in service.last_error


def test_sensor_fallback_when_cache_empty(provider):
    provider.fail = True
    provider.sensor[Commodity.ELECTRICITY] = SensorReading(0.07, NOW - timedelta(minutes=4))
    service = PriceService(provider, make_config(), LOG)
    service.refresh(NOW)
    quote = service.electricity(NOW)
    assert quote.source == "sensor"
    assert quote.consumer_incl_vat == pytest.approx(0.07 + 0.018 + 0.11085)


def test_sensor_fallback_from_previous_hour_is_rejected(provider):
    provider.fail = True
    provider.sensor[Commodity.ELECTRICITY] = SensorReading(0.07, NOW - timedelta(minutes=10))  # 13:55 UTC-ish hour
    service = PriceService(provider, make_config(), LOG)
    assert service.electricity(NOW) is None


def test_refresh_reason(service, provider):
    assert service.refresh_reason(NOW) is None
    late = datetime(2026, 10, 27, 14, 10, tzinfo=UTC)  # 15:10 CET: after the 15:05 refresh time
    assert service.refresh_reason(late) == "daily refresh"
    service.refresh(late)  # tomorrow still not available
    assert service.refresh_reason(late + timedelta(minutes=10)) is None
    assert service.refresh_reason(late + timedelta(minutes=30)) == "tomorrow's prices not loaded yet"
    provider.set_hourly(Commodity.ELECTRICITY, TODAY_START, 48, 0.05)
    service.refresh(late + timedelta(minutes=30))
    assert service.tomorrow_loaded(late)
    assert service.refresh_reason(late + timedelta(hours=2)) is None


def test_refresh_reason_when_current_hour_missing(service):
    tomorrow_noon = NOW + timedelta(days=1)
    assert service.refresh_reason(tomorrow_noon) == "current electricity price missing"


def test_failed_refresh_backs_off(provider):
    service = PriceService(provider, make_config(), LOG)
    provider.fail = True
    assert not service.refresh_if_needed(NOW)
    fetches = provider.fetches
    assert not service.refresh_if_needed(NOW + timedelta(seconds=30))
    assert provider.fetches == fetches  # within the 60 s back-off
    provider.fail = False
    assert service.refresh_if_needed(NOW + timedelta(seconds=61))


def test_quarter_hour_source_averaged(provider):
    from heating_optimizer.prices.base import PricePoint

    hour = datetime(2026, 10, 27, 13, tzinfo=UTC)
    provider.points[Commodity.ELECTRICITY] = [
        PricePoint(hour + timedelta(minutes=15 * i), hour + timedelta(minutes=15 * (i + 1)), p)
        for i, p in enumerate([0.0, 0.04, 0.08, 0.12])
    ]
    service = PriceService(provider, make_config(), LOG)
    service.refresh(NOW)
    assert service.electricity(NOW).tariff.market_incl_vat == pytest.approx(0.06)


class NoTomorrowProvider(FakePriceProvider):
    """Rejects any request touching a date without data, like EnergyZero's service_validation_error 'no_data'."""

    def fetch(self, commodity, start, end):
        points = super().fetch(commodity, start, end)
        if not points:
            raise ValueError("{'error': {'code': 'service_validation_error', 'translation_key': 'no_data'}}")
        return points


def test_each_date_is_fetched_separately_ending_before_midnight(provider):
    calls = []
    original = provider.fetch
    provider.fetch = lambda c, s, e: calls.append((c, s, e)) or original(c, s, e)
    PriceService(provider, make_config(), LOG).refresh(NOW)
    assert len(calls) == 4
    for _, start, end in calls:
        assert (end - start).total_seconds() == 24 * 3600 - 1  # same local date, never the next one


def test_unpublished_tomorrow_is_not_a_failure(provider):
    p = NoTomorrowProvider()
    p.points = provider.points  # today only
    service = PriceService(p, make_config(), LOG)
    assert service.refresh(NOW)
    assert service.consecutive_failures == 0 and service.last_error is None
    assert service.heat_prices(NOW).complete
    assert not service.tomorrow_loaded(NOW)


def test_missing_today_is_a_failure():
    service = PriceService(NoTomorrowProvider(), make_config(), LOG)
    assert not service.refresh(NOW)
    assert service.consecutive_failures == 1
