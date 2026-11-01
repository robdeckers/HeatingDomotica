"""Price service: keeps the price caches filled and turns market prices into €/kWh of useful heat."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from heating_optimizer.config import AppConfig
from heating_optimizer.heat_cost import break_even_elec_price, elec_per_kwh_heat, gas_per_kwh_fuel, gas_per_kwh_heat
from heating_optimizer.prices.base import Commodity, PriceProvider
from heating_optimizer.prices.cache import PriceCache, PriceLookup
from heating_optimizer.scheduling import (
    add_elapsed,
    at_local_time,
    elapsed_s,
    gas_day_bounds,
    gas_day_of,
    hour_start,
    local_day_bounds,
)
from heating_optimizer.tariffs import TariffBreakdown, consumer_price


@dataclass(frozen=True)
class Quote:
    """Consumer price of one commodity at a moment, with provenance."""

    tariff: TariffBreakdown
    applies_from: datetime
    applies_to: datetime
    resolution: str
    source: str  # "cache" or "sensor"
    age_s: float

    @property
    def consumer_incl_vat(self) -> float:
        return self.tariff.consumer_incl_vat


@dataclass(frozen=True)
class HeatPrices:
    """Cost of one kWh of useful heat from each source. A missing quote leaves its heat price None."""

    elec: Quote | None
    gas: Quote | None
    elec_per_kwh_heat: float | None
    gas_per_kwh_fuel: float | None
    gas_per_kwh_heat: float | None
    break_even_elec_consumer: float | None
    gas_day: date

    @property
    def complete(self) -> bool:
        return self.elec_per_kwh_heat is not None and self.gas_per_kwh_heat is not None

    @property
    def delta(self) -> float | None:
        """Gas heat minus electric heat: positive means electric is cheaper."""
        if not self.complete:
            return None
        return self.gas_per_kwh_heat - self.elec_per_kwh_heat  # type: ignore[operator]


def _is_no_data(exc: Exception) -> bool:
    """True when EnergyZero reports that a date has no prices yet (service_validation_error 'no_data')."""
    text = str(exc)
    return "no_data" in text or "No price data available" in text


class PriceService:
    def __init__(self, provider: PriceProvider, config: AppConfig, logger: logging.Logger):
        self._provider = provider
        self._cfg = config
        self._tz = config.tz
        self._log = logger
        self.elec_cache = PriceCache()
        self.gas_cache = PriceCache()
        self.last_attempt: datetime | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    # ── refresh ─────────────────────────────────────────────────────────────

    def refresh(self, now: datetime) -> bool:
        """Fetch electricity for today and tomorrow, and gas for the current and next gas day.

        EnergyZero fetches whole local dates and rejects a request when any requested date has no data yet, so each
        date is requested separately (ending at 23:59:59, or the next date would be included). Tomorrow's prices
        are published in the afternoon: their absence is not an error. Returns True if today's prices loaded.
        """
        self.last_attempt = now
        today = now.astimezone(self._tz).date()
        gas_day = gas_day_of(now, self._tz)  # a gas day's price is published under its start date
        ok = True
        for commodity, cache, day in (
            (Commodity.ELECTRICITY, self.elec_cache, today),
            (Commodity.ELECTRICITY, self.elec_cache, today + timedelta(days=1)),
            (Commodity.GAS, self.gas_cache, gas_day),
            (Commodity.GAS, self.gas_cache, gas_day + timedelta(days=1)),
        ):
            required = day in (today, gas_day)
            start, end = local_day_bounds(day, self._tz)
            try:
                points = self._provider.fetch(commodity, start, add_elapsed(end, -timedelta(seconds=1)))
                cache.update(points, now)
                self._log.info("Fetched %d %s price points for %s", len(points), commodity, day)
            except Exception as exc:
                if not required and _is_no_data(exc):
                    self._log.info("%s prices for %s not published yet", commodity, day)
                    continue
                ok = ok and not required
                self.last_error = f"{commodity} {day}: {type(exc).__name__}: {exc}"
                self._log.error("Price fetch failed (%s)", self.last_error)
        for cache in (self.elec_cache, self.gas_cache):
            cache.prune(now.astimezone(UTC) - timedelta(days=1))
        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
        return ok

    def tomorrow_loaded(self, now: datetime) -> bool:
        tomorrow = now.astimezone(self._tz).date() + timedelta(days=1)
        return self.elec_cache.covers_local_day(tomorrow, self._tz)

    def refresh_reason(self, now: datetime) -> str | None:
        """Why a refresh is needed now, or None. Day-ahead prices never change, so cache age alone is no reason."""
        if self.last_attempt is None:
            return "startup"
        if self.elec_cache.electricity_at(now, self._cfg.electricity.price_resolution) is None:
            return "current electricity price missing"
        if self.gas_cache.gas_for_day(gas_day_of(now, self._tz), self._tz) is None:
            return "current gas price missing"
        local = now.astimezone(self._tz)
        refresh_at = at_local_time(local.date(), self._cfg.prices.daily_refresh_time, self._tz)
        if now >= refresh_at and not self.tomorrow_loaded(now):
            if self.last_attempt < refresh_at:
                return "daily refresh"
            if elapsed_s(now, self.last_attempt) >= self._cfg.prices.retry_interval_minutes * 60:
                return "tomorrow's prices not loaded yet"
        return None

    def refresh_if_needed(self, now: datetime, force_reason: str | None = None) -> bool:
        reason = force_reason or self.refresh_reason(now)
        if reason is None:
            return True
        # Do not hammer the service on failures: honour the retry interval unless prices are missing outright.
        missing = reason.startswith("current") or reason == "startup"
        if self.last_attempt is not None and self.consecutive_failures and not force_reason:
            if elapsed_s(now, self.last_attempt) < (60 if missing else self._cfg.prices.retry_interval_minutes * 60):
                return False
        self._log.info("Refreshing prices: %s", reason)
        return self.refresh(now)

    # ── quotes ──────────────────────────────────────────────────────────────

    def electricity(self, now: datetime) -> Quote | None:
        cfg = self._cfg.electricity
        lookup = self.elec_cache.electricity_at(now, cfg.price_resolution)
        source = "cache"
        if lookup is None:
            lookup = self._sensor_lookup(Commodity.ELECTRICITY, now)
            source = "sensor"
        if lookup is None:
            return None
        tariff = consumer_price(
            lookup.price_incl_vat,
            cfg.supplier_markup_eur_per_kwh_incl_vat,
            cfg.energy_tax_eur_per_kwh_incl_vat,
            cfg.vat_rate,
        )
        return Quote(tariff, lookup.applies_from, lookup.applies_to, lookup.resolution, source, self._age(now, source))

    def gas(self, now: datetime) -> Quote | None:
        cfg = self._cfg.gas
        gas_day = gas_day_of(now, self._tz)
        lookup = self.gas_cache.gas_for_day(gas_day, self._tz)
        source = "cache"
        if lookup is None:
            lookup = self._sensor_lookup(Commodity.GAS, now)
            source = "sensor"
        if lookup is None:
            return None
        tariff = consumer_price(
            lookup.price_incl_vat,
            cfg.supplier_markup_eur_per_m3_incl_vat,
            cfg.energy_tax_eur_per_m3_incl_vat,
            cfg.vat_rate,
        )
        return Quote(tariff, lookup.applies_from, lookup.applies_to, lookup.resolution, source, self._age(now, source))

    def heat_prices(self, now: datetime) -> HeatPrices:
        elec = self.electricity(now)
        gas = self.gas(now)
        efficiency = self._cfg.electric_heating.efficiency
        elec_heat = elec_per_kwh_heat(elec.consumer_incl_vat, efficiency) if elec else None
        gas_fuel = gas_heat = break_even = None
        if gas:
            gas_fuel = gas_per_kwh_fuel(gas.consumer_incl_vat, self._cfg.gas.calorific_value_kwh_per_m3)
            gas_heat = gas_per_kwh_heat(
                gas.consumer_incl_vat, self._cfg.gas.calorific_value_kwh_per_m3, self._cfg.gas.boiler_efficiency
            )
            break_even = break_even_elec_price(gas_heat, efficiency)
        return HeatPrices(elec, gas, elec_heat, gas_fuel, gas_heat, break_even, gas_day_of(now, self._tz))

    def _age(self, now: datetime, source: str) -> float:
        refreshed = self.elec_cache.last_refresh if source == "cache" else None
        return elapsed_s(now, refreshed) if refreshed else 0.0

    def _sensor_lookup(self, commodity: Commodity, now: datetime) -> PriceLookup | None:
        """Sensor fallback: valid only if recent and referring to the current hour (electricity) or gas day."""
        try:
            reading = self._provider.read_sensor(commodity)
        except Exception as exc:
            self._log.warning("Sensor fallback for %s failed: %s", commodity, exc)
            return None
        if reading is None:
            return None
        if elapsed_s(now, reading.last_updated) > self._cfg.prices.sensor_max_age_minutes * 60:
            return None
        if commodity is Commodity.ELECTRICITY:
            start = hour_start(now)
            if reading.last_updated < start:
                return None
            self._log.warning("Using electricity sensor fallback (no cached price for now)")
            return PriceLookup(reading.price_incl_vat, start, add_elapsed(start, timedelta(hours=1)), "hour")
        gas_day = gas_day_of(now, self._tz)
        if gas_day_of(reading.last_updated, self._tz) != gas_day:
            return None
        self._log.warning("Using gas sensor fallback (no cached price for the gas day)")
        start, end = gas_day_bounds(gas_day, self._tz)
        return PriceLookup(reading.price_incl_vat, start, end, "gas_day")
