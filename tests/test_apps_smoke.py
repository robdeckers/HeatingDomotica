"""Smoke tests of the AppDaemon apps against a stub ``hassapi.Hass`` backed by FakeHA.

They exercise the wiring (startup self-test, ticks, enable/disable, safety listeners, demos) that the pure unit tests
do not reach. AppDaemon's real scheduler and HA behavior are verified by the hardware demos.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes import FakeClock, FakeHA, add_shelly_entities, raw_config


class StubHass:
    """The subset of AppDaemon's Hass API the apps use. Timers are recorded; tests fire them explicitly."""

    registry: dict[str, Any] = {}

    def __init__(self, ha: FakeHA, args: dict[str, Any]):
        self._ha = ha
        self.args = args
        self.logs: list[tuple[str, str]] = []
        self.timers: list[tuple[float, Any, dict[str, Any]]] = []
        self.listeners: list[tuple[str, Any, dict[str, Any]]] = []

    def log(self, msg: str, level: str = "INFO") -> None:
        self.logs.append((level, msg))

    def get_state(self, entity: str | None = None, attribute: str | None = None) -> Any:
        if entity == "sensor":
            return {e: v for e, v in self._ha.states.items() if e.startswith("sensor.")}
        return self._ha.get_state(entity, attribute)

    def set_state(
        self, entity: str, state: Any = None, attributes: dict[str, Any] | None = None, replace: bool = False
    ) -> None:
        self._ha.set(entity, state, **(attributes or {}))

    def call_service(self, service: str, **kwargs: Any) -> Any:
        return self._ha.call_service(service, **kwargs)

    def render_template(self, template: str) -> Any:
        return self._ha.render_template(template)

    def run_every(self, cb: Any, start: Any, interval: float) -> int:
        self.timers.append((0, cb, {"every": interval}))
        return len(self.timers)

    def run_daily(self, cb: Any, start: Any) -> int:
        self.timers.append((0, cb, {"daily": start}))
        return len(self.timers)

    def run_in(self, cb: Any, delay: float, **kwargs: Any) -> int:
        self.timers.append((delay, cb, kwargs))
        return len(self.timers)

    def cancel_timer(self, handle: int) -> None:
        delay, _, kwargs = self.timers[handle - 1]
        self.timers[handle - 1] = (delay, None, kwargs)

    def listen_state(self, cb: Any, entity: str, **kwargs: Any) -> None:
        self.listeners.append((entity, cb, kwargs))

    def get_app(self, name: str) -> Any:
        return self.registry.get(name)

    def fire_run_in(self) -> None:
        """Fire pending run_in timers in delay order, including timers added while firing."""
        while True:
            pending = [
                (i, t)
                for i, t in enumerate(self.timers)
                if t[1] is not None and "every" not in t[2] and "daily" not in t[2]
            ]
            if not pending:
                return
            index, (delay, cb, kwargs) = min(pending, key=lambda item: item[1][0])
            self.timers[index] = (delay, None, kwargs)
            cb(kwargs)


@pytest.fixture(scope="module", autouse=True)
def stub_appdaemon():
    module = types.ModuleType("appdaemon.plugins.hass.hassapi")
    module.Hass = StubHass
    names = ["appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass", "appdaemon.plugins.hass.hassapi"]
    saved = {n: sys.modules.get(n) for n in names}
    for name in names[:-1]:
        sys.modules[name] = types.ModuleType(name)
    sys.modules[names[-1]] = module
    yield
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    for name in ("heating_optimizer.main_app", "heating_optimizer.demo_app"):
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def no_read_back_delay(monkeypatch):
    import heating_optimizer.devices.shelly_plug as shelly_plug

    monkeypatch.setattr(shelly_plug.time, "sleep", lambda s: None)


MACS = {"907069543f3c": "plug1", "9070695dba7c": "plug2"}


def make_ha(elec_price: float = 0.0, gas_price: float = 0.60) -> FakeHA:
    clock = FakeClock(datetime.now(UTC) - timedelta(hours=1))  # entities last changed an hour ago: timers elapsed
    ha = FakeHA(clock)
    for prefix in MACS.values():
        add_shelly_entities(ha, prefix)
    ha.set("input_boolean.heating_optimizer_enabled", "on")
    ha.set("sensor.energyzero_today_energy_current_hour_price", 0.1)
    clock.now = datetime.now(UTC)

    def render(template: str) -> str:
        if "config_entry_id" in template:
            return "entry-123"
        prefix = next(p for mac, p in MACS.items() if mac in template)
        return json.dumps([e for e in ha.states if prefix in e])

    ha.render_template = render
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
    hours = [start + timedelta(hours=h) for h in range(24 * 5)]
    ha.service_results["energyzero/get_energy_prices"] = {
        "result": {"response": {"prices": [{"timestamp": h.isoformat(), "price": elec_price} for h in hours]}}
    }
    ha.service_results["energyzero/get_gas_prices"] = {
        "prices": [{"timestamp": h.isoformat(), "price": gas_price} for h in hours]
    }
    return ha


def make_main(ha: FakeHA, tmp_path, **overrides: Any) -> Any:
    from heating_optimizer.main_app import MainApp

    overrides.setdefault("logging.decision_log_dir", str(tmp_path))
    overrides.setdefault("shelly_rpc.enabled", False)  # no network in tests
    app = MainApp(ha, {"config": raw_config(**overrides)})
    StubHass.registry["heating_optimizer_main"] = app
    app.initialize()
    app.fire_run_in()  # startup: self-test, listeners, first evaluation
    return app


def decisions(tmp_path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]


def switch_calls(ha: FakeHA) -> list[tuple[str, str]]:
    return [(s, k["entity_id"]) for s, k in ha.calls if s.startswith("switch/")]


def test_startup_switches_on_when_electric_cheaper(tmp_path):
    ha = make_ha(elec_price=0.0)
    app = make_main(ha, tmp_path)
    assert app.ready and app.diag.self_test_ok, app.diag.self_test_problems
    assert ha.get_state("switch.plug1") == ha.get_state("switch.plug2") == "on"
    record = decisions(tmp_path)[-1]
    assert record["action"] == "ON" and record["reason_code"] == "ELEC_CHEAPER"
    assert {d["name"] for d in record["devices"]} == {"shelly_1", "shelly_2"}
    assert ha.get_state("sensor.heating_optimizer_last_decision") == "ON"
    assert ha.get_state("sensor.heating_optimizer_status") == "running"
    assert ha.get_state("sensor.heating_optimizer_health") == "OK"
    assert any(s == "logbook/log" for s, _ in ha.calls)


def test_gas_cheaper_keeps_plugs_off(tmp_path):
    ha = make_ha(elec_price=0.30)
    make_main(ha, tmp_path)
    assert switch_calls(ha) == []
    assert decisions(tmp_path)[-1]["reason_code"] == "NO_CHANGE"


def test_dry_run_never_switches(tmp_path):
    ha = make_ha(elec_price=0.0)
    app = make_main(ha, tmp_path, dry_run=True)
    assert switch_calls(ha) == []
    assert decisions(tmp_path)[-1]["action"] == "ON"
    assert ha.get_state("sensor.heating_optimizer_status") == "dry_run"
    app.evaluate("aligned")  # simulated state is on now: the second tick holds
    assert decisions(tmp_path)[-1]["action"] == "HOLD"


def test_missing_prices_fail_safe(tmp_path):
    ha = make_ha()
    ha.service_results["energyzero/get_energy_prices"] = RuntimeError("down")
    ha.set("sensor.energyzero_today_energy_current_hour_price", "unavailable")
    app = make_main(ha, tmp_path)
    assert decisions(tmp_path)[-1]["reason_code"] == "STALE_DATA"
    assert ha.get_state("sensor.heating_optimizer_health") == "ERROR"
    assert ha.get_state("sensor.heating_optimizer_status") == "failsafe"
    assert app.diag.self_test_ok is False


def test_disable_switches_off_once_then_hands_off(tmp_path):
    ha = make_ha(elec_price=0.0)
    app = make_main(ha, tmp_path)
    ha.set("input_boolean.heating_optimizer_enabled", "off")
    app._on_control_change("input_boolean.heating_optimizer_enabled", "state", "on", "off", {})
    assert ha.get_state("switch.plug1") == "off"
    ha.set("switch.plug1", "on")  # manual use while disabled
    calls = len(switch_calls(ha))
    app.evaluate("aligned")
    assert len(switch_calls(ha)) == calls
    assert ha.get_state("switch.plug1") == "on"
    assert decisions(tmp_path)[-1]["action"] == "DISABLED"


def test_safety_listener_trips_on_overtemperature(tmp_path):
    ha = make_ha(elec_price=0.0)
    app = make_main(ha, tmp_path)
    ha.set("sensor.plug1_temperature", 80, device_class="temperature")
    entity, cb, kwargs = next(listener for listener in app.listeners if listener[0] == "sensor.plug1_temperature")
    cb(entity, "state", "30", "80", kwargs)
    assert ha.get_state("switch.plug1") == "off"
    assert app.house.device("shelly_1").is_locked_out(datetime.now(UTC))
    app.evaluate("aligned")  # locked-out plug stays off, the other stays on
    assert ha.get_state("switch.plug1") == "off" and ha.get_state("switch.plug2") == "on"
    assert ha.get_state("sensor.heating_optimizer_health") == "DEGRADED"


def test_config_error_does_not_start(tmp_path):
    from heating_optimizer.main_app import MainApp

    ha = make_ha()
    app = MainApp(ha, {"config": raw_config(**{"electricity.energyzero_config_entry": "TODO(user)"})})
    app.initialize()
    assert ha.get_state("sensor.heating_optimizer_status") == "config_error"
    assert any(s == "persistent_notification/create" for s, _ in ha.calls)


def press(demo_app: Any, ha: FakeHA, button: str) -> None:
    entity, cb, kwargs = next(listener for listener in demo_app.listeners if listener[0] == button)
    cb(entity, "state", "unknown", datetime.now(UTC).isoformat(), kwargs)


def make_demo(ha: FakeHA) -> Any:
    from heating_optimizer.demo_app import DemoApp

    demo = DemoApp(ha, {"main_app": "heating_optimizer_main"})
    demo.initialize()
    return demo


def notifications(ha: FakeHA) -> list[str]:
    return [k["title"] for s, k in ha.calls if s == "persistent_notification/create"]


@pytest.mark.parametrize(
    "button", ["input_button.demo_electricity_price", "input_button.demo_gas_price", "input_button.demo_house"]
)
def test_demos_pass_and_restore(tmp_path, button):
    ha = make_ha(elec_price=0.30)  # gas cheaper: plugs off before and after
    main = make_main(ha, tmp_path)
    demo = make_demo(ha)
    press(demo, ha, button)
    assert main._paused_by is not None
    demo.fire_run_in()
    assert notifications(ha)[-1].endswith(": PASS"), ha.get_state("sensor.heating_optimizer_demo_output", "history")
    assert main._paused_by is None
    assert ha.get_state("switch.plug1") == ha.get_state("switch.plug2") == "off"


def test_shelly_demo_power_check_skipped_without_heater(tmp_path):
    ha = make_ha(elec_price=0.30)
    make_main(ha, tmp_path)
    demo = make_demo(ha)
    press(demo, ha, "input_button.demo_shelly_plug")
    demo.fire_run_in()
    history = "\n".join(ha.get_state("sensor.heating_optimizer_demo_output", "history"))
    assert "SKIPPED: shelly_1 power detection" in history
    assert "PASS: shelly_1 switch on + read-back" in history
    assert notifications(ha)[-1].endswith(": PASS")
    assert ha.get_state("switch.plug1") == "off"


def test_demo_stop_restores(tmp_path):
    ha = make_ha(elec_price=0.30)
    make_main(ha, tmp_path)
    demo = make_demo(ha)
    press(demo, ha, "input_button.demo_house")
    first = next(t for t in demo.timers if t[1] is not None)
    first[1](first[2])  # all plugs on
    assert ha.get_state("switch.plug1") == "on"
    entity, cb, kwargs = next(listener for listener in demo.listeners if listener[0] == "input_button.demo_stop")
    cb(entity, "state", "unknown", "now", kwargs)
    assert ha.get_state("switch.plug1") == ha.get_state("switch.plug2") == "off"
    assert notifications(ha)[-1].endswith(": STOPPED")
