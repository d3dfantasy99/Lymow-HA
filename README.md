# 🌿 Lymow Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/d3dfantasy99/Lymow-HA.svg)](https://github.com/d3dfantasy99/Lymow-HA/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Discord](https://img.shields.io/discord/rPyv8mcB?label=Discord&logo=discord)](https://discord.gg/8kmYsP6ZRv)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/d3dfantasy99)

Unofficial Home Assistant integration for the **Lymow robot lawn mower**.  
Control your robot, monitor its status, view zones and map — all from Home Assistant.

> ⚠️ **This integration is not affiliated with or endorsed by Lymow.**  
> It was built by reverse engineering the official Lymow Android app.

---

## Features

- 🤖 **Lawn Mower entity** — start, pause, dock via the standard HA lawn mower card
- 🔘 **Command buttons** — individual Start, Pause, Dock, Cancel Task and Dock & Cancel buttons for use anywhere in Lovelace
- 🔋 **Sensors** — battery, work status, blade height, mow mode, RTK GPS, WiFi/4G signal, firmware version, session area, session progress, mow duration
- 🟢 **Binary sensors** — online, charging, mowing, error, lifted, rain delay, WiFi/4G connected
- 🗺️ **Map camera** — diagnostic PNG rendered from S3 backup map with zone polygons, robot position, dock location and mow path overlay
- 📡 **Live camera** — RTSP stream from the robot's onboard camera, transcoded to HLS by HA's built-in `stream` component
- 🌍 **GeoJSON sensor** — full zone map as a GeoJSON FeatureCollection (WGS84) for use with custom Lovelace map cards or external apps
- 🎛️ **Mow mode selector** — change cutting pattern (Zigzag, Chess Board, Perimeter Only, Adaptive Zigzag)
- 📦 **Firmware update notifications** — HA Updates panel shows available OTA versions with release notes
- 📋 **Session history events** — `event.lymow_session_completed` fires on every completed mow with area, duration, battery and zones
- 🔄 **Multi-region** — Europe, Asia Pacific (Sydney & Hong Kong), US East
- 🔁 **Token auto-refresh** — stays logged in, no manual intervention needed

---

## Requirements

- Home Assistant **2024.1** or newer
- A Lymow account created with **email and password** or Google login
- Your robot must be paired to that account via the official Lymow app

> ⚠️ **Apple login is not supported.**  
> Those accounts use OAuth2 with a mobile deep link (`myapp://callback`) that cannot be replicated in a headless environment.  

---

## Installation

### Via HACS (recommended)

1. Make sure [HACS](https://hacs.xyz) is installed in your Home Assistant instance.
2. Click the button below to add this repository to HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=d3dfantasy99&repository=Lymow-HA&category=integration)

Or manually:
- Go to **HACS → Integrations → ⋮ → Custom repositories**
- Add `https://github.com/d3dfantasy99/Lymow-HA` as an **Integration**
- Search for **Lymow** and click **Download**

3. Restart Home Assistant.

### Manual installation

1. Download the [latest release](https://github.com/d3dfantasy99/Lymow-HA/releases/latest).
2. Copy the `custom_components/lymow` folder into your HA `config/custom_components/` directory.
3. Restart Home Assistant.

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Lymow**
3. Choose your login method and region:

| Region | Use if you are in |
|--------|-------------------|
| Europe (Ireland) | Europe |
| Asia Pacific (Sydney) | Australia, Oceania |
| Asia Pacific (Hong Kong) | Asia |
| US East (Ohio) | Americas |

| Login method | Used for | Steps |
|--------------|--------| ----- |
| Email & Password | Lymow account used to log into official Lymow app | <ol><li>Enter email address and password used to login to the Lymow app</li></ol> |
| Google Account (OAuth) | Google login used in official Lymow app | <ol><li>Copy the provided URL and open it in a new browser tab</li> <li>Follow the steps on that page to sign in and get the authorization code, then paste it in the box</li></ol> |

4. If multiple robots are found, select which one to add.
5. Done — entities will appear under the Lymow device.

> You can add the integration multiple times to manage multiple robots.

---

## Entities

### Controls

| Entity | Type | Description |
|--------|------|-------------|
| Lymow Robot | `lawn_mower` | Main control entity (Start / Pause / Dock) |
| Start Mowing | `button` | Start or resume mowing (state-aware) |
| Pause | `button` | Pause mowing or docking |
| Dock | `button` | Return to dock, keep task progress |
| Cancel Task | `button` | Stop in place and reset to waiting |
| Dock & Cancel | `button` | Return to dock and abandon current task |
| Mow Mode | `select` | Cutting pattern selector |
| Backup Map | `select` | Choose which S3 backup map to load |
| Blade Height | `number` | Current cutting height in mm (read-only, from zone config) |

### Sensors

| Entity | Type | Description |
|--------|------|-------------|
| Status | `sensor` | Work status (Mowing, Docked, Charging…) |
| Battery | `sensor` | Battery level % |
| RTK GPS | `sensor` | GPS fix quality (Not Ready / Float / Fixed) |
| Session Area | `sensor` | Area mowed in current session (m²) |
| Session Progress | `sensor` | Mowing progress % |
| Mow Mode | `sensor` | Active cutting pattern |
| Blade Height | `sensor` | Cutting height in mm |
| Firmware | `sensor` | Current firmware version |
| IP Address | `sensor` | Robot LAN IP (used for RTSP camera) |
| Camera URL | `sensor` | Full RTSP URL for use with external tools |
| Map GeoJSON | `sensor` | Zone map as GeoJSON FeatureCollection |

Additional sensors (disabled by default, enable in entity settings):

| Entity | Description |
|--------|-------------|
| Session Duration | Mowing time in current session |
| Session Remaining | Estimated remaining time |
| Map Total Area | Total mapped area (m²) |
| Zone Count | Number of zones in the map |
| RTK Precision | GPS position accuracy (m) |
| RTK Satellites | Number of satellites tracked |
| WiFi Signal | WiFi RSSI (dBm) |
| 4G Signal | LTE RSSI (dBm) |
| Network Type | Active network (WiFi / LTE) |
| WiFi Network | Connected WiFi SSID |
| MCU Version | MCU firmware string |
| Serial Number | Robot serial number |
| Total Clean Time | Cumulative mowing time |
| Total Clean Area | Cumulative mowed area |

### Binary sensors

| Entity | Type | Default | Description |
|--------|------|---------|-------------|
| Online | `binary_sensor` | ✅ | Robot connectivity |
| Charging | `binary_sensor` | ✅ | Whether robot is charging |
| Mowing | `binary_sensor` | ✅ | Whether robot is actively mowing |
| Error | `binary_sensor` | ✅ | Whether an error is active |
| Lifted | `binary_sensor` | ✅ | Lift/tamper detection |
| WiFi Connected | `binary_sensor` | — | WiFi active |
| 4G Connected | `binary_sensor` | — | LTE active |
| Rain Delay | `binary_sensor` | — | Rain delay active |
| Theft Detection | `binary_sensor` | — | Theft detection enabled |
| Theft Lock | `binary_sensor` | — | Device lock active |

### Cameras

| Entity | Type | Description |
|--------|------|-------------|
| Map | `camera` | Diagnostic PNG — zones, robot, dock, mow path |
| Live Camera | `camera` | RTSP → HLS live stream (requires LAN access) |

### Other

| Entity | Type | Description |
|--------|------|-------------|
| Last Session | `event` | Fires `lymow_session_completed` on mow completion |
| Firmware | `update` | OTA update notification with release notes |

---

## Services

### `lymow.start_zones`
Start mowing one or more specific zones by hashId or name.

```yaml
service: lymow.start_zones
data:
  zones:
    - "vfW8PgjE"       # hashId
    - "Front Lawn"     # or zone name
```

### `lymow.dock_cancel_task`
Return to dock **and cancel** the current task (no recharge-resume).

```yaml
service: lymow.dock_cancel_task
```

### `lymow.cancel_task`
Stop the robot in place and reset to waiting state (equivalent to "Cancel task" in the app).

```yaml
service: lymow.cancel_task
```

> For integrations with multiple robots, add `device_id: <device_id>` to target a specific one.

---

## Coverage & mow history

Per-zone coverage and last-mowed times **persist across restarts and new mows** — they're
saved to the integration's config entry, not just held in memory. Each zone keeps its mowed
footprint on the map, tinted by **mow-age** (brighter = mowed more recently, fading as it
ages past your **Mow Interval**), and the **Overdue Zones** / **Zone Age** sensors track how
long it's been since each zone was last cut. A zone you mowed last week still shows its
coverage today, and starting a fresh task only clears the zone(s) actually being mowed — so a
partial mow never wipes the rest of the map.

---

## Session history automations

The `event.lymow_<<mowername>>_last_session` entity fires whenever a new completed session is detected. Use it to send a notification when mowing finishes:

```yaml
automation:
  trigger:
    platform: state
    entity_id: event.lymow_<<mowername>>_last_session
  action:
    service: notify.mobile_app
    data:
      title: "Mowing complete"
      message: >
        Mowed {{ state_attr('event.lymow_<<mowername>>_last_session', 'area_m2') }} m²
        in {{ (state_attr('event.lymow_<<mowername>>_last_session', 'duration_s') / 60) | round }} min.
        Used {{ state_attr('event.lymow_<<mowername>>_last_session', 'used_battery') }}% battery.
```

> Replace `<<mowername>>` with your own enitity mower name.

---

## Map card (GeoJSON)

The `sensor.lymow_map_geojson` attribute `geojson` contains a standard GeoJSON FeatureCollection with zone polygons, dock position and robot position (WGS84). Use it with a custom Lovelace map card:

```yaml
type: custom:map-card
fit_to_markers: true
entities:
  - entity: sensor.lymow_map_geojson
    geojson:
      attribute: geojson
      filter:
        property: type
        value: zone
      color: "#00c853"
      fill_opacity: 0.25
  - entity: sensor.lymow_map_geojson
    geojson:
      attribute: geojson
      filter:
        property: type
        value: mow_path
      color: "#ff6f00"
      fill_opacity: 0
```

---

## Troubleshooting

### Enable debug logging

Add the following to your `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: warning
  logs:
    custom_components.lymow: debug
```

Logs will appear in **Settings → System → Logs**.

### Common issues

**Integration not found after installation**  
→ Make sure you restarted Home Assistant after copying the files.

**Login fails**  
→ Confirm you are using an account created with **email and password**, not Google or Apple.  
→ Try logging in with the same credentials in the official Lymow app to verify they are correct.

**All sensors unavailable**  
→ The robot may be offline or out of WiFi/4G range. Check the **Online** binary sensor.  
→ Enable debug logging and look for connection errors.

**Map is empty**  
→ The robot needs to have completed at least one mapping session. The map is loaded from the S3 backup map and may take one polling cycle to appear after restart.

**Live camera not working**  
→ The robot's IP must be reachable from the HA host (same LAN or VPN). Check `sensor.lymow_camera_url` for the RTSP address. A DHCP reservation for the robot is recommended.

**Blade height shows Unknown**  
→ The firmware does not expose a global blade height. The value is read from the first zone's configuration and becomes available after the map is loaded.

---

## Support

Join the community Discord server for help, feedback and discussion:

[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/8kmYsP6ZRv)

If you find this integration useful, you can support its development:

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/d3dfantasy99)

To report a bug or request a feature, please [open an issue](https://github.com/d3dfantasy99/Lymow-HA/issues) on GitHub.

---

## Disclaimer

This integration communicates with Lymow's AWS infrastructure (Cognito, API Gateway, IoT MQTT, S3) using credentials obtained by reverse engineering the official Android app. All commands are sent over MQTT — no IoT shadow writes are used. Use at your own risk. The API may change at any time without notice.
