"""AppDaemon app: the four hardware demos, triggered by ``input_button.demo_*``.

Only one demo runs at a time. A demo pauses the main loop, runs its steps with ``run_in`` (no blocking sleep), writes
every line to the log and to ``sensor.heating_optimizer_demo_output``, and restores the plugs at the end or when
``input_button.demo_stop`` is pressed. Demos switch the plugs for real, because switching is what they test.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from heating_optimizer.scheduling import local_day_bounds

DEMO_BUTTONS = {
    "input_button.demo_electricity_price": "electricity_price",
    "input_button.demo_gas_price": "gas_price",
    "input_button.demo_shelly_plug": "shelly_plug",
    "input_button.demo_house": "house",
}
STOP_BUTTON = "input_button.demo_stop"
OUTPUT_SENSOR = "sensor.heating_optimizer_demo_output"
HISTORY_LINES = 50

Step = tuple[float, Callable[[], None]]


@dataclass
class CheckResult:
    name: str
    status: str  # PASS, FAIL, WARN, SKIPPED
    detail: str = ""


class DemoApp(hass.Hass):
    def initialize(self) -> None:
        self.main_app_name = self.args.get("main_app", "heating_optimizer_main")
        self._running: str | None = None
        self._handles: list[Any] = []
        self._lines: deque[str] = deque(maxlen=HISTORY_LINES)
        self._checks: list[CheckResult] = []
        self._restore: dict[str, str] = {}
        self._paused_main = False
        for entity, demo in DEMO_BUTTONS.items():
            self.listen_state(self._on_button, entity, demo=demo)
        self.listen_state(self._on_stop, STOP_BUTTON)

    # ── framework ───────────────────────────────────────────────────────────

    def _main(self) -> Any:
        app = self.get_app(self.main_app_name)
        return app if app is not None and getattr(app, "ready", False) else None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _out(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"{stamp} {line}"
        self._lines.append(text)
        self.log(f"[demo {self._running}] {line}")
        self.set_state(
            OUTPUT_SENSOR,
            state=text[:250],
            attributes={"demo": self._running, "history": list(self._lines)},
            replace=True,
        )

    def _check(self, name: str, status: str, detail: str = "") -> None:
        self._checks.append(CheckResult(name, status, detail))
        self._out(f"{status}: {name}{' – ' + detail if detail else ''}")

    def _notify(self, title: str, message: str) -> None:
        self.call_service(
            "persistent_notification/create", title=title, message=message, notification_id="heating_optimizer_demo"
        )

    def _on_button(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        demo = kwargs["demo"]
        if new in (None, "unavailable", "unknown") or old in (None, "unavailable"):
            return
        if self._running:
            self._notify("Heating optimizer demo", f"Demo {self._running} is still running; {demo} ignored.")
            return
        builders: dict[str, Callable[[Any], list[Step]]] = {
            "electricity_price": self._demo_electricity,
            "gas_price": self._demo_gas,
            "shelly_plug": self._demo_shelly,
            "house": self._demo_house,
        }
        main = self._main()
        if main is None and demo != "electricity_price":
            self._notify("Heating optimizer demo", "The main app is not running (config error?). Check the log.")
            return
        self._running = demo
        self._checks = []
        self._restore = {}
        self._lines.clear()
        self._paused_main = bool(main and main.pause_for_demo(demo))
        self._notify(f"Demo {demo} started", self._start_message(demo))
        try:
            steps = builders[demo](main)
        except Exception as exc:
            self._check("demo setup", "FAIL", str(exc))
            self._finish(stopped=False)
            return
        for offset, fn in steps:
            self._handles.append(self.run_in(self._run_step, offset, fn=fn))
        self._handles.append(self.run_in(self._run_finish, steps[-1][0] + 0.5 if steps else 0))

    @staticmethod
    def _start_message(demo: str) -> str:
        if demo == "shelly_plug":
            return "Switch the heaters ON by hand if you want the power check. Plugs will be switched and restored."
        return "Running for about 30 s. Press 'demo stop' to abort."

    def _run_step(self, kwargs: dict[str, Any]) -> None:
        if not self._running:
            return
        try:
            kwargs["fn"]()
        except Exception as exc:
            self._check("step", "FAIL", f"{type(exc).__name__}: {exc}")

    def _run_finish(self, kwargs: dict[str, Any]) -> None:
        if self._running:
            self._finish(stopped=False)

    def _on_stop(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        if self._running and old not in (None, "unavailable"):
            self._out("STOP pressed: aborting and restoring plugs")
            self._finish(stopped=True)

    def _finish(self, stopped: bool) -> None:
        for handle in self._handles:
            try:
                self.cancel_timer(handle)
            except Exception:
                pass
        self._handles = []
        self._restore_plugs()
        failed = [c for c in self._checks if c.status == "FAIL"]
        verdict = "STOPPED" if stopped else ("FAIL" if failed or not self._checks else "PASS")
        summary = "\n".join(f"- {c.status}: {c.name}{' – ' + c.detail if c.detail else ''}" for c in self._checks)
        self._out(f"Demo {self._running} finished: {verdict}")
        self._notify(f"Demo {self._running}: {verdict}", summary or "no checks ran")
        main = self._main()
        if self._paused_main and main is not None:
            main.resume_after_demo()
        self._paused_main = False
        self._running = None

    def _remember(self, plugs: list[Any]) -> None:
        for plug in plugs:
            self._restore.setdefault(plug.name, plug.real_state())

    def _restore_plugs(self) -> None:
        main = self._main()
        if main is None or not self._restore:
            return
        for name, state in self._restore.items():
            plug = main.house.device(name)
            plug.simulate_failure = False
            if state in ("on", "off") and plug.real_state() != state:
                result = plug.turn_on("demo restore") if state == "on" else plug.turn_off("demo restore")
                self._out(f"{name}: restored to {state} (verified={result.verified})")
        self._restore = {}

    # ── demo 1: electricity ─────────────────────────────────────────────────

    def _discover_energyzero(self) -> None:
        sensors = self.get_state("sensor") or {}
        found = sorted(e for e in sensors if "energyzero" in e)
        self._out(f"EnergyZero entities: {', '.join(found) or 'none found'}")
        if found:
            entry = self.render_template(f"{{{{ config_entry_id('{found[0]}') }}}}")
            self._out(f"EnergyZero config entry id: {entry}  → electricity.energyzero_config_entry")

    def _demo_electricity(self, main: Any) -> list[Step]:
        if main is None:
            # Discovery mode: the main app is not running yet, typically because the config entry is still TODO.
            def discovery() -> None:
                self._discover_energyzero()
                self._check("main app running", "FAIL", "fill in the config entry above and restart AppDaemon")

            return [(0, discovery)]

        tz = main.cfg.tz
        prices = main.prices

        def curve() -> None:
            self._discover_energyzero()
            now = self._now()
            prices.refresh(now)
            self._out(f"Resolution mode: {main.cfg.electricity.price_resolution}")
            start, end = local_day_bounds(now.astimezone(tz).date(), tz)
            hours = []
            cursor = start.astimezone(UTC)
            while cursor < end.astimezone(UTC):
                lookup = prices.elec_cache.electricity_at(cursor, "hour")
                if lookup is not None:
                    quote_total = lookup.price_incl_vat + main.cfg.electricity.supplier_markup_eur_per_kwh_incl_vat
                    quote_total += main.cfg.electricity.energy_tax_eur_per_kwh_incl_vat
                    hours.append((cursor.astimezone(tz), lookup.price_incl_vat, quote_total))
                cursor += timedelta(hours=1)
            for ts, market, consumer in hours:
                self._out(f"  {ts:%H:%M}  market €{market:.4f}  consumer €{consumer:.4f}/kWh")
            if hours:
                ranked = sorted(hours, key=lambda h: h[2])
                self._out("Cheapest hours: " + ", ".join(f"{h[0]:%H}h €{h[2]:.3f}" for h in ranked[:3]))
                self._out("Most expensive: " + ", ".join(f"{h[0]:%H}h €{h[2]:.3f}" for h in ranked[-3:]))
            expected = round((end - start).total_seconds() / 3600)
            self._check(
                "price curve today", "PASS" if len(hours) == expected else "FAIL", f"{len(hours)}/{expected} hours"
            )

        def sample() -> None:
            now = self._now()
            started = time.monotonic()
            ok = prices.refresh(now)
            latency = (time.monotonic() - started) * 1000
            quote = prices.electricity(now)
            if quote is None:
                self._check("current electricity price", "FAIL", prices.last_error or "missing")
                return
            t = quote.tariff.as_dict()
            self._out(
                f"market €{t['market_incl_vat']:.4f} + ANWB €{t['markup_incl_vat']:.4f} + tax €{t['tax_incl_vat']:.4f}"
                f" = consumer €{quote.consumer_incl_vat:.4f}/kWh incl. VAT (VAT €{t['vat_amount']:.4f})"
            )
            self._out(
                f"applies {quote.applies_from.astimezone(tz):%H:%M}-{quote.applies_to.astimezone(tz):%H:%M}, "
                f"source {quote.source}, age {quote.age_s:.0f} s, call latency {latency:.0f} ms, fetch ok={ok}"
            )
            self._check(
                "electricity sample",
                "PASS" if ok and quote.source == "cache" and -1 < quote.consumer_incl_vat < 2 else "FAIL",
                f"€{quote.consumer_incl_vat:.4f}/kWh",
            )

        return [(0, curve)] + [(5 * i + 1, sample) for i in range(6)]

    # ── demo 2: gas ─────────────────────────────────────────────────────────

    def _demo_gas(self, main: Any) -> list[Step]:
        tz = main.cfg.tz
        prices = main.prices

        def sample() -> None:
            now = self._now()
            prices.refresh_if_needed(now)
            heat = prices.heat_prices(now)
            gas = heat.gas
            if gas is None:
                self._check("current gas price", "FAIL", prices.last_error or "missing")
                return
            t = gas.tariff.as_dict()
            self._out(
                f"gas day {heat.gas_day} ({gas.applies_from.astimezone(tz):%d-%m %H:%M} – "
                f"{gas.applies_to.astimezone(tz):%d-%m %H:%M}), source {gas.source}, age {gas.age_s:.0f} s"
            )
            self._out(
                f"market €{t['market_incl_vat']:.4f} + ANWB €{t['markup_incl_vat']:.4f} + tax €{t['tax_incl_vat']:.4f}"
                f" = €{gas.consumer_incl_vat:.4f}/m³ → fuel €{heat.gas_per_kwh_fuel:.4f}/kWh → heat "
                f"€{heat.gas_per_kwh_heat:.4f}/kWh"
            )
            if heat.elec is not None:
                cheaper = "electric" if heat.elec_per_kwh_heat < heat.gas_per_kwh_heat else "gas"
                self._out(
                    f"break-even electricity €{heat.break_even_elec_consumer:.4f}/kWh vs current "
                    f"€{heat.elec.consumer_incl_vat:.4f}/kWh → {cheaper} heat is cheaper now"
                )
            self._check(
                "gas sample", "PASS" if 0.5 < gas.consumer_incl_vat < 5 else "FAIL", f"€{gas.consumer_incl_vat:.4f}/m³"
            )

        return [(5 * i, sample) for i in range(6)]

    # ── demo 3: Shelly plug ─────────────────────────────────────────────────

    def _read_line(self, plug: Any, label: str) -> Any:
        s = plug.read_status()
        self._out(
            f"{plug.name} {label}: state={s.state} power={s.power_w} W current={s.current_a} A voltage={s.voltage_v} V "
            f"energy={s.energy_kwh} kWh temp={s.temperature_c} °C rssi={s.rssi_dbm} dBm"
        )
        return s

    def _demo_shelly(self, main: Any) -> list[Step]:
        steps: list[Step] = []
        self._remember(main.plugs)
        for index, plug in enumerate(main.plugs):
            base = index * 18
            powers: list[float] = []

            def identity(plug: Any = plug) -> None:
                self._out(f"{plug.name}: configured device ID {plug.device_id}, entities {plug.entities}")
                if plug.rpc is None:
                    self._check(f"{plug.name} RPC identity", "SKIPPED", "shelly_rpc disabled")
                else:
                    ip = plug.rpc.resolve()
                    info = plug.rpc.device_info or {}
                    self._out(
                        f"{plug.name}: hostname {plug.rpc.hostname}, IP {ip} via {plug.rpc.resolved_via}, "
                        f"mac {info.get('mac')}, model {info.get('model')}, firmware {info.get('ver')}"
                    )
                    self._check(f"{plug.name} RPC identity", "PASS" if ip else "WARN", plug.rpc.last_error or "")
                self._read_line(plug, "initial")

            def switch_on(plug: Any = plug) -> None:
                result = plug.turn_on("demo 3")
                self._check(
                    f"{plug.name} switch on + read-back",
                    "PASS" if result.verified else "FAIL",
                    f"{result.attempts} attempt(s)",
                )

            def read(plug: Any = plug, powers: list[float] = powers, label: str = "") -> None:
                status = self._read_line(plug, label)
                if status.power_w is not None:
                    powers.append(status.power_w)

            def switch_off(plug: Any = plug, powers: list[float] = powers) -> None:
                threshold = plug.cfg.active_power_threshold_w
                if any(p > threshold for p in powers):
                    self._check(f"{plug.name} power detection", "PASS", f"max {max(powers):.0f} W")
                else:
                    self._check(f"{plug.name} power detection", "SKIPPED", "heater appears switched off by hand")
                result = plug.turn_off("demo 3")
                self._check(
                    f"{plug.name} switch off + read-back",
                    "PASS" if result.verified else "FAIL",
                    f"{result.attempts} attempt(s)",
                )

            def restore(plug: Any = plug) -> None:
                original = self._restore.get(plug.name)
                if original == "on":
                    plug.turn_on("demo 3 restore")
                self._out(f"{plug.name}: original state {original} restored")

            steps += [(base, identity), (base + 1, switch_on)]
            steps += [(base + 1 + 3 * k, lambda k=k, read=read: read(label=f"on +{3 * k}s")) for k in range(1, 5)]
            steps += [(base + 13.5, switch_off), (base + 16.5, lambda read=read: read(label="off +3s"))]
            steps += [(base + 17, restore)]
        return steps

    # ── demo 4: house ───────────────────────────────────────────────────────

    def _house_line(self, main: Any, label: str) -> None:
        status = main.house.status(self._now())
        per_device = ", ".join(f"{d.name}={d.state} {d.power_w} W" for d in status.devices)
        self._out(
            f"house {label}: total {status.total_power_w:.0f} W, today {status.energy_today_kwh:.3f} kWh ({per_device})"
        )

    def _demo_house(self, main: Any) -> list[Step]:
        house = main.house
        self._remember(main.plugs)

        def start() -> None:
            self._house_line(main, "initial")
            results = house.set_heating(True, "demo 4", self._now())
            ok = all(r.verified for r in results if not r.skipped)
            self._check("all plugs on", "PASS" if ok else "FAIL", "; ".join(f"{r.name}:{r.after}" for r in results))

        def off() -> None:
            results = house.set_heating(False, "demo 4", self._now())
            ok = all(r.verified for r in results if not r.skipped)
            self._check("all plugs off", "PASS" if ok else "FAIL", "; ".join(f"{r.name}:{r.after}" for r in results))
            self._restore_plugs()

        def simulated_failure() -> None:
            victim = main.plugs[-1]
            self._remember(main.plugs)
            victim.simulate_failure = True
            try:
                results = house.set_heating(True, "demo 4 simulated failure", self._now())
                for r in results:
                    self._out(f"{r.name}: requested {r.requested}, after {r.after}, skipped={r.skipped}")
                health = house.health(self._now())
                self._out(f"house health: {health}")
                others_ok = all(r.verified for r in results if r.name != victim.name)
                reported = any(r.name == victim.name and r.skipped == "unavailable" for r in results)
                self._check(
                    "one plug failing is reported, the other still controlled",
                    "PASS" if others_ok and reported else "FAIL",
                )
            finally:
                victim.simulate_failure = False
                self._restore_plugs()

        return [
            (0, start),
            (5, lambda: self._house_line(main, "+5s")),
            (10, lambda: self._house_line(main, "+10s")),
            (15, lambda: self._house_line(main, "+15s")),
            (16, off),
            (20, simulated_failure),
        ]
