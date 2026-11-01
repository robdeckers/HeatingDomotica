from datetime import date, time
from pathlib import Path

import pytest
import yaml
from fakes import make_config, raw_config

from heating_optimizer.config import ConfigError, parse_config

APPS_YAML = Path(__file__).parents[1] / "appdaemon" / "apps" / "heating_optimizer.yaml"


def problems(**overrides):
    with pytest.raises(ConfigError) as info:
        make_config(**overrides)
    return "\n".join(info.value.problems)


def test_valid_config_parses_with_defaults():
    cfg = make_config()
    assert cfg.tariffs_valid_until == date(2026, 12, 31)
    assert cfg.prices.daily_refresh_time == time(15, 5)
    assert cfg.loop.interval_minutes == 5
    assert cfg.house.plugs[0].plug_max_current_a == 13.0
    assert cfg.house.plugs[0].switch_entity == "auto"
    assert cfg.decision.failsafe_state == "off"


def test_shipped_app_config_only_lacks_the_config_entry():
    raw = yaml.safe_load(APPS_YAML.read_text())["heating_optimizer_main"]["config"]
    with pytest.raises(ConfigError) as info:
        parse_config(raw)
    assert info.value.problems == ["electricity.energyzero_config_entry: still set to TODO(user), fill in a real value"]
    raw["electricity"]["energyzero_config_entry"] = "x"
    cfg = parse_config(raw)
    assert cfg.dry_run is True
    assert cfg.gas.boiler_efficiency == 0.90
    assert cfg.decision.min_on_minutes == 15


def test_todo_marker_is_rejected():
    assert "still set to TODO(user)" in problems(**{"electricity.energyzero_config_entry": "TODO(user)"})


def test_missing_required_value_is_reported():
    raw = raw_config()
    del raw["gas"]["energy_tax_eur_per_m3_incl_vat"]
    with pytest.raises(ConfigError, match="gas.energy_tax_eur_per_m3_incl_vat: missing"):
        parse_config(raw)


def test_heater_above_plug_rating_is_rejected():
    assert "exceeds plug_max_power_w" in problems(**{"house.plugs.0.heater_power_w": 3500})


def test_heater_equal_to_plug_rating_is_accepted():
    assert make_config(**{"house.plugs.0.heater_power_w": 3000}).house.plugs[0].heater_power_w == 3000


@pytest.mark.parametrize("device_id", ["907069543f3", "907069543f3c0", "90:70:69:54:3f:3c", "zz7069543f3c"])
def test_bad_device_id_is_rejected(device_id):
    assert "12 hex characters" in problems(**{"house.plugs.0.device_id": device_id})


def test_device_id_is_lowercased():
    assert make_config(**{"house.plugs.0.device_id": "907069543F3C"}).house.plugs[0].device_id == "907069543f3c"


def test_duplicate_device_id_is_rejected():
    assert "duplicate device_id" in problems(**{"house.plugs.1.device_id": "907069543f3c"})


@pytest.mark.parametrize("interval", [7, 25])
def test_interval_must_divide_60(interval):
    assert "must divide 60" in problems(**{"loop.interval_minutes": interval})


def test_negative_margin_is_rejected():
    assert "margin_on_eur_per_kwh" in problems(**{"decision.margin_on_eur_per_kwh": -0.01})


@pytest.mark.parametrize("efficiency", [0, 1.2])
def test_boiler_efficiency_range(efficiency):
    assert "boiler_efficiency" in problems(**{"gas.boiler_efficiency": efficiency})


def test_vat_rate_range():
    assert "vat_rate" in problems(**{"electricity.vat_rate": 1.0})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("electricity.supplier_markup_eur_per_kwh_incl_vat", 1.8),  # ct instead of €
        ("electricity.energy_tax_eur_per_kwh_incl_vat", 11.085),
        ("gas.supplier_markup_eur_per_m3_incl_vat", 7.68),
        ("gas.energy_tax_eur_per_m3_incl_vat", 72.68),
    ],
)
def test_implausible_tariffs_are_rejected(path, value):
    assert path in problems(**{path: value})


def test_current_limit_above_16a_is_rejected():
    assert "plug_max_current_a" in problems(**{"house.plugs.0.plug_max_current_a": 20})


def test_all_problems_are_reported_at_once():
    text = problems(**{"loop.interval_minutes": 7, "gas.boiler_efficiency": 2, "house.plugs.0.device_id": "x"})
    assert text.count("\n") >= 2


def test_password_is_masked():
    cfg = make_config(**{"shelly_rpc.password": "hunter2"})
    assert cfg.masked()["shelly_rpc"]["password"] == "***"
    assert "hunter2" not in repr(cfg)


def test_yaml_off_boolean_for_failsafe_is_rejected():
    # Unquoted `off` in YAML becomes False; it must be quoted.
    assert "failsafe_state" in problems(**{"decision.failsafe_state": False})
