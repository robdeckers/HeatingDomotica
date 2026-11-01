"""Shelly Plug M Gen3, switched and read through the Home Assistant Shelly integration.

Switching always goes through HA (single source of truth). The optional direct RPC client is read-only and used for
diagnostics (firmware, error flags). The device ID (MAC) is the identity; IP addresses are only ever a means to reach
the device and are verified against the MAC before use.

The ``ha`` object is duck-typed: ``get_state(entity_id, attribute=None)``, ``call_service(service, **kwargs)`` and
``render_template(template)``.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from heating_optimizer.config import PlugConfig, ShellyRpcConfig
from heating_optimizer.devices.base import DeviceStatus, SwitchResult
from heating_optimizer.devices.safety import SafetyMonitor
from heating_optimizer.scheduling import next_full_hour

UNAVAILABLE_STATES = (None, "unavailable", "unknown")
PROTECTION_KEYWORDS = ("overtemp", "overheat", "overpower", "overcurrent", "overvoltage")

_DISCOVERY_TEMPLATE = """
{%- set ns = namespace(found=[]) -%}
{%- for e in integration_entities('shelly') -%}
  {%- set d = device_id(e) -%}
  {%- if d -%}
    {%- set info = ((device_attr(d, 'connections') or []) | list + (device_attr(d, 'identifiers') or []) | list)
                   | string | lower | replace(':', '') -%}
    {%- if '__MAC__' in info -%}{%- set ns.found = ns.found + [e] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{{ ns.found | to_json }}
"""


@dataclass
class ShellyEntities:
    switch: str | None = None
    power: str | None = None
    energy: str | None = None
    voltage: str | None = None
    current: str | None = None
    temperature: str | None = None
    rssi: str | None = None
    uptime: str | None = None
    protection: list[str] = field(default_factory=list)

    def sensor_entities(self) -> list[str]:
        return [e for e in (self.power, self.current, self.voltage, self.temperature) if e]


def classify_entities(states: Mapping[str, Mapping[str, Any]]) -> ShellyEntities:
    """Map a device's entities to their roles, by domain and device_class (renaming in HA does not matter)."""
    found = ShellyEntities()
    for entity_id in sorted(states):
        attrs = states[entity_id].get("attributes", {}) or {}
        device_class = attrs.get("device_class")
        domain = entity_id.split(".", 1)[0]
        lowered = entity_id.lower()
        if domain == "switch" and found.switch is None:
            found.switch = entity_id
        elif domain == "sensor":
            if device_class == "power" and found.power is None:
                found.power = entity_id
            elif device_class == "energy" and "return" not in lowered and found.energy is None:
                found.energy = entity_id
            elif device_class == "voltage" and found.voltage is None:
                found.voltage = entity_id
            elif device_class == "current" and found.current is None:
                found.current = entity_id
            elif device_class == "temperature" and found.temperature is None:
                found.temperature = entity_id
            elif device_class == "signal_strength" and found.rssi is None:
                found.rssi = entity_id
            elif device_class == "timestamp" and ("restart" in lowered or "uptime" in lowered):
                found.uptime = entity_id
        elif domain == "binary_sensor" and any(k in lowered for k in PROTECTION_KEYWORDS):
            found.protection.append(entity_id)
    return found


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


class ShellyRpcClient:
    """Read-only Gen2+ RPC client. Resolves the IP (mDNS → last good → fallback) and verifies the MAC every time."""

    _resolver = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shelly-mdns")

    def __init__(
        self,
        cfg: ShellyRpcConfig,
        device_id: str,
        fallback_ip: str | None,
        session: Any,
        logger: logging.Logger,
    ):
        self._cfg = cfg
        self._device_id = device_id.lower()
        self._fallback_ip = fallback_ip
        self._session = session
        self._log = logger
        self.hostname = cfg.hostname_pattern.format(device_id=self._device_id)
        self.last_good_ip: str | None = None
        self.resolved_via: str | None = None
        self.device_info: dict[str, Any] | None = None
        self.error_count = 0
        self.last_error: str | None = None
        self._mdns_failure_logged = False

    def _auth(self) -> Any:
        if not self._cfg.password:
            return None
        from requests.auth import HTTPDigestAuth

        return HTTPDigestAuth(self._cfg.username, self._cfg.password)

    def _get(self, ip: str, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self._session.get(
            f"http://{ip}/rpc/{method}", params=params or {}, timeout=self._cfg.timeout_s, auth=self._auth()
        )
        response.raise_for_status()
        return response.json()

    def _resolve_mdns(self) -> str | None:
        future = self._resolver.submit(socket.gethostbyname, self.hostname)
        try:
            return future.result(timeout=self._cfg.timeout_s)
        except (OSError, FutureTimeout) as exc:
            if not self._mdns_failure_logged:
                self._log.info("mDNS lookup of %s failed (%s); using last known or fallback IP", self.hostname, exc)
                self._mdns_failure_logged = True
            return None

    def _verify(self, ip: str) -> dict[str, Any] | None:
        try:
            info = self._get(ip, "Shelly.GetDeviceInfo")
        except Exception as exc:
            self._log.debug("RPC GetDeviceInfo on %s failed: %s", ip, exc)
            return None
        mac = str(info.get("mac", "")).lower().replace(":", "")
        dev_id = str(info.get("id", "")).lower()
        if mac == self._device_id or dev_id.endswith(self._device_id):
            return info
        self._log.warning(
            "IP %s answers as mac=%s id=%s, not %s: rejected (DHCP lease reshuffled?)", ip, mac, dev_id, self._device_id
        )
        return None

    def resolve(self) -> str | None:
        """Return a verified IP, or None. Sets ``resolved_via`` and ``device_info``."""
        candidates = [
            ("mdns", self._resolve_mdns()),
            ("last_known", self.last_good_ip),
            ("fallback", self._fallback_ip),
        ]
        tried: set[str] = set()
        for via, ip in candidates:
            if not ip or ip in tried:
                continue
            tried.add(ip)
            info = self._verify(ip)
            if info is not None:
                self.last_good_ip, self.resolved_via, self.device_info = ip, via, info
                return ip
        self.resolved_via = None
        self.error_count += 1
        self.last_error = f"could not reach a device with MAC {self._device_id} (tried {sorted(tried) or 'nothing'})"
        return None

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        ip = self.last_good_ip or self.resolve()
        if ip is None:
            return None
        try:
            return self._get(ip, method, params)
        except Exception as exc:
            # The IP may have changed: resolve and verify again once.
            self._log.debug("RPC %s on %s failed (%s); re-resolving", method, ip, exc)
            self.last_good_ip = None
            ip = self.resolve()
            if ip is None:
                return None
            try:
                return self._get(ip, method, params)
            except Exception as exc2:
                self.error_count += 1
                self.last_error = f"{method}: {type(exc2).__name__}: {exc2}"
                return None

    def get_status(self) -> dict[str, Any] | None:
        return self.call("Shelly.GetStatus")

    def switch_errors(self) -> list[str]:
        status = self.call("Switch.GetStatus", {"id": 0}) or {}
        return [str(e) for e in status.get("errors", [])]

    @property
    def firmware(self) -> str | None:
        return (self.device_info or {}).get("ver")


class ShellyPlug:
    def __init__(
        self,
        cfg: PlugConfig,
        ha: Any,
        logger: logging.Logger,
        dry_run: bool,
        rpc: ShellyRpcClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self.name = cfg.name
        self.device_id = cfg.device_id
        self._ha = ha
        self._log = logger
        self.dry_run = dry_run
        self.rpc = rpc
        self._clock = clock
        self._sleep = sleep or time.sleep
        self.entities = ShellyEntities(switch=None if cfg.switch_entity == "auto" else cfg.switch_entity)
        self.safety = SafetyMonitor(cfg)
        self.lockout_until: datetime | None = None
        self.lockout_reason: str | None = None
        self.simulate_failure = False  # demo 4: pretend the plug is unavailable
        self._sim_state: str | None = None
        self._sim_since: datetime | None = None
        self.commanded_state: str | None = None  # last state this app requested
        self.verify_failures = 0
        self.lockout_count = 0
        self.protection_trips = 0
        self.last_seen: datetime | None = None

    # ── discovery ───────────────────────────────────────────────────────────

    def discover(self) -> ShellyEntities:
        """Find this plug's entities by MAC. Raises RuntimeError if no switch entity can be determined."""
        explicit_switch = self.entities.switch if self.cfg.switch_entity != "auto" else None
        entity_ids: list[str] = []
        try:
            rendered = self._ha.render_template(_DISCOVERY_TEMPLATE.replace("__MAC__", self.device_id))
            entity_ids = json.loads(rendered) if isinstance(rendered, str) else list(rendered or [])
        except Exception as exc:
            self._log.warning("%s: entity discovery template failed: %s", self.name, exc)
        states = {e: self._ha.get_state(e, attribute="all") or {} for e in entity_ids}
        found = classify_entities(states)
        if explicit_switch:
            found.switch = explicit_switch
        if found.switch is None:
            raise RuntimeError(
                f"{self.name}: no switch entity found for MAC {self.device_id}. "
                f"Set house.plugs[].switch_entity explicitly in apps.yaml."
            )
        self.entities = found
        self._log.info("%s (%s): discovered entities %s", self.name, self.device_id, found)
        return found

    # ── state ───────────────────────────────────────────────────────────────

    def _entity_value(self, entity: str | None) -> float | None:
        if not entity:
            return None
        return _to_float(self._ha.get_state(entity))

    def _switch_state(self) -> tuple[str | None, datetime | None]:
        if self.entities.switch is None:
            return None, None
        full = self._ha.get_state(self.entities.switch, attribute="all") or {}
        return full.get("state"), _parse_dt(full.get("last_changed"))

    def real_state(self) -> str | None:
        """Actual switch state in HA, ignoring any dry-run simulation."""
        return self._switch_state()[0]

    def clear_simulation(self) -> None:
        self._sim_state = self._sim_since = None

    def is_available(self) -> bool:
        if self.simulate_failure:
            return False
        state, _ = self._switch_state()
        return state not in UNAVAILABLE_STATES

    def read_status(self) -> DeviceStatus:
        state, last_changed = self._switch_state()
        available = state not in UNAVAILABLE_STATES and not self.simulate_failure
        if available:
            self.last_seen = self._clock()
        simulated = False
        if self.dry_run and self._sim_state is not None:
            state, last_changed, simulated = self._sim_state, self._sim_since, True
        energy = self._entity_value(self.entities.energy)
        if energy is not None and self.entities.energy:
            unit = (self._ha.get_state(self.entities.energy, attribute="unit_of_measurement") or "kWh").lower()
            energy = energy / 1000.0 if unit == "wh" else energy
        power = self._entity_value(self.entities.power)
        real_state = self._switch_state()[0] if simulated else state
        return DeviceStatus(
            name=self.name,
            device_id=self.device_id,
            available=available,
            state=state if available else None,
            last_changed=last_changed,
            power_w=power,
            energy_kwh=energy,
            voltage_v=self._entity_value(self.entities.voltage),
            current_a=self._entity_value(self.entities.current),
            temperature_c=self._entity_value(self.entities.temperature),
            rssi_dbm=self._entity_value(self.entities.rssi),
            heater_active=real_state == "on" and power is not None and power > self.cfg.active_power_threshold_w,
            simulated=simulated,
        )

    # ── switching ───────────────────────────────────────────────────────────

    def turn_on(self, reason: str, dry_run: bool | None = None) -> SwitchResult:
        return self._switch("on", reason, self.dry_run if dry_run is None else dry_run)

    def turn_off(self, reason: str, dry_run: bool | None = None) -> SwitchResult:
        return self._switch("off", reason, self.dry_run if dry_run is None else dry_run)

    def _switch(self, target: str, reason: str, dry_run: bool) -> SwitchResult:
        if dry_run:
            before = self._sim_state or self._switch_state()[0]
            if before != target:
                self._sim_state, self._sim_since = target, self._clock()
            else:
                self._sim_state = target
            self._log.info("%s: [dry-run] would switch %s → %s (%s)", self.name, before, target, reason)
            return SwitchResult(self.name, before, target, target, True, 0, True)

        before, _ = self._switch_state()
        self.commanded_state = target
        service = f"switch/turn_{target}"
        attempts = 0
        state = before
        for attempt in range(4):  # first try + 3 retries
            attempts = attempt + 1
            try:
                self._ha.call_service(service, entity_id=self.entities.switch)
            except Exception as exc:
                self._log.warning("%s: %s failed (attempt %d): %s", self.name, service, attempts, exc)
            for delay in (0.3, 0.7, 1.0):
                self._sleep(delay)
                state, _ = self._switch_state()
                if state == target:
                    break
            if state == target:
                break
            if attempt < 3:
                self._sleep(0.5 * 2**attempt)
        verified = state == target
        if verified:
            self._sim_state = None  # a real switch resets any dry-run simulation
            self._log.info("%s: %s → %s verified after %d attempt(s) (%s)", self.name, before, target, attempts, reason)
        else:
            self.verify_failures += 1
            self._log.error(
                "%s: switch to %s NOT verified after %d attempts, state=%s", self.name, target, attempts, state
            )
        return SwitchResult(
            self.name,
            before,
            state,
            target,
            verified,
            attempts,
            False,
            error=None if verified else f"read-back {state!r} != {target!r}",
        )

    # ── safety ──────────────────────────────────────────────────────────────

    def is_locked_out(self, now: datetime) -> bool:
        if self.lockout_until is not None and now >= self.lockout_until:
            self._log.info("%s: lockout expired (%s)", self.name, self.lockout_reason)
            self.lockout_until = self.lockout_reason = None
        return self.lockout_until is not None

    def lock_out(self, now: datetime, reason: str) -> None:
        self.lockout_until = next_full_hour(now)
        self.lockout_reason = reason
        self.lockout_count += 1
        self.safety.reset()
        self._log.warning("%s: LOCKED OUT until %s: %s", self.name, self.lockout_until.isoformat(), reason)

    def check_safety(self, now: datetime) -> str | None:
        """Evaluate the live readings. On a trip: switch off (always for real, even in dry-run) and lock out."""
        if self.entities.switch is None:
            return None
        status = self.read_status()
        reason = self.safety.evaluate(now, status.power_w, status.current_a, status.voltage_v, status.temperature_c)
        if (
            reason is None
            and status.power_w is not None
            and status.power_w > self.cfg.plug_max_power_w
            and not self.safety.above_plug_rating_logged
        ):
            self.safety.above_plug_rating_logged = True
            self._log.info(
                "%s: power %.0f W above plug rating %.0f W at %.0f V (expected for a 3000 W heater above 230 V)",
                self.name,
                status.power_w,
                self.cfg.plug_max_power_w,
                status.voltage_v or 0,
            )
        if reason is None:
            return None
        self._switch("off", f"safety: {reason}", dry_run=False)
        self.lock_out(now, reason)
        return reason

    def protection_flags(self) -> list[str]:
        """Device-protection error flags from the HA binary sensors and, if enabled, the plug's RPC status."""
        flags = [e for e in self.entities.protection if self._ha.get_state(e) == "on"]
        if self.rpc is not None:
            try:
                flags += self.rpc.switch_errors()
            except Exception as exc:
                self._log.debug("%s: RPC switch_errors failed: %s", self.name, exc)
        return flags

    def check_protection_trip(self, now: datetime) -> str | None:
        """Called when the plug turned off while the app wanted it on. Returns the trip reason, or None (manual)."""
        flags = self.protection_flags()
        if not flags:
            return None
        reason = f"device protection tripped: {', '.join(flags)}"
        self.protection_trips += 1
        self.lock_out(now, reason)
        return reason
