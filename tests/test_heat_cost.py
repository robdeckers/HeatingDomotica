import pytest

from heating_optimizer.heat_cost import break_even_elec_price, elec_per_kwh_heat, gas_per_kwh_fuel, gas_per_kwh_heat


def test_gas_per_kwh_fuel():
    assert gas_per_kwh_fuel(1.4652, 9.769) == pytest.approx(0.149985, abs=1e-6)


def test_gas_per_kwh_heat_includes_boiler_efficiency():
    # €1.40/m³ at 9.769 kWh/m³ and 90 % efficiency
    assert gas_per_kwh_heat(1.40, 9.769, 0.90) == pytest.approx(1.40 / 9.769 / 0.90)


def test_lower_boiler_efficiency_makes_gas_heat_more_expensive():
    assert gas_per_kwh_heat(1.40, 9.769, 0.85) > gas_per_kwh_heat(1.40, 9.769, 0.95)


def test_elec_per_kwh_heat_resistive_and_heat_pump():
    assert elec_per_kwh_heat(0.25, 1.0) == pytest.approx(0.25)
    assert elec_per_kwh_heat(0.25, 4.0) == pytest.approx(0.0625)


def test_break_even():
    gas_heat = gas_per_kwh_heat(1.40, 9.769, 0.90)
    assert break_even_elec_price(gas_heat, 1.0) == pytest.approx(gas_heat)
    assert elec_per_kwh_heat(break_even_elec_price(gas_heat, 3.0), 3.0) == pytest.approx(gas_heat)


def test_realistic_anwb_example():
    # Market gas €0.50/m³ incl. VAT + ANWB €0.0768 + tax €0.7268 = €1.3036/m³ → €0.1483/kWh heat at 90 %.
    gas_heat = gas_per_kwh_heat(0.50 + 0.0768 + 0.7268, 9.769, 0.90)
    assert gas_heat == pytest.approx(0.14827, abs=1e-5)
    # Electric is cheaper when market electricity < 0.14827 - 0.018 - 0.11085 ≈ €0.0194/kWh incl. VAT.
    assert gas_heat - 0.018 - 0.11085 == pytest.approx(0.01942, abs=1e-5)
