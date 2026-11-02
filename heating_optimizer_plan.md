# Heating Optimizer – Implementation Plan

> **For Claude Code:** Build this project phase by phase, in the order given in section 12. Finish each phase, including its demo and tests, before starting the next. Ask the user before guessing an entity ID, IP address, or tariff value. All values marked `TODO(user)` must come from the user's config, never be hard-coded.

> **Repository context:** the application lives in the directory `HeatingOptimizer/` at the root of the `agx_robotics` repository. It has **nothing to do with the ROS 2 packages** in that repository: it does not use colcon, CMake, or ROS, and the C++/ROS sections of `AGENTS.md` do not apply. It does follow the repository's pre-commit hooks (section 4a).

---

## 1. Goal

Use electric heating (resistive heaters on Shelly plugs) instead of the gas boiler whenever **one kWh of useful heat** is cheaper from electricity than from gas. The household has a **dynamic electricity contract** (hourly prices) and a **dynamic gas contract** (daily price) on the Dutch market.

- **Supplier:** **ANWB Energie** (dynamic contract only). ANWB buys through the **EnergyZero** platform, so the EnergyZero integration's market prices are exactly the prices ANWB bills on. ANWB bills electricity **per hour** and gas **per gas day**, and publishes tomorrow's prices around 15:00.

- **Language:** Python 3.11+
- **Platform:** Raspberry Pi 4 running Home Assistant OS 18.4 (see section 2a)
- **Runtime:** the AppDaemon add-on
- **Principle:** use native Home Assistant integrations for prices and devices. AppDaemon contains the logic, the scheduling, the logging, and the diagnostics.

### Operating model (by design)

- The app switches the **Shelly plugs**, not the heaters. A plug ON means "electric heating is allowed because it is cheaper"; a plug OFF means "gas heats".
- A **person** switches each heater itself on or off. The app has no room-temperature input, thermostat control, or time windows. Whether a room needs heat is the person's decision.
- Consequently, **plug ON with ~0 W is a normal state** (the heater is switched off by hand). It is never treated as a fault; it is reported as `heater_active: false`.
- The heaters resume heating by themselves when the plug restores power (mechanical switch), so switching the plug is sufficient.

### Why AppDaemon

- It runs plain Python classes and modules, so the "house" model can grow to include solar panels later.
- Its built-in scheduler supports runs aligned to the clock, such as `run_every` with an explicit start time.
- It has full read and write access to Home Assistant entities and services.
- It provides proper log files, and the code can be unit-tested outside Home Assistant.

---

## 2. Architecture overview

```
┌──────────── Raspberry Pi 4 · Home Assistant OS 18.4 ────────────┐
│                                                                 │
│  Integrations (native)        Helpers (UI-created)              │
│  ─ EnergyZero (elec + gas)    ─ input_boolean.heating_optimizer_enabled
│  ─ Shelly (2× Plug M Gen3)    ─ input_button.demo_* (4 + stop)  │
│                               ─ input_number.heating_optimizer_margin_ct
│                                                                 │
│  ┌──────────── AppDaemon add-on ────────────┐                   │
│  │ heating_optimizer (package)              │                   │
│  │  ├─ PriceService  → tariffs → €/kWh heat │                   │
│  │  ├─ ShellyPlug (via HA entities)         │                   │
│  │  ├─ House (collection of plugs, future   │                   │
│  │  │         solar)                        │                   │
│  │  ├─ DecisionEngine (pure Python)         │                   │
│  │  ├─ MainApp (5-min, clock-aligned loop)  │                   │
│  │  ├─ DemoApp (4 demos, 30 s each + stop)  │                   │
│  │  └─ Diagnostics + DecisionLogger         │                   │
│  └──────────────────────────────────────────┘                   │
│        ↑ publishes sensor.heating_optimizer_* back to HA        │
└─────────────────────────────────────────────────────────────────┘
```

**Rule:** the business logic (tariff math, decision engine, house model, price cache, clock alignment) is **pure Python with no AppDaemon imports**. That makes it testable with `pytest` on any machine. Thin adapter classes connect it to Home Assistant. Use AppDaemon's synchronous `hass.Hass` API (no `async` apps).

**Dependencies:** only the Python standard library plus what the AppDaemon add-on already ships (e.g. `requests` for the RPC diagnostics). If anything else is needed, it goes in the add-on's `python_packages` option and is documented in the README. Development-only dependencies (`pytest`) go in `requirements-dev.txt`.

---

## 2a. Hardware and platform facts

### Host
- **Raspberry Pi 4** running **Home Assistant OS 18.4** (aarch64). The AppDaemon add-on supports this architecture.
- **Storage wear:** if the Pi boots from an SD card, the logging must be careful with writes. The decision log adds about 288 small lines a day, which is fine. The DEBUG level, however, is meant for diagnosis sessions only, not as a permanent setting, and every log file rotates. Booting from a USB SSD is recommended. **Claude Code:** add a short note on this to the README.
- **Recorder database:** keep the attributes of the app's sensors small. Provide a `recorder: exclude:` snippet in `ha/recorder_exclude.yaml` for `sensor.heating_optimizer_demo_output` and for the attribute-heavy diagnostics sensors, so the SQLite database on the Pi does not grow quickly.
- **Clock accuracy:** the clock-aligned loop depends on the system time. Home Assistant OS syncs the time over NTP. The diagnostics measure the drift of each tick (section 9), and the startup self-test logs the current UTC offset.
- **Resources:** keep CPU and RAM use low. Make no busy loops; only the AppDaemon scheduler triggers work. Use a single shared HTTP session for the RPC diagnostics.

### Shelly plugs

Both plugs are **Shelly Plug M Gen3**, model `S3PL-30110EU`. Each is rated **13 A / 3000 W for a resistive load** and has built-in overheating protection. They are Gen3 devices, so they use the Gen2+ RPC API.

| Plug | Device ID (MAC) | Decimal ID | Current IP (DHCP, may change) |
|------|-----------------|------------|-------------------------------|
| shelly_1 | `907069543f3c` | 158812477865788 | 192.168.2.18 |
| shelly_2 | `9070695dba7c` | 158812478487164 | 192.168.2.19 |

- The two plugs are on **separate electrical circuits** (groups), so no combined house power limit is needed.
- The connected heaters resume heating when power returns (see the operating model in section 1).
- **Heaters run up to 3000 W, which is the plug's rated limit.** A resistive heater's power scales with voltage squared: a 3000 W (at 230 V) heater draws about 3130 W / 13.3 A at 235 V and 3270 W / 13.6 A at 240 V. The user has chosen to allow this. Consequences:
  - The software safety check is based on **measured current**, not on `plug_max_power_w` (section 6.3), otherwise it would trip at every normal run.
  - The plug's own built-in overheat protection may cut power during long runs at full load. The app detects and reports this (section 6.3).
  - **No on-device power limit is configured on the plugs** (user's choice). The software protection works only while HA and AppDaemon are running; the plug's built-in overheat protection is the only hardware backstop. **Claude Code:** put this warning prominently in the README.

**Identity rules:**
- The **device ID (MAC)** is the permanent key of each plug in the config, code, and logs. **The IP address is never used as an identity.** The decimal ID is recorded for reference only and is not used.
- The IPs come from DHCP and can change. The HA Shelly integration follows a changed IP automatically through zeroconf/mDNS, so **switching and reading through HA keeps working**.
- The direct-RPC diagnostics (section 6.3) resolve the IP at runtime in this order:
  1. the mDNS hostname, expected to be `shellyplugmg3-<mac>.local` (Claude Code verifies the exact hostname in demo 3; mDNS resolution from inside the add-on container may not work, in which case step 2/3 apply and this is logged once at INFO);
  2. the last IP that worked, kept in memory;
  3. the `fallback_ip` from the config.

  After resolving, the app always calls `Shelly.GetDeviceInfo` and checks that the returned `mac`/`id` matches the configured device ID. On a mismatch, the IP is rejected and logged, so a reshuffled DHCP lease can never cause the app to talk to the wrong device.
- Recommended for the user, though not required: set a DHCP reservation for both MACs in the router.

**Matter:** these plugs also support Matter. Add them **only through the Shelly integration**, not also through Matter. Adding both creates duplicate switch entities, and the Shelly integration exposes more diagnostic data.

---

## 3. Prerequisites (the user does these in the HA UI)

1. Install the **AppDaemon** add-on from the Add-on Store and start it.
2. Install the **Advanced SSH & Web Terminal** add-on, used by the deploy script (section 4b).
3. Add the **EnergyZero** integration. It provides electricity and gas **market** prices (ANWB Energie uses the same EnergyZero prices). ANWB's purchase fee and the energy tax are added by the app from the config (section 6.1); ANWB has no HA integration of its own.
4. Add the **Shelly** integration and adopt both Shelly Plug M Gen3 devices (see section 2a). A DHCP reservation is recommended but not required.
5. Create these helpers (Settings → Devices & Services → Helpers):
   - `input_boolean.heating_optimizer_enabled`, which starts and stops the main logic.
   - `input_button.demo_electricity_price`
   - `input_button.demo_gas_price`
   - `input_button.demo_shelly_plug`
   - `input_button.demo_house`
   - `input_button.demo_stop`, an emergency stop for a running demo.
   - Optional live tuning: `input_number.heating_optimizer_margin_ct` (switch-on margin in **ct/kWh**, range 0–10, step 0.1).
6. Create a dashboard card with the buttons above, plus the sensors the app publishes (section 9).

> **Claude Code:** produce `docs/ha_setup.md` with exact click-paths. Also provide a ready-made dashboard YAML in `ha/dashboard_heating_optimizer.yaml` and helper definitions in `ha/helpers.yaml`, for users who prefer YAML.

---

## 4. Project structure

The repository directory is `HeatingOptimizer/`; the Python package inside it is `heating_optimizer` (package names must be snake_case).

```
HeatingOptimizer/
├── README.md
├── pyproject.toml                    # pytest config (pythonpath, testpaths); no build system
├── requirements-dev.txt              # pytest
├── docs/ha_setup.md
├── scripts/deploy.sh                 # rsync the app to the Pi (section 4b)
├── ha/
│   ├── helpers.yaml
│   ├── dashboard_heating_optimizer.yaml
│   └── recorder_exclude.yaml
├── appdaemon/apps/
│   ├── apps.yaml                     # AppDaemon app registration + config
│   └── heating_optimizer/
│       ├── __init__.py
│       ├── config.py                 # dataclasses + validation of apps.yaml args
│       ├── tariffs.py                # pure: market price → consumer price
│       ├── heat_cost.py              # pure: consumer price → €/kWh useful heat
│       ├── scheduling.py             # pure: next aligned tick, drift, DST handling
│       ├── prices/
│       │   ├── base.py               # PriceProvider protocol, PricePoint dataclass
│       │   ├── energyzero.py         # HA adapter: EnergyZero service/sensor
│       │   └── cache.py              # pure: day/next-day price cache keyed by timestamp
│       ├── devices/
│       │   ├── base.py               # HeatingDevice protocol (future: heat pump)
│       │   └── shelly_plug.py        # ShellyPlug via HA entities (+ optional RPC)
│       ├── house.py                  # House: plugs now, EnergySource slot for solar
│       ├── decision.py               # pure DecisionEngine with hysteresis + timers
│       ├── diagnostics.py            # health checks, metrics, staleness, latency
│       ├── decision_log.py           # JSONL + CSV decision log
│       ├── logging_setup.py          # rotating file handlers
│       ├── main_app.py               # AppDaemon app: clock-aligned 5-min loop
│       └── demo_app.py               # AppDaemon app: demo buttons, 30 s loops
└── tests/
    ├── fakes.py                      # FakePriceProvider, FakePlug, FakeClock
    ├── test_config.py
    ├── test_tariffs.py
    ├── test_heat_cost.py
    ├── test_price_cache.py           # gas day, 15-min averaging, missing points
    ├── test_decision.py
    ├── test_house.py
    └── test_scheduling.py            # alignment calculations
```

Tests run locally with `cd HeatingOptimizer && pytest`. They are not added to the repository's Bitbucket pipeline.

### 4a. Repository tooling

- The repository's pre-commit hooks apply: `ruff` (lint, `--fix`), `ruff-format` with line length 120, `prettier` (YAML/Markdown/JSON), `cspell`, and the whitespace/EOF hooks. Write code that passes them; run `pre-commit run --files HeatingOptimizer/**` before each commit.
- Add the Dutch and domain terms that `cspell` flags (e.g. `energiebelasting`, `inkoopvergoeding`, `energyzero`, `appdaemon`, `shellyplugmg`) to the repository's `.dictionary.txt`; the `sort-dictionary` hook keeps it sorted.
- Do not change the repository's root configuration (`colcon_defaults.yaml`, `bitbucket-pipelines.yml`, `.pre-commit-config.yaml`) for this project.

### 4b. Deployment

On Home Assistant OS, the AppDaemon add-on's app folder is `/addon_configs/a0d7b954_appdaemon/apps/` (seen inside the add-on as `/config/apps/`). **Claude Code:** confirm the slug with the user on first deploy.

`scripts/deploy.sh`:
- Uses `rsync` over SSH (Advanced SSH & Web Terminal add-on, key-based login) to copy `appdaemon/apps/` to the app folder. Host, user, and port come from environment variables or a git-ignored `scripts/deploy.env`, never hard-coded. Defaults: `HA_HOST=homeassistant.local`, `HA_PORT=22`; `HA_USER` must be set (it is the username from the SSH add-on's config). Provide `scripts/deploy.env.example`.
- Copies only the app: excludes `tests/`, `__pycache__/`, and local files. `apps.yaml` is deployed too, but secrets stay in the add-on's `secrets.yaml` (not in the repository).
- Supports `--dry-run` (passes `rsync -n`) to preview changes.
- If `rsync` is not available in the SSH add-on, falls back to `tar | ssh` and says so.
- AppDaemon reloads changed apps automatically; the script then prints the command to tail the app log.

---

## 5. Configuration (`apps.yaml`)

All values are configurable. Defaults are shown; anything marked `TODO(user)` must be filled in.

```yaml
heating_optimizer_main:
  module: heating_optimizer.main_app
  class: MainApp
  config: &cfg
    timezone: Europe/Amsterdam

    # ── Tariffs ─────────────────────────────────────────────────────────────
    # ALL tariff components are entered INCLUDING 21% VAT, exactly as shown on
    # the ANWB invoice / website and the Belastingdienst incl.-VAT tables.
    # Update these every 1 January (energy tax) and whenever ANWB changes its fees.
    tariffs_valid_until: 2026-12-31             # after this date → health DEGRADED (reminder to update)

    electricity:
      source: energyzero
      energyzero_config_entry: TODO(user)        # discovered in demo 1
      price_resolution: hour                    # hour | quarter (see 6.1); ANWB bills per hour
      supplier_markup_eur_per_kwh_incl_vat: 0.018     # ANWB inkoopvergoeding
      energy_tax_eur_per_kwh_incl_vat: 0.11085        # energiebelasting 2026, tier 1 (0–2900 kWh)
      vat_rate: 0.21                            # only used to show the excl.-VAT breakdown

    gas:
      source: energyzero
      supplier_markup_eur_per_m3_incl_vat: 0.07680    # ANWB inkoopvergoeding, from 2026-09-01
      energy_tax_eur_per_m3_incl_vat: 0.72680         # energiebelasting 2026, tier 1
      vat_rate: 0.21                            # only used to show the excl.-VAT breakdown
      calorific_value_kwh_per_m3: 9.769         # Dutch reference (upper value 35.17 MJ/m³)
      boiler_efficiency: 0.90                   # HR boiler, realistic, on upper calorific value

    electric_heating:
      efficiency: 1.0                           # resistive heater; COP for heat pump later

    decision:
      margin_on_eur_per_kwh: 0.02               # electric must be ≥2 ct cheaper to switch ON
      margin_off_eur_per_kwh: 0.005             # switch OFF once electric ≥ gas − 0.5 ct
      min_on_minutes: 15
      min_off_minutes: 15
      margin_override_entity: input_number.heating_optimizer_margin_ct   # optional; value in ct/kWh, ÷100 → €
      failsafe_state: off                       # if prices are unknown → plugs off (gas heats)
      max_switches_per_hour: 4                  # per plug; above this → flapping warning

    prices:
      daily_refresh_time: "15:05"               # ANWB/EnergyZero publish tomorrow's prices ~15:00
      retry_interval_minutes: 30                # until tomorrow's prices are loaded
      tomorrow_expected_by: "16:30"             # health warning after this time
      sensor_max_age_minutes: 70                # only for the sensor fallback (see 6.1)

    loop:
      interval_minutes: 5                       # ticks at hh:00, hh:05, … hh:55; must divide 60
      evaluate_on_startup: true                 # one evaluation immediately, then aligned

    house:
      name: Home
      plugs:
        - name: shelly_1
          model: S3PL-30110EU                   # Shelly Plug M Gen3
          device_id: "907069543f3c"             # MAC: permanent identity
          switch_entity: auto                   # auto-discover, or set e.g. switch.xxx
          fallback_ip: 192.168.2.18             # DHCP, may change: only a fallback
          heater_power_w: 3000                  # heater's max rating at 230 V
          plug_max_power_w: 3000                # plug rating; informational (see 6.3)
          plug_max_current_a: 13.0              # plug rating; the actual safety trip criterion
          overcurrent_min_duration_s: 10        # current must exceed the limit this long to trip
          max_device_temp_c: 70                 # safety cutoff (plug internal temperature)
          active_power_threshold_w: 10          # above this, the heater counts as active
        - name: shelly_2
          model: S3PL-30110EU
          device_id: "9070695dba7c"
          switch_entity: auto
          fallback_ip: 192.168.2.19
          heater_power_w: 3000
          plug_max_power_w: 3000
          plug_max_current_a: 13.0
          overcurrent_min_duration_s: 10
          max_device_temp_c: 70
          active_power_threshold_w: 10
    shelly_rpc:
      enabled: true                             # read-only diagnostics
      timeout_s: 3
      hostname_pattern: "shellyplugmg3-{device_id}.local"   # verify in demo 3

    logging:
      level: INFO                               # DEBUG for extended diagnostics
      decision_log_dir: /config/logs/heating_optimizer   # = /addon_configs/a0d7b954_appdaemon/logs/heating_optimizer
      rotate_mb: 5
      backups: 10
    control_entity: input_boolean.heating_optimizer_enabled

heating_optimizer_demo:
  module: heating_optimizer.demo_app
  class: DemoApp
  config: *cfg
```

`config.py` validates this config at startup. On a missing `TODO(user)` value, the app refuses to start and logs a clear message naming the key. It also rejects:
- a `heater_power_w` above `plug_max_power_w`, or a `plug_max_current_a` above 16;
- tariff components outside a plausible incl.-VAT range (markup 0–0.10 €/kWh and 0–0.30 €/m³; energy tax 0–0.25 €/kWh and 0–1.50 €/m³). This catches excl./incl.-VAT or ct/€ mistakes;
- a `device_id` that is not 12 hex characters, or duplicate device IDs;
- an `interval_minutes` that does not divide 60;
- negative margins, efficiencies outside (0, 1] for the boiler, or a `vat_rate` outside [0, 1).

At startup the app checks that `decision_log_dir` is writable and creates it if needed; if that fails, it logs an ERROR and continues with the AppDaemon log only.

---

## 6. Component specifications

### 6.1 Step 1 – Electricity price (€/kWh, consumer, current hour)

**Source:** the EnergyZero integration. Preferred method: call the service `energyzero.get_energy_prices` with `config_entry`, `incl_vat: true`, and a `start` and `end` covering today and tomorrow, then cache the result in `prices/cache.py`. Fallback method: read the current-hour price sensor. Claude Code discovers the exact entity ID in demo 1.

**Why a cache rather than only the sensor:** at hh:00:00 the sensor can lag by a few seconds and still show the previous hour. Looking up the price by timestamp in a cached day series is exact.

**Cache refresh:** on startup; daily at `prices.daily_refresh_time`; every `retry_interval_minutes` until tomorrow's prices are present; and immediately whenever the price point for the current interval is missing. At midnight, today's series is dropped and tomorrow's becomes today's.

**Staleness:** day-ahead prices do not change once published, so the age of the cache is **not** a staleness criterion. The data counts as missing (→ `STALE_DATA`) when **no cached price point covers the current interval** and the sensor fallback also fails. The sensor fallback is valid only if its `last_updated` is within `sensor_max_age_minutes` **and** its value refers to the current hour.

**15-minute prices:** the Dutch day-ahead market moved to 15-minute intervals in late 2025. If the source returns quarter-hour points, then with `price_resolution: hour` the app uses the **average of the four quarters** of the current hour, as the user requested. If fewer than four quarters are present, the hour counts as missing. With `price_resolution: quarter`, it uses the current quarter. Log which mode is active. Because ANWB bills electricity per hour, `hour` is the correct setting.

**Consumer price formula (`tariffs.py`):** every component is incl. VAT (the market price is requested with `incl_vat: true`), so the consumer price is a plain sum:
```
consumer_incl_vat = market_incl_vat + supplier_markup_incl_vat + energy_tax_incl_vat
```
VAT is linear, so this equals `(market + markup + tax)_excl_vat × (1 + vat_rate)`. `vat_rate` is used only to show the excl.-VAT breakdown in demos and logs. If the integration's sensor fallback returns an excl.-VAT price, the adapter converts it once with `vat_rate`, and logs that. Fixed charges (standing charge, grid operator fees, tax credit) **do not affect the marginal decision**, so they are deliberately excluded. Document this in the README. The README also notes that the energy tax uses the **first consumption tier** rate, which applies to normal household usage.

**Negative prices:** these must be handled correctly. The consumer price can still be positive because of energy tax, or it can be negative.

**Demo 1 (30 s):** every 5 s, print and log:
- the market price
- each tariff component
- the consumer price per kWh
- the price timestamp and the hour it applies to
- the data source (cache or sensor) and the data age
- the call latency

It also prints the full price curve for today, listing the cheapest and most expensive hours. At the end it reports PASS or FAIL with reasons.

### 6.2 Step 2 – Gas price (€/m³ → €/kWh of useful heat)

**Source:** EnergyZero, via `energyzero.get_gas_prices` with `incl_vat: true` (cached the same way as electricity). The dynamic gas price is set per gas day, which changes at **06:00** local time, not at midnight. The cache lookup must use the gas-day rule: between 00:00 and 06:00, the previous calendar day's gas price applies.

**Formulas (`tariffs.py`, `heat_cost.py`):**
```
gas_consumer_per_m3   = market_incl_vat + supplier_markup_incl_vat + energy_tax_incl_vat
gas_per_kwh_fuel      = gas_consumer_per_m3 / calorific_value_kwh_per_m3
gas_per_kwh_heat      = gas_per_kwh_fuel / boiler_efficiency
elec_per_kwh_heat     = elec_consumer_per_kwh / electric_heating.efficiency
```

**Demo 2 (30 s):** every 5 s, log the gas price per m³ with each component, the price per kWh of fuel, the price per kWh of heat, which gas day the price applies to, and the data age. Also show the **break-even electricity price**, meaning the price at which electric heating becomes cheaper, and compare it with the current electricity price.

### 6.3 Step 3 – Shelly Plug M Gen3

**Primary method:** use the HA Shelly integration's entities: `switch.*`, and the `sensor.*` entities for power, energy, voltage, current, and device temperature, plus WiFi signal and uptime where they are exposed. When `switch_entity: auto`, discover the plug's entities by its **device ID (MAC)**, for example with a template rendered through AppDaemon that matches the Shelly device's MAC connection. Once found, cache the entity IDs and log them. If discovery fails, ask the user to set `switch_entity` explicitly. Renaming a device in HA must not break discovery.

**Optional extended diagnostics:** read-only direct calls to the Gen3 RPC API (`http://<ip>/rpc/Shelly.GetStatus` and `Shelly.GetDeviceInfo`), with the IP resolved and its identity verified as described in section 2a. These provide firmware version, `apower`, `aenergy`, voltage, current, frequency, temperature, WiFi RSSI, and uptime. **Never switch the plug via RPC.** Switching always goes through HA, so HA stays the single source of truth.

**`ShellyPlug` class (`devices/shelly_plug.py`):**
- `turn_on()` and `turn_off()`. Each call is followed by a **read-back verification** of the switch state, with up to 3 retries and a back-off delay. A mismatch is logged as an error.
- `read_status()` returns a dataclass with state, power in W, energy in kWh, voltage, current, temperature, RSSI, availability, the last-changed time, and `heater_active` (`state == on` and `power_w > active_power_threshold_w`).
- `is_available()`: an entity state of `unavailable` or `unknown` counts as unavailable.
- **Safety checks** run on every state change of the plug's current, power, and temperature sensors (`listen_state`), not only on the 5-minute tick, so a fault is caught within seconds. The plug is switched off and locked out until the next full hour when:
  - the **current exceeds `plug_max_current_a` continuously for at least `overcurrent_min_duration_s`** (a short inrush spike does not trip);
  - the device temperature exceeds `max_device_temp_c`;
  - the measured power exceeds the voltage-corrected heater rating by more than 15%: `power_w > heater_power_w × (voltage / 230)² × 1.15`. This detects a wrong or faulty appliance on the plug.

  Power above `plug_max_power_w` alone does **not** trip, because a 3000 W heater normally draws more than 3000 W at mains voltage above 230 V (section 2a); it is logged at INFO once per run.

  A trip is logged at WARNING level with the readings, and creates a `persistent_notification`. The lockout is in memory; an app restart clears it, and the next tick re-checks the readings. Low or zero power while ON is **not** a safety condition (see section 1).
- **Device-protection trips:** if the plug turns off while the app wants it ON, the app checks the plug's error flags (RPC `Switch.GetStatus` → `errors`, e.g. `overtemp`, `overpower`, `overcurrent`, or the matching HA entity if the Shelly integration exposes one). A device-protection trip is logged at WARNING, counted in the diagnostics, makes health DEGRADED, and locks out the plug until the next full hour. Without an error flag, the change is treated as manual and the next tick re-applies the decision.

**Demo 3 (30 s), per plug:** first show the identity check: the configured device ID, the discovered entities, the resolved IP and how it was resolved (mDNS, last known, or fallback), and the MAC/firmware returned by the plug. Then read the initial state, turn the plug on, then read all data every 3 s for about 12 s. Next, turn it off and read the data for another 3 s or so. Finally, **restore the original state**. Log every reading with a timestamp. Report PASS or FAIL for switching and read-back. Power detection is reported separately: PASS if power rises above `active_power_threshold_w` while ON, otherwise **SKIPPED – heater appears switched off by hand**. The start notification asks the user to switch the heater on beforehand if they want the power check.

### 6.4 Step 4 – House

**`House` class (`house.py`):**
- It holds a list of `HeatingDevice` objects (currently two `ShellyPlug`s) and an **empty `energy_sources` list**. Define the `EnergySource` protocol now, with `current_production_w()` and `available()`, so that solar can be added later without refactoring.
- Methods: `set_heating(on: bool, reason: str)` applies a decision to all available plugs. `status()` returns the aggregated total power, energy today, and per-device states (including `heater_active`). `health()` reports per-device availability.
- If one plug is unavailable, the other is still controlled, and the decision log records the failure.
- Design note for the future: the decision engine takes a `HouseSnapshot`, so solar surplus can later become a third cost input with an effective price close to €0 or the feed-in value.

**Demo 4 (30 s):** show the house status, switch all plugs on together, show the aggregated power every 5 s, then switch them off and restore their original states. Then simulate the failure of one plug, show how the house reports it, and restore the plugs.

### 6.5 Step 5 – Main logic

**Scheduling (`main_app.py`, alignment math in `scheduling.py`):**
- Ticks are aligned to the wall clock: **hh:00:00, hh:05:00, … hh:55:00** in Europe/Amsterdam time. Use AppDaemon `run_every` with an explicitly computed start time (the next multiple of `interval_minutes`). Because the Dutch UTC offset is a whole number of hours, 5-minute alignment in UTC equals alignment in local time, so the interval schedule stays aligned across daylight-saving transitions. Hour-change detection and gas-day logic use local time. After each tick, check for drift and log it.
- If `evaluate_on_startup` is true, the app evaluates once at startup and then follows the aligned schedule.
- Ticks must not overlap: if a tick is still running, skip the next one and log a warning.

**Enable/disable (`input_boolean.heating_optimizer_enabled`):**
- **Enabled:** the loop evaluates and switches on every tick. On the off→on transition, it evaluates immediately rather than waiting for the next tick.
- **On the on→off transition:** the app turns both plugs **off once** (with read-back verification), logs this, and from then on is **hands-off**: it does not touch the plugs, so the user can switch them by hand in HA. Ticks still run to keep prices and diagnostics up to date, and log `action: DISABLED`.
- **Startup while disabled:** hands-off; the app does not switch the plugs.

**Decision engine (`decision.py`, pure):**

The inputs are `elec_per_kwh_heat`, `gas_per_kwh_heat`, the current plug state, the timers, the margins, and the data availability. The output is a `Decision` containing `action` (ON, OFF, or HOLD), `reason_code`, a readable `reason`, and every input value. Whether the heaters themselves are active does **not** affect the decision.

The rules are evaluated in order:
1. Data missing → **failsafe OFF** (`reason_code: STALE_DATA`).
2. A safety lockout is active → OFF (`SAFETY_LOCKOUT`).
3. Heating is off and `elec + margin_on < gas`, and `min_off` has elapsed → **ON** (`ELEC_CHEAPER`).
4. Heating is on and `elec ≥ gas − margin_off`, and `min_on` has elapsed → **OFF** (`GAS_CHEAPER`).
5. A timer is blocking the change from rule 3 or 4 → HOLD (`MIN_OFF_TIMER` or `MIN_ON_TIMER`).
6. Otherwise → HOLD (`NO_CHANGE` or `IN_HYSTERESIS_BAND`).

Rules 1 and 2 ignore the min-on timer: safety and failsafe always win. "Heating is on" means the plugs are on according to HA at the start of the tick, not the app's memory, so manual changes and restarts are handled.

At an hour boundary (minute 00), the app forces a price lookup for the new hour (and a cache refresh if it is missing) and logs `HOUR_CHANGE` with the previous and new prices.

**Note for the README:** when the electric heaters warm a room, the gas thermostat fires less. This interaction is how the savings come about, but the thermostat setpoint and its location matter. Controlling the thermostat, and deciding whether a room needs heat, are out of scope; a person switches the heaters (section 1).

---

## 7. Demo framework (`demo_app.py`)

- The app listens for state changes of each `input_button.demo_*`. A button press changes the button's state to a timestamp.
- Only **one demo runs at a time**. Starting a demo **pauses the main loop**: a shared lock stops the main loop's switching, and its state is restored afterwards. A press during a running demo is ignored, with a notification.
- The demos switch the plugs for real (switching is what they test) and restore them afterwards.
- Each demo runs a **30 s loop**, implemented with AppDaemon's scheduler or `run_in` rather than blocking `sleep`. Each demo:
  - writes every step to the log and to `sensor.heating_optimizer_demo_output`, putting the last line in the state and the rolling history (last 50 lines) in attributes;
  - creates a `persistent_notification` at the start and at the end, with a PASS or FAIL summary;
  - stops immediately when `input_button.demo_stop` is pressed, then restores all plugs to their state before the demo.
- The main logic is **not** a demo. It starts the real application through `input_boolean.heating_optimizer_enabled`.

---

## 8. Logging

**Decision log**, at `<decision_log_dir>/decisions.jsonl` with a mirror in `decisions.csv`. One record is written **for every tick**, including HOLD and DISABLED ticks. Both files rotate (`rotate_mb`, `backups`).

```json
{
  "ts": "2026-10-27T14:05:00+01:00",
  "tick": "aligned",
  "hour_change": false,
  "action": "ON",
  "reason_code": "ELEC_CHEAPER",
  "reason": "Electric heat €0.118/kWh is €0.024 cheaper than gas heat €0.142/kWh (margin €0.020)",
  "elec": {"market": 0.012, "markup": 0.02, "tax": 0.0, "vat": 0.21, "consumer_kwh": 0.118, "per_kwh_heat": 0.118, "applies_to": "14:00-15:00", "resolution": "hour", "source": "cache"},
  "gas":  {"market_m3": 0.45, "consumer_m3": 1.31, "per_kwh_heat": 0.142, "gas_day": "2026-10-27"},
  "margins": {"on": 0.02, "off": 0.005},
  "timers": {"heating_on_since": null, "min_off_elapsed": true},
  "devices": [{"name": "shelly_1", "before": "off", "after": "on", "verified": true, "power_w": 1980, "heater_active": true}],
  "duration_ms": 412
}
```
(The values above are illustrative only.)

**Application log:** a rotating file `heating_optimizer.log` that also goes to the AppDaemon log. Its level is configurable.

**HA logbook:** each **state change** (ON or OFF, not HOLD) is also written through the `logbook.log` service, so it appears in the HA history.

**Notifications:** only HA `persistent_notification`s (demos, safety lockouts, startup self-test failure). No mobile push notifications.

---

## 9. Extended diagnostics (`diagnostics.py`)

The app publishes these to HA as sensors, with rich attributes:
- `sensor.heating_optimizer_status`: running, paused (demo), disabled, or failsafe; plus the time of the last tick, the time of the next tick, and the drift in ms.
- `sensor.heating_optimizer_elec_heat_price`, `sensor.heating_optimizer_gas_heat_price`, and `sensor.heating_optimizer_price_delta` (all €/kWh, with every component as an attribute).
- `sensor.heating_optimizer_last_decision`, containing the action, the reason, and the timestamp.
- `sensor.heating_optimizer_health`, which is OK, DEGRADED, or ERROR, with these checks as attributes:
  - time of the last cache refresh, and whether tomorrow's prices are loaded yet (warning after `tomorrow_expected_by`);
  - latency of the EnergyZero calls, with the last error message and an error counter;
  - per plug: availability, last-seen time, RSSI, temperature, voltage, current, firmware (RPC), `heater_active`, switch-verification failures, software lockouts, and device-protection trips;
  - tariff validity: DEGRADED once today is after `tariffs_valid_until` (reminder to update the energy tax and ANWB fees);
  - counts of ticks run, skipped, and failed since startup, plus uptime;
  - counts of switching actions today per plug, to detect flapping. A warning is raised above `max_switches_per_hour`.
- On DEBUG level: raw service responses, timing for every step, and full stack traces.
- At startup, a **self-test** logs the configuration with secrets masked, checks that every configured entity exists, and does a price fetch. The result is written to the health sensor.

---

## 10. Error handling and safety

- Every external call (HA service, entity read, RPC) has a timeout, catches exceptions, and is counted in the diagnostics.
- An unknown price leads to a failsafe OFF, because gas is the safe default heat source.
- An AppDaemon restart or HA restart triggers re-initialization and the self-test, followed by a decision on the next tick. The app does not assume the plugs' previous state; it reads it from HA.
- A switch-verification failure leads to 3 retries, then an ERROR log entry and DEGRADED health.
- The code contains no secrets. The optional Shelly RPC credentials go in AppDaemon's `secrets.yaml`. Deploy settings go in the git-ignored `scripts/deploy.env`.

---

## 11. Testing

- `pytest` unit tests for:
  - config validation, including each rejection rule in section 5 and the `tariffs_valid_until` check;
  - the tariff math, with known inputs and outputs (incl.-VAT sum, excl.-VAT breakdown), and negative prices;
  - the safety checks: overcurrent shorter vs. longer than `overcurrent_min_duration_s`, 3200 W at 238 V not tripping, the voltage-corrected overpower rule, and overtemperature;
  - the gas-to-heat conversion;
  - every branch of the decision engine, including the hysteresis band, the min-on and min-off timers, and safety/failsafe overriding the min-on timer;
  - the house when one device fails, and a plug ON with 0 W (heater off by hand) not counting as a failure;
  - the clock alignment, including startup at hh:03:27, hh:59:59, and the daylight-saving transitions (last Sunday of March and October);
  - the gas-day boundary at 06:00, the averaging of 15-minute prices into an hour, and an hour with missing quarters.
- Use fake adapters (`FakePriceProvider`, `FakePlug`, `FakeClock` in `tests/fakes.py`) so the tests need no HA or AppDaemon.

---

## 12. Build order

1. Scaffold `HeatingOptimizer/`, `pyproject.toml`, `config.py`, logging, the tests setup, and the `.dictionary.txt` additions. Check that pre-commit passes.
2. Write `tariffs.py` and `heat_cost.py` with their tests.
3. Electricity price adapter and cache, then **demo 1**. Write `scripts/deploy.sh` here, because demo 1 is the first thing that runs on the Pi.
4. Gas price adapter, then **demo 2**.
5. `ShellyPlug`, then **demo 3**.
6. `House`, then **demo 4**.
7. `DecisionEngine` and `scheduling.py` with their tests.
8. `MainApp`: scheduler, enable/disable handling, decision log, diagnostics sensors, logbook.
9. HA dashboard YAML, the recorder exclusions, `docs/ha_setup.md`, and the README (including the Raspberry Pi notes, the operating model, and the deploy steps).

**Acceptance criteria:**
- All demos report PASS on the real hardware. For demo 3's power check, the user switches the heaters on beforehand.
- The main loop runs for 2 hours with ticks within 2 s of each 5-minute mark, and the decision log shows a correct price lookup at every hour change.
- Switching `heating_optimizer_enabled` off turns both plugs off once, after which manual switching in HA is not overridden.
- All unit tests pass and pre-commit passes on `HeatingOptimizer/`.

---

## 13. Open items the user must supply

- The EnergyZero config entry and the exact entity IDs, which demo 1 helps discover.
- The SSH username (`HA_USER`) from the SSH add-on's config, and confirmation of the AppDaemon add-on slug (`a0d7b954_appdaemon`).

Resolved (values in section 5; all incl. 21% VAT):

| Item | Value | Source / validity |
|------|-------|-------------------|
| Supplier | ANWB Energie, dynamic | prices via EnergyZero |
| Electricity markup | €0.018/kWh | ANWB website, confirmed incl. VAT by user |
| Gas markup | €0.07680/m³ | ANWB, from 2026-09-01 |
| Energy tax electricity | €0.11085/kWh (€0.09161 excl.) | 2026, tier 1; changes 2027-01-01 |
| Energy tax gas | €0.72680/m³ (€0.60066 excl.) | 2026, tier 1; changes 2027-01-01 |
| Boiler efficiency | 0.90 | realistic HR boiler, upper calorific value |
| Heaters | 3000 W max, both plugs | at plug limit, see section 2a |
| Decision tuning | on 2 ct, off 0.5 ct, 15 min | wider band, fewer switches |
| Deploy target | `homeassistant.local:22` | SSH add-on |

**Claude Code:** the energy tax figures came from secondary sources (consumer sites citing the Belastingdienst). Ask the user to check them against the official Belastingdienst table before going live.

## 14. Future (out of scope, but keep the design ready)

- Solar panels as an `EnergySource`. Their surplus would make electric heat close to free.
- Planning ahead with tomorrow's prices, for example to pre-heat in the cheapest hours.
- Room temperature sensors and a target temperature per room.
- A heat pump as a `HeatingDevice`, with an efficiency set by its COP.
