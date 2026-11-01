from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fakes import LOG, FakePlug

from heating_optimizer.house import House

NOW = datetime(2026, 10, 27, 13, 5, tzinfo=UTC)
AMS = ZoneInfo("Europe/Amsterdam")


def make_house(*plugs):
    return House("Home", list(plugs), AMS, LOG)


def test_set_heating_switches_all_plugs():
    a, b = FakePlug("a"), FakePlug("b")
    results = make_house(a, b).set_heating(True, "test", NOW)
    assert [r.after for r in results] == ["on", "on"]
    assert a.state == b.state == "on"


def test_plug_already_in_target_state_is_not_switched():
    a = FakePlug("a", state="on")
    result = make_house(a).set_heating(True, "test", NOW)[0]
    assert a.switch_calls == []
    assert result.verified and result.attempts == 0


def test_one_unavailable_plug_does_not_stop_the_other():
    a, b = FakePlug("a", available=False), FakePlug("b")
    results = make_house(a, b).set_heating(True, "test", NOW)
    assert results[0].skipped == "unavailable" and not results[0].verified
    assert b.state == "on"


def test_one_plug_raising_does_not_stop_the_other():
    a, b = FakePlug("a"), FakePlug("b")
    a.fail_switch = True
    results = make_house(a, b).set_heating(True, "test", NOW)
    assert results[0].error == "boom"
    assert b.state == "on"


def test_locked_out_plug_is_kept_off():
    a, b = FakePlug("a", state="on"), FakePlug("b")
    a.locked_until = NOW + timedelta(minutes=30)
    results = make_house(a, b).set_heating(True, "test", NOW)
    assert a.state == "off"
    assert b.state == "on"
    assert results[1].after == "on"


def test_snapshot_heating_on_and_since():
    a, b = FakePlug("a", state="on"), FakePlug("b", state="on")
    a.last_changed, b.last_changed = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    snap = make_house(a, b).snapshot(NOW)
    assert snap.heating_on
    assert snap.state_since == NOW - timedelta(minutes=20)


def test_snapshot_heating_off_since_last_change():
    a, b = FakePlug("a"), FakePlug("b")
    a.last_changed, b.last_changed = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    snap = make_house(a, b).snapshot(NOW)
    assert not snap.heating_on
    assert snap.state_since == NOW - timedelta(minutes=5)


def test_all_locked_out_only_counts_available_plugs():
    a, b = FakePlug("a"), FakePlug("b", available=False)
    a.locked_until = NOW + timedelta(minutes=30)
    assert make_house(a, b).snapshot(NOW).all_locked_out
    c = FakePlug("c")
    assert not make_house(a, c).snapshot(NOW).all_locked_out


def test_plug_on_with_zero_power_is_not_a_failure():
    a = FakePlug("a", state="on", power_w=0.0)  # heater switched off by hand
    house = make_house(a)
    status = house.status(NOW).devices[0]
    assert status.available and status.is_on and not status.heater_active
    assert house.health(NOW)["a"]["available"]
    assert house.set_heating(True, "test", NOW)[0].verified


def test_status_aggregates_power_and_energy_today():
    a, b = FakePlug("a", state="on", power_w=2000), FakePlug("b", state="on", power_w=1500)
    house = make_house(a, b)
    a.energy_kwh, b.energy_kwh = 10.0, 5.0
    assert house.status(NOW).energy_today_kwh == 0.0
    a.energy_kwh, b.energy_kwh = 11.0, 5.5
    status = house.status(NOW + timedelta(hours=1))
    assert status.total_power_w == 3500
    assert status.energy_today_kwh == 1.5


def test_energy_today_rebases_at_midnight_and_counter_reset():
    a = FakePlug("a")
    house = make_house(a)
    a.energy_kwh = 10.0
    house.status(NOW)
    a.energy_kwh = 12.0
    assert house.status(NOW + timedelta(days=1)).energy_today_kwh == 0.0
    a.energy_kwh = 0.5  # counter reset
    assert house.status(NOW + timedelta(days=1, hours=1)).energy_today_kwh == 0.0


def test_unavailable_plug_power_not_counted():
    a, b = FakePlug("a", state="on", power_w=2000), FakePlug("b", available=False, power_w=999)
    assert make_house(a, b).status(NOW).total_power_w == 2000


def test_energy_sources_slot_is_empty_by_default():
    house = make_house(FakePlug("a"))
    assert house.energy_sources == []
    assert house.snapshot(NOW).production_w is None
