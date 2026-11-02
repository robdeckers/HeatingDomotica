"""Typed configuration parsed and validated from the ``config`` block in ``apps.yaml``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TODO_MARKER = "TODO(user)"
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class ConfigError(ValueError):
    """Raised when the configuration is incomplete or invalid. Holds every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid heating optimizer config:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class ElectricityConfig:
    source: str
    energyzero_config_entry: str
    price_resolution: str
    supplier_markup_eur_per_kwh_incl_vat: float
    energy_tax_eur_per_kwh_incl_vat: float
    vat_rate: float
    fallback_sensor: str
    fallback_sensor_includes_vat: bool


@dataclass(frozen=True)
class GasConfig:
    source: str
    supplier_markup_eur_per_m3_incl_vat: float
    energy_tax_eur_per_m3_incl_vat: float
    vat_rate: float
    calorific_value_kwh_per_m3: float
    boiler_efficiency: float
    fallback_sensor: str
    fallback_sensor_includes_vat: bool


@dataclass(frozen=True)
class ElectricHeatingConfig:
    efficiency: float


@dataclass(frozen=True)
class DecisionConfig:
    margin_on_eur_per_kwh: float
    margin_off_eur_per_kwh: float
    min_on_minutes: float
    min_off_minutes: float
    margin_override_entity: str | None
    failsafe_state: str
    max_switches_per_hour: int


@dataclass(frozen=True)
class PricesConfig:
    daily_refresh_time: time
    retry_interval_minutes: float
    tomorrow_expected_by: time
    sensor_max_age_minutes: float


@dataclass(frozen=True)
class LoopConfig:
    interval_minutes: int
    evaluate_on_startup: bool


@dataclass(frozen=True)
class PlugConfig:
    name: str
    model: str
    device_id: str
    switch_entity: str
    fallback_ip: str | None
    heater_power_w: float
    plug_max_power_w: float
    plug_max_current_a: float
    overcurrent_min_duration_s: float
    max_device_temp_c: float
    active_power_threshold_w: float


@dataclass(frozen=True)
class HouseConfig:
    name: str
    plugs: tuple[PlugConfig, ...]


@dataclass(frozen=True)
class ShellyRpcConfig:
    enabled: bool
    timeout_s: float
    hostname_pattern: str
    username: str = "admin"
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    decision_log_dir: str
    rotate_mb: float
    backups: int


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    tariffs_valid_until: date
    electricity: ElectricityConfig
    gas: GasConfig
    electric_heating: ElectricHeatingConfig
    decision: DecisionConfig
    prices: PricesConfig
    loop: LoopConfig
    house: HouseConfig
    shelly_rpc: ShellyRpcConfig
    logging: LoggingConfig
    control_entity: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def masked(self) -> dict[str, Any]:
        """Config as a dict with secrets masked, for logging."""
        data = asdict(self)
        if data["shelly_rpc"].get("password"):
            data["shelly_rpc"]["password"] = "***"
        return data


class _Reader:
    """Reads values from nested mappings and collects every problem instead of failing on the first."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def section(self, raw: Mapping[str, Any], key: str, path: str = "") -> Mapping[str, Any]:
        value = raw.get(key, {})
        if not isinstance(value, Mapping):
            self.problems.append(f"{path}{key}: expected a mapping")
            return {}
        return value

    def get(self, raw: Mapping[str, Any], key: str, path: str, default: Any = ...) -> Any:
        full = f"{path}{key}"
        if key not in raw or raw[key] is None:
            if default is ...:
                self.problems.append(f"{full}: missing")
            return None if default is ... else default
        value = raw[key]
        if isinstance(value, str) and TODO_MARKER in value:
            self.problems.append(f"{full}: still set to {TODO_MARKER}, fill in a real value")
            return None
        return value

    def number(
        self,
        raw: Mapping[str, Any],
        key: str,
        path: str,
        default: Any = ...,
        lo: float | None = None,
        hi: float | None = None,
        lo_open: bool = False,
    ) -> float:
        value = self.get(raw, key, path, default)
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.problems.append(f"{path}{key}: expected a number, got {value!r}")
            return 0.0
        value = float(value)
        if lo is not None and (value < lo or (lo_open and value == lo)):
            bound = f"> {lo}" if lo_open else f">= {lo}"
            self.problems.append(f"{path}{key}: {value} must be {bound}")
        if hi is not None and value > hi:
            self.problems.append(f"{path}{key}: {value} must be <= {hi}")
        return value

    def boolean(self, raw: Mapping[str, Any], key: str, path: str, default: Any = ...) -> bool:
        value = self.get(raw, key, path, default)
        if value is None:
            return False
        if not isinstance(value, bool):
            self.problems.append(f"{path}{key}: expected true/false, got {value!r}")
            return False
        return value

    def string(self, raw: Mapping[str, Any], key: str, path: str, default: Any = ...) -> str:
        value = self.get(raw, key, path, default)
        if value is None:
            return ""
        return str(value)

    def clock_time(self, raw: Mapping[str, Any], key: str, path: str, default: str) -> time:
        value = self.get(raw, key, path, default)
        try:
            return time.fromisoformat(str(value))
        except ValueError:
            self.problems.append(f"{path}{key}: expected HH:MM, got {value!r}")
            return time(0, 0)

    def day(self, raw: Mapping[str, Any], key: str, path: str) -> date:
        value = self.get(raw, key, path)
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            if value is not None:
                self.problems.append(f"{path}{key}: expected YYYY-MM-DD, got {value!r}")
            return date.min


def _parse_plug(r: _Reader, raw: Mapping[str, Any], index: int) -> PlugConfig:
    path = f"house.plugs[{index}]."
    device_id = r.string(raw, "device_id", path).lower()
    if device_id and not _DEVICE_ID_RE.match(device_id):
        r.problems.append(f"{path}device_id: {device_id!r} must be 12 hex characters (the MAC without separators)")
    plug = PlugConfig(
        name=r.string(raw, "name", path),
        model=r.string(raw, "model", path, "S3PL-30110EU"),
        device_id=device_id,
        switch_entity=r.string(raw, "switch_entity", path, "auto"),
        fallback_ip=r.get(raw, "fallback_ip", path, None),
        heater_power_w=r.number(raw, "heater_power_w", path, lo=0, lo_open=True),
        plug_max_power_w=r.number(raw, "plug_max_power_w", path, 3000, lo=0, lo_open=True),
        plug_max_current_a=r.number(raw, "plug_max_current_a", path, 13.0, lo=0, hi=16, lo_open=True),
        overcurrent_min_duration_s=r.number(raw, "overcurrent_min_duration_s", path, 10, lo=0),
        max_device_temp_c=r.number(raw, "max_device_temp_c", path, 70, lo=0, lo_open=True),
        active_power_threshold_w=r.number(raw, "active_power_threshold_w", path, 10, lo=0),
    )
    if plug.heater_power_w > plug.plug_max_power_w:
        r.problems.append(
            f"{path}heater_power_w: {plug.heater_power_w} W exceeds plug_max_power_w {plug.plug_max_power_w} W"
        )
    return plug


def parse_config(raw: Mapping[str, Any]) -> AppConfig:
    """Parse and validate the raw config mapping. Raises ConfigError listing every problem."""
    r = _Reader()

    timezone = r.string(raw, "timezone", "", "Europe/Amsterdam")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        r.problems.append(f"timezone: unknown time zone {timezone!r}")
    if "dry_run" in raw:
        r.problems.append("dry_run: no longer supported (plugs are always switched); remove it from apps.yaml")

    e_raw = r.section(raw, "electricity")
    p = "electricity."
    electricity = ElectricityConfig(
        source=r.string(e_raw, "source", p, "energyzero"),
        energyzero_config_entry=r.string(e_raw, "energyzero_config_entry", p),
        price_resolution=r.string(e_raw, "price_resolution", p, "hour"),
        supplier_markup_eur_per_kwh_incl_vat=r.number(e_raw, "supplier_markup_eur_per_kwh_incl_vat", p, lo=0, hi=0.10),
        energy_tax_eur_per_kwh_incl_vat=r.number(e_raw, "energy_tax_eur_per_kwh_incl_vat", p, lo=0, hi=0.25),
        vat_rate=r.number(e_raw, "vat_rate", p, 0.21, lo=0, hi=0.99),
        fallback_sensor=r.string(e_raw, "fallback_sensor", p, "sensor.energyzero_today_energy_current_hour_price"),
        fallback_sensor_includes_vat=r.boolean(e_raw, "fallback_sensor_includes_vat", p, True),
    )
    if electricity.price_resolution not in ("hour", "quarter"):
        r.problems.append(f"{p}price_resolution: must be 'hour' or 'quarter', got {electricity.price_resolution!r}")
    if electricity.source != "energyzero":
        r.problems.append(f"{p}source: only 'energyzero' is supported")

    g_raw = r.section(raw, "gas")
    p = "gas."
    gas = GasConfig(
        source=r.string(g_raw, "source", p, "energyzero"),
        supplier_markup_eur_per_m3_incl_vat=r.number(g_raw, "supplier_markup_eur_per_m3_incl_vat", p, lo=0, hi=0.30),
        energy_tax_eur_per_m3_incl_vat=r.number(g_raw, "energy_tax_eur_per_m3_incl_vat", p, lo=0, hi=1.50),
        vat_rate=r.number(g_raw, "vat_rate", p, 0.21, lo=0, hi=0.99),
        calorific_value_kwh_per_m3=r.number(g_raw, "calorific_value_kwh_per_m3", p, 9.769, lo=0, lo_open=True),
        boiler_efficiency=r.number(g_raw, "boiler_efficiency", p, 0.90, lo=0, hi=1.0, lo_open=True),
        fallback_sensor=r.string(g_raw, "fallback_sensor", p, "sensor.energyzero_today_gas_current_hour_price"),
        fallback_sensor_includes_vat=r.boolean(g_raw, "fallback_sensor_includes_vat", p, True),
    )
    if gas.source != "energyzero":
        r.problems.append(f"{p}source: only 'energyzero' is supported")

    h_raw = r.section(raw, "electric_heating")
    electric_heating = ElectricHeatingConfig(
        efficiency=r.number(h_raw, "efficiency", "electric_heating.", 1.0, lo=0, hi=10, lo_open=True)
    )

    d_raw = r.section(raw, "decision")
    p = "decision."
    decision = DecisionConfig(
        margin_on_eur_per_kwh=r.number(d_raw, "margin_on_eur_per_kwh", p, 0.02, lo=0),
        margin_off_eur_per_kwh=r.number(d_raw, "margin_off_eur_per_kwh", p, 0.005, lo=0),
        min_on_minutes=r.number(d_raw, "min_on_minutes", p, 15, lo=0),
        min_off_minutes=r.number(d_raw, "min_off_minutes", p, 15, lo=0),
        margin_override_entity=r.get(d_raw, "margin_override_entity", p, None),
        failsafe_state=r.string(d_raw, "failsafe_state", p, "off"),
        max_switches_per_hour=int(r.number(d_raw, "max_switches_per_hour", p, 4, lo=1)),
    )
    if decision.failsafe_state != "off":
        r.problems.append(f"{p}failsafe_state: only 'off' is supported (gas is the safe default)")

    pr_raw = r.section(raw, "prices")
    p = "prices."
    prices = PricesConfig(
        daily_refresh_time=r.clock_time(pr_raw, "daily_refresh_time", p, "15:05"),
        retry_interval_minutes=r.number(pr_raw, "retry_interval_minutes", p, 30, lo=1),
        tomorrow_expected_by=r.clock_time(pr_raw, "tomorrow_expected_by", p, "16:30"),
        sensor_max_age_minutes=r.number(pr_raw, "sensor_max_age_minutes", p, 70, lo=1),
    )

    l_raw = r.section(raw, "loop")
    loop = LoopConfig(
        interval_minutes=int(r.number(l_raw, "interval_minutes", "loop.", 5, lo=1, hi=60)),
        evaluate_on_startup=r.boolean(l_raw, "evaluate_on_startup", "loop.", True),
    )
    if loop.interval_minutes and 60 % loop.interval_minutes != 0:
        r.problems.append(f"loop.interval_minutes: {loop.interval_minutes} must divide 60")

    house_raw = r.section(raw, "house")
    plugs_raw = house_raw.get("plugs") or []
    if not isinstance(plugs_raw, list) or not plugs_raw:
        r.problems.append("house.plugs: at least one plug is required")
        plugs_raw = []
    plugs = tuple(_parse_plug(r, plug_raw, i) for i, plug_raw in enumerate(plugs_raw))
    ids = [plug.device_id for plug in plugs if plug.device_id]
    names = [plug.name for plug in plugs if plug.name]
    if len(ids) != len(set(ids)):
        r.problems.append("house.plugs: duplicate device_id")
    if len(names) != len(set(names)):
        r.problems.append("house.plugs: duplicate name")
    house = HouseConfig(name=r.string(house_raw, "name", "house.", "Home"), plugs=plugs)

    rpc_raw = r.section(raw, "shelly_rpc")
    p = "shelly_rpc."
    shelly_rpc = ShellyRpcConfig(
        enabled=r.boolean(rpc_raw, "enabled", p, True),
        timeout_s=r.number(rpc_raw, "timeout_s", p, 3, lo=0, lo_open=True),
        hostname_pattern=r.string(rpc_raw, "hostname_pattern", p, "shellyplugmg3-{device_id}.local"),
        username=r.string(rpc_raw, "username", p, "admin"),
        password=r.get(rpc_raw, "password", p, None),
    )

    log_raw = r.section(raw, "logging")
    p = "logging."
    logging_cfg = LoggingConfig(
        level=r.string(log_raw, "level", p, "INFO").upper(),
        decision_log_dir=r.string(log_raw, "decision_log_dir", p, "/config/logs/heating_optimizer"),
        rotate_mb=r.number(log_raw, "rotate_mb", p, 5, lo=0, lo_open=True),
        backups=int(r.number(log_raw, "backups", p, 10, lo=1)),
    )
    if logging_cfg.level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        r.problems.append(f"{p}level: must be DEBUG, INFO, WARNING or ERROR")

    config = AppConfig(
        timezone=timezone,
        tariffs_valid_until=r.day(raw, "tariffs_valid_until", ""),
        electricity=electricity,
        gas=gas,
        electric_heating=electric_heating,
        decision=decision,
        prices=prices,
        loop=loop,
        house=house,
        shelly_rpc=shelly_rpc,
        logging=logging_cfg,
        control_entity=r.string(raw, "control_entity", "", "input_boolean.heating_optimizer_enabled"),
    )
    if r.problems:
        raise ConfigError(r.problems)
    return config
