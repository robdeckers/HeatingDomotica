"""Pure decision engine: should the plugs allow electric heating?

Rules, evaluated in order:
1. Price data missing → OFF (STALE_DATA). Gas is the safe default.
2. All available plugs are locked out → OFF (SAFETY_LOCKOUT). Per-plug lockouts are enforced by the House.
3. Heating off, elec + margin_on < gas, min_off elapsed → ON (ELEC_CHEAPER).
4. Heating on, elec ≥ gas − margin_off, min_on elapsed → OFF (GAS_CHEAPER).
5. A timer blocks the change from rule 3 or 4 → HOLD (MIN_OFF_TIMER / MIN_ON_TIMER).
6. Otherwise → HOLD (IN_HYSTERESIS_BAND when between the thresholds, else NO_CHANGE).

Electric heat is valued at the market price excl. VAT (solar surplus: the feed-in value), gas at its consumer price;
see ``prices.service.HeatPrices``. Rules 1 and 2 ignore the min-on timer. Whether the heaters themselves are switched on by a person does not matter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from heating_optimizer.scheduling import elapsed_s


class Action(StrEnum):
    ON = "ON"
    OFF = "OFF"
    HOLD = "HOLD"
    DISABLED = "DISABLED"  # used by the app when the control boolean is off; never returned by decide()


class ReasonCode(StrEnum):
    STALE_DATA = "STALE_DATA"
    SAFETY_LOCKOUT = "SAFETY_LOCKOUT"
    ELEC_CHEAPER = "ELEC_CHEAPER"
    GAS_CHEAPER = "GAS_CHEAPER"
    MIN_ON_TIMER = "MIN_ON_TIMER"
    MIN_OFF_TIMER = "MIN_OFF_TIMER"
    NO_CHANGE = "NO_CHANGE"
    IN_HYSTERESIS_BAND = "IN_HYSTERESIS_BAND"
    DISABLED = "DISABLED"
    DEMO_RUNNING = "DEMO_RUNNING"


@dataclass(frozen=True)
class DecisionInput:
    now: datetime
    elec_per_kwh_heat: float | None
    gas_per_kwh_heat: float | None
    heating_on: bool
    state_since: datetime | None  # None (unknown, e.g. after a restart) counts as "timer elapsed"
    margin_on: float
    margin_off: float
    min_on_minutes: float
    min_off_minutes: float
    all_locked_out: bool = False

    def minutes_in_state(self) -> float | None:
        return None if self.state_since is None else elapsed_s(self.now, self.state_since) / 60.0


@dataclass(frozen=True)
class Decision:
    action: Action
    reason_code: ReasonCode
    reason: str
    inputs: DecisionInput

    @property
    def wants_on(self) -> bool:
        """Desired plug state after this decision (HOLD keeps the current state)."""
        if self.action is Action.HOLD:
            return self.inputs.heating_on
        return self.action is Action.ON

    def as_dict(self) -> dict[str, Any]:
        inputs = asdict(self.inputs)
        inputs["now"] = self.inputs.now.isoformat()
        inputs["state_since"] = self.inputs.state_since.isoformat() if self.inputs.state_since else None
        return {"action": str(self.action), "reason_code": str(self.reason_code), "reason": self.reason, **inputs}


def _eur(value: float) -> str:
    return f"€{value:.3f}"


def decide(inp: DecisionInput) -> Decision:
    def make(action: Action, code: ReasonCode, reason: str) -> Decision:
        return Decision(action, code, reason, inp)

    if inp.elec_per_kwh_heat is None or inp.gas_per_kwh_heat is None:
        missing = [n for n, v in (("electricity", inp.elec_per_kwh_heat), ("gas", inp.gas_per_kwh_heat)) if v is None]
        return make(Action.OFF, ReasonCode.STALE_DATA, f"No current {' and '.join(missing)} price: failsafe to gas")
    if inp.all_locked_out:
        return make(Action.OFF, ReasonCode.SAFETY_LOCKOUT, "All available plugs are in a safety lockout")

    elec, gas = inp.elec_per_kwh_heat, inp.gas_per_kwh_heat
    minutes = inp.minutes_in_state()
    prices = f"electric heat (market) {_eur(elec)}/kWh vs gas heat {_eur(gas)}/kWh"

    if not inp.heating_on:
        if elec + inp.margin_on < gas:
            if minutes is not None and minutes < inp.min_off_minutes:
                return make(
                    Action.HOLD,
                    ReasonCode.MIN_OFF_TIMER,
                    f"Electric is cheaper ({prices}) but off for only {minutes:.1f} of {inp.min_off_minutes:g} min",
                )
            return make(
                Action.ON,
                ReasonCode.ELEC_CHEAPER,
                f"Electric heat (market) {_eur(elec)}/kWh is {_eur(gas - elec)} cheaper than gas heat {_eur(gas)}/kWh "
                f"(margin {_eur(inp.margin_on)})",
            )
        if elec < gas - inp.margin_off:
            return make(
                Action.HOLD,
                ReasonCode.IN_HYSTERESIS_BAND,
                f"Electric is cheaper by less than the switch-on margin {_eur(inp.margin_on)} ({prices})",
            )
        return make(Action.HOLD, ReasonCode.NO_CHANGE, f"Gas is cheaper, heating stays off ({prices})")

    if elec >= gas - inp.margin_off:
        if minutes is not None and minutes < inp.min_on_minutes:
            return make(
                Action.HOLD,
                ReasonCode.MIN_ON_TIMER,
                f"Gas is cheaper ({prices}) but on for only {minutes:.1f} of {inp.min_on_minutes:g} min",
            )
        return make(
            Action.OFF,
            ReasonCode.GAS_CHEAPER,
            f"Electric heat (market) {_eur(elec)}/kWh is no longer cheaper than gas heat {_eur(gas)}/kWh "
            f"by the switch-off margin {_eur(inp.margin_off)}",
        )
    if elec + inp.margin_on >= gas:
        return make(
            Action.HOLD,
            ReasonCode.IN_HYSTERESIS_BAND,
            f"Electric still cheaper, within the hysteresis band ({prices})",
        )
    return make(Action.HOLD, ReasonCode.NO_CHANGE, f"Electric is cheaper, heating stays on ({prices})")
