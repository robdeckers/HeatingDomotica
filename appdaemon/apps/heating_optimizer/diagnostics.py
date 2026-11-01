"""Runtime metrics and the OK / DEGRADED / ERROR health evaluation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from heating_optimizer.config import AppConfig
from heating_optimizer.devices.base import DeviceStatus
from heating_optimizer.prices.service import HeatPrices, PriceService
from heating_optimizer.scheduling import at_local_time, elapsed_s, local_midnight


class Health(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


_SEVERITY = {Health.OK: 0, Health.DEGRADED: 1, Health.ERROR: 2}


@dataclass(frozen=True)
class Check:
    name: str
    level: Health
    detail: str


def overall(checks: list[Check]) -> Health:
    return max((c.level for c in checks), key=_SEVERITY.__getitem__, default=Health.OK)


class SwitchCounter:
    """Switching actions per plug, to detect flapping."""

    def __init__(self) -> None:
        self._events: dict[str, deque[datetime]] = {}

    def record(self, name: str, now: datetime) -> None:
        events = self._events.setdefault(name, deque())
        events.append(now)
        while events and elapsed_s(now, events[0]) > 2 * 86400:
            events.popleft()

    def last_hour(self, name: str, now: datetime) -> int:
        return sum(1 for t in self._events.get(name, ()) if elapsed_s(now, t) <= 3600)

    def today(self, name: str, now: datetime, tz: ZoneInfo) -> int:
        midnight = local_midnight(now.astimezone(tz).date(), tz)
        return sum(1 for t in self._events.get(name, ()) if t >= midnight)


@dataclass
class Diagnostics:
    started_at: datetime
    ticks_run: int = 0
    ticks_skipped: int = 0
    ticks_failed: int = 0
    last_tick: datetime | None = None
    last_tick_failed: bool = False
    next_tick: datetime | None = None
    last_drift_ms: float | None = None
    max_drift_ms: float = 0.0
    self_test_ok: bool | None = None
    self_test_problems: list[str] = field(default_factory=list)
    decision_log_error: str | None = None
    switches: SwitchCounter = field(default_factory=SwitchCounter)

    def record_tick(self, scheduled: datetime | None, actual: datetime, drift: float | None) -> None:
        self.ticks_run += 1
        self.last_tick = actual
        if drift is not None:
            self.last_drift_ms = drift
            self.max_drift_ms = max(self.max_drift_ms, abs(drift))

    def uptime_s(self, now: datetime) -> float:
        return elapsed_s(now, self.started_at)


def evaluate_health(
    now: datetime,
    cfg: AppConfig,
    diag: Diagnostics,
    prices: PriceService,
    heat: HeatPrices | None,
    plugs: list[Any],
    statuses: dict[str, DeviceStatus],
    provider_stats: dict[str, Any],
) -> tuple[Health, dict[str, Any]]:
    """Evaluate every check. ``plugs`` are ShellyPlug-like objects; ``statuses`` their latest status by name."""
    tz = cfg.tz
    local = now.astimezone(tz)
    checks: list[Check] = []

    if diag.self_test_ok is False:
        checks.append(Check("self_test", Health.ERROR, "; ".join(diag.self_test_problems)))

    if heat is not None and not heat.complete:
        checks.append(Check("prices", Health.ERROR, "current price missing: failsafe (gas)"))
    elif prices.consecutive_failures:
        checks.append(Check("prices", Health.DEGRADED, f"last refresh failed: {prices.last_error}"))

    expected_by = at_local_time(local.date(), cfg.prices.tomorrow_expected_by, tz)
    tomorrow_loaded = prices.tomorrow_loaded(now)
    if now >= expected_by and not tomorrow_loaded:
        checks.append(Check("tomorrow_prices", Health.DEGRADED, "tomorrow's prices not loaded yet"))

    if local.date() > cfg.tariffs_valid_until:
        checks.append(
            Check("tariffs", Health.DEGRADED, f"tariffs valid until {cfg.tariffs_valid_until}: update energy tax/fees")
        )

    plug_attrs: dict[str, Any] = {}
    unavailable = 0
    for plug in plugs:
        status = statuses.get(plug.name)
        locked = plug.lockout_until is not None and now < plug.lockout_until
        per_hour = diag.switches.last_hour(plug.name, now)
        rpc = getattr(plug, "rpc", None)
        plug_attrs[plug.name] = {
            "available": bool(status and status.available),
            "state": status.state if status else None,
            "heater_active": bool(status and status.heater_active),
            "power_w": status.power_w if status else None,
            "voltage_v": status.voltage_v if status else None,
            "current_a": status.current_a if status else None,
            "temperature_c": status.temperature_c if status else None,
            "rssi_dbm": status.rssi_dbm if status else None,
            "last_seen": plug.last_seen.isoformat() if plug.last_seen else None,
            "firmware": rpc.firmware if rpc else None,
            "ip_resolved_via": rpc.resolved_via if rpc else None,
            "verify_failures": plug.verify_failures,
            "lockouts": plug.lockout_count,
            "locked_out_until": plug.lockout_until.isoformat() if locked else None,
            "lockout_reason": plug.lockout_reason if locked else None,
            "protection_trips": plug.protection_trips,
            "switches_last_hour": per_hour,
            "switches_today": diag.switches.today(plug.name, now, tz),
        }
        if not (status and status.available):
            unavailable += 1
            checks.append(Check(f"{plug.name}.available", Health.DEGRADED, "unavailable"))
        if plug.verify_failures:
            checks.append(Check(f"{plug.name}.verify", Health.DEGRADED, f"{plug.verify_failures} verify failures"))
        if locked:
            checks.append(Check(f"{plug.name}.lockout", Health.DEGRADED, plug.lockout_reason or "locked out"))
        if plug.protection_trips:
            checks.append(Check(f"{plug.name}.protection", Health.DEGRADED, f"{plug.protection_trips} device trips"))
        if per_hour > cfg.decision.max_switches_per_hour:
            checks.append(Check(f"{plug.name}.flapping", Health.DEGRADED, f"{per_hour} switches in the last hour"))
    if plugs and unavailable == len(plugs):
        checks.append(Check("plugs", Health.ERROR, "no plug available"))

    if diag.last_tick_failed:
        checks.append(Check("ticks", Health.DEGRADED, "last tick failed"))
    if diag.decision_log_error:
        checks.append(Check("decision_log", Health.DEGRADED, diag.decision_log_error))

    state = overall(checks)
    attrs = {
        "checks": [f"{c.level}: {c.name}: {c.detail}" for c in checks if c.level is not Health.OK],
        "price_cache_refreshed": prices.elec_cache.last_refresh.isoformat() if prices.elec_cache.last_refresh else None,
        "tomorrow_loaded": tomorrow_loaded,
        "energyzero_latency_ms": provider_stats.get("latency_ms"),
        "energyzero_errors": provider_stats.get("error_count"),
        "energyzero_last_error": provider_stats.get("last_error"),
        "plugs": plug_attrs,
        "ticks_run": diag.ticks_run,
        "ticks_skipped": diag.ticks_skipped,
        "ticks_failed": diag.ticks_failed,
        "max_drift_ms": round(diag.max_drift_ms, 1),
        "uptime_h": round(diag.uptime_s(now) / 3600, 2),
        "tariffs_valid_until": cfg.tariffs_valid_until.isoformat(),
    }
    return state, attrs
