import { useCallback, useEffect, useMemo, useState } from 'react'

import api from './api'
import { AiStatusCard, ExplainPanel } from './components/ExplainPanel'
import { AnomaliesPanel, CoverageCard, ForecastPanel, InsightsPanel } from './components/AnalysisPanels'
import { BillLines, ProjectedBillPanel, TariffEditor, TariffPanel } from './components/BillPanel'
import { ProfileChart, UsageChart } from './components/UsageChart'
import { Banner, Card, Empty, ErrorBox, EstimateTag, num, pct } from './components/ui'

const VIEWS = [
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
]

/**
 * Fetch helper.
 *
 * `enabled` exists so a panel can wait its turn. Every one of these endpoints rebuilds a
 * time series from the raw readings, and they are all CPU-bound, so asking for all of them
 * at once makes each one wait on the others and the whole page goes slow together. The
 * summary and usage load first so the cards appear quickly; the analysis panels follow.
 */
function useAsync(fn, deps, enabled = true) {
  const [state, setState] = useState({ data: null, error: null, loading: true })

  const run = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true }))
    try {
      const data = await fn()
      setState({ data, error: null, loading: false })
    } catch (error) {
      setState({ data: null, error, loading: false })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (enabled) run()
  }, [run, enabled])

  return { ...state, reload: run }
}

export default function App() {
  const [view, setView] = useState('month')
  const [deviceId, setDeviceId] = useState(null)
  const [devices, setDevices] = useState([])
  const [tariff, setTariff] = useState(null)
  const [editingTariff, setEditingTariff] = useState(false)
  const [savingTariff, setSavingTariff] = useState(false)
  const [explain, setExplain] = useState({ data: null, pending: false })

  const meta = useAsync(() => api.meta(), [])
  const deviceList = useAsync(() => api.devices(), [])
  const tariffState = useAsync(() => api.tariff(), [])
  const aiStatus = useAsync(() => api.aiStatus(), [])

  // Nothing is fetched against "no device chosen": the summary would be computed twice,
  // once for the default device and again for its real id, and the first copy is thrown
  // away. This also covers the no-device case, where meta reports has_device = false.
  const deviceResolved = deviceId !== null || (meta.data ? !meta.data.has_device : false)

  const summary = useAsync(() => api.summary(deviceId), [deviceId], deviceResolved)
  const usage = useAsync(() => api.usage(view, deviceId), [view, deviceId], deviceResolved)

  // The analysis panels wait for a summary that belongs to the selected device, so
  // switching device or view does not fire six CPU-heavy requests at once and leave the
  // cards waiting behind the slowest one. Comparing the device on the returned summary
  // rather than a loading flag keeps this correct on the very first render after a change.
  const summaryDeviceId = summary.data?.device?.id ?? null
  const analysisReady = Boolean(summary.data) && summaryDeviceId === deviceId
  const forecast = useAsync(() => api.forecast(7, deviceId), [deviceId], analysisReady)
  const anomalies = useAsync(() => api.anomalies(deviceId), [deviceId], analysisReady)
  const insights = useAsync(() => api.insights(deviceId), [deviceId], analysisReady)

  useEffect(() => {
    if (meta.data?.device_id && deviceId === null) setDeviceId(meta.data.device_id)
  }, [meta.data, deviceId])

  useEffect(() => {
    if (deviceList.data?.devices) setDevices(deviceList.data.devices)
    if (tariffState.data?.tariff) setTariff(tariffState.data.tariff)
  }, [deviceList.data, tariffState.data])

  const askForExplanation = useCallback(async () => {
    setExplain((prev) => ({ ...prev, pending: true }))
    try {
      const data = await api.explain({ device_id: deviceId, horizon_days: 7 })
      setExplain({ data, pending: false })
    } catch (error) {
      setExplain({ data: { status: 'error', message: error.message, generated: {}, calculated: {} }, pending: false })
    }
  }, [deviceId])

  const saveTariff = useCallback(async (payload) => {
    setSavingTariff(true)
    try {
      const result = await fetch('/api/billing/tariff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const text = await result.text()
      const data = text ? JSON.parse(text) : null
      if (!result.ok) {
        const detail = data?.detail
        const message = Array.isArray(detail) ? detail.map((item) => item.msg).join('; ') : detail
        throw new Error(message || 'Could not save the tariff')
      }
      setTariff(data.tariff)
      setEditingTariff(false)
      // Every figure in the app is priced off the tariff, so refetch what depends on it.
      await Promise.all([tariffState.reload(), summary.reload(), usage.reload(), forecast.reload(), insights.reload()])
    } catch (error) {
      window.alert(error.message)
    } finally {
      setSavingTariff(false)
    }
  }, [tariffState, summary, usage, forecast, insights])

  const current = devices.find((device) => device.id === deviceId)
  const currency = summary.data?.currency || tariff?.currency || 'INR'
  const provenance = summary.data?.data_provenance
  const noDevice = summary.data?.status === 'no_device'

  const banner = useMemo(() => {
    if (noDevice) return null
    if (provenance?.fully_simulated) {
      return <Banner kind="simulated">{provenance.statement}</Banner>
    }
    if (provenance && provenance.readings_from_hardware === 0 && provenance.readings_unlabelled > 0) {
      return <Banner kind="unknown">{provenance.statement}</Banner>
    }
    return null
  }, [provenance, noDevice])

  const totals = usage.data?.totals
  const power = summary.data?.current_power

  if (meta.loading) {
    return <div className="app">Loading…</div>
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>WattXpense</h1>
          <div className="subtitle">
            Home energy tracking · times shown in {meta.data?.timezone}
            {tariff ? ` · priced on ${tariff.name}` : ''}
          </div>
        </div>
        {summary.data?.generated_at ? (
          <div className="subtitle">Updated {new Date(summary.data.generated_at).toLocaleString()}</div>
        ) : null}
      </header>

      {banner}

      <ErrorBox error={meta.error} onRetry={meta.reload} />
      <ErrorBox error={summary.error} onRetry={summary.reload} />

      {noDevice ? (
        <div className="panel">
          <h2>No device has reported yet</h2>
          <p className="panel-note">
            Seed some data to see the dashboard:
          </p>
          <pre style={{ background: '#0f1419', padding: 12, borderRadius: 6, fontSize: 12, overflow: 'auto' }}>
{`.venv/bin/python -m simulator.cli seed --days 35 --url http://127.0.0.1:8011 --reset`}
          </pre>
        </div>
      ) : null}

      <div className="controls">
        <div className="tabs">
          {VIEWS.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? 'active' : ''}
              onClick={() => setView(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        {devices.length > 1 ? (
          <select value={deviceId || ''} onChange={(event) => setDeviceId(event.target.value)}>
            {devices.map((device) => (
              <option key={device.id} value={device.id}>
                {deviceLabel(device)}
              </option>
            ))}
          </select>
        ) : null}
        <button className="action" onClick={() => Promise.all([summary.reload(), usage.reload(), forecast.reload(), anomalies.reload(), insights.reload()])}>
          Refresh
        </button>
        <button className="action" onClick={askForExplanation} disabled={explain.pending || noDevice}>
          {explain.pending ? 'Asking the model…' : 'Explain this in plain language'}
        </button>
      </div>

      {!noDevice ? (
        <>
          <div className="cards">
            <Card
              label="Energy today"
              value={num(summary.data?.energy_today?.energy_kwh, 2)}
              unit="kWh"
              hint={
                summary.data?.energy_today?.change_note ||
                (summary.data?.energy_today?.change_vs_yesterday_percent !== null
                  ? `${summary.data?.energy_today?.change_vs_yesterday_percent}% vs yesterday`
                  : undefined)
              }
              tag={summary.data?.data_label === 'Simulated data' ? <span className="tag simulated">simulated</span> : null}
            />
            <Card
              label="Right now"
              value={power?.latest_w !== null && power?.latest_w !== undefined ? num(power.latest_w, 0) : '—'}
              unit="W"
              hint={
                power?.is_stale
                  ? `Last reading ${num(power.age_minutes, 0)} min ago.`
                  : power?.clock_note || 'Latest reading.'
              }
              tag={power?.is_stale ? <span className="tag bad">stale</span> : null}
            />
            <Card
              label="Energy this period"
              value={num(summary.data?.energy_this_period?.energy_kwh, 1)}
              unit="kWh"
              hint={`${summary.data?.energy_this_period?.days_metered ?? 0} fully covered days${
                summary.data?.energy_this_period?.days_skipped
                  ? `, ${summary.data.energy_this_period.days_skipped} excluded for gaps`
                  : ''
              }.`}
            />
            <Card
              label="Projected bill"
              value={num(summary.data?.projected_bill?.total, 0)}
              unit={currency}
              hint={
                summary.data?.projected_bill?.is_estimate
                  ? 'Metered days plus forecast days.'
                  : 'Complete period, all metered.'
              }
              tag={summary.data?.projected_bill?.is_estimate ? <EstimateTag /> : null}
            />
            {totals ? (
              <CoverageCard
                coverage={usage.data?.daily?.length ? avgCoverage(usage.data.daily) : 0}
                readingCount={totals.reading_count}
                daysWithData={totals.days_with_data}
              />
            ) : null}
            <AiStatusCard status={aiStatus.data} />
          </div>

          <div className="panel">
            <h2>
              Usage
              {usage.data?.data_label === 'Simulated data' ? (
                <span className="tag simulated">simulated</span>
              ) : null}
            </h2>
            <p className="panel-note">
              {usage.data?.range
                ? `${usage.data.range.start} to ${usage.data.range.end}, bucketed by ${usage.data.bucket?.toLowerCase()}.`
                : 'Loading…'}
              {totals?.comparison_note ? ` ${totals.comparison_note}` : ''}
            </p>
            {usage.loading ? (
              <Empty>Loading…</Empty>
            ) : (
              <UsageChart
                buckets={usage.data?.buckets}
                bucket={usage.data?.bucket}
                previous={usage.data?.previous_buckets}
                previousLabel="Previous period"
              />
            )}
          </div>

          <div className="two-col">
            <div className="panel">
              <h2>Average daily load</h2>
              <p className="panel-note">Mean power in each hour of the day, averaged over this window.</p>
              <ProfileChart profile={usage.data?.average_profile} />
            </div>
            <ProjectedBillPanel summary={summary.data} currency={currency} />
          </div>

          <ExplainPanel
            result={explain.data}
            status={aiStatus.data?.status}
            pending={explain.pending}
            modelStatus={aiStatus.data}
            onRefresh={askForExplanation}
          />

          <div className="two-col">
            <ForecastPanel forecast={forecast.data} />
            <AnomaliesPanel anomalies={anomalies.data} />
          </div>

          <InsightsPanel insights={insights.data} currency={currency} />

          {editingTariff ? (
            <TariffEditor onSave={saveTariff} onCancel={() => setEditingTariff(false)} />
          ) : (
            <TariffPanel tariff={tariff} onEdit={() => setEditingTariff(true)} saving={savingTariff} />
          )}

          {current && current.simulated_reading_share > 0 ? (
            <p className="muted small" style={{ marginTop: 12 }}>
              {pct(current.simulated_reading_share)} of this device's readings are labelled
              simulated.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function avgCoverage(daily) {
  if (!daily || daily.length === 0) return 0
  return daily.reduce((total, row) => total + (row.coverage_ratio || 0), 0) / daily.length
}

function deviceLabel(device) {
  const name = device.name || device.id
  // The simulator already says "simulated" in its device names, so this avoids
  // rendering "Main supply (simulated) (simulated)".
  return device.is_simulated && !/simulat/i.test(name) ? `${name} (simulated)` : name
}

export { BillLines }
