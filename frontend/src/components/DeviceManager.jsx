import { useState } from 'react'

import api from '../api'
import { CopyButton, Empty, ErrorBox, Field, Loading, Panel, Tag, num, relativeTime } from './ui'

const SOURCES = [
  { value: 'esp32', label: 'ESP32 / hardware meter' },
  { value: 'simulator', label: 'Simulator' },
  { value: 'csv', label: 'CSV import' },
  { value: 'manual', label: 'Manual entry' },
]

function parseJsonField(value, name) {
  const text = (value || '').trim()
  if (!text) return null
  try {
    const parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error(`${name} must be a JSON object`)
    }
    return parsed
  } catch (error) {
    throw new Error(`${name}: ${error.message}`)
  }
}

function relayTag(state) {
  if (state === 'on') return <Tag kind="good">on (acknowledged)</Tag>
  if (state === 'off') return <Tag>off (acknowledged)</Tag>
  if (state === 'error') return <Tag kind="bad">device reported an error</Tag>
  if (String(state).startsWith('pending')) return <Tag kind="estimate">pending</Tag>
  return <Tag>unknown</Tag>
}

function RelayControl({ device, controlAvailable, onChanged }) {
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  if (!controlAvailable) {
    return (
      <p className="muted small">
        Relay control is disabled for this installation by <code>WATTXPENSE_RELAY_CONTROL_ENABLED</code>.
      </p>
    )
  }
  if (!device.relay_enabled) {
    return (
      <p className="muted small">
        Relay control is off for this device. It stays off until you deliberately enable it.
        WattXpense never switches a relay automatically.
      </p>
    )
  }

  async function send(state) {
    if (!armed) return
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api.commandRelay(device.id, state, true)
      setMessage(result.message || 'Command recorded.')
      setArmed(false)
      onChanged?.()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relay-box">
      <div className="relay-state">
        <span className="muted small">Relay state</span> {relayTag(device.relay_state)}
      </div>
      <label className="checkline">
        <input type="checkbox" checked={armed} onChange={(event) => setArmed(event.target.checked)} />
        I understand this sends a real switching command if hardware is connected.
      </label>
      <div className="button-row">
        <button type="button" className="action" disabled={!armed || busy} onClick={() => send('on')}>
          Request ON
        </button>
        <button type="button" className="action" disabled={!armed || busy} onClick={() => send('off')}>
          Request OFF
        </button>
      </div>
      {message ? <p className="muted small">{message}</p> : null}
      <ErrorBox error={error} />
    </div>
  )
}

function DeviceRow({ device, controlAvailable, onChanged, onToken }) {
  const [rotating, setRotating] = useState(false)
  const [error, setError] = useState(null)

  async function rotate() {
    if (
      !window.confirm(
        'Rotate this device token? The device will stop working until you install the new token.',
      )
    ) {
      return
    }
    setRotating(true)
    setError(null)
    try {
      const result = await api.rotateDeviceToken(device.id)
      onToken(result.device_token)
      onChanged?.()
    } catch (err) {
      setError(err)
    } finally {
      setRotating(false)
    }
  }

  return (
    <article className="device-card">
      <div className="device-head">
        <div>
          <h3>
            {device.name || device.id}
            {device.is_simulated ? <Tag kind="simulated">simulated</Tag> : null}
            {device.is_primary ? <Tag kind="good">primary</Tag> : null}
          </h3>
          <div className="muted small">
            <code>{device.id}</code>
            {device.location ? ` · ${device.location}` : ''}
          </div>
        </div>
        <div className="device-meta">
          <span>Last reading {relativeTime(device.last_seen)}</span>
          <span>Source {device.source || 'unknown'}</span>
        </div>
      </div>

      <dl className="device-facts">
        <div>
          <dt>Sensor model</dt>
          <dd>{device.sensor_model || 'not configured'}</dd>
        </div>
        <div>
          <dt>Sampling interval</dt>
          <dd>{device.sampling_interval_seconds ? `${device.sampling_interval_seconds} s` : 'not set'}</dd>
        </div>
        <div>
          <dt>Simulated readings</dt>
          <dd>{num((device.simulated_reading_share || 0) * 100, 0)}%</dd>
        </div>
        <div>
          <dt>Device token</dt>
          <dd>{device.has_device_token ? `issued (${device.device_token_prefix}…)` : 'not issued'}</dd>
        </div>
      </dl>

      {Object.keys(device.calibration || {}).length || Object.keys(device.gpio_config || {}).length ? (
        <div className="config-grid">
          <div>
            <div className="muted small">Calibration</div>
            <pre>{JSON.stringify(device.calibration || {}, null, 2)}</pre>
          </div>
          <div>
            <div className="muted small">GPIO configuration</div>
            <pre>{JSON.stringify(device.gpio_config || {}, null, 2)}</pre>
          </div>
        </div>
      ) : null}

      <RelayControl device={device} controlAvailable={controlAvailable} onChanged={onChanged} />

      <div className="button-row">
        <button type="button" className="action" onClick={rotate} disabled={rotating}>
          {rotating ? 'Rotating…' : 'Rotate device token'}
        </button>
      </div>
      <ErrorBox error={error} />
    </article>
  )
}

export default function DeviceManager({ devices, devicesState, meta, onChanged }) {
  const [showForm, setShowForm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [issuedToken, setIssuedToken] = useState(null)
  const [form, setForm] = useState({
    id: '',
    name: '',
    location: '',
    source: 'esp32',
    sensor_model: '',
    sampling_interval_seconds: '60',
    nominal_voltage_v: '230',
    calibration: '',
    gpio_config: '',
    relay_enabled: false,
  })

  function update(name, value) {
    setForm((prev) => ({ ...prev, [name]: value }))
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setIssuedToken(null)
    try {
      const payload = {
        id: form.id.trim(),
        name: form.name.trim() || null,
        location: form.location.trim() || null,
        source: form.source,
        sensor_model: form.sensor_model.trim() || null,
        sampling_interval_seconds: Number(form.sampling_interval_seconds) || null,
        nominal_voltage_v: Number(form.nominal_voltage_v) || null,
        calibration: parseJsonField(form.calibration, 'Calibration'),
        gpio_config: parseJsonField(form.gpio_config, 'GPIO configuration'),
        relay_enabled: Boolean(form.relay_enabled),
      }
      if (!payload.id) throw new Error('A device id is required')
      const result = await api.createDevice(payload)
      setIssuedToken({ deviceId: result.device.id, token: result.device_token, note: result.token_note })
      setForm((prev) => ({ ...prev, id: '', name: '', location: '' }))
      setShowForm(false)
      onChanged?.()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Panel
        title="Devices"
        tag={<span className="tag">{devices.length}</span>}
        note="Each device belongs to this account only. A device token can write telemetry for its own device and nothing else."
        actions={
          <button type="button" className="action" onClick={() => setShowForm((value) => !value)}>
            {showForm ? 'Close form' : 'Register a device'}
          </button>
        }
      >
        {issuedToken ? (
          <div className="token-box" role="status">
            <div>
              <strong>Device token for {issuedToken.deviceId}</strong>
              <p className="muted small">{issuedToken.note}</p>
            </div>
            <code className="token-value">{issuedToken.token}</code>
            <CopyButton value={issuedToken.token} label="Copy token" />
          </div>
        ) : null}

        {showForm ? (
          <form className="device-form" onSubmit={submit}>
            <div className="form-grid">
              <Field label="Device id" hint="Stable id used by the firmware, e.g. esp32-main-01">
                <input required value={form.id} onChange={(event) => update('id', event.target.value)} />
              </Field>
              <Field label="Display name">
                <input value={form.name} onChange={(event) => update('name', event.target.value)} />
              </Field>
              <Field label="Location">
                <input value={form.location} onChange={(event) => update('location', event.target.value)} />
              </Field>
              <Field label="Source">
                <select value={form.source} onChange={(event) => update('source', event.target.value)}>
                  {SOURCES.map((source) => (
                    <option key={source.value} value={source.value}>
                      {source.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Sensor model" hint="Use the exact part number. ACS71 is not a model name.">
                <input
                  value={form.sensor_model}
                  onChange={(event) => update('sensor_model', event.target.value)}
                  placeholder="e.g. ACS712-30A"
                />
              </Field>
              <Field label="Sampling interval (seconds)">
                <input
                  type="number"
                  min="1"
                  value={form.sampling_interval_seconds}
                  onChange={(event) => update('sampling_interval_seconds', event.target.value)}
                />
              </Field>
              <Field label="Nominal voltage (V)">
                <input
                  type="number"
                  min="1"
                  step="0.1"
                  value={form.nominal_voltage_v}
                  onChange={(event) => update('nominal_voltage_v', event.target.value)}
                />
              </Field>
              <label className="checkline field">
                <input
                  type="checkbox"
                  checked={form.relay_enabled}
                  onChange={(event) => update('relay_enabled', event.target.checked)}
                />
                Enable relay control for this device
              </label>
            </div>

            <div className="form-grid">
              <Field label="Calibration (JSON)" hint='Example: {"adc_midpoint": 2048, "mv_per_amp": 66}'>
                <textarea
                  rows={4}
                  value={form.calibration}
                  onChange={(event) => update('calibration', event.target.value)}
                  placeholder='{"adc_midpoint": 2048, "mv_per_amp": 66}'
                />
              </Field>
              <Field label="GPIO configuration (JSON)" hint='Example: {"current_adc": 34, "relay": 26}'>
                <textarea
                  rows={4}
                  value={form.gpio_config}
                  onChange={(event) => update('gpio_config', event.target.value)}
                  placeholder='{"current_adc": 34, "relay": 26}'
                />
              </Field>
            </div>

            <ErrorBox error={error} />
            <div className="button-row">
              <button className="primary" type="submit" disabled={busy}>
                {busy ? 'Registering…' : 'Register device and issue token'}
              </button>
              <button type="button" className="action" onClick={() => setShowForm(false)}>
                Cancel
              </button>
            </div>
          </form>
        ) : null}

        <ErrorBox error={devicesState.error} onRetry={devicesState.reload} />
        {devicesState.loading ? <Loading label="Loading devices…" /> : null}
        {!devicesState.loading && devices.length === 0 ? (
          <Empty>
            No device has reported yet. Register a device above, or seed the simulator from the
            command line. Simulated data is always labelled.
          </Empty>
        ) : null}
        <div className="device-list">
          {devices.map((device) => (
            <DeviceRow
              key={device.id}
              device={device}
              controlAvailable={Boolean(meta?.relay_control_enabled)}
              onChanged={onChanged}
              onToken={(token) =>
                setIssuedToken({
                  deviceId: device.id,
                  token,
                  note: 'The previous token no longer works. Store this one now.',
                })
              }
            />
          ))}
        </div>
      </Panel>

      <Panel title="Telemetry contract" note="The same validated ingestion path handles simulated and hardware samples.">
        <div className="contract">
          <div>
            <div className="muted small">Endpoint</div>
            <code>POST /api/device/telemetry</code>
            <div className="muted small">Authorization: Bearer wxdev_…</div>
          </div>
          <div>
            <div className="muted small">Per reading</div>
            <code>device_id, ts (ISO 8601 with offset), current, voltage, power_w, energy_wh</code>
            <div className="muted small">
              Optional: power_factor, sensor_status, measurement_status, simulated
            </div>
          </div>
        </div>
        <ul className="notes">
          <li>A current-only sample is refused: a current sensor cannot supply real power by itself.</li>
          <li>Voltage × current is stored as an estimate, flagged in the reading.</li>
          <li>Energy derived from power × sampling interval is also flagged as an estimate.</li>
          <li>
            The relay is never switched automatically. A command stays <code>pending</code> until
            the device acknowledges it.
          </li>
          <li>No physical hardware has been tested by this project. See the hardware guide before wiring anything.</li>
        </ul>
      </Panel>
    </>
  )
}
