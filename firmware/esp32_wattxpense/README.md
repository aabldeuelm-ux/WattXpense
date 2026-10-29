# ESP32 example firmware

`esp32_wattxpense.ino` is an example implementation of the telemetry and relay contract in
[`../docs/HARDWARE.md`](../docs/HARDWARE.md). It has **not** been flashed or tested on physical
hardware.

## Before flashing

1. Read `docs/HARDWARE.md`, including the mains-safety warning.
2. Install **ArduinoJson** through the Arduino Library Manager.
3. Change the configuration block at the top of the sketch: Wi-Fi credentials, API origin,
   `DEVICE_TOKEN`, `DEVICE_ID`, GPIO pins, ADC midpoint and mV/A calibration.
4. Register the device in the dashboard first and copy its `wxdev_…` token. The token is shown
   once.
5. Set `RELAY_HARDWARE_PRESENT` to `true` only if a relay is wired and the installation has
   been checked. Relay control also has to be enabled for the installation and for the device.

## What the sketch sends

One sample per interval:

```json
{
  "readings": [
    {
      "ts": "2026-09-26T14:00:00Z",
      "voltage": 231.4,
      "current": 3.12,
      "power_factor": 1.0,
      "sensor_status": "ok",
      "measurement_status": "ok",
      "simulated": false
    }
  ]
}
```

If no voltage sensor is fitted, the sketch sends the nominal voltage and sets
`measurement_status` to say the voltage was assumed. The backend then stores the derived power
with an estimate flag. A current-only sample would be refused.

## Limitations

- Plain HTTP only. Add `WiFiClientSecure` and a trusted certificate for HTTPS.
- No OTA update, provisioning portal, persistent offline buffer or secure element.
- The relay acknowledgement reports the state the firmware applied, not a measured contact
  state. Add feedback sensing if you need to verify the physical relay.
- Calibration values are examples. Measure your own sensor and update them.
