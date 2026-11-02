# Heating Optimizer

An AppDaemon app for Home Assistant OS. It allows electric heating (resistive heaters on two Shelly Plug M Gen3)
whenever one kWh of useful heat is cheaper from electricity than from the gas boiler. Prices come from ANWB Energie's
dynamic contract: hourly electricity, daily gas, both on EnergyZero market prices.

This directory has nothing to do with the ROS 2 packages in this repository. It uses neither colcon nor ROS. It only
shares the repository's pre-commit hooks.

## How it works

- **Operating model.** The app switches the **plugs**, not the heaters. Plug ON means electric heating is allowed
  because it is cheaper; plug OFF means gas heats. A person switches each heater itself on or off, so "plug ON at
  0 W" is normal and is reported as `heater_active: false`, never as a fault. There is no room-temperature control.
- **Every 5 minutes** (hh:00, hh:05, …, aligned to the clock) the app compares the cost of one kWh of heat from each
  source and decides ON, OFF, or HOLD:
  ```
  electric heat = (market + ANWB fee + energy tax) per kWh            / heater efficiency (1.0)
  gas heat      = (market + ANWB fee + energy tax) per m³ / 9.769 kWh/m³ / boiler efficiency (0.90)
  ```
  It switches ON when electric heat is at least 2 ct/kWh cheaper, OFF when the advantage drops below 0.5 ct/kWh,
  and keeps each state for at least 15 minutes. Every tariff component is entered **incl. 21% VAT**.
- **Fixed charges** (standing charge, grid fees, energy tax credit) are excluded on purpose: they do not change with
  consumption, so they do not affect which source is cheaper for the next kWh. The energy tax used is the
  **first-tier** rate, which applies to normal household usage.
- **Gas day.** The dynamic gas price changes at 06:00, not at midnight.
- **Failsafe.** If the current price is unknown, the plugs go OFF: gas is the safe default.
- **Disable.** Switching `input_boolean.heating_optimizer_enabled` off turns both plugs off once. After that the app
  stays hands-off, so you can switch the plugs by hand in HA.
- **Thermostat interaction.** Savings come from the gas thermostat firing less while the electric heaters warm the
  room. Thermostat setpoint and location matter. Controlling the thermostat is out of scope.

## ⚠️ 3000 W heaters on a 13 A plug

The heaters are rated 3000 W at 230 V, which is exactly the Shelly Plug M Gen3's limit (13 A / 3000 W resistive).
A resistive heater's power rises with voltage squared: about 3130 W / 13.3 A at 235 V and 3270 W / 13.6 A at 240 V.

- The software safety check trips on **current above 13 A for at least 10 s**, on plug temperature above 70 °C, and
  on power more than 15% above the voltage-corrected heater rating (wrong or faulty appliance). Power above 3000 W
  alone does not trip.
- **Consequence:** with the 13 A limit, a heater on its 3000 W setting **will trip whenever mains voltage is above
  about 230 V**, which is most of the time in the Netherlands. Each trip switches the plug off and locks it out until
  the next full hour. Use the heater's lower setting (e.g. 2000 W), or decide deliberately to raise
  `plug_max_current_a`, knowing that the plug is then run above its rating.
- The plug's built-in overheat protection may also cut power during long full-load runs. The app detects this
  through the plug's error flags, logs it, and locks the plug out until the next full hour.
- **No on-device power limit is configured on the plugs** (deliberate choice). The software protection only works
  while Home Assistant and AppDaemon are running. When they are down, the plug's built-in overheat protection is the
  only hardware backstop.

## Layout

```
appdaemon/apps/
├── heating_optimizer.yaml      # AppDaemon app config (fill in energyzero_config_entry)
├── heating_optimizer_main.py   # AppDaemon entry module → heating_optimizer.main_app.MainApp
├── heating_optimizer_demo.py   # AppDaemon entry module → heating_optimizer.demo_app.DemoApp
└── heating_optimizer/          # the package; everything except main_app/demo_app is pure Python
docs/ha_setup.md                # Home Assistant setup, step by step
ha/                             # helpers, dashboard and recorder YAML
scripts/deploy.sh               # rsync over SSH to the Pi
tests/                          # pytest, no HA or AppDaemon needed
```

Differences from the original plan:

- The app config is `heating_optimizer.yaml`, not `apps.yaml`, so deploying never overwrites other apps' config.
  AppDaemon reads every `*.yaml` in the apps folder.
- Thin top-level entry modules are used because AppDaemon loads apps by top-level module name.
- `prices/service.py` and `devices/safety.py` were added to keep the price logic and the safety rules pure and
  testable.

## Development

```bash
cd HeatingOptimizer
pip install -r requirements-dev.txt
pytest
pre-commit run --files $(git ls-files HeatingOptimizer)   # from the repository root
```

The tests cover tariff math, gas/heat conversion, the price cache (gas day, 15-minute averaging, DST days), every
decision branch, the house with a failing plug, the safety rules, clock alignment across DST, the decision log, and
smoke tests of both AppDaemon apps against a stubbed AppDaemon API.

## Deploy

```bash
cp scripts/deploy.env.example scripts/deploy.env   # set HA_USER
scripts/deploy.sh --dry-run                         # preview
scripts/deploy.sh                                   # copy and restart AppDaemon
```

`heating_optimizer.yaml` is only copied when it does not exist on the Pi yet. Use `--config` to overwrite it
deliberately. See [docs/ha_setup.md](docs/ha_setup.md) for the one-time Home Assistant setup.

## First run

1. Deploy, then press **Demo 1**. With `energyzero_config_entry` still `TODO(user)`, it lists the EnergyZero
   entities and the config entry ID. Fill that in on the Pi and restart AppDaemon.
2. Check the energy tax figures against the official Belastingdienst table. The 2026 values in the config
   (€0.11085/kWh, €0.72680/m³ incl. VAT) came from secondary sources.
3. Run the demos. Each runs about 30 s, posts a notification with PASS/FAIL, and restores the
   plugs afterwards. **Stop demo** aborts and restores immediately. A demo pauses the main loop while it runs.
   - **Demo 1 – electricity:** today's price curve, cheapest/most expensive hours, consumer price breakdown, data
     source and age, call latency.
   - **Demo 2 – gas:** price per m³, per kWh fuel and per kWh heat, gas day, break-even electricity price.
   - **Demo 3 – Shelly plugs:** identity check (MAC via RPC, resolved IP and how), switch on, read every 3 s, switch
     off, restore. Switch the heaters on by hand first if you want the power check; otherwise it reports SKIPPED.
   - **Demo 4 – house:** all plugs on together with aggregated power, off, restore, then a simulated failure of one
     plug to show how it is reported (the other plug is switched for real and restored).
4. Turn on `input_boolean.heating_optimizer_enabled`. From then on the app switches the plugs. Watch it for a
   couple of hours and check `decisions.csv`. Ticks should be within 2 s of each 5-minute mark, with a
   `HOUR_CHANGE` price lookup every hour.

## Logs and diagnostics

On the Pi, under `/addon_configs/a0d7b954_appdaemon/logs/heating_optimizer/`:

- `decisions.jsonl` and `decisions.csv`: one record per tick (including HOLD), with every price component, the
  margins, timers, and per-plug before/after/verified/power. Both rotate at 5 MB, 10 backups.
- `heating_optimizer.log`: the application log (also in the AppDaemon log).
- `switch_on_counts.json`: the switch-on counter per plug, keyed by MAC. Delete it to reset the counters.

Sensors in HA: `sensor.heating_optimizer_status`, `_elec_heat_price`, `_gas_heat_price`, `_price_delta`,
`_last_decision`, `_health` (OK / DEGRADED / ERROR with the failing checks as attributes), and `_demo_output`.
Per plug, `sensor.heating_optimizer_<name>_switch_on_count` counts every off → on of its switch, whether the app,
a demo or a person switched it, and survives restarts. Switch-ons while AppDaemon is not running are not counted.
ON/OFF changes also go to the HA logbook.

Health turns DEGRADED after `tariffs_valid_until` (2026-12-31) as a reminder to update the energy tax and the ANWB
fees. Both change on 1 January.

## Raspberry Pi notes

- **SD-card wear:** the decision log adds about 288 small lines a day, which is fine. Use `level: DEBUG` only for
  diagnosis sessions. All log files rotate. Booting the Pi from a USB SSD is recommended.
- **Recorder:** merge `ha/recorder_exclude.yaml`, so the per-tick status, health and demo sensors do not grow the
  SQLite database.
- **Clock:** HA OS syncs time over NTP. The status sensor reports each tick's drift, and the self-test logs the UTC
  offset at startup.
