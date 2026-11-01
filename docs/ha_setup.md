# Home Assistant setup

Click-paths are for Home Assistant OS 18.x. Do these steps once, in this order.

> Recent Home Assistant releases call add-ons **Apps** (**Settings → Apps**) and developer tools **Tools**. Older
> guides, and some folder and command names on the system, still say "add-on".

## 1. AppDaemon app

1. **Settings → Apps**, open the app store (button at the bottom right), search **AppDaemon**, click it,
   **Install**.
2. On the app's **Info** tab, enable **Start on boot** and **Watchdog**, then **Start**.
3. Open the **Log** tab and check that AppDaemon starts without errors.

The apps folder is `/addon_configs/a0d7b954_appdaemon/apps/`. Inside the AppDaemon app it is `/config/apps/`. The
slug (`a0d7b954_appdaemon`) appears in the browser URL when you open **Settings → Apps → AppDaemon**. If yours is
different, set `APPDAEMON_SLUG` in `scripts/deploy.env`.

## 2. Advanced SSH & Web Terminal app (for `scripts/deploy.sh`)

1. **Settings → Apps**, open the app store, search **Advanced SSH & Web Terminal**, **Install**.
2. **Configuration** tab:
   - `username`: pick one (this is `HA_USER` in `scripts/deploy.env`).
   - `authorized_keys`: paste your public key (`cat ~/.ssh/id_ed25519.pub`).
   - `password`: leave empty (key login only).
   - Under **Network**, keep port **22**.
3. Disable **Protection mode** on the **Info** tab, so the deploy script can restart AppDaemon with the `ha` CLI.
4. **Start** the app. Test from your laptop: `ssh <username>@homeassistant.local 'ls /addon_configs'`. The list
   should contain `a0d7b954_appdaemon`.

## 3. EnergyZero integration (electricity and gas prices)

ANWB Energie buys on the EnergyZero platform, so these are the market prices you are billed on.

1. **Settings → Devices & services → Add integration**, search **EnergyZero**, **Submit**.
2. After setup it shows a device **Energy market price** and a device **Gas market price**.
3. The config entry ID is needed for `electricity.energyzero_config_entry`. Demo 1 prints it. You can also open
   **Settings → Devices & services → EnergyZero**: the ID is the last part of the URL
   (`.../config/integrations/integration/energyzero#config_entry=<ID>`).

## 4. Shelly integration (two Shelly Plug M Gen3)

1. Make sure both plugs are on your WiFi (Shelly app or the plug's own web UI).
2. **Settings → Devices & services**. The plugs are usually auto-discovered: click **Configure** on each
   discovered **Shelly** card. Otherwise **Add integration → Shelly** and enter the plug's IP
   (`192.168.2.18`, `192.168.2.19`).
3. **Do not also add them through Matter.** That creates duplicate switch entities.
4. Open each device and enable the disabled-by-default diagnostic entities the app uses, if present:
   **Device temperature**, **Signal strength**, and the **Overheating / Overpowering / Overcurrent** binary
   sensors (device page → _x entities not shown_ → entity → ⚙ → **Enabled**).
5. Recommended, not required: in your router, set a DHCP reservation for MACs `90:70:69:54:3F:3C` and
   `90:70:69:5D:BA:7C`.

The app finds each plug's entities by MAC, so renaming devices or entities in HA does not break it.

## 5. Helpers

**Settings → Devices & services → Helpers → Create helper**:

| Type   | Name (gives the entity ID)               | Entity ID                                  |
| ------ | ---------------------------------------- | ------------------------------------------ |
| Toggle | `heating_optimizer_enabled`              | `input_boolean.heating_optimizer_enabled`  |
| Button | `demo_electricity_price`                 | `input_button.demo_electricity_price`      |
| Button | `demo_gas_price`                         | `input_button.demo_gas_price`              |
| Button | `demo_shelly_plug`                       | `input_button.demo_shelly_plug`            |
| Button | `demo_house`                             | `input_button.demo_house`                  |
| Button | `demo_stop`                              | `input_button.demo_stop`                   |
| Number | `heating_optimizer_margin_ct` (optional) | `input_number.heating_optimizer_margin_ct` |

Create each helper with the name from the table first, so HA generates exactly that entity ID. Rename the display
name afterwards if you like. For the number helper, set minimum 0, maximum 10, step 0.1, display mode _input field_,
unit `ct/kWh`. **Then set its value to 2.0.** While this helper exists it replaces the configured switch-on margin,
and a new number helper starts at 0.

Prefer YAML? Add `ha/helpers.yaml` to `configuration.yaml` instead.

## 6. Recorder exclusions

Edit `configuration.yaml` (File editor or Studio Code Server app) and merge `ha/recorder_exclude.yaml` into the
`recorder:` section. Then **Settings → Tools → YAML → Check configuration** and restart Home Assistant.

## 7. Dashboard

**Settings → Dashboards → Add dashboard → New dashboard from scratch**, name it _Heating optimizer_, open it,
**⋮ → Edit dashboard → ⋮ → Raw configuration editor**, paste `ha/dashboard_heating_optimizer.yaml`, **Save**.

## 8. Deploy and configure the app

1. On the laptop: `cp scripts/deploy.env.example scripts/deploy.env` and fill in `HA_USER`.
2. `scripts/deploy.sh --dry-run`, then `scripts/deploy.sh`.
3. Fill in the EnergyZero config entry ID in the app's config **on the Pi**. The deploy copied
   `heating_optimizer.yaml` to `/addon_configs/a0d7b954_appdaemon/apps/`, and it still contains
   `energyzero_config_entry: TODO(user)`. Until that is replaced, the main app refuses to start and creates a
   notification saying so. Later deploys never overwrite this file, so your edit stays.
   - Find the ID: press **Demo 1** (it prints it), or open **Settings → Devices & services → EnergyZero**, where
     the ID is the last part of the browser URL (`…#config_entry=<ID>`).
   - Replace the placeholder, e.g. over SSH:
     `ssh root@homeassistant.local "sed -i 's/energyzero_config_entry: TODO(user).*/energyzero_config_entry: <ID>/' /addon_configs/a0d7b954_appdaemon/apps/heating_optimizer.yaml"`
   - AppDaemon reloads the app automatically when the file changes. Check **Settings → Apps → AppDaemon → Log**
     for `Self-test passed`.
4. Leave `dry_run: true` and run the four demos (see the README).
