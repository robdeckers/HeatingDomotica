"""The house: the heating devices (two plugs now) and an empty slot for energy sources (solar later)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from heating_optimizer.devices.base import DeviceStatus, HeatingDevice, SwitchResult


class EnergySource(Protocol):
    """Local production, e.g. solar panels. Its surplus can later become a third, near-free cost input."""

    name: str

    def current_production_w(self) -> float | None: ...

    def available(self) -> bool: ...


@dataclass(frozen=True)
class HouseSnapshot:
    """What the decision engine needs to know about the house at the start of a tick."""

    devices: tuple[DeviceStatus, ...]
    heating_on: bool
    state_since: datetime | None  # when heating last turned on (if on) or off (if off), from HA last_changed
    locked_out: tuple[str, ...]
    available: tuple[str, ...]
    production_w: float | None = None

    @property
    def all_locked_out(self) -> bool:
        return bool(self.available) and set(self.available) <= set(self.locked_out)


@dataclass(frozen=True)
class HouseStatus:
    name: str
    total_power_w: float
    energy_today_kwh: float
    devices: tuple[DeviceStatus, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_power_w": round(self.total_power_w, 1),
            "energy_today_kwh": round(self.energy_today_kwh, 3),
            "devices": [d.as_dict() for d in self.devices],
        }


@dataclass
class House:
    name: str
    devices: list[HeatingDevice]
    tz: ZoneInfo
    logger: logging.Logger
    energy_sources: list[EnergySource] = field(default_factory=list)
    _energy_baseline: dict[str, tuple[date, float]] = field(default_factory=dict)

    def device(self, name: str) -> HeatingDevice:
        return next(d for d in self.devices if d.name == name)

    def snapshot(self, now: datetime) -> HouseSnapshot:
        statuses = tuple(d.read_status() for d in self.devices)
        available = tuple(s.name for s in statuses if s.available)
        on = [s for s in statuses if s.available and s.is_on]
        if on:
            changes = [s.last_changed for s in on if s.last_changed]
            since = min(changes) if changes else None
        else:
            changes = [s.last_changed for s in statuses if s.available and s.last_changed]
            since = max(changes) if changes else None
        locked = tuple(d.name for d in self.devices if d.is_locked_out(now))
        production = None
        if self.energy_sources:
            values = [s.current_production_w() for s in self.energy_sources if s.available()]
            production = sum(v for v in values if v is not None)
        return HouseSnapshot(statuses, bool(on), since, locked, available, production)

    def set_heating(self, on: bool, reason: str, now: datetime, dry_run: bool | None = None) -> list[SwitchResult]:
        """Bring every available plug to the requested state. Locked-out plugs are kept off. One failing plug does
        not stop the others."""
        target = "on" if on else "off"
        results = []
        for device in self.devices:
            try:
                results.append(self._apply(device, target, reason, now, dry_run))
            except Exception as exc:
                self.logger.exception("%s: switching to %s failed", device.name, target)
                results.append(SwitchResult(device.name, None, None, target, False, 0, bool(dry_run), error=str(exc)))
        return results

    def _apply(
        self, device: HeatingDevice, target: str, reason: str, now: datetime, dry_run: bool | None
    ) -> SwitchResult:
        status = device.read_status()
        dry = getattr(device, "dry_run", False) if dry_run is None else dry_run
        if not status.available:
            self.logger.warning("%s: unavailable, not switched to %s", device.name, target)
            return SwitchResult(device.name, None, None, target, False, 0, dry, skipped="unavailable")
        if device.is_locked_out(now):
            if status.is_on:
                return device.turn_off(f"locked out ({reason})", dry_run)
            return SwitchResult(device.name, status.state, status.state, target, True, 0, dry, skipped="locked out")
        if status.state == target:
            return SwitchResult(device.name, status.state, status.state, target, True, 0, dry)
        if target == "on":
            return device.turn_on(reason, dry_run)
        return device.turn_off(reason, dry_run)

    def status(self, now: datetime) -> HouseStatus:
        statuses = tuple(d.read_status() for d in self.devices)
        total_power = sum(s.power_w or 0.0 for s in statuses if s.available)
        return HouseStatus(self.name, total_power, self._energy_today(statuses, now), statuses)

    def _energy_today(self, statuses: tuple[DeviceStatus, ...], now: datetime) -> float:
        """Energy since local midnight, from each plug's cumulative counter (rebased on a new day or a reset)."""
        today = now.astimezone(self.tz).date()
        total = 0.0
        for s in statuses:
            if s.energy_kwh is None:
                continue
            day, base = self._energy_baseline.get(s.name, (None, None))
            if day != today or base is None or s.energy_kwh < base:
                self._energy_baseline[s.name] = (today, s.energy_kwh)
                base = s.energy_kwh
            total += s.energy_kwh - base
        return total

    def health(self, now: datetime) -> dict[str, dict[str, Any]]:
        report = {}
        for device in self.devices:
            status = device.read_status()
            report[device.name] = {
                "available": status.available,
                "locked_out": device.is_locked_out(now),
                "lockout_reason": getattr(device, "lockout_reason", None),
            }
        return report
