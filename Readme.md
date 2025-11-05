# EVSEMaster Charger — Indigo Plugin

Control and monitor EVSE chargers that use the EVSEMaster app/protocol directly from Indigo. This plugin talks to the charger over your local network and exposes rich real‑time status, per‑phase telemetry, and control actions.

- Start/Stop charging (mapped to Indigo relay On/Off)
- Set the output current limit (Amps)
- Live device status and charging session details
- Human‑readable state names and summaries for control pages and triggers
- Conservative polling that complements the charger’s push events

This plugin uses the evsemaster Python library by RafaelSchridi (based on johnwoo‑nl/emproto).

---

## Supported EVSE

Any charger that uses the EVSEMaster app/protocol should work. Confirmed models reported by users include variants branded as BS20/Telestar, etc. If yours speaks EVSEMaster, it will likely work.

---

## How it works

- Transport: UDP socket listening on 28376; the charger’s reply/discovered port is learned at runtime (initially 7248).
- Authentication: Login with your EVSE’s 6‑digit PIN is required for control operations. The charger will still push unsolicited status events even if login fails, so the plugin will remain in a safe passive “listen” mode until it can authenticate.
- Polling: A single periodic status refresh runs every 60 seconds. Most updates arrive via push events.
- Fault‑tolerant: If login briefly fails, the plugin backs off and will retry opportunistically without tearing down the transport or spamming the log.

---

## Requirements

- Indigo 2024.2 or later (Server API 3.6+)
- Python 3 (bundled with Indigo)

---

## Installation

1. Double‑click the bundle to install/enable it in Indigo.
2. In Indigo, create a new device:
   - Type: EVSE Charger (from this plugin)
   - Enter:
     - IP/Hostname of the charger
     - 6‑digit PIN (the EVSE’s login PIN)

---

Once created:
- The plugin will open a UDP listener and attempt a login.
- Even if login fails, you’ll still see status updates if the EVSE pushes them.
- A modest periodic status request (60s) runs when authenticated.

Control:
- Relay On → Start Charging
- Relay Off → Stop Charging
- Actions (Action Groups): Start Charging, Stop Charging, Set Output Amperage

---

## Actions

- Start Charging
- Stop Charging
- Set Output Amperage (6–32 A)
  - Enforced against device capabilities.

Relay mapping:
- Turn On = Start Charging
- Turn Off = Stop Charging
- Toggle = Start/Stop according to current charging state

---

## Device states

Human‑readable labels are provided for Indigo triggers and control pages.

Connection and summary:
- connected (bool) — transport/socket established
- auth_ok (bool) — authenticated (logged in)
- status_text (string) — concise status
- state_summary (string) — “Current State | Plug: …”
- last_update (string)
- last_error (string, if any)
- vehicle_connected (bool)
- charging (bool)
- onOffState (bool) — mirrors charging

Device Info (EvseDeviceInfo):
- di_type (int)
- di_brand (string)
- di_model (string)
- di_hardware_version (string)
- di_max_power_w (int)
- di_max_power_kw (number)
- di_max_amps (int)
- di_serial_number (string)
- di_nickname (string)
- di_configured_max_amps (int)

Status (EvseStatus):
- st_line_id (int)
- st_inner_temp_c (number)
- st_outer_temp_c (number)
- st_emergency_stop (bool)
- safety_status (string) — “OK” or “EMERGENCY STOP ACTIVE”
- st_plug_state (int), st_plug_state_name (string)
- st_output_state (int), st_output_state_name (string)
- st_current_state (int), st_current_state_name (string)
- st_errors (int)
- st_l1_voltage_v (number), st_l1_amps_a (number)
- st_l2_voltage_v (number), st_l2_amps_a (number)
- st_l3_voltage_v (number), st_l3_amps_a (number)
- phase_summary (string) — “L1 238.0V 0.00A | …”
- st_current_power_w (int), st_current_power_kw (number)
- st_total_kwh (number)

Charging Session (ChargingStatus):
- ch_line_id (int)
- ch_current_state (int), ch_current_state_name (string)
- ch_charge_id (string)
- ch_start_type (int), ch_start_type_name (string)
- ch_charge_type (int), ch_charge_type_name (string)
- ch_max_duration_min (int)
- ch_max_energy_kwh (number)
- ch_charge_param3 (number)
- ch_reservation_datetime (string), ch_reservation_time (string)
- ch_user_id (string)
- ch_max_electricity_a (int)
- ch_set_datetime (string)
- ch_duration_seconds (int), ch_session_duration (string)
- ch_start_kwh_counter (number)
- ch_current_kwh_counter (number)
- ch_charge_kwh (number)
- ch_charge_price (number)
- ch_fee_type (int), ch_fee_type_name (string)
- ch_charge_fee (number)
- ch_charge_limits (string) — e.g., “16A, 65535 min, 20.00 kWh”

Notes:
- Enum names (plug/current state, types) are provided in “_name” states for easy UI/triggers.
- Power also shown in kW for control pages.

---

## Logging

Enable Debugging in Indigo to see:
- Startup lifecycle lines (transport, login attempts)
- “Event received: …” with full payloads for diagnostics
- Periodic: requesting status
- Passive mode messages if login fails (the plugin will listen for events and retry later)

If you see repeated “Login failed” but events keep arriving:
- The PIN may be wrong, or the EVSE may temporarily reject logins.
- The plugin will stay in passive listen mode and retry logins conservatively (with backoff), avoiding log spam.

---

## Troubleshooting

- No states updating:
  - Check the charger IP/hostname and network/firewall rules (UDP).
  - Ensure the Indigo Mac and the EVSE are on the same LAN.
- Can’t control (start/stop/amps) but status updates appear:
  - Verify the PIN (6 digits).
  - Try closing the vendor mobile app; some units limit concurrent sessions.
- Set Output Amperage fails:
  - Ensure your value is between 6 and 32 A and does not exceed the device’s maximum.

If problems persist, enable debug logging and capture the “Event received:” lines and any “Poll error:” messages.

---

## Security

- The PIN is stored in the device configuration. Treat it as sensitive.
- Communication is unencrypted UDP on your LAN.

---

## Credits

- evsemaster Python library by RafaelSchridi (based on the original TypeScript by johnwoo‑nl).
- Indigo Platform by Indigo Domotics.

---

## License

This plugin is provided “as is” without warranty. Refer to the evsemaster library’s repository for its license terms.
