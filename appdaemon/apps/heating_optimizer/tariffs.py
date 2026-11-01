"""Pure tariff math: market price → consumer price.

Every component is entered incl. VAT (as on the ANWB invoice), and the market price is requested incl. VAT, so the
consumer price is a plain sum. VAT is linear, so this equals ``(market + markup + tax)_excl_vat × (1 + vat_rate)``.
Fixed charges (standing charge, grid fees, tax credit) do not change the marginal decision and are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass


def to_excl_vat(price_incl_vat: float, vat_rate: float) -> float:
    return price_incl_vat / (1.0 + vat_rate)


def to_incl_vat(price_excl_vat: float, vat_rate: float) -> float:
    return price_excl_vat * (1.0 + vat_rate)


@dataclass(frozen=True)
class TariffBreakdown:
    """Consumer price per unit (kWh or m³), all components incl. VAT."""

    market_incl_vat: float
    markup_incl_vat: float
    tax_incl_vat: float
    vat_rate: float

    @property
    def consumer_incl_vat(self) -> float:
        return self.market_incl_vat + self.markup_incl_vat + self.tax_incl_vat

    @property
    def consumer_excl_vat(self) -> float:
        return to_excl_vat(self.consumer_incl_vat, self.vat_rate)

    @property
    def vat_amount(self) -> float:
        return self.consumer_incl_vat - self.consumer_excl_vat

    def as_dict(self) -> dict[str, float]:
        def excl(value: float) -> float:
            return round(to_excl_vat(value, self.vat_rate), 5)

        return {
            "market_incl_vat": round(self.market_incl_vat, 5),
            "markup_incl_vat": round(self.markup_incl_vat, 5),
            "tax_incl_vat": round(self.tax_incl_vat, 5),
            "market_excl_vat": excl(self.market_incl_vat),
            "markup_excl_vat": excl(self.markup_incl_vat),
            "tax_excl_vat": excl(self.tax_incl_vat),
            "vat_rate": self.vat_rate,
            "vat_amount": round(self.vat_amount, 5),
            "consumer_incl_vat": round(self.consumer_incl_vat, 5),
        }


def consumer_price(
    market_incl_vat: float, markup_incl_vat: float, tax_incl_vat: float, vat_rate: float
) -> TariffBreakdown:
    """Consumer price from incl.-VAT components. Negative market prices are passed through unchanged."""
    return TariffBreakdown(market_incl_vat, markup_incl_vat, tax_incl_vat, vat_rate)
