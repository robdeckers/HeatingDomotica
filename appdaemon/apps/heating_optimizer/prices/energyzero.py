"""Home Assistant adapter for the EnergyZero integration (ANWB Energie bills on EnergyZero prices).

The ``ha`` object is duck-typed (an AppDaemon ``Hass`` app in production, a fake in tests): it needs
``call_service(service, **kwargs)`` and ``get_state(entity_id, attribute=None)``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from heating_optimizer.prices.base import Commodity, PricePoint, SensorReading, extract_price_entries, points_from_raw
from heating_optimizer.tariffs import to_incl_vat

_SERVICES = {
    Commodity.ELECTRICITY: "energyzero/get_energy_prices",
    Commodity.GAS: "energyzero/get_gas_prices",
}


class EnergyZeroProvider:
    def __init__(
        self,
        ha: Any,
        config_entry: str,
        sensors: dict[Commodity, tuple[str, bool]],
        vat_rate: float,
        logger: logging.Logger,
    ):
        """``sensors`` maps each commodity to (fallback sensor entity, whether its value includes VAT)."""
        self._ha = ha
        self._config_entry = config_entry
        self._sensors = sensors
        self._vat_rate = vat_rate
        self._log = logger
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None
        self.error_count = 0
        self.call_count = 0

    def fetch(self, commodity: Commodity, start: datetime, end: datetime) -> list[PricePoint]:
        started = time.monotonic()
        self.call_count += 1
        try:
            result = self._ha.call_service(
                _SERVICES[commodity],
                config_entry=self._config_entry,
                incl_vat=True,
                start=start.astimezone(UTC).isoformat(),
                end=end.astimezone(UTC).isoformat(),
                return_response=True,  # AppDaemon 4.5: ask HA for the service response
            )
            entries = extract_price_entries(result)
            if entries is None:
                raise ValueError(f"no 'prices' in response: {str(result)[:300]}")
            self._log.debug("EnergyZero %s raw response: %s", commodity, result)
            return points_from_raw(entries)
        except Exception as exc:
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.last_latency_ms = (time.monotonic() - started) * 1000.0

    def read_sensor(self, commodity: Commodity) -> SensorReading | None:
        entity, includes_vat = self._sensors[commodity]
        try:
            state = self._ha.get_state(entity, attribute="all")
        except Exception as exc:
            self._log.warning("Reading fallback sensor %s failed: %s", entity, exc)
            return None
        if not state or state.get("state") in (None, "unknown", "unavailable"):
            return None
        try:
            value = float(state["state"])
            updated = datetime.fromisoformat(str(state["last_updated"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            return None
        if not includes_vat:
            value = to_incl_vat(value, self._vat_rate)
            self._log.info("Fallback sensor %s is excl. VAT; converted with vat_rate %.2f", entity, self._vat_rate)
        return SensorReading(value, updated)
