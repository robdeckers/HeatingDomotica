"""Heating device protocol and shared data types."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class DeviceStatus:
    name: str
    device_id: str
    available: bool
    state: str | None  # "on", "off", or None when unknown
    last_changed: datetime | None
    power_w: float | None = None
    energy_kwh: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    temperature_c: float | None = None
    rssi_dbm: float | None = None
    heater_active: bool = False

    @property
    def is_on(self) -> bool:
        return self.state == "on"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_changed"] = self.last_changed.isoformat() if self.last_changed else None
        return data


@dataclass(frozen=True)
class SwitchResult:
    name: str
    before: str | None
    after: str | None
    requested: str
    verified: bool
    attempts: int
    skipped: str | None = None  # why the device was not switched (unavailable, locked out)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HeatingDevice(Protocol):
    name: str
    device_id: str

    def turn_on(self, reason: str) -> SwitchResult: ...

    def turn_off(self, reason: str) -> SwitchResult: ...

    def read_status(self) -> DeviceStatus: ...

    def is_available(self) -> bool: ...

    def is_locked_out(self, now: datetime) -> bool: ...
