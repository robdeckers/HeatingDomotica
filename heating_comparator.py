#!/usr/bin/env python3
"""Compare the cost of heating a house with gas vs. with electricity.

Every 5 minutes this script reuses `nordpool_price_monitor` (Nord Pool
day-ahead electricity price, NL) and `ttf_gas_price_monitor` (EEX/TTF based
ANWB consumer gas price) to fetch the current prices, converts both to a
price per kWh of *delivered heat*, and prints which energy source is
currently cheaper to heat a house with.

Conversion assumptions:
    - Gas boiler (CV-ketel):
        - Calorific value of Dutch gas: GAS_KWH_PER_M3 kWh/m3 (from
          ttf_gas_price_monitor).
        - Boiler efficiency (rendement): GAS_BOILER_EFFICIENCY (HR-ketel,
          ~0.95 for space heating / lower heating value based efficiency).
    - Electric heating:
        - Heat pump COP (Coefficient Of Performance): HEATPUMP_COP, i.e. how
          many kWh of heat are delivered per kWh of electricity consumed.
        - The Nord Pool price is a wholesale EUR/MWh price; it is converted
          to an all-in consumer price using ELEC_MARKUP_PER_KWH (retailer
          markup) and ELEC_ENERGIEBELASTING_PER_KWH (Dutch energy tax on
          electricity), then VAT is applied, mirroring the gas price
          composition used by ttf_gas_price_monitor.

Usage:
    python3 heating_comparator.py

Requires:
    pip install requests
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Reuse the existing monitor scripts (same directory).
import nordpool_price_monitor as nordpool
import ttf_gas_price_monitor as ttf_gas

POLL_INTERVAL_SECONDS = 5 * 60

# --- Gas heating assumptions -------------------------------------------------

# Efficiency (rendement) of a modern condensing (HR) gas boiler used purely
# for space heating.
GAS_BOILER_EFFICIENCY = 0.95

# --- Electric heating assumptions -------------------------------------------

# Average Coefficient Of Performance of an air/water heat pump: kWh of heat
# delivered per kWh of electricity consumed.
HEATPUMP_COP = 1.0

# Dutch VAT (btw) factor (same as used for gas).
BTW = ttf_gas.BTW

def gas_consumer_price_to_heat_price_per_kwh(gas_price_eur_per_m3: float) -> float:
    """Convert a consumer gas price (EUR/m3) into a price per kWh of
    delivered heat, taking boiler efficiency into account."""
    price_per_kwh_gas = gas_price_eur_per_m3 / ttf_gas.GAS_KWH_PER_M3
    return price_per_kwh_gas / GAS_BOILER_EFFICIENCY


def elec_consumer_price_to_heat_price_per_kwh(elec_price_eur_per_kwh: float) -> float:
    """Convert an electricity consumer price (EUR/kWh) into a price per kWh
    of delivered heat, taking the heat pump COP into account."""
    return elec_price_eur_per_kwh / HEATPUMP_COP


def fetch_prices() -> tuple[Optional[float], Optional[float]]:
    """Fetch the current Nord Pool electricity price and TTF-based gas
    price. Returns (elec_price_eur_per_mwh, gas_price_eur_per_m3)."""
    now = datetime.now(timezone.utc)

    elec_price = nordpool.get_all_in_price_per_hour(now)

    ttf_price = ttf_gas.fetch_ttf_price_eur_per_mwh()
    gas_price = (
        ttf_gas.ttf_price_to_consumer_price(ttf_price) if ttf_price is not None else None
    )

    return elec_price, gas_price


def run() -> None:
    print(
        "Starting gas-vs-electricity heating cost comparator "
        f"(polling every {POLL_INTERVAL_SECONDS // 60} minutes). Press Ctrl+C to stop."
    )
    while True:
        now = datetime.now(timezone.utc)
        timestamp = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        elec_price_eur_per_kwh, gas_price_eur_per_m3 = fetch_prices()

        if elec_price_eur_per_kwh is None or gas_price_eur_per_m3 is None:
            print(f"[{timestamp}] Could not determine both prices, skipping comparison.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        gas_heat_price = gas_consumer_price_to_heat_price_per_kwh(gas_price_eur_per_m3)
        elec_heat_price = elec_consumer_price_to_heat_price_per_kwh(
            elec_price_eur_per_kwh
        )

        if gas_heat_price < elec_heat_price:
            cheaper = "GAS"
            diff = elec_heat_price - gas_heat_price
        else:
            cheaper = "ELECTRICITY"
            diff = gas_heat_price - elec_heat_price

        print(
            f"[{timestamp}] Gas: {gas_price_eur_per_m3:.5f} EUR/m3 -> "
            f"{gas_heat_price:.5f} EUR/kWh heat (boiler eff. {GAS_BOILER_EFFICIENCY}) | "
            f"Electricity: {elec_price_eur_per_kwh:.2f} EUR/kWh -> "
            f"{elec_heat_price:.5f} EUR/kWh heat (heat pump COP {HEATPUMP_COP}) | "
            f"Cheaper: {cheaper} (by {diff:.5f} EUR/kWh heat)"
        )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped by user.")
