#!/usr/bin/env python3
"""Poll the EEX/TTF gas market price and compute the ANWB consumer gas price.

Every 5 minutes this script fetches the daily EEX/TTF natural gas market
price (EUR/MWh, Dutch TTF hub) and converts it into a consumer price per m3,
following the same formula ANWB uses:

    consumer_price_per_m3 = (ttf_price_per_m3
                              + energiebelasting_per_m3
                              + anwb_inkoopkosten_excl_btw_per_m3) * btw_factor

Where:
    - ttf_price_per_m3 is derived from the daily EEX/TTF market price
      (EUR/MWh), converted using the calorific value of Dutch (H-gas) natural
      gas.
    - anwb_inkoopkosten_excl_btw_per_m3 is derived from ANWB's published
      purchasing cost of EUR 0.07680/m3 (incl. btw).
    - energiebelasting_per_m3 is the Dutch energy tax on gas (excl. btw).
    - btw_factor is the Dutch VAT factor (21%).

Usage:
    python3 ttf_gas_price_monitor.py

Requires:
    pip install requests
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# Yahoo Finance chart endpoint used to retrieve the (near) real-time
# front-month Dutch TTF natural gas futures price, quoted in EUR/MWh.
TTF_QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/TTF=F"
POLL_INTERVAL_SECONDS = 5 * 60

# Calorific value used to convert EUR/MWh (TTF) into EUR/m3 for Dutch
# H-gas: 1 m3 ~= 9.769 kWh.
GAS_KWH_PER_M3 = 9.769

# Dutch VAT (btw) factor.
BTW = 1.21

# ANWB's published purchasing costs, incl. btw (EUR/m3).
ANWB_INKOOPKOSTEN_INCL_BTW = 0.07680

# Dutch energiebelasting for gas (excl. btw), EUR/m3 (2024 tariff, first
# bracket up to 170,000 m3/year).
ENERGIEBELASTING_PER_M3 = 0.70544


def fetch_ttf_price_eur_per_mwh() -> Optional[float]:
    """Fetch the latest daily EEX/TTF gas market price in EUR/MWh."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(TTF_QUOTE_URL, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to fetch TTF price: {exc}", file=sys.stderr)
        return None

    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        return float(price)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"[ERROR] Unexpected TTF response format: {exc}", file=sys.stderr)
        return None


def ttf_price_to_consumer_price(ttf_price_eur_per_mwh: float) -> float:
    """Convert an EEX/TTF market price (EUR/MWh) into a consumer price (EUR/m3)."""
    ttf_price_per_m3 = ttf_price_eur_per_mwh * GAS_KWH_PER_M3 / 1000

    anwb_inkoopkosten_excl_btw = ANWB_INKOOPKOSTEN_INCL_BTW / BTW

    total_price = (
        ttf_price_per_m3 + ENERGIEBELASTING_PER_M3 + anwb_inkoopkosten_excl_btw
    ) * BTW
    return total_price


def run() -> None:
    print(
        "Starting EEX/TTF gas price monitor "
        f"(polling every {POLL_INTERVAL_SECONDS // 60} minutes). Press Ctrl+C to stop."
    )
    while True:
        now = datetime.now(timezone.utc)
        ttf_price = fetch_ttf_price_eur_per_mwh()

        timestamp = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if ttf_price is not None:
            consumer_price = ttf_price_to_consumer_price(ttf_price)
            print(
                f"[{timestamp}] EEX/TTF market price: {ttf_price:.3f} EUR/MWh | "
                f"Consumer gas price: {consumer_price:.5f} EUR/m3"
            )
        else:
            print(f"[{timestamp}] Could not determine current EEX/TTF gas price.")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped by user.")
