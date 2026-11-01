"""Test doubles so the tests need neither Home Assistant nor AppDaemon."""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from heating_optimizer.config import AppConfig, parse_config
from heating_optimizer.devices.base import DeviceStatus, SwitchResult
from heating_optimizer.prices.base import Commodity, PricePoint, SensorReading

LOG = logging.getLogger("tests")

RAW_CONFIG: dict[str, Any] = {
    "timezone": "Europe/Amsterdam",
    "dry_run": False,
    "tariffs_valid_until": "2026-12-31",
    "electricity": {
        "energyzero_config_entry": "abc123",
        "price_resolution": "hour",
        "supplier_markup_eur_per_kwh_incl_vat": 0.018,
        "energy_tax_eur_per_kwh_incl_vat": 0.11085,
        "vat_rate": 0.21,
    },
    "gas": {
        "supplier_markup_eur_per_m3_incl_vat": 0.07680,
        "energy_tax_eur_per_m3_incl_vat": 0.72680,
        "vat_rate": 0.21,
        "calorific_value_kwh_per_m3": 9.769,
        "boiler_efficiency": 0.90,
    },
    "decision": {"margin_on_eur_per_kwh": 0.02, "margin_off_eur_per_kwh": 0.005},
    "house": {
        "name": "Home",
        "plugs": [
            {"name": "shelly_1", "device_id": "907069543f3c", "heater_power_w": 3000},
            {"name": "shelly_2", "device_id": "9070695dba7c", "heater_power_w": 3000},
        ],
    },
    "logging": {"decision_log_dir": "/tmp/unused"},
}


def raw_config(**overrides: Any) -> dict[str, Any]:
    """Deep copy of RAW_CONFIG with dotted-path overrides, e.g. raw_config(**{"gas.vat_rate": 0.09})."""
    raw = copy.deepcopy(RAW_CONFIG)
    for path, value in overrides.items():
        node = raw
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node.setdefault(key, {})
        if isinstance(node, list):
            node[int(leaf)] = value
        else:
            node[leaf] = value
    return raw


def make_config(**overrides: Any) -> AppConfig:
    return parse_config(raw_config(**overrides))


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeHA:
    """Minimal stand-in for the AppDaemon Hass API used by the adapters."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.states: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.service_results: dict[str, Any] = {}
        self.template_result: Any = "[]"
        self.stuck_switches: set[str] = set()  # switches that ignore turn_on/turn_off

    def set(self, entity: str, state: Any, **attributes: Any) -> None:
        self.states[entity] = {
            "state": None if state is None else str(state),
            "last_changed": self.clock().isoformat(),
            "last_updated": self.clock().isoformat(),
            "attributes": attributes,
        }

    def get_state(self, entity: str, attribute: str | None = None) -> Any:
        full = self.states.get(entity)
        if full is None:
            return None
        if attribute == "all":
            return full
        if attribute is None:
            return full["state"]
        return full["attributes"].get(attribute)

    def call_service(self, service: str, **kwargs: Any) -> Any:
        self.calls.append((service, kwargs))
        if service in ("switch/turn_on", "switch/turn_off"):
            entity = kwargs["entity_id"]
            if entity not in self.stuck_switches:
                target = service.rsplit("_", 1)[1]
                if self.get_state(entity) != target:
                    self.set(entity, target)
            return None
        result = self.service_results.get(service)
        if isinstance(result, Exception):
            raise result
        return result

    def render_template(self, template: str) -> Any:
        return self.template_result if isinstance(self.template_result, str) else json.dumps(self.template_result)


def add_shelly_entities(
    ha: FakeHA, prefix: str, state: str = "off", power: float = 0.0, **readings: float
) -> list[str]:
    """Create the HA entities of one plug and return their ids."""
    ha.set(f"switch.{prefix}", state)
    ha.set(f"sensor.{prefix}_power", power, device_class="power")
    ha.set(f"sensor.{prefix}_energy", readings.get("energy", 1.0), device_class="energy", unit_of_measurement="kWh")
    ha.set(f"sensor.{prefix}_voltage", readings.get("voltage", 230.0), device_class="voltage")
    ha.set(f"sensor.{prefix}_current", readings.get("current", 0.0), device_class="current")
    ha.set(f"sensor.{prefix}_temperature", readings.get("temperature", 30.0), device_class="temperature")
    ha.set(f"binary_sensor.{prefix}_overtemp", "off", device_class="problem")
    return [e for e in ha.states if prefix in e]


class FakePriceProvider:
    def __init__(self) -> None:
        self.points: dict[Commodity, list[PricePoint]] = {Commodity.ELECTRICITY: [], Commodity.GAS: []}
        self.sensor: dict[Commodity, SensorReading | None] = {Commodity.ELECTRICITY: None, Commodity.GAS: None}
        self.fail = False
        self.fetches = 0

    def fetch(self, commodity: Commodity, start: datetime, end: datetime) -> list[PricePoint]:
        self.fetches += 1
        if self.fail:
            raise ConnectionError("EnergyZero down")
        return [p for p in self.points[commodity] if start <= p.start < end]

    def read_sensor(self, commodity: Commodity) -> SensorReading | None:
        return self.sensor[commodity]

    def set_hourly(self, commodity: Commodity, start: datetime, hours: int, price: float | list[float]) -> None:
        prices = price if isinstance(price, list) else [price] * hours
        self.points[commodity] = [
            PricePoint(start + timedelta(hours=i), start + timedelta(hours=i + 1), prices[i]) for i in range(hours)
        ]


class FakePlug:
    """HeatingDevice double for House tests."""

    def __init__(self, name: str, state: str = "off", available: bool = True, power_w: float = 0.0):
        self.name = name
        self.device_id = name
        self.state = state
        self.available = available
        self.power_w = power_w
        self.energy_kwh = 0.0
        self.last_changed: datetime | None = None
        self.locked_until: datetime | None = None
        self.lockout_reason: str | None = None
        self.dry_run = False
        self.fail_switch = False
        self.switch_calls: list[str] = []

    def _switch(self, target: str, dry_run: bool | None) -> SwitchResult:
        self.switch_calls.append(target)
        if self.fail_switch:
            raise RuntimeError("boom")
        before = self.state
        self.state = target
        return SwitchResult(self.name, before, target, target, True, 1, bool(dry_run))

    def turn_on(self, reason: str, dry_run: bool | None = None) -> SwitchResult:
        return self._switch("on", dry_run)

    def turn_off(self, reason: str, dry_run: bool | None = None) -> SwitchResult:
        return self._switch("off", dry_run)

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            name=self.name,
            device_id=self.device_id,
            available=self.available,
            state=self.state if self.available else None,
            last_changed=self.last_changed,
            power_w=self.power_w,
            energy_kwh=self.energy_kwh,
            heater_active=self.state == "on" and self.power_w > 10,
        )

    def is_available(self) -> bool:
        return self.available

    def is_locked_out(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until
