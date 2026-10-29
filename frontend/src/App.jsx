import { useCallback, useEffect, useMemo, useState } from 'react'

import api from './api'
import AccountPanel from './components/AccountPanel'
import { AnomaliesPanel, CoverageCard, ForecastPanel, InsightsPanel } from './components/AnalysisPanels'
import AuthScreen from './components/AuthScreen'
import { BillLines, ProjectedBillPanel, TariffEditor, TariffPanel } from './components/BillPanel'
import DeviceManager from './components/DeviceManager'
import { AiStatusCard, ExplainPanel } from './components/ExplainPanel'
import { ProfileChart, UsageChart } from './components/UsageChart'
import {
  Banner,
  Card,
  Empty,
  ErrorBox,
  EstimateTag,
  Loading,
  Panel,
  Tag,
  num,
  pct,
  relativeTime,
} from './components/ui'

const VIEWS = [
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
]

const HORIZONS = [
  { id: 7, label: '7 days' },
  { id: 14, label: '14 days' },
  { id: 30, label: '30 days' },
]

const SECTIONS = [
  { id: 'overview', label: 'Overview', title: 'Your home, at a glance' },
  { id: 'usage', label: 'Usage & forecast', title: 'Usage and forecast' },
  { id: 'anomalies', label: 'Anomalies', title: 'Unusual days and gaps' },
  { id: 'insights', label: 'Insights', title: 'Where the money goes' },
  { id: 'bill', label: 'Bill & tariff', title: 'Bill and tariff' },
  { id: 'ai', label: 'AI explanation', title: 'Written explanation' },
  { id: 'devices', label: 'Devices', title: 'Devices and hardware' },
  { id: 'account', label: 'Account', title: 'Account and security' },
]

/**
 * Fetch helper.
 *
 * `enabled` exists so a panel can wait its turn. Every one of these endpoints rebuilds a
 * time series from the raw readings, and they are all CPU-bound, so asking for all of them
 * at once makes each one wait on the others and the whole page goes slow together.
 */
function useAsync(fn, deps, enabled = true) {
  const [state, setState] = useState({ data: null, error: null, loading: Boolean(enabled) })

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
  const [auth, setAuth] = useState({ loading: true, user: null, registrationEnabled: true, error: null })

  const loadAuth = useCallback(async () => {
    setAuth((prev) => ({ ...prev, loading: true, error: null }))
    try {
      const status = await api.authStatus()
      setAuth({
        loading: false,
        user: status.authenticated ? status.user : null,
        registrationEnabled: status.registration_enabled,
        error: null,
      })
    } catch (error) {
      setAuth({ loading: false, user: null, registrationEnabled: true, error })
    }
  }, [])

  useEffect(() => {
    loadAuth()
  }, [loadAuth])

  useEffect(() => {
    const onUnauthorized = () =>
      setAuth((prev) => ({ ...prev, loading: false, user: null }))
    window.addEventListener('wattxpense:unauthorized', onUnauthorized)
    return () => window.removeEventListener('wattxpense:unauthorized', onUnauthorized)
  }, [])

  if (auth.loading) {
    return (
      <div className="splash">
        <div className="brand-mark" aria-hidden="true">
          ⚡
        </div>
        <Loading label="Checking your session…" />
      </div>
    )
  }

  if (auth.error) {
    return (
      <div className="app narrow">
        <Panel title="WattXpense is not reachable" note="The dashboard could not ask the API for a session.">
          <ErrorBox error={auth.error} onRetry={loadAuth} />
          <p className="muted small">
            Start the API with <code>uvicorn backend.app.main:app</code> and try again.
          </p>
        </Panel>
      </div>
    )
  }

  if (!auth.user) {
    return (
      <AuthScreen
        registrationEnabled={auth.registrationEnabled}
        onSuccess={(user) => setAuth((prev) => ({ ...prev, user }))}
      />
    )
  }

  return (
    <Dashboard
      user={auth.user}
      onSignedOut={() => setAuth((prev) => ({ ...prev, user: null }))}
    />
  )
}

function Dashboard({ user, onSignedOut }) {
  const [section, setSection] = useState('overview')
  const [view, setView] = useState('month')
  const [horizon, setHorizon] = useState(7)
  const [deviceId, setDeviceId] = useState(null)
  const [editingTariff, setEditingTariff] = useState(false)
  const [savingTariff, setSavingTariff] = useState(false)
  const [tariffError, setTariffError] = useState(null)
  const [explain, setExplain] = useState({ data: null, pending: false })

  const meta = useAsync(() => api.meta(), [])
  const devicesState = useAsync(() => api.devices(), [])
  const tariffState = useAsync(() => api.tariff(), [])
  const aiStatus = useAsync(() => api.aiStatus(), [])

  const devices = devicesState.data?.devices || []
  const tariff = tariffState.data?.tariff || null
  const deviceResolved = meta.data ? (meta.data.has_device ? deviceId !== null : true) : false

  const summary = useAsync(() => api.summary(deviceId), [deviceId], deviceResolved)
  const usage = useAsync(() => api.usage(view, deviceId), [view, deviceId], deviceResolved)

  const summaryDeviceId = summary.data?.device?.id ?? null
  const analysisReady = Boolean(summary.data) && summaryDeviceId === deviceId && summary.data?.status !== 'no_device'
  const forecast = useAsync(() => api.forecast(horizon, deviceId), [deviceId, horizon], analysisReady)
  const anomalies = useAsync(() => api.anomalies(deviceId), [deviceId], analysisReady)
  const insights = useAsync(() => api.insights(deviceId), [deviceId], analysisReady)

  useEffect(() => {
    if (meta.data?.device_id && deviceId === null) setDeviceId(meta.data.device_id)
  }, [meta.data, deviceId])

  useEffect(() => {
    if (devices.length === 0) return
    if (!deviceId || !devices.some((device) => device.id === deviceId)) {
      setDeviceId(devices[0].id)
    }
  }, [devices, deviceId])

  const refreshAll = useCallback(async () => {
    await Promise.all([
      meta.reload(),
      devicesState.reload(),
      tariffState.reload(),
      aiStatus.reload(),
      summary.reload(),
      usage.reload(),
      forecast.reload(),
      anomalies.reload(),
      insights.reload(),
    ])
  }, [meta, devicesState, tariffState, aiStatus, summary, usage, forecast, anomalies, insights])

  const askForExplanation = useCallback(async () => {
    setExplain((prev) => ({ ...prev, pending: true }))
    try {
      const data = await api.explain({ device_id: deviceId, horizon_days: horizon })
      setExplain({ data, pending: false })
    } catch (error) {
      setExplain({
        data: { status: 'error', message: error.message, generated: {}, calculated: {} },
        pending: false,
      })
    }
  }, [deviceId, horizon])

  const saveTariff = useCallback(
    async (payload) => {
      setSavingTariff(true)
      setTariffError(null)
      try {
        await api.setTariff(payload)
        setEditingTariff(false)
        await Promise.all([tariffState.reload(), summary.reload(), usage.reload(), forecast.reload(), insights.reload()])
      } catch (error) {
        setTariffError(error)
      } finally {
        setSavingTariff(false)
      }
    },
    [tariffState, summary, usage, forecast, insights],
  )

  const current = devices.find((device) => device.id === deviceId)
  const currency = summary.data?.currency || tariff?.currency || meta.data?.currency || 'INR'
  const noDevice = summary.data?.status === 'no_device' || (meta.data && !meta.data.has_device)
  const totals = usage.data?.totals
  const power = summary.data?.current_power
  const coverage = usage.data?.daily?.length ? avgCoverage(usage.data.daily) : 0
  const activeSection = SECTIONS.find((item) => item.id === section) || SECTIONS[0]

  const banner = useMemo(() => <ProvenanceBanner summary={summary.data} />, [summary.data])

  const updatedAt = summary.data?.generated_at || usage.data?.generated_at

  const rangeControls = (
    <div className="segmented" role="group" aria-label="Usage range">
      {VIEWS.map((item) => (
        <button
          key={item.id}
          type="button"
          aria-pressed={view === item.id}
          className={view === item.id ? 'active' : ''}
          onClick={() => setView(item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )

  return (
    <div className="app-shell">
      <header className="app-header">
        <button className="brand" type="button" onClick={() => setSection('overview')}>
          <span className="brand-mark small" aria-hidden="true">
            ⚡
          </span>
          <span>
            <strong>WattXpense</strong>
            <span className="brand-sub">private household energy</span>
          </span>
        </button>

        <div className="header-status">
          {updatedAt ? <span className="muted small">Updated {relativeTime(updatedAt)}</span> : null}
          {meta.data?.timezone ? <span className="muted small">{meta.data.timezone}</span> : null}
          {current?.is_simulated ? <Tag kind="simulated">simulated device</Tag> : null}
        </div>

        <div className="user-menu">
          <span className="user-name">{user.display_name || user.email}</span>
          <button type="button" className="action small-action" onClick={() => setSection('account')}>
            Account
          </button>
        </div>
      </header>

      <nav className="app-nav" aria-label="Dashboard sections">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-current={section === item.id ? 'page' : undefined}
            className={section === item.id ? 'active' : ''}
            onClick={() => setSection(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="app-main">
        <div className="page-head">
          <div>
            <p className="eyebrow">{user.display_name || user.email}</p>
            <h1>{activeSection.title}</h1>
            <p className="page-note">{sectionNote(section)}</p>
          </div>
          <div className="page-actions">
            {devices.length > 1 ? (
              <label className="inline-field">
                <span className="sr-only">Device</span>
                <select value={deviceId || ''} onChange={(event) => setDeviceId(event.target.value)}>
                  {devices.map((device) => (
                    <option key={device.id} value={device.id}>
                      {deviceLabel(device)}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <button type="button" className="action" onClick={refreshAll}>
              Refresh
            </button>
          </div>
        </div>

        {banner}
        <ErrorBox error={meta.error} onRetry={meta.reload} />
        <ErrorBox error={devicesState.error} onRetry={devicesState.reload} />
        <ErrorBox error={summary.error} onRetry={summary.reload} />

        {section === 'overview' ? (
          <Overview
            summary={summary}
            usage={usage}
            power={power}
            totals={totals}
            coverage={coverage}
            currency={currency}
            aiStatus={aiStatus}
            noDevice={noDevice}
            rangeControls={rangeControls}
            onOpenDevices={() => setSection('devices')}
            onOpenUsage={() => setSection('usage')}
            onOpenBill={() => setSection('bill')}
          />
        ) : null}

        {section === 'usage' ? (
          <UsageSection
            usage={usage}
            forecast={forecast}
            horizon={horizon}
            setHorizon={setHorizon}
            rangeControls={rangeControls}
            coverage={coverage}
            noDevice={noDevice}
          />
        ) : null}

        {section === 'anomalies' ? (
          <div className="section-stack">
            <Panel
              title="How findings are produced"
              note="A finding is only shown with the figures behind it. An anomaly is a statistical difference from the recent pattern, not proof of a fault or of an appliance."
            >
              <p className="muted small">
                Forecasts and anomaly findings are models, not measurements. A data gap is reported as
                a gap; it is never scaled up into invented energy.
              </p>
            </Panel>
            {anomalies.loading ? <Loading label="Looking for unusual days…" /> : null}
            <ErrorBox error={anomalies.error} onRetry={anomalies.reload} />
            <AnomaliesPanel anomalies={anomalies.data} />
          </div>
        ) : null}

        {section === 'insights' ? (
          <div className="section-stack">
            {insights.loading ? <Loading label="Building savings estimates…" /> : null}
            <ErrorBox error={insights.error} onRetry={insights.reload} />
            <InsightsPanel insights={insights.data} currency={currency} />
          </div>
        ) : null}

        {section === 'bill' ? (
          <div className="section-stack">
            <ErrorBox error={tariffState.error} onRetry={tariffState.reload} />
            <ErrorBox error={tariffError} />
            <ProjectedBillPanel summary={summary.data} currency={currency} />
            {summary.data?.projected_bill ? (
              <Panel title="Bill lines" note="Every slab stays visible. A single blended rate would be tidier and wrong.">
                <BillLines bill={summary.data.projected_bill} currency={currency} />
              </Panel>
            ) : null}
            {editingTariff ? (
              <TariffEditor onSave={saveTariff} onCancel={() => setEditingTariff(false)} />
            ) : (
              <TariffPanel
                tariff={tariff}
                onEdit={() => setEditingTariff(true)}
                saving={savingTariff}
              />
            )}
          </div>
        ) : null}

        {section === 'ai' ? (
          <AiSection
            explain={explain}
            aiStatus={aiStatus}
            noDevice={noDevice}
            onAsk={askForExplanation}
          />
        ) : null}

        {section === 'devices' ? (
          <DeviceManager devices={devices} devicesState={devicesState} meta={meta.data} onChanged={refreshAll} />
        ) : null}

        {section === 'account' ? (
          <AccountPanel user={user} meta={meta.data} onSignedOut={onSignedOut} />
        ) : null}

        {current && current.simulated_reading_share > 0 && section !== 'devices' ? (
          <p className="muted small footer-note">
            {pct(current.simulated_reading_share)} of this device's readings are labelled simulated.
          </p>
        ) : null}
      </main>
    </div>
  )
}

function Overview({
  summary,
  usage,
  power,
  totals,
  coverage,
  currency,
  aiStatus,
  noDevice,
  rangeControls,
  onOpenDevices,
  onOpenUsage,
  onOpenBill,
}) {
  if (summary.loading && !summary.data) {
    return <Loading label="Loading your summary…" />
  }

  if (noDevice) {
    return (
      <div className="section-stack">
        <Panel
          title="No device has reported yet"
          note="The dashboard is usable with simulated data. Nothing is invented to fill an empty account."
        >
          <p>
            Register a device to get a private device token, or seed the bundled simulator into this
            account from the command line. Simulated readings are labelled everywhere they appear.
          </p>
          <div className="button-row">
            <button type="button" className="primary" onClick={onOpenDevices}>
              Register a device
            </button>
          </div>
          <pre className="code-block">
{`.venv/bin/python -m simulator.cli seed --days 35 --url http://127.0.0.1:8011 --reset --email ${'<your-account-email>'}`}
          </pre>
          <p className="muted small">
            The simulator signs in with the account email and password. Add <code>--password</code> if
            the account does not use the demo password.
          </p>
        </Panel>
        <AiStatusCard status={aiStatus.data} />
      </div>
    )
  }

  const energyToday = summary.data?.energy_today || {}
  const period = summary.data?.energy_this_period || {}
  const bill = summary.data?.projected_bill

  return (
    <div className="section-stack">
      <div className="cards">
        <Card
          label="Energy today"
          value={num(energyToday.energy_kwh, 2)}
          unit="kWh"
          hint={energyToday.change_note || (energyToday.change_vs_yesterday_percent !== null ? `${energyToday.change_vs_yesterday_percent}% vs yesterday` : undefined)}
          tag={summary.data?.data_label === 'Simulated data' ? <Tag kind="simulated">simulated</Tag> : null}
        />
        <Card
          label="Right now"
          value={power?.latest_w !== null && power?.latest_w !== undefined ? num(power.latest_w, 0) : '—'}
          unit="W"
          hint={
            power?.is_stale
              ? `Last reading ${num(power.age_minutes, 0)} min ago. The figure is stale.`
              : power?.clock_note || 'Latest reading.'
          }
          tag={power?.is_stale ? <Tag kind="bad">stale</Tag> : null}
          tone={power?.is_stale ? 'warn' : undefined}
        />
        <Card
          label="This billing period"
          value={num(period.energy_kwh, 1)}
          unit="kWh"
          hint={`${period.days_metered ?? 0} fully covered days${
            period.days_skipped ? `, ${period.days_skipped} excluded for gaps` : ''
          }.`}
        />
        <Card
          label="Projected bill"
          value={num(bill?.total, 0)}
          unit={currency}
          hint={bill?.is_estimate ? 'Metered days plus forecast days.' : 'Complete period, all metered.'}
          tag={bill?.is_estimate ? <EstimateTag /> : null}
        />
        {totals ? (
          <CoverageCard coverage={coverage} readingCount={totals.reading_count} daysWithData={totals.days_with_data} />
        ) : null}
        <AiStatusCard status={aiStatus.data} />
      </div>

      <Panel
        title="Usage"
        tag={usage.data?.data_label === 'Simulated data' ? <Tag kind="simulated">simulated</Tag> : null}
        note={
          usage.data?.range
            ? `${usage.data.range.start} to ${usage.data.range.end}, bucketed by ${usage.data.bucket?.toLowerCase()}.${
                totals?.comparison_note ? ` ${totals.comparison_note}` : ''
              }`
            : 'Loading the selected window…'
        }
        actions={
          <>
            {rangeControls}
            <button type="button" className="action small-action" onClick={onOpenUsage}>
              Open usage
            </button>
          </>
        }
      >
        {usage.loading ? (
          <Loading label="Loading usage…" />
        ) : (
          <UsageChart
            buckets={usage.data?.buckets}
            bucket={usage.data?.bucket}
            previous={usage.data?.previous_buckets}
            previousLabel="Previous period"
          />
        )}
        <ErrorBox error={usage.error} onRetry={usage.reload} />
        {totals && coverage < 0.8 && totals.reading_count > 0 ? (
          <p className="warning">
            This window is only {pct(coverage)} covered. Comparisons and per-day figures are shown with
            that limitation rather than filled in.
          </p>
        ) : null}
      </Panel>

      <div className="two-col">
        <Panel title="Average daily load" note="Mean power in each hour of the day, averaged over this window.">
          {usage.loading ? <Loading label="Loading profile…" /> : <ProfileChart profile={usage.data?.average_profile} />}
        </Panel>
        <ProjectedBillPanel summary={summary.data} currency={currency} />
      </div>

      <Panel
        title="Next steps"
        note="Every section is separate so a missing model or missing hardware never hides the calculations."
      >
        <div className="quick-links">
          <button type="button" className="action" onClick={onOpenUsage}>
            Usage and forecast
          </button>
          <button type="button" className="action" onClick={onOpenBill}>
            Bill and tariff
          </button>
          <button type="button" className="action" onClick={onOpenDevices}>
            Devices and hardware
          </button>
        </div>
      </Panel>
    </div>
  )
}

function UsageSection({ usage, forecast, horizon, setHorizon, rangeControls, coverage, noDevice }) {
  if (noDevice) {
    return <Empty>No device has reported yet, so there is no usage history to chart.</Empty>
  }
  const totals = usage.data?.totals
  const peaks = usage.data?.peaks || {}

  return (
    <div className="section-stack">
      <Panel
        title="Energy over time"
        tag={usage.data?.data_label === 'Simulated data' ? <Tag kind="simulated">simulated</Tag> : null}
        note={
          usage.data?.range
            ? `${usage.data.range.start} to ${usage.data.range.end}, bucketed by ${usage.data.bucket?.toLowerCase()}.`
            : undefined
        }
        actions={rangeControls}
      >
        {usage.loading ? (
          <Loading label="Loading usage…" />
        ) : (
          <UsageChart
            buckets={usage.data?.buckets}
            bucket={usage.data?.bucket}
            previous={usage.data?.previous_buckets}
            previousLabel="Previous period"
          />
        )}
        <ErrorBox error={usage.error} onRetry={usage.reload} />
        <div className="cards compact">
          <Card label="Window total" value={num(totals?.energy_kwh, 2)} unit="kWh" hint={`${totals?.reading_count ?? 0} readings`} />
          <Card label="Average load" value={num(totals?.avg_power_w, 0)} unit="W" />
          <Card label="Peak load" value={num(totals?.peak_power_w, 0)} unit="W" />
          <Card label="Coverage" value={pct(coverage)} tag={coverage < 0.8 ? <Tag kind="bad">partial</Tag> : null} />
        </div>
        {peaks.busiest_day ? (
          <p className="muted small">
            Busiest day {peaks.busiest_day} ({num(peaks.busiest_day_kwh, 2)} kWh). Quietest day{' '}
            {peaks.quietest_day}.
          </p>
        ) : null}
        {totals?.comparison_note ? <p className="muted small">{totals.comparison_note}</p> : null}
      </Panel>

      <div className="two-col">
        <Panel title="Average daily load" note="Mean power in each hour of the day. A narrow overnight band is usually standby load, not something you can switch off.">
          {usage.loading ? <Loading label="Loading profile…" /> : <ProfileChart profile={usage.data?.average_profile} />}
        </Panel>
        <div>
          <div className="panel-toolbar">
            <span className="muted small">Forecast horizon</span>
            <div className="segmented" role="group" aria-label="Forecast horizon">
              {HORIZONS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  aria-pressed={horizon === item.id}
                  className={horizon === item.id ? 'active' : ''}
                  onClick={() => setHorizon(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          {forecast.loading ? <Loading label="Building forecast…" /> : null}
          <ErrorBox error={forecast.error} onRetry={forecast.reload} />
          <ForecastPanel forecast={forecast.data} />
        </div>
      </div>
    </div>
  )
}

function AiSection({ explain, aiStatus, noDevice, onAsk }) {
  if (noDevice) {
    return <Empty>No readings have been recorded, so there is nothing to explain yet.</Empty>
  }

  const status = aiStatus.data
  return (
    <div className="section-stack">
      <AiStatusCard status={status} />
      {!status ? <Loading label="Checking the local model…" /> : null}
      {status && !status.ai_available ? (
        <Banner kind="unknown">
          The written explanation is unavailable ({String(status.status).replace(/_/g, ' ')}). The
          calculated figures below the prose are still available, and the rest of the dashboard does not
          depend on the model.
        </Banner>
      ) : null}
      <Panel
        title="Ask for a written explanation"
        note="The model writes prose from figures WattXpense calculated. It is never asked to invent measurements, bills or device identities. If the text disagrees with the calculated block, trust the calculated block."
        actions={
          <button type="button" className="primary" onClick={onAsk} disabled={explain.pending}>
            {explain.pending ? 'Asking the model…' : 'Ask for an explanation'}
          </button>
        }
      >
        <p className="muted small">
          A local 7B model on CPU often takes 10–30 seconds. The dashboard works without it.
        </p>
      </Panel>
      <ErrorBox error={aiStatus.error} onRetry={aiStatus.reload} />
      <ExplainPanel
        result={explain.data}
        status={status?.status}
        pending={explain.pending}
        modelStatus={status}
        onRefresh={onAsk}
      />
    </div>
  )
}

function ProvenanceBanner({ summary }) {
  const provenance = summary?.data_provenance
  if (!provenance || !provenance.statement || summary?.status === 'no_device') return null
  if (provenance.fully_simulated) {
    return (
      <Banner kind="simulated">
        <strong>Simulated data.</strong> {provenance.statement}
      </Banner>
    )
  }
  if (provenance.readings_from_hardware === 0 && provenance.readings_unlabelled > 0) {
    return (
      <Banner kind="unknown">
        <strong>Source not declared.</strong> {provenance.statement}
      </Banner>
    )
  }
  if (provenance.readings_from_hardware > 0 && provenance.readings_simulated > 0) {
    return (
      <Banner kind="unknown">
        <strong>Mixed sources.</strong> {provenance.statement}
      </Banner>
    )
  }
  if (provenance.readings_from_hardware > 0) {
    return (
      <Banner kind="measured">
        <strong>Hardware readings.</strong> {provenance.statement}
      </Banner>
    )
  }
  return null
}

function sectionNote(section) {
  switch (section) {
    case 'overview':
      return 'Summary cards, the selected usage window and the projected bill. Estimates carry a label.'
    case 'usage':
      return 'Measured history and a labelled forecast. The range control changes the comparison window.'
    case 'anomalies':
      return 'Statistical differences and data gaps, each with its evidence.'
    case 'insights':
      return 'Savings suggestions are estimates from observed patterns, not promises.'
    case 'bill':
      return 'Slab arithmetic stays visible, and every projection is marked as an estimate.'
    case 'ai':
      return 'Optional local-model prose, kept separate from the figures it was written from.'
    case 'devices':
      return 'Per-account devices, hardware configuration, one-time tokens and relay commands.'
    case 'account':
      return 'Your account, password and session security.'
    default:
      return ''
  }
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
