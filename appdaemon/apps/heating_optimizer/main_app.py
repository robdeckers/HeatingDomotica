"""AppDaemon app: the clock-aligned main loop, enable/disable handling, safety listeners, diagnostics sensors."""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from heating_optimizer.config import AppConfig, ConfigError, parse_config
from heating_optimizer.counters import SwitchOnCounters
from heating_optimizer.decision import Action, Decision, DecisionInput, ReasonCode, decide
from heating_optimizer.decision_log import DecisionLogger
from heating_optimizer.devices.base import SwitchResult
from heating_optimizer.devices.shelly_plug import ShellyPlug, ShellyRpcClient
from heating_optimizer.diagnostics import Diagnostics, Health, evaluate_health
from heating_optimizer.house import House, HouseSnapshot
from heating_optimizer.logging_setup import setup_logger
from heating_optimizer.prices.base import Commodity
from heating_optimizer.prices.energyzero import EnergyZeroProvider
from heating_optimizer.prices.service import HeatPrices, PriceService
from heating_optimizer.tariffs import to_excl_vat
from heating_optimizer.scheduling import drift_ms, hour_start, nearest_aligned_tick, next_aligned_tick

SENSOR_PREFIX = "sensor.heating_optimizer_"
STATUS_SENSOR = f"{SENSOR_PREFIX}status"
HEALTH_SENSOR = f"{SENSOR_PREFIX}health"
NOTIFICATION_ID = "heating_optimizer"
SAFETY_SWEEP_S = 5


def switch_on_count_sensor(plug_name: str) -> str:
    return f"{SENSOR_PREFIX}{re.sub(r'[^a-z0-9_]+', '_', plug_name.lower())}_switch_on_count"


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


class MainApp(hass.Hass):
    # ── lifecycle ───────────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.ready = False
        self._tick_lock = threading.Lock()
        self._paused_by: str | None = None
        self._last_hour: datetime | None = None
        self._last_heat: HeatPrices | None = None
        self._last_decision: Decision | None = None
        try:
            self.cfg: AppConfig = parse_config(self.args.get("config", {}))
        except ConfigError as exc:
            self.log(str(exc), level="ERROR")
            self._notify("Heating optimizer: config error", "\n".join(f"- {p}" for p in exc.problems))
            self._publish(STATUS_SENSOR, state="config_error", attributes={"problems": exc.problems})
            return

        cfg = self.cfg
        self.app_log, log_error = setup_logger(
            cfg.logging.decision_log_dir, cfg.logging.level, cfg.logging.rotate_mb, cfg.logging.backups, self._forward
        )
        self.diag = Diagnostics(started_at=self._now())
        if log_error:
            self.app_log.error("%s; continuing with the AppDaemon log only", log_error)
        self.decision_log: DecisionLogger | None = None
        try:
            self.decision_log = DecisionLogger(cfg.logging.decision_log_dir, cfg.logging.rotate_mb, cfg.logging.backups)
        except OSError as exc:
            self.diag.decision_log_error = f"cannot write decision log in {cfg.logging.decision_log_dir}: {exc}"
            self.app_log.error(self.diag.decision_log_error)
        self.switch_on_counters = SwitchOnCounters(
            Path(cfg.logging.decision_log_dir) / "switch_on_counts.json", self.app_log
        )

        self.provider = EnergyZeroProvider(
            self,
            cfg.electricity.energyzero_config_entry,
            {
                Commodity.ELECTRICITY: (cfg.electricity.fallback_sensor, cfg.electricity.fallback_sensor_includes_vat),
                Commodity.GAS: (cfg.gas.fallback_sensor, cfg.gas.fallback_sensor_includes_vat),
            },
            cfg.electricity.vat_rate,
            self.app_log,
        )
        self.prices = PriceService(self.provider, cfg, self.app_log)

        session = None
        if cfg.shelly_rpc.enabled:
            import requests

            session = requests.Session()  # one shared HTTP session for all RPC diagnostics
        self.plugs = [
            ShellyPlug(
                plug_cfg,
                self,
                self.app_log,
                rpc=ShellyRpcClient(cfg.shelly_rpc, plug_cfg.device_id, plug_cfg.fallback_ip, session, self.app_log)
                if session is not None
                else None,
            )
            for plug_cfg in cfg.house.plugs
        ]
        self.house = House(cfg.house.name, list(self.plugs), cfg.tz, self.app_log)

        interval_s = cfg.loop.interval_minutes * 60
        self.run_every(self._on_tick, next_aligned_tick(self._now(), cfg.loop.interval_minutes), interval_s)
        self.run_daily(self._on_daily_refresh, cfg.prices.daily_refresh_time)
        self.listen_state(self._on_control_change, cfg.control_entity)
        self.run_in(self._on_startup, 1)

    def _publish(self, entity: str, state: Any, attributes: dict[str, Any]) -> None:
        """Set a sensor, replacing (not merging) its attributes so stale ones disappear."""
        self.set_state(entity, state=state, attributes=attributes, replace=True)

    def _forward(self, message: str, level: str) -> None:
        self.log(message, level=level)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _on_startup(self, kwargs: dict[str, Any]) -> None:
        """Self-test, plug discovery and safety listeners; then the first evaluation. Holds the tick lock."""
        with self._tick_lock:
            self._self_test()
            self._register_plug_listeners()
            for plug in self.plugs:
                self._publish_switch_on_count(plug)
            self.ready = True
        self.app_log.info(
            "Heating optimizer started; next aligned tick %s",
            next_aligned_tick(self._now(), self.cfg.loop.interval_minutes).astimezone(self.cfg.tz).isoformat(),
        )
        if self.cfg.loop.evaluate_on_startup:
            self.evaluate("startup")
        else:
            self._publish_health(self._now())

    def _self_test(self) -> None:
        cfg, now = self.cfg, self._now()
        problems: list[str] = []
        self.app_log.info("Config: %s", cfg.masked())
        offset = now.astimezone(cfg.tz).utcoffset()
        self.app_log.info("Local time %s (UTC offset %s)", now.astimezone(cfg.tz).isoformat(), offset)

        if self.get_state(cfg.control_entity) is None:
            problems.append(f"entity {cfg.control_entity} does not exist")
        override = cfg.decision.margin_override_entity
        if override and self.get_state(override) is None:
            self.app_log.info("Optional margin helper %s not found; using margin_on from the config", override)
        elif override:
            self.app_log.info("Switch-on margin comes from %s: %s ct/kWh", override, self.get_state(override))

        for plug in self.plugs:
            try:
                entities = plug.discover()
                for entity in [entities.switch, *entities.sensor_entities()]:
                    if entity and self.get_state(entity) is None:
                        problems.append(f"{plug.name}: entity {entity} does not exist")
                if entities.current is None:
                    problems.append(f"{plug.name}: no current sensor found; the overcurrent check cannot work")
            except RuntimeError as exc:
                problems.append(str(exc))
            if plug.rpc is not None:
                ip = plug.rpc.resolve()
                if ip:
                    self.app_log.info(
                        "%s: RPC reachable at %s via %s, firmware %s",
                        plug.name,
                        ip,
                        plug.rpc.resolved_via,
                        plug.rpc.firmware,
                    )
                else:
                    self.app_log.warning("%s: RPC diagnostics unavailable: %s", plug.name, plug.rpc.last_error)

        self.prices.refresh(now)
        heat = self.prices.heat_prices(now)
        if not heat.complete:
            problems.append(f"price fetch incomplete: {self.prices.last_error or 'no price for now'}")

        self.diag.self_test_ok = not problems
        self.diag.self_test_problems = problems
        if problems:
            self.app_log.error("Self-test FAILED: %s", "; ".join(problems))
            self._notify("Heating optimizer: self-test failed", "\n".join(f"- {p}" for p in problems))
        else:
            self.app_log.info("Self-test passed")

    def _register_plug_listeners(self) -> None:
        for plug in self.plugs:
            for entity in plug.entities.sensor_entities():
                self.listen_state(self._on_plug_sensor, entity, plug_name=plug.name)
            if plug.entities.switch:
                self.listen_state(self._on_plug_switch, plug.entities.switch, plug_name=plug.name)
        self.run_every(self._on_safety_sweep, self._now() + timedelta(seconds=SAFETY_SWEEP_S), SAFETY_SWEEP_S)

    # ── demo coordination (called from the DemoApp) ─────────────────────────

    def pause_for_demo(self, demo: str) -> bool:
        """Stop the main loop from switching while a demo runs. Waits for a running tick to finish."""
        if not self._tick_lock.acquire(timeout=30):
            return False
        try:
            if self._paused_by:
                return False
            self._paused_by = demo
        finally:
            self._tick_lock.release()
        self.app_log.info("Main loop paused for demo %s", demo)
        self._publish_status(self._now())
        return True

    def resume_after_demo(self) -> None:
        self._paused_by = None
        self.app_log.info("Main loop resumed after demo")
        self.run_in(lambda kwargs: self.evaluate("resume"), 1)

    # ── callbacks ───────────────────────────────────────────────────────────

    def _on_tick(self, kwargs: dict[str, Any]) -> None:
        self.evaluate("aligned")

    def _on_daily_refresh(self, kwargs: dict[str, Any]) -> None:
        self.prices.refresh_if_needed(self._now(), force_reason="daily refresh")

    def _on_control_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        if not self.ready:
            return
        if old == "on" and new == "off":
            self._disable()
        elif new == "on" and old != "on":
            self.app_log.info("Optimizer enabled")
            self.evaluate("enabled")

    def _disable(self) -> None:
        """On the on→off transition: switch the plugs off once, then stay hands-off."""
        if not self._tick_lock.acquire(timeout=30):
            self.app_log.error("Could not disable: a tick is stuck")
            return
        try:
            now = self._now()
            results = self.house.set_heating(False, "optimizer disabled", now)
            self._count_switches(results, now)
            self.app_log.info("Optimizer disabled: plugs switched off once, now hands-off")
            self._logbook("Disabled: plugs switched off; manual control in HA is not overridden")
            self._write_record(
                now,
                "disabled",
                False,
                Action.DISABLED,
                ReasonCode.DISABLED,
                "Optimizer disabled",
                self.prices.heat_prices(now),
                None,
                results,
                0.0,
            )
        finally:
            self._tick_lock.release()
        self._publish_status(self._now())

    def _on_plug_sensor(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        self._check_plug_safety(self.house.device(kwargs["plug_name"]))

    def _on_safety_sweep(self, kwargs: dict[str, Any]) -> None:
        # A constant overcurrent produces no state changes, so the duration criterion also needs a periodic check.
        for plug in self.plugs:
            self._check_plug_safety(plug)

    def _check_plug_safety(self, plug: ShellyPlug) -> None:
        try:
            reason = plug.check_safety(self._now())
        except Exception:
            self.app_log.exception("%s: safety check failed", plug.name)
            return
        if reason:
            self._notify(
                f"Heating optimizer: {plug.name} switched off for safety",
                f"{reason}. Locked out until the next full hour.",
            )
            self._logbook(f"{plug.name} SAFETY OFF: {reason}")
            self._publish_health(self._now())

    def _on_plug_switch(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        plug = self.house.device(kwargs["plug_name"])
        if old == "off" and new == "on":  # any switch-on: by the app, a demo or by hand (not unavailable → on)
            self.switch_on_counters.increment(plug.device_id, self._now())
            self._publish_switch_on_count(plug)
            return
        if not (old == "on" and new == "off" and plug.commanded_state == "on"):
            return
        now = self._now()
        reason = plug.check_protection_trip(now)
        if reason:
            self._notify(
                f"Heating optimizer: {plug.name} protection trip", f"{reason}. Locked out until the next full hour."
            )
            self._logbook(f"{plug.name} turned off by device protection: {reason}")
            self._publish_health(now)
        else:
            self.app_log.info(
                "%s: switched off outside the app (manual); the next tick re-applies the decision", plug.name
            )

    # ── main loop ───────────────────────────────────────────────────────────

    def evaluate(self, tick: str) -> None:
        if not self.ready:
            return
        if not self._tick_lock.acquire(blocking=False):
            self.diag.ticks_skipped += 1
            self.app_log.warning("Tick (%s) skipped: previous tick still running", tick)
            return
        started = time.monotonic()
        now = self._now()
        try:
            self._evaluate(tick, now, started)
            self.diag.last_tick_failed = False
        except Exception:
            self.diag.ticks_failed += 1
            self.diag.last_tick_failed = True
            self.app_log.exception("Tick (%s) failed", tick)
        finally:
            self._tick_lock.release()
        try:
            self._publish_status(now)
            self._publish_health(now)
        except Exception:
            self.app_log.exception("Publishing sensors failed")

    def _evaluate(self, tick: str, now: datetime, started: float) -> None:
        cfg = self.cfg
        scheduled = nearest_aligned_tick(now, cfg.loop.interval_minutes) if tick == "aligned" else None
        drift = drift_ms(now, scheduled) if scheduled else None
        self.diag.record_tick(scheduled, now, drift)
        if drift is not None and abs(drift) > 2000:
            self.app_log.warning("Tick drift %.0f ms", drift)

        current_hour = hour_start(now)
        hour_change = self._last_hour is not None and current_hour != self._last_hour
        # At an hour change a missing price is refreshed immediately, bypassing the failure back-off.
        missing_at_hour_change = hour_change and self.prices.electricity(now) is None
        self.prices.refresh_if_needed(now, "hour change, current price missing" if missing_at_hour_change else None)
        heat = self.prices.heat_prices(now)
        if hour_change:
            prev = self._last_heat
            self.app_log.info(
                "HOUR_CHANGE %s: electric heat %s → %s €/kWh, gas heat %s → %s €/kWh",
                current_hour.astimezone(cfg.tz).strftime("%H:%M"),
                _r(prev.elec_per_kwh_heat) if prev else None,
                _r(heat.elec_per_kwh_heat),
                _r(prev.gas_per_kwh_heat) if prev else None,
                _r(heat.gas_per_kwh_heat),
            )

        snapshot = self.house.snapshot(now)
        decision: Decision | None = None
        results: list[SwitchResult] = []
        if self._paused_by:
            action, code, reason = Action.HOLD, ReasonCode.DEMO_RUNNING, f"Demo {self._paused_by} running"
        elif self.get_state(cfg.control_entity) != "on":
            action, code, reason = Action.DISABLED, ReasonCode.DISABLED, "Optimizer disabled: hands-off"
        else:
            decision = decide(self._decision_input(now, heat, snapshot))
            action, code, reason = decision.action, decision.reason_code, decision.reason
            results = self.house.set_heating(decision.wants_on, reason, now)
            changed = self._count_switches(results, now)
            if changed and action in (Action.ON, Action.OFF):
                self._logbook(f"{action}: {reason}")
            if decision.reason_code is ReasonCode.STALE_DATA:
                self.app_log.warning(reason)
            self._last_decision = decision

        duration = (time.monotonic() - started) * 1000.0
        self._write_record(now, tick, hour_change, action, code, reason, heat, decision, results, duration, snapshot)
        self.app_log.info("%s %s: %s", action, code, reason)
        self._publish_prices(heat)
        self._publish(
            f"{SENSOR_PREFIX}last_decision",
            state=str(action),
            attributes={"reason_code": str(code), "reason": reason, "timestamp": now.astimezone(cfg.tz).isoformat()},
        )
        self._last_hour = current_hour
        self._last_heat = heat

    def _decision_input(self, now: datetime, heat: HeatPrices, snapshot: HouseSnapshot) -> DecisionInput:
        d = self.cfg.decision
        margin_on = d.margin_on_eur_per_kwh
        if d.margin_override_entity:
            try:
                override = float(self.get_state(d.margin_override_entity))
                margin_on = override / 100.0  # helper is in ct/kWh
            except (TypeError, ValueError):
                pass
        return DecisionInput(
            now=now,
            elec_per_kwh_heat=heat.elec_per_kwh_heat,
            gas_per_kwh_heat=heat.gas_per_kwh_heat,
            heating_on=snapshot.heating_on,
            state_since=snapshot.state_since,
            margin_on=margin_on,
            margin_off=d.margin_off_eur_per_kwh,
            min_on_minutes=d.min_on_minutes,
            min_off_minutes=d.min_off_minutes,
            all_locked_out=snapshot.all_locked_out,
        )

    def _count_switches(self, results: list[SwitchResult], now: datetime) -> int:
        changed = [r for r in results if r.before != r.after and not r.skipped]
        for r in changed:
            self.diag.switches.record(r.name, now)
        return len(changed)

    # ── output ──────────────────────────────────────────────────────────────

    def _write_record(
        self,
        now: datetime,
        tick: str,
        hour_change: bool,
        action: Action,
        code: ReasonCode,
        reason: str,
        heat: HeatPrices,
        decision: Decision | None,
        results: list[SwitchResult],
        duration_ms: float,
        snapshot: HouseSnapshot | None = None,
    ) -> None:
        tz = self.cfg.tz
        elec, gas = heat.elec, heat.gas
        powers = {s.name: s for s in snapshot.devices} if snapshot else {}
        inputs = decision.inputs if decision else None
        record = {
            "ts": now.astimezone(tz).isoformat(timespec="seconds"),
            "tick": tick,
            "hour_change": hour_change,
            "action": str(action),
            "reason_code": str(code),
            "reason": reason,
            "elec": None
            if elec is None
            else {
                **elec.tariff.as_dict(),
                "consumer_kwh": _r(elec.consumer_incl_vat, 5),
                "per_kwh_heat": _r(heat.elec_per_kwh_heat, 5),
                "applies_to": f"{elec.applies_from.astimezone(tz):%H:%M}-{elec.applies_to.astimezone(tz):%H:%M}",
                "resolution": elec.resolution,
                "source": elec.source,
                "age_s": round(elec.age_s),
            },
            "gas": None
            if gas is None
            else {
                **gas.tariff.as_dict(),
                "consumer_m3": _r(gas.consumer_incl_vat, 5),
                "per_kwh_fuel": _r(heat.gas_per_kwh_fuel, 5),
                "per_kwh_heat": _r(heat.gas_per_kwh_heat, 5),
                "gas_day": heat.gas_day.isoformat(),
                "source": gas.source,
            },
            "margins": {"on": inputs.margin_on, "off": inputs.margin_off} if inputs else None,
            "timers": {
                "heating_on": inputs.heating_on,
                "state_since": inputs.state_since.astimezone(tz).isoformat() if inputs.state_since else None,
                "minutes_in_state": _r(inputs.minutes_in_state(), 1),
            }
            if inputs
            else None,
            "devices": [
                {
                    **r.as_dict(),
                    "power_w": powers[r.name].power_w if r.name in powers else None,
                    "heater_active": powers[r.name].heater_active if r.name in powers else None,
                }
                for r in results
            ],
            "duration_ms": round(duration_ms),
        }
        if self.decision_log is None:
            return
        try:
            self.decision_log.write(record)
        except OSError as exc:
            self.diag.decision_log_error = str(exc)
            self.app_log.error("Writing decision log failed: %s", exc)

    def _publish_prices(self, heat: HeatPrices) -> None:
        unit = {"unit_of_measurement": "€/kWh", "state_class": "measurement"}
        elec_attrs = heat.elec.tariff.as_dict() if heat.elec else {}
        if heat.elec:
            elec_attrs.update(source=heat.elec.source, resolution=heat.elec.resolution)
        gas_attrs = heat.gas.tariff.as_dict() if heat.gas else {}
        if heat.gas:
            gas_attrs.update(
                gas_day=heat.gas_day.isoformat(),
                per_kwh_fuel=_r(heat.gas_per_kwh_fuel, 5),
                break_even_elec_consumer=_r(heat.break_even_elec_consumer, 5),
            )
        for name, value, attrs in (
            ("elec_heat_price", heat.elec_per_kwh_heat, elec_attrs),
            ("gas_heat_price", heat.gas_per_kwh_heat, gas_attrs),
            ("price_delta", heat.delta, {"meaning": "gas heat minus electric heat; positive = electric cheaper"}),
        ):
            self._publish(
                f"{SENSOR_PREFIX}{name}",
                state=_r(value) if value is not None else "unknown",
                attributes={**unit, **attrs},
            )
        self._publish_market_price(heat)

    def _publish_market_price(self, heat: HeatPrices) -> None:
        """Raw EnergyZero electricity price excl. VAT: the same quote the decision used (cache or fallback sensor)."""
        elec, tz = heat.elec, self.cfg.tz
        attrs: dict[str, Any] = {
            "friendly_name": "Electricity market price",
            "icon": "mdi:transmission-tower",
            "unit_of_measurement": "€/kWh",
            "state_class": "measurement",
        }
        if elec is None:
            self._publish(f"{SENSOR_PREFIX}elec_market_price", state="unknown", attributes=attrs)
            return
        tariff = elec.tariff
        attrs.update(
            market_incl_vat=_r(tariff.market_incl_vat, 5),
            vat_rate=tariff.vat_rate,
            applies_from=elec.applies_from.astimezone(tz).isoformat(),
            applies_to=elec.applies_to.astimezone(tz).isoformat(),
            resolution=elec.resolution,
            source=elec.source,
        )
        self._publish(
            f"{SENSOR_PREFIX}elec_market_price",
            state=_r(to_excl_vat(tariff.market_incl_vat, tariff.vat_rate), 5),
            attributes=attrs,
        )

    def _publish_switch_on_count(self, plug: ShellyPlug) -> None:
        counter = self.switch_on_counters.get(plug.device_id)
        self._publish(
            switch_on_count_sensor(plug.name),
            state=counter["count"],
            attributes={
                "friendly_name": f"{plug.name} switch-ons",
                "icon": "mdi:counter",
                "unit_of_measurement": "switch-ons",
                "state_class": "total_increasing",
                "device_id": plug.device_id,
                "counting_since": counter["since"],
                "last_switched_on": counter["last_on"],
            },
        )

    def _status_state(self) -> str:
        if self._paused_by:
            return "paused"
        if self.get_state(self.cfg.control_entity) != "on":
            return "disabled"
        if self._last_decision and self._last_decision.reason_code is ReasonCode.STALE_DATA:
            return "failsafe"
        return "running"

    def _publish_status(self, now: datetime) -> None:
        tz = self.cfg.tz
        self._publish(
            STATUS_SENSOR,
            state=self._status_state(),
            attributes={
                "last_tick": self.diag.last_tick.astimezone(tz).isoformat() if self.diag.last_tick else None,
                "next_tick": next_aligned_tick(now, self.cfg.loop.interval_minutes).astimezone(tz).isoformat(),
                "drift_ms": _r(self.diag.last_drift_ms, 1),
                "demo": self._paused_by,
            },
        )

    def _publish_health(self, now: datetime) -> None:
        statuses = {s.name: s for s in (p.read_status() for p in self.plugs)}
        state, attrs = evaluate_health(
            now,
            self.cfg,
            self.diag,
            self.prices,
            self._last_heat,
            list(self.plugs),
            statuses,
            {
                "latency_ms": _r(self.provider.last_latency_ms, 0),
                "error_count": self.provider.error_count,
                "last_error": self.provider.last_error,
            },
        )
        if state is not Health.OK:
            self.app_log.debug("Health %s: %s", state, attrs["checks"])
        self._publish(HEALTH_SENSOR, state=str(state), attributes=attrs)

    def _notify(self, title: str, message: str) -> None:
        try:
            self.call_service(
                "persistent_notification/create", title=title, message=message, notification_id=NOTIFICATION_ID
            )
        except Exception as exc:
            self.log(f"persistent_notification failed: {exc}", level="WARNING")

    def _logbook(self, message: str) -> None:
        try:
            self.call_service("logbook/log", name="Heating optimizer", message=message, entity_id=STATUS_SENSOR)
        except Exception as exc:
            self.app_log.warning("logbook.log failed: %s", exc)
