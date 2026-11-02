from datetime import UTC, datetime, timedelta

import pytest
from fakes import LOG, FakeClock, FakeHA, add_shelly_entities, make_config

from heating_optimizer.config import ShellyRpcConfig
from heating_optimizer.devices.shelly_plug import ShellyPlug, ShellyRpcClient, classify_entities

NOW = datetime(2026, 10, 27, 13, 5, tzinfo=UTC)


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.fixture
def ha(clock):
    ha = FakeHA(clock)
    ha.template_result = add_shelly_entities(ha, "plug1")
    return ha


def make_plug(ha, clock, **overrides):
    cfg = make_config(**overrides).house.plugs[0]
    plug = ShellyPlug(cfg, ha, LOG, clock=clock, sleep=lambda s: None)
    plug.discover()
    return plug


def test_classify_entities_by_device_class():
    states = {
        "switch.living": {"attributes": {}},
        "sensor.living_power": {"attributes": {"device_class": "power"}},
        "sensor.living_energy_returned": {"attributes": {"device_class": "energy"}},
        "sensor.living_energy": {"attributes": {"device_class": "energy"}},
        "sensor.living_current": {"attributes": {"device_class": "current"}},
        "sensor.living_voltage": {"attributes": {"device_class": "voltage"}},
        "sensor.living_device_temperature": {"attributes": {"device_class": "temperature"}},
        "sensor.living_rssi": {"attributes": {"device_class": "signal_strength"}},
        "sensor.living_last_restart": {"attributes": {"device_class": "timestamp"}},
        "binary_sensor.living_overpowering": {"attributes": {"device_class": "problem"}},
    }
    found = classify_entities(states)
    assert found.switch == "switch.living"
    assert found.energy == "sensor.living_energy"
    assert found.temperature == "sensor.living_device_temperature"
    assert found.uptime == "sensor.living_last_restart"
    assert found.protection == ["binary_sensor.living_overpowering"]


def test_discovery_template_contains_mac(ha, clock):
    seen = []
    ha.render_template = lambda t: seen.append(t) or '["switch.plug1"]'
    make_plug(ha, clock)
    assert "907069543f3c" in seen[0]


def test_discovery_failure_asks_for_explicit_entity(ha, clock):
    ha.template_result = "[]"
    with pytest.raises(RuntimeError, match="switch_entity"):
        make_plug(ha, clock)


def test_explicit_switch_entity_wins(ha, clock):
    ha.set("switch.manual", "off")
    plug = make_plug(ha, clock, **{"house.plugs.0.switch_entity": "switch.manual"})
    assert plug.entities.switch == "switch.manual"
    assert plug.entities.power == "sensor.plug1_power"


def test_turn_on_verifies_read_back(ha, clock):
    plug = make_plug(ha, clock)
    result = plug.turn_on("test")
    assert result.verified and result.before == "off" and result.after == "on" and result.attempts == 1
    assert ha.calls == [("switch/turn_on", {"entity_id": "switch.plug1"})]


def test_turn_on_retries_three_times_then_reports_error(ha, clock):
    ha.stuck_switches.add("switch.plug1")
    plug = make_plug(ha, clock)
    result = plug.turn_on("test")
    assert not result.verified and result.attempts == 4 and result.error
    assert plug.verify_failures == 1
    assert len([c for c in ha.calls if c[0] == "switch/turn_on"]) == 4


def test_read_status_and_heater_active(ha, clock):
    plug = make_plug(ha, clock)
    ha.set("switch.plug1", "on")
    ha.set("sensor.plug1_power", 0.4, device_class="power")
    assert not plug.read_status().heater_active  # heater switched off by hand
    ha.set("sensor.plug1_power", 2950, device_class="power")
    status = plug.read_status()
    assert status.heater_active and status.power_w == 2950 and status.voltage_v == 230.0


def test_unavailable_switch(ha, clock):
    plug = make_plug(ha, clock)
    ha.set("switch.plug1", "unavailable")
    assert not plug.is_available()
    assert plug.read_status().state is None


def test_energy_in_wh_is_converted(ha, clock):
    ha.set("sensor.plug1_energy", 1500, device_class="energy", unit_of_measurement="Wh")
    assert make_plug(ha, clock).read_status().energy_kwh == 1.5


def test_safety_trip_switches_off_and_locks_out_until_next_hour(ha, clock):
    plug = make_plug(ha, clock)
    ha.set("switch.plug1", "on")
    ha.set("sensor.plug1_temperature", 75, device_class="temperature")
    reason = plug.check_safety(NOW)
    assert "temperature" in reason
    assert ha.get_state("switch.plug1") == "off"
    assert plug.is_locked_out(NOW)
    assert plug.lockout_until == datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
    assert not plug.is_locked_out(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


def test_protection_trip_detected_from_binary_sensor(ha, clock):
    plug = make_plug(ha, clock)
    assert plug.check_protection_trip(NOW) is None  # no flags: manual change
    ha.set("binary_sensor.plug1_overtemp", "on")
    assert "overtemp" in plug.check_protection_trip(NOW)
    assert plug.protection_trips == 1 and plug.is_locked_out(NOW)


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, devices):
        self.devices = devices  # ip -> GetDeviceInfo response
        self.urls = []

    def get(self, url, params=None, timeout=None, auth=None):
        self.urls.append(url)
        ip = url.split("/")[2]
        if ip not in self.devices:
            raise ConnectionError(ip)
        return FakeResponse(self.devices[ip])


def rpc_client(session, fallback="192.168.2.18"):
    cfg = ShellyRpcConfig(enabled=True, timeout_s=0.1, hostname_pattern="nonexistent-{device_id}.invalid")
    return ShellyRpcClient(cfg, "907069543f3c", fallback, session, LOG)


def test_rpc_resolves_fallback_and_verifies_mac():
    session = FakeSession({"192.168.2.18": {"mac": "907069543F3C", "id": "shellyplugmg3-907069543f3c", "ver": "1.5"}})
    client = rpc_client(session)
    assert client.resolve() == "192.168.2.18"
    assert client.resolved_via == "fallback"
    assert client.firmware == "1.5"


def test_rpc_rejects_ip_answering_with_other_mac():
    session = FakeSession({"192.168.2.18": {"mac": "9070695DBA7C", "id": "shellyplugmg3-9070695dba7c"}})
    client = rpc_client(session)
    assert client.resolve() is None
    assert "907069543f3c" in client.last_error


def test_rpc_prefers_last_known_ip():
    session = FakeSession({"192.168.2.50": {"mac": "907069543F3C"}, "192.168.2.18": {"mac": "907069543F3C"}})
    client = rpc_client(session)
    client.last_good_ip = "192.168.2.50"
    assert client.resolve() == "192.168.2.50" and client.resolved_via == "last_known"


def test_rpc_switch_errors():
    info = {"mac": "907069543F3C", "errors": ["overtemp"]}
    client = rpc_client(FakeSession({"192.168.2.18": info}))
    assert client.switch_errors() == ["overtemp"]


def test_lockout_expiry_uses_elapsed_time(ha, clock):
    plug = make_plug(ha, clock)
    plug.lock_out(NOW, "test")
    assert plug.is_locked_out(NOW + timedelta(minutes=54))
    assert not plug.is_locked_out(NOW + timedelta(minutes=55))
