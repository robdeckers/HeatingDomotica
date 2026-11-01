from datetime import UTC, datetime, timedelta

import pytest

from heating_optimizer.decision import Action, DecisionInput, ReasonCode, decide

NOW = datetime(2026, 10, 27, 13, 5, tzinfo=UTC)
GAS = 0.150


def inp(elec, heating_on=False, minutes_in_state=60.0, gas=GAS, **kwargs):
    since = None if minutes_in_state is None else NOW - timedelta(minutes=minutes_in_state)
    defaults = dict(margin_on=0.02, margin_off=0.005, min_on_minutes=15, min_off_minutes=15)
    defaults.update(kwargs)
    return DecisionInput(NOW, elec, gas, heating_on, since, **defaults)


def check(decision_input, action, code):
    decision = decide(decision_input)
    assert (decision.action, decision.reason_code) == (action, code), decision.reason
    return decision


@pytest.mark.parametrize("elec,gas", [(None, GAS), (0.10, None), (None, None)])
def test_missing_data_is_failsafe_off(elec, gas):
    d = check(inp(elec, heating_on=True, gas=gas), Action.OFF, ReasonCode.STALE_DATA)
    assert not d.wants_on


def test_failsafe_ignores_min_on_timer():
    check(inp(None, heating_on=True, minutes_in_state=1), Action.OFF, ReasonCode.STALE_DATA)


def test_safety_lockout_ignores_min_on_timer():
    check(inp(0.05, heating_on=True, minutes_in_state=1, all_locked_out=True), Action.OFF, ReasonCode.SAFETY_LOCKOUT)


def test_switch_on_when_cheaper_by_more_than_margin():
    d = check(inp(0.120), Action.ON, ReasonCode.ELEC_CHEAPER)
    assert d.wants_on
    assert "cheaper than gas" in d.reason


def test_exactly_at_margin_does_not_switch_on():
    # elec + margin_on == gas is not strictly cheaper
    check(inp(0.130), Action.HOLD, ReasonCode.IN_HYSTERESIS_BAND)


def test_min_off_timer_blocks_switch_on():
    d = check(inp(0.100, minutes_in_state=5), Action.HOLD, ReasonCode.MIN_OFF_TIMER)
    assert not d.wants_on


def test_min_off_timer_elapsed_exactly():
    check(inp(0.100, minutes_in_state=15), Action.ON, ReasonCode.ELEC_CHEAPER)


def test_unknown_state_since_counts_as_elapsed():
    check(inp(0.100, minutes_in_state=None), Action.ON, ReasonCode.ELEC_CHEAPER)


def test_off_and_gas_cheaper_is_no_change():
    check(inp(0.200), Action.HOLD, ReasonCode.NO_CHANGE)


def test_off_in_band():
    check(inp(0.140), Action.HOLD, ReasonCode.IN_HYSTERESIS_BAND)


def test_switch_off_when_no_longer_cheaper_by_margin_off():
    d = check(inp(0.146, heating_on=True), Action.OFF, ReasonCode.GAS_CHEAPER)
    assert not d.wants_on


def test_min_on_timer_blocks_switch_off():
    d = check(inp(0.200, heating_on=True, minutes_in_state=10), Action.HOLD, ReasonCode.MIN_ON_TIMER)
    assert d.wants_on


def test_on_within_band_holds():
    # gas - margin_on <= elec < gas - margin_off
    d = check(inp(0.140, heating_on=True), Action.HOLD, ReasonCode.IN_HYSTERESIS_BAND)
    assert d.wants_on


def test_on_and_clearly_cheaper_is_no_change():
    check(inp(0.100, heating_on=True), Action.HOLD, ReasonCode.NO_CHANGE)


def test_hysteresis_prevents_flapping_on_small_moves():
    # Starts off; a price that dips into the band does not switch on, and once on it does not switch off in the band.
    assert decide(inp(0.135)).action is Action.HOLD
    assert decide(inp(0.125)).action is Action.ON
    assert decide(inp(0.135, heating_on=True)).action is Action.HOLD
    assert decide(inp(0.145, heating_on=True)).action is Action.OFF


def test_negative_prices():
    check(inp(-0.05), Action.ON, ReasonCode.ELEC_CHEAPER)


def test_zero_margins():
    check(inp(0.1499, margin_on=0.0, margin_off=0.0), Action.ON, ReasonCode.ELEC_CHEAPER)
    check(inp(0.150, heating_on=True, margin_on=0.0, margin_off=0.0), Action.OFF, ReasonCode.GAS_CHEAPER)


def test_as_dict_is_serializable():
    data = decide(inp(0.120)).as_dict()
    assert data["action"] == "ON"
    assert data["reason_code"] == "ELEC_CHEAPER"
    assert data["now"].startswith("2026-10-27")
