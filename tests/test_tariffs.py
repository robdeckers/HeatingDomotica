import pytest

from heating_optimizer.tariffs import consumer_price, to_excl_vat, to_incl_vat


def test_consumer_price_is_sum_of_incl_vat_components():
    t = consumer_price(0.10, 0.018, 0.11085, 0.21)
    assert t.consumer_incl_vat == pytest.approx(0.22885)


def test_sum_equals_excl_vat_formula():
    # (market + markup + tax)_excl × (1 + vat) must equal the incl.-VAT sum, because VAT is linear.
    market_excl, markup_excl, tax_excl = 0.08, 0.018 / 1.21, 0.09161
    t = consumer_price(to_incl_vat(market_excl, 0.21), 0.018, to_incl_vat(tax_excl, 0.21), 0.21)
    assert t.consumer_incl_vat == pytest.approx((market_excl + markup_excl + tax_excl) * 1.21)
    assert t.consumer_excl_vat == pytest.approx(market_excl + markup_excl + tax_excl)


def test_excl_vat_breakdown():
    t = consumer_price(0.121, 0.0, 0.11085, 0.21).as_dict()
    assert t["market_excl_vat"] == pytest.approx(0.1)
    assert t["tax_excl_vat"] == pytest.approx(0.09161, abs=1e-5)
    assert t["vat_amount"] == pytest.approx(t["consumer_incl_vat"] - t["consumer_incl_vat"] / 1.21, abs=1e-5)


def test_negative_market_price_still_positive_consumer_price():
    t = consumer_price(-0.05, 0.018, 0.11085, 0.21)
    assert t.consumer_incl_vat == pytest.approx(0.07885)


def test_very_negative_market_price_gives_negative_consumer_price():
    t = consumer_price(-0.30, 0.018, 0.11085, 0.21)
    assert t.consumer_incl_vat == pytest.approx(-0.17115)
    assert t.consumer_excl_vat < 0


def test_zero_vat():
    t = consumer_price(0.10, 0.02, 0.10, 0.0)
    assert t.consumer_excl_vat == t.consumer_incl_vat == pytest.approx(0.22)


def test_vat_round_trip():
    assert to_excl_vat(to_incl_vat(0.123, 0.21), 0.21) == pytest.approx(0.123)
