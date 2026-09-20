#!/usr/bin/env python3
"""Poll Nord Pool for the day-ahead electricity price of the Dutch (NL) market.

Every 5 minutes this script fetches the day-ahead prices for the current day
from the Nord Pool data portal and prints the price that applies to the
current hour.

Usage:
    python3 nordpool_price_monitor.py

Requires:
    pip install requests
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

NORDPOOL_API_URL = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
MARKET_AREA = "NL"
CURRENCY = "EUR"
POLL_INTERVAL_SECONDS = 5 * 60


def fetch_day_ahead_prices(date: datetime) -> Optional[dict]:
    """Fetch the day-ahead prices for the given date and market area."""
    params = {
        "date": date.strftime("%Y-%m-%d"),
        "market": "DayAhead",
        "deliveryArea": MARKET_AREA,
        "currency": CURRENCY,
    }
    try:
        response = requests.get(NORDPOOL_API_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to fetch Nord Pool prices: {exc}", file=sys.stderr)
        return None

def extract_current_hour_price(
        data: dict, now: datetime
) -> Optional[float]:
    """Calculate the current hourly price from four 15-minute prices."""

    if not data:
        return None

    entries = data.get("multiAreaEntries") or data.get("entries") or []

    current_hour_start = now.replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    current_hour_end = current_hour_start +  timedelta(hours=1)

    prices = []

    for entry in entries:
        start_str = entry.get("deliveryStart") or entry.get("start")
        end_str = entry.get("deliveryEnd") or entry.get("end")

        if not start_str or not end_str:
            continue

        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        # Select the four 15-minute periods belonging to this hour.
        if start >= current_hour_start and end <= current_hour_end:
            entry_per_area = entry.get("entryPerArea")

            if entry_per_area and MARKET_AREA in entry_per_area:
                prices.append(float(entry_per_area[MARKET_AREA]))
            elif entry.get("price") is not None:
                prices.append(float(entry["price"]))

    if len(prices) != 4:
        return None

    return sum(prices) / 4

#def extract_current_hour_price(data: dict, now: datetime) -> Optional[float]:
    """Extract the price entry that matches the current hour from the API response."""
    if not data:
        return None

    entries = data.get("multiAreaEntries") or data.get("entries") or []
    for entry in entries:
        start_str = entry.get("deliveryStart") or entry.get("start")
        end_str = entry.get("deliveryEnd") or entry.get("end")
        if not start_str or not end_str:
            continue

        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        if start <= now < end:
            entry_per_area = entry.get("entryPerArea")
            if entry_per_area and MARKET_AREA in entry_per_area:
                return float(entry_per_area[MARKET_AREA])
            price = entry.get("price")
            if price is not None:
                return float(price)
    return None

def toConsumerPrice(price: float) -> None:
    price_Kwh = price / 1000
    BTW = 1.21
    ANWB_inkoopkosten = (0.018 / BTW)
    Energiebelasting = 0.09161

    total_price = price_Kwh + (ANWB_inkoopkosten + Energiebelasting) * BTW
    return total_price

def run() -> None:
    print(
        f"Starting Nord Pool day-ahead price monitor for market '{MARKET_AREA}' "
        f"(polling every {POLL_INTERVAL_SECONDS // 60} minutes). Press Ctrl+C to stop."
    )
    while True:
        now = datetime.now(timezone.utc)
        data = fetch_day_ahead_prices(now)
        price = extract_current_hour_price(data, now) if data else None
        consumer_price = toConsumerPrice(price)

        timestamp = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if consumer_price is not None:
            print(f"[{timestamp}] Current hour day-ahead price ({MARKET_AREA}): {consumer_price:.2f} {CURRENCY}/kWh")
        else:
            print(f"[{timestamp}] Could not determine current hour price for {MARKET_AREA}.")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped by user.")
