"""Pure safety evaluation for a plug with a resistive heater.

The heaters are rated 3000 W at 230 V, the plug's limit. Power scales with voltage squared, so at 235–240 V a healthy
heater draws more than 3000 W. The trip criteria are therefore:

- current above ``plug_max_current_a`` continuously for at least ``overcurrent_min_duration_s``;
- plug temperature above ``max_device_temp_c``;
- power more than 15% above the voltage-corrected heater rating (wrong or faulty appliance).

Power above ``plug_max_power_w`` alone does not trip.
"""

from __future__ import annotations

from datetime import datetime

from heating_optimizer.config import PlugConfig
from heating_optimizer.scheduling import elapsed_s

NOMINAL_VOLTAGE = 230.0
OVERPOWER_TOLERANCE = 1.15


def expected_power_w(heater_power_w: float, voltage_v: float | None) -> float:
    """Power a resistive heater rated at 230 V draws at ``voltage_v``."""
    voltage = voltage_v if voltage_v and voltage_v > 0 else NOMINAL_VOLTAGE
    return heater_power_w * (voltage / NOMINAL_VOLTAGE) ** 2


class SafetyMonitor:
    def __init__(self, cfg: PlugConfig):
        self._cfg = cfg
        self._overcurrent_since: datetime | None = None
        self.above_plug_rating_logged = False

    def reset(self) -> None:
        self._overcurrent_since = None

    def evaluate(
        self,
        now: datetime,
        power_w: float | None,
        current_a: float | None,
        voltage_v: float | None,
        temperature_c: float | None,
    ) -> str | None:
        """Return a trip reason, or None when the readings are safe."""
        cfg = self._cfg
        if temperature_c is not None and temperature_c > cfg.max_device_temp_c:
            return f"plug temperature {temperature_c:.1f} °C > {cfg.max_device_temp_c:.0f} °C"

        if current_a is not None and current_a > cfg.plug_max_current_a:
            if self._overcurrent_since is None:
                self._overcurrent_since = now
            duration = elapsed_s(now, self._overcurrent_since)
            if duration >= cfg.overcurrent_min_duration_s:
                return (
                    f"current {current_a:.2f} A > {cfg.plug_max_current_a:.1f} A for {duration:.0f} s "
                    f"(limit {cfg.overcurrent_min_duration_s:.0f} s)"
                )
        else:
            self._overcurrent_since = None

        if power_w is not None:
            limit = expected_power_w(cfg.heater_power_w, voltage_v) * OVERPOWER_TOLERANCE
            if power_w > limit:
                return f"power {power_w:.0f} W > {limit:.0f} W (heater {cfg.heater_power_w:.0f} W at {voltage_v or NOMINAL_VOLTAGE:.0f} V + 15%)"
        return None
