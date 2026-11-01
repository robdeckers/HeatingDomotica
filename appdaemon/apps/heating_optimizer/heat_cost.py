"""Pure conversion of consumer prices into the cost of one kWh of useful heat."""

from __future__ import annotations


def gas_per_kwh_fuel(gas_consumer_per_m3: float, calorific_value_kwh_per_m3: float) -> float:
    return gas_consumer_per_m3 / calorific_value_kwh_per_m3


def gas_per_kwh_heat(gas_consumer_per_m3: float, calorific_value_kwh_per_m3: float, boiler_efficiency: float) -> float:
    return gas_per_kwh_fuel(gas_consumer_per_m3, calorific_value_kwh_per_m3) / boiler_efficiency


def elec_per_kwh_heat(elec_consumer_per_kwh: float, efficiency: float) -> float:
    return elec_consumer_per_kwh / efficiency


def break_even_elec_price(gas_heat_per_kwh: float, efficiency: float) -> float:
    """Consumer electricity price (€/kWh) at which electric heat costs the same as gas heat."""
    return gas_heat_per_kwh * efficiency
