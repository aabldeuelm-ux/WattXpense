# Hardware guide: ESP32, current sensor and relay

This document describes the hardware boundary WattXpense implements. It is a design and
integration guide, **not** a tested installation. No ESP32, current sensor or relay has been
connected to this project, and nothing here has been checked against a live mains circuit.

> **Mains electricity is lethal.** A whole-home current sensor and a relay both involve
> exposed mains wiring. Work only with a qualified electrician, use a properly rated
> enclosure and isolation, and never touch a circuit while it is energised. The example
> firmware is not a certified product and must not be used as a safety device.

## What this path can and cannot measure

A current sensor measures current. Real power is:

```
real power (W) = voltage (V) × current (A) × power factor
```

A current-only sample therefore cannot produce real power. WattXpense refuses it rather than
inventing watts. If the firmware sends voltage and current, the backend calculates apparent
power and stores it with an estimate flag, assuming a power factor of 1 unless the firmware
reports one. Energy calculated from power × the sampling interval is also stored as an
estimate.

That is useful for trends and cost estimates. It is not a revenue-grade meter, and it is not
a substitute for a calibrated energy meter.

### About "ACS71"

"ACS71" is not a recognised part number. It appears to be a user label for an ACS712-family
Hall-effect current sensor, but that is an assumption, not a verified fact. Use the exact
model printed on your board and enter it in the device's `sensor_model` field. The example
firmware uses the common ACS712 calibration shape, which you must adjust for your specific
variant (5 A, 20 A or 30 A) and for your ADC.

## Suggested parts

| Part | Notes |
| --- | --- |
| ESP32 development board | Any common ESP32 with Wi-Fi and an ADC pin. The example uses GPIO 34 for current. |
| ACS712-family current sensor | Match the variant to the circuit's expected current. Do not exceed its rating. |
| Voltage sensor (optional) | A properly isolated voltage-sense module or a qualified divider. Without one, the firmware must clearly mark voltage as assumed. |
| Relay module | Opto-isolated, rated for the load, with a flyback diode and adequate creepage. Many cheap modules are not safe for mains. |
| 5 V supply | A certified supply, not a bare phone charger dangling inside a mains enclosure. |

## Wiring overview

The exact wiring depends on the sensor variant and the relay module. At a high level:

1. The current sensor is placed in series with the live conductor. Its output goes to an ESP32
   ADC input. The ESP32 and the sensor share a ground.
2. An optional voltage sensor connects across live and neutral through a properly rated,
   isolated module. Its output goes to a second ADC input.
3. The relay module's control input goes to a GPIO. The relay contacts switch the load. Keep
   mains and low-voltage wiring physically separated and strain-relieved.
4. Power the ESP32 from a certified 5 V supply. Do not power it from the mains side.

Pins and calibration are **not hardcoded in WattXpense**. They are stored per device in
`gpio_config` and `calibration`, and the example firmware reads its own constants from the top
of the sketch. Change them to match your board.

## Register the device and get a token

1. Sign in to the dashboard.
2. Open **Devices → Register a device**.
3. Use a stable device id such as `esp32-main-01`, choose source `esp32`, and enter the exact
   sensor model, sampling interval, calibration JSON and GPIO JSON.
4. Copy the `wxdev_…` token immediately. It is shown once and stored only as a hash.

Example device payload:

```json
{
  "id": "esp32-main-01",
  "name": "Main supply",
  "source": "esp32",
  "sensor_model": "ACS712-30A",
  "sampling_interval_seconds": 60,
  "calibration": {
    "adc_midpoint": 2048,
    "mv_per_amp": 66.0,
    "adc_reference_mv": 3300
  },
  "gpio_config": {
    "current_adc": 34,
    "voltage_adc": 35,
    "relay": 26
  },
  "relay_enabled": false
}
```

A device id is globally unique. If it is already owned by another account, registration
returns `409`; the id is not taken over.

## Telemetry contract

The device authenticates with its own bearer token, not a user session:

```http
POST /api/device/telemetry
Authorization: Bearer wxdev_…
Content-Type: application/json
```

```json
{
  "readings": [
    {
      "ts": "2026-09-26T14:00:00Z",
      "voltage": 231.4,
      "current": 3.12,
      "power_factor": 0.98,
      "sensor_status": "ok",
      "measurement_status": "ok",
      "simulated": false
    }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `ts` | yes | ISO 8601 timestamp with an offset. Naive timestamps are rejected. |
| `voltage` | one measurement | RMS voltage in volts. |
| `current` | one measurement | RMS current in amperes. |
| `power_w` | one measurement | Real power in watts, if the device can measure or calculate it. |
| `energy_wh` | one measurement | Energy for this one interval, not a cumulative meter reading. |
| `power_factor` | no | 0–1. Used only when power is derived from voltage and current. |
| `device_id` | no | If present it must match the token's device, otherwise the sample is rejected. |
| `sensor_status` | no | Free text such as `ok`, `sensor_fault`, `warming_up`. Non-ok values are flagged. |
| `measurement_status` | no | Free text such as `ok`, `suspect`, `voltage_assumed_nominal`. Non-ok values are flagged. |
| `simulated` | no | `true` keeps a sample labelled simulated even over the hardware endpoint. |

At least one of `voltage`, `current`, `power_w` or `energy_wh` is required. A batch can contain
many readings; one rejected sample does not discard the rest.

The response reports `received`, `inserted`, `updated`, `duplicates`, `rejected` and a list of
`errors` with the index and reason.

### What the backend derives

| Input | Stored result | Flag |
| --- | --- | --- |
| `voltage` + `current` | `power_w = voltage × current × power_factor`, with `power_factor` defaulting to 1 | `real power estimated ... power factor assumed to be 1` |
| `power_w` only | `energy_wh = power_w × sampling interval` | `energy estimated from power and the configured sampling interval` |
| `current` only | rejected | reason explains that a current-only sample cannot provide real power |

Send `energy_wh` directly if your device integrates energy itself. Send `power_w` if it
measures real power. Send `voltage` and `current` only if you accept the estimate flag.

## Relay protocol

Relay control is off by default:

- `WATTXPENSE_RELAY_CONTROL_ENABLED=false` disables it for the whole installation.
- Even when that is true, a device has `relay_enabled=false` until an account owner turns it
  on deliberately.
- The AI layer never sends relay commands. There is no automatic switching path.

The user command and the physical state are separate:

1. The dashboard records an explicit command: `pending_on` or `pending_off`.
2. Firmware polls `GET /api/device/relay` with its bearer token.
3. If the state is `pending_on` or `pending_off`, firmware drives the relay pin and then calls
   `POST /api/device/relay/ack` with the state it actually observed.
4. The dashboard shows `on` or `off` only after that acknowledgement. A command is never
   presented as proof that the relay moved.

Acknowledgement payload:

```json
{ "state": "on", "note": "relay closed" }
```

Allowed states are `on`, `off`, `unknown` and `error`. Use `error` if the relay did not
respond as expected, and `unknown` if the firmware cannot determine the state.

Firmware must fail safe: if the network is unavailable or the acknowledgement fails, the
relay should return to a safe state for the load. Do not retry a switching command blindly.

## Example firmware

[`firmware/esp32_wattxpense/esp32_wattxpense.ino`](../firmware/esp32_wattxpense/esp32_wattxpense.ino)
implements this contract with:

- Wi-Fi connection,
- ACS712 current sampling with configurable ADC midpoint and mV/A,
- optional voltage sensing, or a clearly labelled nominal-voltage assumption,
- batched telemetry over HTTP,
- relay polling and acknowledgement.

It is an example, not a tested product. It has no TLS, no provisioning portal, no watchdog
strategy for every failure mode and no certification. Read it before flashing it, and change
the configuration block at the top.

## Calibration

1. With the load off, read the ADC. That is the midpoint. For a 12-bit ADC it is often around
   2048, but it varies with the board and supply.
2. Clamp a known resistive load (for example a known lamp or heater) and measure the current
   with a trusted clamp meter.
3. Adjust `mv_per_amp` until the firmware's current reading matches the clamp meter.
4. If you have a voltage sensor, calibrate it against a trusted multimeter.
5. Record the final values in the device's `calibration` JSON so the installation is
   reproducible.

Do not calibrate against a value you have not measured. A calibration copied from a datasheet
is a starting point, not a verified calibration.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `401` from telemetry | Missing, rotated or wrong `wxdev_…` token. Rotate and reflash. |
| `device_id does not match` | Payload names a different device than the token. Omit `device_id` or fix it. |
| `current-only sample cannot provide real power` | Send voltage, power or interval energy as well. |
| Readings appear as simulated | Firmware sent `simulated: true`, or the device source is a simulator. |
| Relay command stays pending | Firmware has not polled or acknowledged. Check the token and the relay endpoint. |
| Relay command is refused | Install-wide relay control is off, or the device's `relay_enabled` is false. |

## What is not implemented

- No over-the-air firmware update.
- No TLS pinning or secure element. Use HTTPS and a trusted certificate if the device talks
  over an untrusted network.
- No local buffering for long outages beyond a small in-memory queue.
- No physical hardware validation of any kind.
